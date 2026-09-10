"""
RosterFlow 클락인/아웃 및 지오펜싱 모듈.

직원 클락인/아웃, 휴게 시작/종료, 관리자용 시간기록 조회·수동생성·수정·삭제,
매장 위치 기반 지오펜싱 설정을 담당합니다. helpers.py에만 의존합니다.
"""
from datetime import date, timedelta, datetime, timezone
from flask import request, jsonify, g, Blueprint
import secrets

from helpers import (
    NZ_TZ, load_state, save_state, require_login, require_owner, require_employee_login,
    _log_audit, _week_dates, _week_key_for_date, _actual_break_hours, _sanitize_breaks,
    _actual_hours_for_entry, _default_geofence, _haversine_meters, _geofence_check,
    DEFAULT_PAYROLL_ROUNDING_MINUTES, DEFAULT_GEOFENCE_RADIUS_M,
)

time_tracking_bp = Blueprint("time_tracking", __name__)

@time_tracking_bp.route("/api/employee-auth/clock-status", methods=["GET"])
@require_employee_login
def employee_clock_status(company_id, employee):
    state = load_state(company_id)
    open_entry = next(
        (e for e in state["time_entries"] if e["employee_id"] == employee["id"] and not e.get("clock_out")),
        None,
    )
    on_break = False
    break_start = None
    if open_entry:
        breaks = open_entry.get("breaks") or []
        if breaks and not breaks[-1].get("end"):
            on_break = True
            break_start = breaks[-1]["start"]
    now = datetime.now(timezone.utc)
    today_nz = now.astimezone(NZ_TZ).date()
    today_iso = today_nz.isoformat()
    done_for_today = any(
        e["employee_id"] == employee["id"] and e.get("date") == today_iso and e.get("clock_out")
        for e in state["time_entries"]
    ) and not open_entry

    # 이번 주 스케줄을 확인(Agree)했는지 — 클락인 가능 여부를 화면에 미리 보여주기 위함.
    this_week_key = _week_key_for_date(today_nz)
    this_week = state["weeks"].get(this_week_key)
    week_published = bool(this_week and this_week.get("published"))
    week_agreed = bool(week_published and (this_week.get("agreements") or {}).get(employee["id"], {}).get("agreed"))

    return jsonify({
        "clocked_in": bool(open_entry),
        "clock_in": open_entry["clock_in"] if open_entry else None,
        "on_break": on_break,
        "break_start": break_start,
        "week_published": week_published,
        "week_agreed": week_agreed,
        "done_for_today": done_for_today,
    })

@time_tracking_bp.route("/api/employee-auth/clock-in", methods=["POST"])
@require_employee_login
def employee_clock_in(company_id, employee):
    state = load_state(company_id)
    open_entry = next(
        (e for e in state["time_entries"] if e["employee_id"] == employee["id"] and not e.get("clock_out")),
        None,
    )
    if open_entry:
        return jsonify({"error": "이미 클락인되어 있습니다. 먼저 클락아웃해주세요."}), 400

    now = datetime.now(timezone.utc)
    today_nz = now.astimezone(NZ_TZ).date()
    today_iso = today_nz.isoformat()

    # 이번 주 스케줄을 "확인했습니다" 버튼으로 먼저 확인해야만 클락인할 수 있습니다 —
    # 이렇게 해야 "이 직원이 스케줄을 언제/정말로 확인했는지"가 명확한 시각 기록으로
    # 남아서, 나중에 "몰랐다"는 식의 분쟁을 막을 수 있습니다.
    this_week_key = _week_key_for_date(today_nz)
    this_week = state["weeks"].get(this_week_key)
    if not this_week or not this_week.get("published"):
        return jsonify({"error": "이번 주 스케줄이 아직 게시(퍼블리시)되지 않았습니다. 관리자에게 문의해주세요."}), 400
    agreement = (this_week.get("agreements") or {}).get(employee["id"], {})
    if not agreement.get("agreed"):
        return jsonify({"error": "클락인하기 전에, 먼저 이번 주 스케줄을 확인하고 '확인했습니다' 버튼을 눌러주세요."}), 400

    # 오늘 이미 클락아웃(퇴근 처리)한 기록이 있으면 다시 클락인할 수 없습니다 — 하루에
    # 여러 번 클락인/아웃 하는 건 "휴게" 버튼으로 처리해야 하고, 클락아웃은 그날의
    # 근무가 완전히 끝났다는 뜻이어야 관리자 입장에서도 헷갈리지 않습니다.
    already_done_today = any(
        e["employee_id"] == employee["id"] and e.get("date") == today_iso and e.get("clock_out")
        for e in state["time_entries"]
    )
    if already_done_today:
        return jsonify({"error": "오늘은 이미 퇴근(클락아웃) 처리되었습니다. 잠깐 쉬는 거라면 '휴게 시작' 버튼을 이용해주세요."}), 400

    # 지오펜싱(매장 위치 기반 검증)이 켜져 있으면, 클락인은 하드 차단합니다 — "출근하는
    # 길에 미리 클락인해서 시간을 버는" 걸 막기 위한 기능이라, 매장 반경 밖이면 아예
    # 기록 자체를 만들지 않습니다.
    payload = request.get_json(silent=True) or {}
    lat, lng = payload.get("lat"), payload.get("lng")
    geofence = state.get("geofence") or _default_geofence()
    if geofence.get("enabled"):
        if lat is None or lng is None:
            return jsonify({"error": "위치 정보가 필요합니다. 브라우저의 위치 정보 권한을 허용해주세요."}), 400
        ok, distance = _geofence_check(state, lat, lng)
        if not ok:
            return jsonify({
                "error": f"매장에서 너무 멀리 떨어져 있어 클락인할 수 없습니다 (매장까지 약 {distance:.0f}m).",
                "error_code": "geofence_out_of_range",
                "distance_m": distance,
            }), 400

    entry = {
        "id": secrets.token_hex(8),
        "employee_id": employee["id"],
        "date": today_iso,
        "clock_in": now.isoformat(),
        "clock_out": None,
        "breaks": [],
        "clock_in_location": {"lat": lat, "lng": lng} if lat is not None and lng is not None else None,
        "clock_out_location": None,
        "edited": False,
        "edit_history": [],
    }
    state["time_entries"].append(entry)
    save_state(company_id, state)
    return jsonify(entry), 201

@time_tracking_bp.route("/api/employee-auth/break-start", methods=["POST"])
@require_employee_login
def employee_break_start(company_id, employee):
    state = load_state(company_id)
    open_entry = next(
        (e for e in state["time_entries"] if e["employee_id"] == employee["id"] and not e.get("clock_out")),
        None,
    )
    if not open_entry:
        return jsonify({"error": "먼저 클락인해주세요."}), 400
    breaks = open_entry.setdefault("breaks", [])
    if breaks and not breaks[-1].get("end"):
        return jsonify({"error": "이미 휴게 중입니다."}), 400
    # 휴게 시작/종료는 위치를 이유로 막지 않습니다(정당하게 매장 밖에서 식사하는 경우도
    # 많으므로) — 다만 위치는 그대로 기록해서, 관리자가 필요하면 나중에 확인할 수
    # 있게 남겨둡니다.
    payload = request.get_json(silent=True) or {}
    lat, lng = payload.get("lat"), payload.get("lng")
    breaks.append({
        "id": secrets.token_hex(6), "start": datetime.now(timezone.utc).isoformat(), "end": None,
        "start_location": {"lat": lat, "lng": lng} if lat is not None and lng is not None else None,
    })
    save_state(company_id, state)
    return jsonify(open_entry)

@time_tracking_bp.route("/api/employee-auth/break-end", methods=["POST"])
@require_employee_login
def employee_break_end(company_id, employee):
    state = load_state(company_id)
    open_entry = next(
        (e for e in state["time_entries"] if e["employee_id"] == employee["id"] and not e.get("clock_out")),
        None,
    )
    if not open_entry:
        return jsonify({"error": "먼저 클락인해주세요."}), 400
    breaks = open_entry.get("breaks") or []
    if not breaks or breaks[-1].get("end"):
        return jsonify({"error": "진행 중인 휴게가 없습니다."}), 400
    payload = request.get_json(silent=True) or {}
    lat, lng = payload.get("lat"), payload.get("lng")
    breaks[-1]["end"] = datetime.now(timezone.utc).isoformat()
    if lat is not None and lng is not None:
        breaks[-1]["end_location"] = {"lat": lat, "lng": lng}
    save_state(company_id, state)
    return jsonify(open_entry)

@time_tracking_bp.route("/api/employee-auth/clock-out", methods=["POST"])
@require_employee_login
def employee_clock_out(company_id, employee):
    state = load_state(company_id)
    open_entry = next(
        (e for e in state["time_entries"] if e["employee_id"] == employee["id"] and not e.get("clock_out")),
        None,
    )
    if not open_entry:
        return jsonify({"error": "클락인 기록이 없습니다."}), 400
    now_iso = datetime.now(timezone.utc).isoformat()
    # 휴게 중에 클락아웃을 누르면, 열려있던 휴게도 같이 닫아줍니다 — 휴게가 계속 "진행
    # 중"인 채로 남아있는 이상한 상태를 방지하기 위함입니다.
    breaks = open_entry.get("breaks") or []
    if breaks and not breaks[-1].get("end"):
        breaks[-1]["end"] = now_iso
    # 클락아웃은 위치를 이유로 막지는 않습니다(퇴근길에 매장을 벗어난 뒤 찍는 경우가
    # 흔하므로) — 다만 어디서 찍었는지는 그대로 기록해서, 관리자가 나중에 확인할 수
    # 있게 남겨둡니다.
    payload = request.get_json(silent=True) or {}
    lat, lng = payload.get("lat"), payload.get("lng")
    open_entry["clock_out"] = now_iso
    if lat is not None and lng is not None:
        open_entry["clock_out_location"] = {"lat": lat, "lng": lng}
    save_state(company_id, state)
    return jsonify(open_entry)

@time_tracking_bp.route("/api/time-entries", methods=["GET"])
@require_login
def list_time_entries(company_id):
    """사장/매니저 전용: 특정 주(week_key 쿼리 파라미터)의 전 직원 클락인/아웃 기록을
    보여줍니다. 반올림(클락인 올림/클락아웃 버림) 적용 후 실제 근무시간, 실제 기록된
    휴게시간 합계, 그리고 지오펜싱이 설정되어 있으면 매장으로부터의 거리도 같이
    계산해서 내려줍니다(관리자가 "매장에서 너무 멀리서 찍었는지" 한눈에 볼 수 있도록)."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    week_key = request.args.get("week_key")
    state = load_state(company_id)
    entries = state["time_entries"]
    if week_key:
        try:
            week_dates = {d.isoformat() for d in _week_dates(week_key)}
            entries = [e for e in entries if e.get("date") in week_dates]
        except ValueError:
            return jsonify({"error": "Invalid week_key."}), 400
    employee_names = {e["id"]: e["name"] for e in state["employees"]}
    rounding = state.get("payroll_rounding_minutes", DEFAULT_PAYROLL_ROUNDING_MINUTES)
    geofence = state.get("geofence") or _default_geofence()
    has_geofence = geofence.get("lat") is not None

    def _distance_for(loc):
        if not has_geofence or not loc or loc.get("lat") is None or loc.get("lng") is None:
            return None
        return round(_haversine_meters(geofence["lat"], geofence["lng"], loc["lat"], loc["lng"]), 0)

    out = []
    for e in sorted(entries, key=lambda x: (x.get("date") or "", x.get("clock_in") or "")):
        row = dict(e)
        row["employee_name"] = employee_names.get(e["employee_id"], "?")
        row["actual_hours"] = _actual_hours_for_entry(e, rounding)
        row["break_hours"] = round(_actual_break_hours(e), 2)
        row["clock_in_distance_m"] = _distance_for(e.get("clock_in_location"))
        row["clock_out_distance_m"] = _distance_for(e.get("clock_out_location"))
        out.append(row)
    return jsonify(out)

@time_tracking_bp.route("/api/time-entries", methods=["POST"])
@require_login
def create_time_entry(company_id):
    """사장/매니저 전용: 직원이 그날 클락인 자체를 아예 안 찍어서 기록이 하나도
    없는 경우, 관리자가 직접 그 날짜의 클락인/아웃 기록을 새로 만들어 넣습니다.
    수정과 마찬가지로 사유가 필수이고, 감사기록·수정이력에 남습니다 — "실제 클락인
    기록"이 아니라 "관리자가 나중에 채워 넣은 기록"이라는 게 항상 투명하게
    구분되도록 하기 위함입니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 작업은 사장 또는 매니저만 할 수 있습니다."}), 403
    payload = request.get_json(silent=True) or {}
    employee_id = payload.get("employee_id")
    reason = (payload.get("reason") or "").strip()
    if not employee_id:
        return jsonify({"error": "직원을 선택해주세요."}), 400
    if not reason:
        return jsonify({"error": "기록을 추가하는 사유를 입력해주세요."}), 400

    state = load_state(company_id)
    employee = next((e for e in state["employees"] if e["id"] == employee_id), None)
    if not employee:
        return jsonify({"error": "Employee not found."}), 404

    clock_in = payload.get("clock_in")
    clock_out = payload.get("clock_out")
    if not clock_in:
        return jsonify({"error": "클락인 시각을 입력해주세요."}), 400
    try:
        clock_in_dt = datetime.fromisoformat(clock_in)
        if clock_out:
            clock_out_dt = datetime.fromisoformat(clock_out)
            if clock_out_dt < clock_in_dt:
                return jsonify({"error": "클락아웃 시각이 클락인 시각보다 빠를 수 없습니다."}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "날짜/시간 형식이 올바르지 않습니다."}), 400

    entry_date = clock_in_dt.astimezone(NZ_TZ).date().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    new_breaks, breaks_error = _sanitize_breaks(payload.get("breaks"), clock_in, clock_out)
    if breaks_error:
        return jsonify({"error": breaks_error}), 400
    entry = {
        "id": secrets.token_hex(8),
        "employee_id": employee_id,
        "date": entry_date,
        "clock_in": clock_in,
        "clock_out": clock_out or None,
        "breaks": new_breaks or [],
        "clock_in_location": None, "clock_out_location": None,
        # 실제 클락인이 아니라 관리자가 수기로 채워 넣은 기록임을 표시합니다 — 화면에서
        # "수정됨"과는 별도로 "수동 등록"이라고 구분해서 보여줄 수 있게 하기 위함입니다.
        "manually_created": True,
        "edited": True,
        "edit_history": [{
            "edited_by": g.user_name, "edited_by_role": g.role, "edited_at": now_iso,
            "reason": reason[:300], "old_clock_in": None, "old_clock_out": None,
        }],
    }
    state["time_entries"].append(entry)
    _log_audit(state, "time_entry_created", f"{employee['name']}의 {entry_date} 클락인/아웃 기록을 수동 등록",
               {"employee_id": employee_id, "date": entry_date})
    save_state(company_id, state)
    return jsonify(entry), 201

@time_tracking_bp.route("/api/time-entries/<entry_id>", methods=["PUT"])
@require_login
def edit_time_entry(company_id, entry_id):
    """사장/매니저 전용: 클락인/아웃 시간을 수정합니다(직원이 깜빡하고 안 찍었을 때
    등). 사유(reason)는 필수이고, 누가·언제·왜 고쳤는지 edit_history에 남습니다 —
    투명성을 위해 이 기록은 지워지지 않습니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 작업은 사장 또는 매니저만 할 수 있습니다."}), 403
    payload = request.get_json(silent=True) or {}
    reason = (payload.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "수정 사유를 입력해주세요."}), 400

    state = load_state(company_id)
    entry = next((e for e in state["time_entries"] if e["id"] == entry_id), None)
    if not entry:
        return jsonify({"error": "Time entry not found."}), 404

    new_clock_in = payload.get("clock_in")
    new_clock_out = payload.get("clock_out")
    try:
        if new_clock_in:
            datetime.fromisoformat(new_clock_in)
        if new_clock_out:
            datetime.fromisoformat(new_clock_out)
    except ValueError:
        return jsonify({"error": "날짜/시간 형식이 올바르지 않습니다."}), 400

    # 휴게 시작/종료도 여기서 같이 수정할 수 있습니다 — 직원이 깜빡하고 휴게 버튼을
    # 안 누른 경우, 관리자가 나중에 채워 넣을 수 있도록. 휴게 시간이 반영되면
    # 실제 근무시간(_actual_hours_for_entry)이 자동으로 그만큼 줄어듭니다.
    new_breaks = None
    if "breaks" in payload:
        effective_clock_in = new_clock_in or entry.get("clock_in")
        effective_clock_out = new_clock_out if new_clock_out is not None else entry.get("clock_out")
        new_breaks, breaks_error = _sanitize_breaks(payload["breaks"], effective_clock_in, effective_clock_out)
        if breaks_error:
            return jsonify({"error": breaks_error}), 400

    entry.setdefault("edit_history", []).append({
        "edited_by": g.user_name, "edited_by_role": g.role,
        "edited_at": datetime.now(timezone.utc).isoformat(), "reason": reason[:300],
        "old_clock_in": entry.get("clock_in"), "old_clock_out": entry.get("clock_out"),
    })
    if new_clock_in:
        entry["clock_in"] = new_clock_in
    if new_clock_out is not None:
        entry["clock_out"] = new_clock_out or None
    if new_breaks is not None:
        entry["breaks"] = new_breaks
    entry["edited"] = True
    save_state(company_id, state)
    return jsonify(entry)

@time_tracking_bp.route("/api/time-entries/<entry_id>", methods=["DELETE"])
@require_login
def delete_time_entry(company_id, entry_id):
    """사장/매니저 전용: 클락인/아웃 기록을 완전히 삭제합니다(예: 직원이 실수로
    잘못 찍은 기록을 아예 없애고 싶을 때). 되돌릴 수 없는 작업입니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 작업은 사장 또는 매니저만 할 수 있습니다."}), 403
    state = load_state(company_id)
    before_count = len(state["time_entries"])
    state["time_entries"] = [e for e in state["time_entries"] if e["id"] != entry_id]
    if len(state["time_entries"]) == before_count:
        return jsonify({"error": "Time entry not found."}), 404
    save_state(company_id, state)
    return "", 204

@time_tracking_bp.route("/api/geofence", methods=["GET"])
@require_owner
def get_geofence(company_id):
    state = load_state(company_id)
    return jsonify(state.get("geofence") or _default_geofence())

@time_tracking_bp.route("/api/geofence", methods=["POST"])
@require_owner
def set_geofence(company_id):
    """사장 전용: 매장 위치(위도/경도)와 허용 반경, 켜짐/꺼짐 여부를 저장합니다.
    "현재 위치를 매장 위치로 저장" 버튼을 누르면, 그 순간 사장의 브라우저가 잡은
    GPS 좌표가 그대로 lat/lng로 들어옵니다."""
    payload = request.get_json(silent=True) or {}
    try:
        lat = float(payload.get("lat"))
        lng = float(payload.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "위치 좌표가 올바르지 않습니다."}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"error": "위치 좌표 범위가 올바르지 않습니다."}), 400
    try:
        radius_m = int(payload.get("radius_m", DEFAULT_GEOFENCE_RADIUS_M))
    except (TypeError, ValueError):
        return jsonify({"error": "radius_m must be a number."}), 400
    if radius_m < 50 or radius_m > 5000:
        return jsonify({"error": "반경은 50m~5000m 사이여야 합니다."}), 400

    state = load_state(company_id)
    state["geofence"] = {
        "enabled": bool(payload.get("enabled", True)),
        "lat": lat, "lng": lng, "radius_m": radius_m,
    }
    _log_audit(state, "geofence_updated", f"매장 위치 등록/수정 (반경 {radius_m}m)", {"radius_m": radius_m})
    save_state(company_id, state)
    return jsonify(state["geofence"])

@time_tracking_bp.route("/api/geofence/toggle", methods=["POST"])
@require_owner
def toggle_geofence(company_id):
    """사장 전용: 매장 위치는 그대로 두고 켜짐/꺼짐만 바꿉니다(위치를 다시 등록할
    필요 없이 잠깐 꺼두고 싶을 때 씁니다)."""
    payload = request.get_json(silent=True) or {}
    state = load_state(company_id)
    geofence = state.get("geofence") or _default_geofence()
    if geofence.get("lat") is None:
        return jsonify({"error": "먼저 매장 위치를 등록해주세요."}), 400
    geofence["enabled"] = bool(payload.get("enabled"))
    state["geofence"] = geofence
    _log_audit(state, "geofence_toggled", f"지오펜싱 {'켜짐' if geofence['enabled'] else '꺼짐'}", {"enabled": geofence["enabled"]})
    save_state(company_id, state)
    return jsonify(geofence)
