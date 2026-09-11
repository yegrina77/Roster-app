"""
RosterFlow 스케줄 관리 모듈.

부서/근무유형 설정, 근무요건, 자동 스케줄 생성(OR-Tools), 수동 조정, 퍼블리시,
공휴일 등록, 근무 패턴 제안, 노쇼 알림 등을 담당합니다. helpers.py와
payroll.py(퍼블리시 시 Lieu Day/애뉴얼 리브 자동 적립)에 의존합니다.
"""
import random
import time
from datetime import date, timedelta, datetime, timezone
from flask import request, jsonify, g, Blueprint

from scheduler import (
    Employee, ShiftRequirement, solve_schedule, DAYS, SHIFT_TYPES,
    DEPARTMENTS, DEPARTMENT_LABEL_KO, DEPARTMENT_SHIFTS, SHIFT_LABEL_KO, SHIFT_TIME_RANGES,
    SHIFT_DEFS, MAX_CONSECUTIVE_DAYS,
)
from helpers import (
    NZ_TZ, load_state, save_state, require_login, _log_audit, FREQUENCY_WINDOW_WEEKS,
    _effective_shift_times, _effective_shift_hours, _slugify_id, empty_week,
    _scheduler_shift_defs, _week_key_for_date, _week_locked, _leave_forced_days,
    _credited_leave_hours, _default_departments, _default_shift_types, _worked_that_weekday,
    REGIONS, _national_holidays_for_year, _regional_anniversary_for_year,
)
from payroll import _credit_lieu_days_for_week, _accrue_annual_leave_for_week

schedule_bp = Blueprint("schedule", __name__)

MIN_PATTERN_OCCURRENCES = 3
FREQUENCY_HIGHLIGHT_THRESHOLD = 5  # 이 값(포함) 이상이면 화면에서 강조 표시 (강제 배정 제한 아님)
CARRY_IN_LOOKBACK_DAYS = 14  # 전주 끝자락부터 최대 이만큼(2주)까지만 거슬러 올라가며 연속근무를 셉니다.
RECENT_FAIRNESS_WINDOW_WEEKS = 4  # 공정성 판단에 참고하는 "최근" 기간


@schedule_bp.route("/api/departments", methods=["GET"])
@require_login
def list_departments(company_id):
    state = load_state(company_id)
    return jsonify(state["departments"])

@schedule_bp.route("/api/departments", methods=["POST"])
@require_login
def add_department(company_id):
    state = load_state(company_id)
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Department name is required."}), 400
    existing_ids = {d["id"] for d in state["departments"]}
    if any(d["name"].lower() == name.lower() for d in state["departments"]):
        return jsonify({"error": "A department with this name already exists."}), 400
    dept = {"id": _slugify_id(name, existing_ids), "name": name}
    state["departments"].append(dept)
    _log_audit(state, "department_added", f"부서 추가: {name}", {"department_id": dept["id"]})
    save_state(company_id, state)
    return jsonify(dept), 201

@schedule_bp.route("/api/departments/<dept_id>", methods=["PUT"])
@require_login
def rename_department(company_id, dept_id):
    state = load_state(company_id)
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Department name is required."}), 400
    for d in state["departments"]:
        if d["id"] == dept_id:
            old_name = d["name"]
            d["name"] = name
            _log_audit(state, "department_renamed", f"부서 이름 변경: {old_name} → {name}", {"department_id": dept_id})
            save_state(company_id, state)
            return jsonify(d)
    return jsonify({"error": "Department not found."}), 404

@schedule_bp.route("/api/departments/<dept_id>", methods=["DELETE"])
@require_login
def delete_department(company_id, dept_id):
    state = load_state(company_id)
    employees_using_it = [e for e in state["employees"] if e.get("department") == dept_id]
    if employees_using_it:
        names = ", ".join(e["name"] for e in employees_using_it[:5])
        return jsonify({
            "error": f"Cannot delete: {len(employees_using_it)} employee(s) are still assigned to this "
                     f"department ({names}{'...' if len(employees_using_it) > 5 else ''}). "
                     f"Please reassign them to a different department first.",
        }), 400
    state["departments"] = [d for d in state["departments"] if d["id"] != dept_id]
    removed_shift_ids = {s["id"] for s in state["shift_types"] if s["department_id"] == dept_id}
    state["shift_types"] = [s for s in state["shift_types"] if s["department_id"] != dept_id]
    for shift_id in removed_shift_ids:
        state.get("shift_time_overrides", {}).pop(shift_id, None)
    for e in state["employees"]:
        e["blocked_shift_types"] = [s for s in e.get("blocked_shift_types", []) if s not in removed_shift_ids]
        e["preferred"] = [p for p in e.get("preferred", []) if p[1] not in removed_shift_ids]
    _log_audit(state, "department_deleted", f"부서 삭제: {dept_id}", {"department_id": dept_id})
    save_state(company_id, state)
    return "", 204

@schedule_bp.route("/api/shift-types", methods=["GET"])
@require_login
def list_shift_types(company_id):
    state = load_state(company_id)
    return jsonify(state["shift_types"])

@schedule_bp.route("/api/shift-types", methods=["POST"])
@require_login
def add_shift_type(company_id):
    state = load_state(company_id)
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    department_id = payload.get("department_id")
    start = payload.get("start") or "09:00"
    end = payload.get("end") or "17:00"
    is_closing = bool(payload.get("is_closing", False))
    blocked_after_closing = bool(payload.get("blocked_after_closing", False))

    if not name:
        return jsonify({"error": "Shift type name is required."}), 400
    if not any(d["id"] == department_id for d in state["departments"]):
        return jsonify({"error": "That department does not exist."}), 400
    if any(s["name"].lower() == name.lower() and s["department_id"] == department_id for s in state["shift_types"]):
        return jsonify({"error": "A shift type with this name already exists in this department."}), 400

    existing_ids = {s["id"] for s in state["shift_types"]}
    shift = {
        "id": _slugify_id(name, existing_ids), "name": name, "department_id": department_id,
        "start": start, "end": end, "is_closing": is_closing, "blocked_after_closing": blocked_after_closing,
    }
    state["shift_types"].append(shift)
    _log_audit(state, "shift_type_added", f"근무유형 추가: {name}", {"shift_type_id": shift["id"]})
    save_state(company_id, state)
    return jsonify(shift), 201

@schedule_bp.route("/api/shift-types/<shift_id>", methods=["PUT"])
@require_login
def update_shift_type(company_id, shift_id):
    state = load_state(company_id)
    payload = request.get_json(silent=True) or {}
    for s in state["shift_types"]:
        if s["id"] == shift_id:
            if "name" in payload and payload["name"].strip():
                s["name"] = payload["name"].strip()
            if "start" in payload:
                s["start"] = payload["start"]
            if "end" in payload:
                s["end"] = payload["end"]
            if "is_closing" in payload:
                s["is_closing"] = bool(payload["is_closing"])
            if "blocked_after_closing" in payload:
                s["blocked_after_closing"] = bool(payload["blocked_after_closing"])
            _log_audit(state, "shift_type_updated", f"근무유형 수정: {s['name']}", {"shift_type_id": shift_id})
            save_state(company_id, state)
            return jsonify(s)
    return jsonify({"error": "Shift type not found."}), 404

@schedule_bp.route("/api/shift-types/<shift_id>", methods=["DELETE"])
@require_login
def delete_shift_type(company_id, shift_id):
    state = load_state(company_id)
    state["shift_types"] = [s for s in state["shift_types"] if s["id"] != shift_id]
    state.get("shift_time_overrides", {}).pop(shift_id, None)
    for e in state["employees"]:
        e["blocked_shift_types"] = [s for s in e.get("blocked_shift_types", []) if s != shift_id]
        e["preferred"] = [p for p in e.get("preferred", []) if p[1] != shift_id]
    _log_audit(state, "shift_type_deleted", f"근무유형 삭제: {shift_id}", {"shift_type_id": shift_id})
    save_state(company_id, state)
    return "", 204

@schedule_bp.route("/api/shift-time-settings", methods=["GET"])
@require_login
def get_shift_time_settings(company_id):
    state = load_state(company_id)
    times = _effective_shift_times(state)
    overrides = state.get("shift_time_overrides", {})
    return jsonify({
        shift: {"start": s, "end": e, "is_default": shift not in overrides}
        for shift, (s, e) in times.items()
    })

@schedule_bp.route("/api/shift-time-settings", methods=["POST"])
@require_login
def set_shift_time_setting(company_id):
    """body: {shift_type, start, end}"""
    state = load_state(company_id)
    payload = request.get_json()
    shift = payload.get("shift_type")
    start = payload.get("start")
    end = payload.get("end")
    valid_ids = {s["id"] for s in state.get("shift_types", [])}
    if shift not in valid_ids:
        return jsonify({"error": "This shift type does not exist."}), 400
    if not start or not end:
        return jsonify({"error": "start and end are required."}), 400
    state.setdefault("shift_time_overrides", {})[shift] = {"start": start, "end": end}
    save_state(company_id, state)
    return jsonify({"shift_type": shift, "start": start, "end": end})

@schedule_bp.route("/api/shift-time-settings/<shift_type>", methods=["DELETE"])
@require_login
def reset_shift_time_setting(company_id, shift_type):
    """이 근무유형의 시간을, 회사가 근무유형을 만들 때 정한 원래 기본값으로 되돌립니다."""
    state = load_state(company_id)
    state.get("shift_time_overrides", {}).pop(shift_type, None)
    save_state(company_id, state)
    shift_def = next((s for s in state.get("shift_types", []) if s["id"] == shift_type), None)
    default_start = shift_def["start"] if shift_def else None
    default_end = shift_def["end"] if shift_def else None
    return jsonify({"shift_type": shift_type, "start": default_start, "end": default_end})

@schedule_bp.route("/api/weeks", methods=["GET"])
@require_login
def list_weeks(company_id):
    state = load_state(company_id)
    return jsonify(sorted(state["weeks"].keys()))

@schedule_bp.route("/api/weeks", methods=["POST"])
@require_login
def create_week(company_id):
    """새 주차를 만듭니다.
    근무요건은 선택(copy_from)과 상관없이, 캘린더 기준 바로 전주 데이터가 있으면
    항상 자동으로 이어받습니다 (매주 똑같은 근무요건을 반복 입력하는 번거로움을 없애기 위함).
    휴무(Off) 지정은 copy_from을 넘긴 경우에만(즉 "전주와 동일하게 시작"을 선택한 경우에만)
    그 주로부터 복사됩니다."""
    state = load_state(company_id)
    payload = request.get_json()
    week_key = payload.get("week_key")
    copy_from = payload.get("copy_from")

    if not week_key:
        return jsonify({"error": "week_key is required."}), 400

    if week_key in state["weeks"]:
        return jsonify(state["weeks"][week_key])

    y, m, d = map(int, week_key.split("-"))
    immediate_prev_key = (date(y, m, d) - timedelta(days=7)).isoformat()
    immediate_prev_week = state["weeks"].get(immediate_prev_key)
    carried_requirements = (
        [dict(r) for r in immediate_prev_week["requirements"]] if immediate_prev_week else []
    )

    if copy_from and copy_from in state["weeks"]:
        source = state["weeks"][copy_from]
        new_week = {
            "requirements": carried_requirements,
            "off_days": {k: list(v) for k, v in source["off_days"].items()},
            "schedule": None,
            "auto_assignments": [],
            "locked": False,
            "published": False,
            "agreements": {},
        }
    else:
        new_week = empty_week()
        new_week["requirements"] = carried_requirements

    state["weeks"][week_key] = new_week
    save_state(company_id, state)
    return jsonify(new_week), 201

@schedule_bp.route("/api/weeks/<week_key>", methods=["GET"])
@require_login
def get_week(company_id, week_key):
    state = load_state(company_id)
    week = state["weeks"].get(week_key)
    if week is None:
        return jsonify(None)
    return jsonify(week)

@schedule_bp.route("/api/weeks/<week_key>", methods=["DELETE"])
@require_login
def delete_week(company_id, week_key):
    state = load_state(company_id)
    state["weeks"].pop(week_key, None)
    save_state(company_id, state)
    return "", 204

@schedule_bp.route("/api/weeks/<week_key>/lock", methods=["POST"])
@require_login
def set_week_lock(company_id, week_key):
    """body: {locked: true/false} - 이 주차를 잠그거나 풉니다. 잠긴 동안엔 이 주의
    근무요건/휴무지정/스케줄 생성·수동조정이 모두 서버에서도 거부됩니다.

    이미 퍼블리시된 주를 다시 풀면(unlock), 퍼블리시 상태는 취소되지만 직원들의
    "확인(Agree)" 기록은 이제 더 이상 전부 초기화하지 않습니다 — 대신, 나중에 다시
    퍼블리시하는 순간에 "실제로 스케줄이 바뀐 직원"만 골라서 그 사람의 확인만
    초기화합니다(publish_week 참고). 그래야 한 명 스케줄만 급하게 바꿨을 때, 나머지
    직원들이 매번 다시 확인 버튼을 누를 필요가 없습니다."""
    state = load_state(company_id)
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    payload = request.get_json(silent=True) or {}
    new_locked = bool(payload.get("locked", False))
    week = state["weeks"][week_key]
    week["locked"] = new_locked
    republish_reset = False
    if not new_locked and week.get("published"):
        week["published"] = False
        republish_reset = True
    _log_audit(state, "week_lock_changed", f"{week_key} 주 {'잠금' if new_locked else '잠금 해제'}",
               {"week_key": week_key, "locked": new_locked, "republish_reset": republish_reset})
    save_state(company_id, state)
    return jsonify({"locked": week["locked"], "published": week.get("published", False), "republish_reset": republish_reset})

def _assignments_by_employee(assignments):
    """배정 목록을, 직원별로 "(요일, 근무유형, 커스텀 시작/종료)" 조합의 집합으로
    묶습니다. 두 시점의 배정을 비교해서 "이 직원의 스케줄이 실제로 바뀌었는지"를
    판단할 때 씁니다(republish 시 누구만 다시 확인이 필요한지 계산하는 데 사용)."""
    by_emp = {}
    for a in assignments or []:
        key = (a.get("day"), a.get("shift_type"), a.get("custom_start"), a.get("custom_end"))
        by_emp.setdefault(a.get("employee_id"), set()).add(key)
    return by_emp

@schedule_bp.route("/api/weeks/<week_key>/publish", methods=["POST"])
@require_login
def publish_week(company_id, week_key):
    """사장/매니저 전용: 이 주의 스케줄을 직원들에게 공개합니다. 공개와 동시에 이 주를
    자동으로 잠급니다(locked=True) — 직원이 이미 확인(Agree)한 스케줄을 몰래 바꾸는
    상황을 막기 위해서입니다.

    처음 퍼블리시하는 게 아니라 "수정 후 재퍼블리시"하는 경우엔, 지난번 퍼블리시
    시점의 배정과 지금 배정을 직원별로 비교해서, **실제로 스케줄이 바뀐 직원의
    확인 기록만** 초기화합니다 — 한 명 스케줄만 급하게 바꿨다고 해서 나머지
    전원이 다시 확인 버튼을 누를 필요가 없도록 하기 위함입니다. 이때 이 주에
    공휴일 근무(카테고리 A)가 있으면 Lieu Day도 같이 크레딧됩니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 작업은 사장 또는 매니저만 할 수 있습니다."}), 403
    state = load_state(company_id)
    week = state["weeks"].get(week_key)
    if not week:
        return jsonify({"error": "This week does not exist."}), 404
    if not week.get("schedule") or not (week["schedule"].get("assignments")):
        return jsonify({"error": "게시할 스케줄이 없습니다. 먼저 스케줄을 생성해주세요."}), 400

    new_assignments = week["schedule"]["assignments"]
    old_assignments = week.get("last_published_assignments") or []
    old_by_emp = _assignments_by_employee(old_assignments)
    new_by_emp = _assignments_by_employee(new_assignments)
    agreements = week.setdefault("agreements", {})
    employee_names = {e["id"]: e["name"] for e in state["employees"]}
    changed_employee_ids = []
    for emp_id in set(old_by_emp) | set(new_by_emp):
        if old_by_emp.get(emp_id, set()) != new_by_emp.get(emp_id, set()):
            if emp_id in agreements:
                del agreements[emp_id]
            changed_employee_ids.append(emp_id)

    week["published"] = True
    week["locked"] = True
    week["last_published_assignments"] = new_assignments
    _credit_lieu_days_for_week(state, week_key)
    _accrue_annual_leave_for_week(state, week_key)
    _log_audit(state, "week_published", f"{week_key} 주 스케줄 퍼블리시", {"week_key": week_key})
    save_state(company_id, state)
    return jsonify({
        "published": True, "locked": True,
        "changed_employee_count": len(changed_employee_ids),
        "changed_employee_names": [employee_names.get(eid, eid) for eid in changed_employee_ids],
    })

@schedule_bp.route("/api/weeks/<week_key>/agreements", methods=["GET"])
@require_login
def get_week_agreements(company_id, week_key):
    """사장/매니저 전용: 이 주 스케줄을 직원별로 확인(Agree)했는지 여부를 보여줍니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    state = load_state(company_id)
    week = state["weeks"].get(week_key)
    if not week:
        return jsonify({"error": "This week does not exist."}), 404
    agreements = week.get("agreements") or {}
    rows = []
    for e in state["employees"]:
        a = agreements.get(e["id"], {})
        rows.append({
            "employee_id": e["id"], "employee_name": e["name"],
            "agreed": bool(a.get("agreed")), "agreed_at": a.get("agreed_at"),
        })
    return jsonify({"published": bool(week.get("published")), "agreements": rows})

@schedule_bp.route("/api/no-show-alerts", methods=["GET"])
@require_login
def get_no_show_alerts(company_id):
    """사장/매니저 전용: 오늘 스케줄된 근무 중, 시작 시각이 지났는데 아직 클락인
    기록이 없는 직원을 보여줍니다 — 진짜 노쇼든, 단순히 클락인을 깜빡한 것이든 둘 다
    잡아냅니다(관리자가 직접 판단해서 연락하면 됩니다). 쿼리파라미터
    grace_minutes(기본 15)로 "몇 분 지나야 알림 대상으로 볼지" 조절합니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    try:
        grace_minutes = int(request.args.get("grace_minutes", 15))
    except (TypeError, ValueError):
        grace_minutes = 15

    state = load_state(company_id)
    now_nz = datetime.now(NZ_TZ)
    today = now_nz.date()
    today_iso = today.isoformat()
    this_week_key = _week_key_for_date(today)
    week = state["weeks"].get(this_week_key)
    if not week or not week.get("published"):
        return jsonify([])

    today_day = DAYS[today.weekday()]
    assignments = (week.get("schedule") or {}).get("assignments") or []
    shift_times = _effective_shift_times(state)
    shift_names = {s["id"]: s["name"] for s in state["shift_types"]}
    employee_names = {e["id"]: e["name"] for e in state["employees"]}

    # 오늘 이미 클락인한(진행중이든 완료든) 직원은 알림 대상에서 제외합니다.
    clocked_in_today = {te["employee_id"] for te in state["time_entries"] if te.get("date") == today_iso}

    results = []
    for a in assignments:
        if a["day"] != today_day or a["employee_id"] in clocked_in_today:
            continue
        default_start, _default_end = shift_times.get(a["shift_type"], ("09:00", "17:00"))
        start_str = a.get("custom_start") or default_start
        try:
            sh, sm = map(int, start_str.split(":"))
        except ValueError:
            continue
        scheduled_start = datetime(today.year, today.month, today.day, sh, sm, tzinfo=NZ_TZ)
        minutes_late = (now_nz - scheduled_start).total_seconds() / 60
        if minutes_late >= grace_minutes:
            results.append({
                "employee_id": a["employee_id"], "employee_name": employee_names.get(a["employee_id"], "?"),
                "shift_type": a["shift_type"], "shift_name": shift_names.get(a["shift_type"], a["shift_type"]),
                "scheduled_start": start_str,
                "minutes_late": round(minutes_late),
            })
    results.sort(key=lambda r: -r["minutes_late"])
    return jsonify(results)

@schedule_bp.route("/api/weeks/<week_key>/requirements", methods=["POST"])
@require_login
def set_week_requirements(company_id, week_key):
    state = load_state(company_id)
    if _week_locked(state, week_key):
        return jsonify({"error": "This week is locked. Please unlock it first."}), 403
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    payload = request.get_json()
    if not isinstance(payload, list):
        return jsonify({"error": "Requirements list must be an array."}), 400
    state["weeks"][week_key]["requirements"] = payload
    _log_audit(state, "requirements_changed", f"{week_key} 주 근무 요건 수정", {"week_key": week_key, "count": len(payload)})
    save_state(company_id, state)
    return jsonify(state["weeks"][week_key]["requirements"])

@schedule_bp.route("/api/weeks/<week_key>/off-days", methods=["POST"])
@require_login
def set_week_off_days(company_id, week_key):
    """body: {employee_id, off_days: [day, ...]} - 그 직원의 그 주 휴무일 전체를 교체"""
    state = load_state(company_id)
    if _week_locked(state, week_key):
        return jsonify({"error": "This week is locked. Please unlock it first."}), 403
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    payload = request.get_json()
    employee_id = payload.get("employee_id")
    off_days = payload.get("off_days", [])
    if not employee_id:
        return jsonify({"error": "employee_id is required."}), 400
    state["weeks"][week_key]["off_days"][employee_id] = off_days
    save_state(company_id, state)
    return jsonify(state["weeks"][week_key]["off_days"])

@schedule_bp.route("/api/weeks/<week_key>/generate-schedule", methods=["POST"])
@require_login
def generate_week_schedule(company_id, week_key):
    state = load_state(company_id)
    week = state["weeks"].get(week_key)
    if week is None:
        return jsonify({"error": "This week does not exist."}), 404
    if week.get("locked"):
        return jsonify({"error": "This week is locked. Please unlock it first."}), 403

    if not state["employees"]:
        return jsonify({"error": "No employees registered."}), 400
    if not week["requirements"]:
        return jsonify({"error": "Shift requirements have not been set for this week."}), 400

    off_days_map = week.get("off_days", {})

    employees = []
    for e in state["employees"]:
        night_count, weekend_count = _recent_shift_counts(state, e["id"], week_key)
        employees.append(Employee(
            id=e["id"],
            name=e["name"],
            department=e.get("department", "kitchen"),
            min_hours_per_week=e.get("min_hours_per_week", 30),
            target_days_per_week=e.get("target_days_per_week"),
            forced_off_days=list(set(
                off_days_map.get(e["id"], []) + _leave_forced_days(e, week_key)
            )),
            blocked_shift_types=e.get("blocked_shift_types", []),
            day_off_pattern=e.get("day_off_pattern"),
            preferred=[tuple(p) for p in e.get("preferred", [])],
            preferred_off_days=e.get("preferred_off_days", []),
            carry_in_streak=_carry_in_streak(state, e["id"], week_key),
            recent_night_count=night_count,
            recent_weekend_count=weekend_count,
            credited_off_hours=_credited_leave_hours(e, week_key),
        ))

    requirements = [
        ShiftRequirement(day=r["day"], shift_type=r["shift_type"], required_count=r["required_count"])
        for r in week["requirements"]
    ]

    # "다시 생성" 요청이면 body에 exclude(이전에 봤던 조합들)가 담겨 옵니다.
    # 사용자가 생성 전에 수동으로 미리 배치해둔 자리가 있으면 pin으로 담겨 옵니다.
    payload = request.get_json(silent=True) or {}
    exclude_raw = payload.get("exclude", [])
    exclude_solutions = [
        [(a["employee_id"], a["day"], a["shift_type"]) for a in sol]
        for sol in exclude_raw
    ]
    pin_raw = payload.get("pin", [])
    pinned = [(a["employee_id"], a["day"], a["shift_type"]) for a in pin_raw]
    random_seed = random.randint(1, 10_000_000) if exclude_solutions else None

    # ---- 근무 요건이 아예 없는 요일/근무유형에는 암묵적으로 "필요인원 0"을 채워 넣습니다 ----
    # 지금까지는 요건 칸이 빈칸(=요건 없음)이면 그 요일/근무유형에 아예 상한이 없어서,
    # 스케줄러가 다른 목적(예: 최소시간 채우기)을 위해 그 자리에 자유롭게 사람을 넣을 수
    # 있었습니다. "요건을 안 넣었다"는 걸 "아무도 배치하면 안 된다"는 뜻으로 정확히
    # 반영하기 위해, 요건이 없는 칸은 필요인원 0으로 하드 고정합니다 — 다만 그 칸에 수동
    # 고정(pin) 배치가 있으면, 그 인원수만큼은 예외로 허용합니다(관리자가 일부러 넣은
    # 자리는 존중해야 하므로).
    existing_req_keys = {(r.day, r.shift_type) for r in requirements}
    pin_count_by_day_shift = {}
    for emp_id, day, shift in pinned:
        pin_count_by_day_shift[(day, shift)] = pin_count_by_day_shift.get((day, shift), 0) + 1
    for st in state["shift_types"]:
        shift_id = st["id"]
        for day in DAYS:
            key = (day, shift_id)
            if key not in existing_req_keys:
                requirements.append(ShiftRequirement(
                    day=day, shift_type=shift_id,
                    required_count=pin_count_by_day_shift.get(key, 0),
                ))

    # ---- pin(수동 사전 배치) 검증 ----
    # pin은 model.Add(x == 1)로 하드 고정되는데, 이게 다른 하드 규칙과 모순되면
    # 계산 자체가 "원인불명 INFEASIBLE"로 실패해버립니다. 계산을 돌리기 전에 미리
    # 걸러내서, 정확히 어떤 pin이 왜 문제인지 알려줍니다.
    emp_by_id = {e.id: e for e in employees}
    pin_errors = []
    req_required = {(r.day, r.shift_type): r.required_count for r in requirements}
    pin_count_by_req = {}
    for emp_id, day, shift in pinned:
        emp = emp_by_id.get(emp_id)
        if emp is None:
            pin_errors.append(f"Pinned employee '{emp_id}' was not found.")
            continue
        if day in emp.forced_off_days:
            pin_errors.append(
                f"{emp.name} is pinned to work on {day}, but that day is already marked as a "
                f"forced day off (Off / Leave Request) for them. Remove one of the two."
            )
        if shift in emp.blocked_shift_types:
            pin_errors.append(
                f"{emp.name} is pinned to '{shift}' on {day}, but that shift type is in their "
                f"blocked list. Remove the pin or un-block the shift type for this employee."
            )
        key = (day, shift)
        pin_count_by_req[key] = pin_count_by_req.get(key, 0) + 1

    for (day, shift), count in pin_count_by_req.items():
        required = req_required.get((day, shift))
        if required is not None and count > required:
            pin_errors.append(
                f"{day} {shift}: {count} employees are pinned, but the requirement for that "
                f"shift is only {required}. Either raise the requirement or remove some pins."
            )

    if pin_errors:
        return jsonify({"error": " ".join(pin_errors)}), 400

    # ---- 최소시간 사전 검증 ----
    # 어떤 직원의 이번 주 실효 최소시간(유급 리브 크레딧 반영 후)이, 남은 근무 가능
    # 일수 + 그 부서에서 가능한 가장 긴 근무유형만으로도 원천적으로 채울 수 없는
    # 수준이면 계산을 돌리기 전에 미리 걸러내서, 정확히 어떤 직원과 어떤 요일 때문인지
    # 알려줍니다 (안 그러면 이것도 "원인불명 INFEASIBLE"로 실패해버립니다).
    shift_hours_map = _effective_shift_hours(state)
    dept_max_shift_hours = {}
    for st in state["shift_types"]:
        dept_max_shift_hours.setdefault(st["department_id"], []).append(shift_hours_map.get(st["id"], 0))
    dept_max_shift_hours = {d: max(hrs) if hrs else 0 for d, hrs in dept_max_shift_hours.items()}

    min_hours_errors = []
    for e in employees:
        available_days = 7 - len(e.forced_off_days)
        target_after_credit = max(0.0, e.min_hours_per_week - e.credited_off_hours)
        if available_days <= 0 or target_after_credit <= 0:
            continue
        proportional_cap = e.min_hours_per_week * available_days / 7
        effective_min_hours = min(target_after_credit, proportional_cap)
        max_possible = available_days * dept_max_shift_hours.get(e.department, 0)
        if effective_min_hours > max_possible + 0.01:
            min_hours_errors.append(
                f"{e.name}: needs at least {effective_min_hours:.1f}h this week, but is only "
                f"available {available_days} day(s) (forced off on: {', '.join(e.forced_off_days) or 'none'}), "
                f"and the longest shift in their department ('{e.department}') is "
                f"{dept_max_shift_hours.get(e.department, 0):.1f}h — so at most {max_possible:.1f}h is "
                f"reachable. Check for leftover Off marks copied from a previous week, or adjust their "
                f"minimum hours / off days."
            )

    if min_hours_errors:
        return jsonify({"error": " ".join(min_hours_errors)}), 400

    # ---- 부서별 총 용량(capacity) vs 최소시간 합계 사전 검증 ----
    # 근무 요건(required_count)은 "최소 이만큼 필요하다"일 뿐 아니라, 스케줄러 안에서
    # "절대 이 인원을 넘길 수 없다"는 하드 상한으로도 동시에 쓰입니다(한 시프트에
    # 필요인원보다 더 많은 사람을 몰아넣지 않기 위함). 그래서 어떤 부서 직원들의
    # 최소시간 합계가, 그 부서의 이번 주 근무 요건(required_count × 시간)을 다 더한
    # "총 용량"보다 크면 — 아무리 잘 배치해도 물리적으로 다 못 채웁니다. 인원 자체가
    # 부족한 게 아니라, 오히려 "필요인원 칸이 너무 적어서" 넘치는 경우도 여기 해당됩니다.
    shift_dept_map = {st["id"]: st["department_id"] for st in state["shift_types"]}
    dept_capacity_hours = {}
    for r in week["requirements"]:
        dept = shift_dept_map.get(r["shift_type"])
        if dept is None:
            continue
        hours = shift_hours_map.get(r["shift_type"], 0)
        dept_capacity_hours[dept] = dept_capacity_hours.get(dept, 0) + r["required_count"] * hours

    dept_demand_hours = {}
    dept_employee_names = {}
    for e in employees:
        available_days = 7 - len(e.forced_off_days)
        target_after_credit = max(0.0, e.min_hours_per_week - e.credited_off_hours)
        if available_days <= 0 or target_after_credit <= 0:
            continue
        proportional_cap = e.min_hours_per_week * available_days / 7
        effective_min_hours = min(target_after_credit, proportional_cap)
        dept_demand_hours[e.department] = dept_demand_hours.get(e.department, 0) + effective_min_hours
        dept_employee_names.setdefault(e.department, []).append(e.name)

    capacity_errors = []
    for dept, demand in dept_demand_hours.items():
        capacity = dept_capacity_hours.get(dept, 0)
        if demand > capacity + 0.01:
            capacity_errors.append(
                f"Department '{dept}': employees there need at least {demand:.1f}h combined this week "
                f"({', '.join(dept_employee_names[dept])}), but this week's shift requirements for that "
                f"department only add up to {capacity:.1f}h of total capacity (required headcount × "
                f"shift length, added across all shifts). Either raise the required headcount for some "
                f"shifts in this department, or lower some employees' minimum hours."
            )

    if capacity_errors:
        return jsonify({"error": " ".join(capacity_errors)}), 400

    shift_types, shift_defs, departments = _scheduler_shift_defs(state)
    result = solve_schedule(
        employees, requirements,
        exclude_solutions=exclude_solutions or None,
        random_seed=random_seed,
        pinned=pinned or None,
        shift_hours=_effective_shift_hours(state),
        shift_types=shift_types,
        shift_defs=shift_defs,
        departments=departments,
    )

    if result.status == "INFEASIBLE":
        # ---- 자동 원인 진단 ----
        # 여기까지 왔다는 건 pin/최소시간/부서용량처럼 미리 걸러낸 흔한 원인들은 다
        # 아니라는 뜻입니다. 남은 하드 규칙들(최소시간, 마감→오픈 연속근무 금지, 금지된
        # 근무유형, 타 부서 배정 금지)을 하나씩 꺼보면서 다시 계산해, "이걸 빼면
        # 풀린다"를 자동으로 찾아 사용자에게 정확한 원인을 알려줍니다.
        #
        # ⚠️ 진단 재계산은 반드시 "전체 진단 시간 예산"을 두고 그 안에서만 돕니다 —
        # 직원 수가 많은 회사는 "규칙 4개 + 직원별 최소시간 확인"을 다 돌리면 계산
        # 횟수가 많아져서, 예산 없이 돌리면 Render 등 호스팅의 요청 제한시간을 넘겨
        # 연결이 통째로 끊기고, 그러면 화면엔 아무 설명도 없는 "요청을 처리하지
        # 못했습니다"만 뜨는 문제가 있었습니다(진단하려다 오히려 더 불친절해진 것).
        # 예산을 넘기면, 지금까지 찾은 것만으로 답하거나 일반 안내로 넘어갑니다.
        diagnosis_deadline = time.time() + 12.0  # 진단 전체에 쓸 수 있는 최대 시간(초)
        relax_candidates = [
            ("min_hours", "이 직원(들)의 주당 최소시간 규칙"),
            ("forbidden_consecutive", "마감 근무 다음날 오픈 근무 금지 규칙"),
            ("blocked_shift_types", "이 직원(들)에게 금지된 근무유형 설정"),
            ("cross_department", "다른 부서 근무유형 배정 금지 규칙"),
        ]
        found_causes = []
        min_hours_is_cause = False
        for relax_key, relax_label_ko in relax_candidates:
            if time.time() >= diagnosis_deadline:
                break
            trial = solve_schedule(
                employees, requirements,
                exclude_solutions=exclude_solutions or None,
                random_seed=random_seed,
                pinned=pinned or None,
                shift_hours=_effective_shift_hours(state),
                shift_types=shift_types,
                shift_defs=shift_defs,
                departments=departments,
                relax={relax_key},
                time_limit_seconds=1.5,
            )
            if trial.status in ("OPTIMAL", "FEASIBLE"):
                found_causes.append(relax_label_ko)
                if relax_key == "min_hours":
                    min_hours_is_cause = True

        # ---- 2단계 진단: 최소시간이 원인이면, 정확히 어느 직원 때문인지까지 찾아봅니다 ----
        # (단, 남은 진단 예산 안에서 확인 가능한 직원까지만 — 직원이 아주 많은 회사는
        # 전원을 다 확인 못 할 수 있고, 그 경우 부분적으로 찾은 이름만 알려줍니다.)
        culprit_names = []
        if min_hours_is_cause:
            for e in employees:
                if e.min_hours_per_week <= 0:
                    continue
                if time.time() >= diagnosis_deadline:
                    break
                trial = solve_schedule(
                    employees, requirements,
                    exclude_solutions=exclude_solutions or None,
                    random_seed=random_seed,
                    pinned=pinned or None,
                    shift_hours=_effective_shift_hours(state),
                    shift_types=shift_types,
                    shift_defs=shift_defs,
                    departments=departments,
                    relax_min_hours_employee_ids={e.id},
                    time_limit_seconds=1.5,
                )
                if trial.status in ("OPTIMAL", "FEASIBLE"):
                    culprit_names.append(e.name)

        if culprit_names:
            result.diagnostics = [
                "스케줄을 만들 수 없는 이유를 자동으로 찾아봤습니다. "
                f"{', '.join(culprit_names)} 직원의 주당 최소시간을 이번 주에 채울 수 있는 "
                "자리가 부족합니다 — 그 직원이 속한 부서의 이번 주 근무 요건(칸 수)이 "
                "너무 적거나, 그 직원만 접근 가능한 요일/근무유형이 너무 제한되어 있을 "
                "수 있습니다. 이 직원의 최소시간을 낮추거나, 근무 요건을 늘려보세요."
            ]
        elif found_causes:
            result.diagnostics = [
                "스케줄을 만들 수 없는 이유를 자동으로 찾아봤습니다. 다음 규칙(들)이 "
                "다른 규칙과 충돌하고 있는 것으로 보입니다 — 이 중 하나를 완화하면 "
                "생성이 가능해집니다: " + " / ".join(found_causes) + "."
                " 관련된 직원의 규칙 설정이나 이번 주 근무 요건을 확인해주세요."
            ]
        # 둘 다 비어있으면(=하나씩 빼봐도 안 풀리면) 여러 규칙이 동시에 얽혀있다는
        # 뜻이라, 기존의 일반 안내 메시지(diagnostics)를 그대로 둡니다.

    result_dict = {
        "status": result.status,
        "assignments": result.assignments,
        "unmet_requirements": result.unmet_requirements,
        "diagnostics": result.diagnostics,
        "day_count_issues": result.day_count_issues,
        "preferred_off_issues": result.preferred_off_issues,
        "pattern_issues": result.pattern_issues,
        "preference_issues": result.preference_issues,
        "fairness_issues": result.fairness_issues,
        "consecutive_issues": result.consecutive_issues,
    }

    state["weeks"][week_key]["schedule"] = result_dict
    # auto_assignments는 "자동 생성 직후"의 원본 스냅샷입니다. 이후 수동 조정(manual-adjust)이
    # 있어도 이 값은 덮어쓰지 않아서, 나중에 "사람이 뭘 얼마나 고쳤는지" 비교할 수 있습니다.
    state["weeks"][week_key]["auto_assignments"] = result.assignments
    _log_audit(state, "schedule_generated", f"{week_key} 주 스케줄 자동 생성 ({result.status})",
               {"week_key": week_key, "status": result.status, "assignment_count": len(result.assignments)})
    save_state(company_id, state)

    return jsonify(result_dict)

@schedule_bp.route("/api/weeks/<week_key>/manual-adjust", methods=["POST"])
@require_login
def manual_adjust_week(company_id, week_key):
    state = load_state(company_id)
    if _week_locked(state, week_key):
        return jsonify({"error": "This week is locked. Please unlock it first."}), 403
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    payload = request.get_json()
    schedule = state["weeks"][week_key]["schedule"] or {
        "status": "MANUAL", "assignments": [], "unmet_requirements": [], "diagnostics": [], "day_count_issues": [], "preferred_off_issues": [],
        "pattern_issues": [], "preference_issues": [], "fairness_issues": [], "consecutive_issues": []
    }
    schedule["assignments"] = payload.get("assignments", [])
    schedule["status"] = "MANUAL"
    schedule["diagnostics"] = ["This schedule was manually rearranged."]
    state["weeks"][week_key]["schedule"] = schedule
    _log_audit(state, "schedule_manual_adjust", f"{week_key} 주 스케줄 수동 조정",
               {"week_key": week_key, "assignment_count": len(schedule["assignments"])})
    save_state(company_id, state)
    return jsonify(schedule)

@schedule_bp.route("/api/weeks/<week_key>/reset-schedule", methods=["POST"])
@require_login
def reset_week_schedule(company_id, week_key):
    """이 주의 스케줄(자동 생성 결과 + 수동 조정 결과)만 완전히 초기화합니다.
    근무요건, 휴무(Off) 지정, 잠금 상태는 건드리지 않습니다. Leave Request는
    직원별 전역 데이터라 애초에 이 주차 데이터에 포함되지 않으므로 영향을 받지 않습니다.
    트레이닝 실습용으로 스케줄을 새로 시작하고 싶을 때 사용합니다."""
    state = load_state(company_id)
    if _week_locked(state, week_key):
        return jsonify({"error": "This week is locked. Please unlock it first."}), 403
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    else:
        state["weeks"][week_key]["schedule"] = None
        state["weeks"][week_key]["auto_assignments"] = []
    _log_audit(state, "schedule_reset", f"{week_key} 주 스케줄 초기화", {"week_key": week_key})
    save_state(company_id, state)
    return jsonify({"status": "reset"})

def _analyze_edit_patterns(state):
    """자동 생성된 스케줄(auto_assignments)과 최종 저장된 스케줄(schedule.assignments)을
    주차별로 비교해서, 사람이 반복적으로 고쳐온 패턴을 찾아 규칙으로 제안합니다.
    (진짜 머신러닝이 아니라, 반복 횟수를 세는 단순 집계입니다 — 그래서 항상 "왜 이 제안이
    나왔는지"를 숫자로 설명할 수 있습니다.)"""
    from collections import defaultdict

    day_removed = defaultdict(int)          # (employee_id, day) -> 그 요일 전체가 빠진 횟수
    shift_removed = defaultdict(int)        # (employee_id, shift_type) -> 그 유형에서 빠진 횟수
    day_shift_added = defaultdict(int)      # (employee_id, day, shift_type) -> 자동엔 없었는데 수동으로 넣은 횟수

    for week in state["weeks"].values():
        auto = week.get("auto_assignments")
        final = (week.get("schedule") or {}).get("assignments")
        if not auto or not final:
            continue

        auto_by_emp_day = {(a["employee_id"], a["day"]): a["shift_type"] for a in auto}
        final_by_emp_day = {(a["employee_id"], a["day"]): a["shift_type"] for a in final}

        for (emp_id, day), shift in auto_by_emp_day.items():
            if (emp_id, day) not in final_by_emp_day:
                day_removed[(emp_id, day)] += 1
                shift_removed[(emp_id, shift)] += 1

        for (emp_id, day), shift in final_by_emp_day.items():
            if (emp_id, day) not in auto_by_emp_day:
                day_shift_added[(emp_id, day, shift)] += 1

    employees_by_id = {e["id"]: e for e in state["employees"]}
    suggestions = []

    for (emp_id, day), count in day_removed.items():
        emp = employees_by_id.get(emp_id)
        if emp and count >= MIN_PATTERN_OCCURRENCES and day not in emp.get("preferred_off_days", []):
            suggestions.append({
                "type": "preferred_off_day",
                "employee_id": emp_id, "employee_name": emp["name"],
                "day": day, "count": count,
            })

    for (emp_id, shift), count in shift_removed.items():
        emp = employees_by_id.get(emp_id)
        if emp and count >= MIN_PATTERN_OCCURRENCES and shift not in emp.get("blocked_shift_types", []):
            suggestions.append({
                "type": "blocked_shift_type",
                "employee_id": emp_id, "employee_name": emp["name"],
                "shift_type": shift, "count": count,
            })

    for (emp_id, day, shift), count in day_shift_added.items():
        emp = employees_by_id.get(emp_id)
        if emp and count >= MIN_PATTERN_OCCURRENCES and [day, shift] not in emp.get("preferred", []):
            suggestions.append({
                "type": "preferred_shift",
                "employee_id": emp_id, "employee_name": emp["name"],
                "day": day, "shift_type": shift, "count": count,
            })

    suggestions.sort(key=lambda s: -s["count"])
    return suggestions

@schedule_bp.route("/api/public-holidays", methods=["GET"])
@require_login
def list_public_holidays(company_id):
    state = load_state(company_id)
    return jsonify(sorted(state["public_holidays"], key=lambda h: h["date"]))

@schedule_bp.route("/api/public-holidays", methods=["POST"])
@require_login
def add_public_holiday(company_id):
    state = load_state(company_id)
    payload = request.get_json()
    h_date = payload.get("date")
    name = payload.get("name", "")
    if not h_date:
        return jsonify({"error": "date is required."}), 400
    if any(h["date"] == h_date for h in state["public_holidays"]):
        return jsonify({"error": "This date is already registered."}), 400
    holiday = {"date": h_date, "name": name}
    state["public_holidays"].append(holiday)
    save_state(company_id, state)
    return jsonify(holiday), 201

@schedule_bp.route("/api/public-holidays/<h_date>", methods=["DELETE"])
@require_login
def delete_public_holiday(company_id, h_date):
    state = load_state(company_id)
    state["public_holidays"] = [h for h in state["public_holidays"] if h["date"] != h_date]
    save_state(company_id, state)
    return "", 204


@schedule_bp.route("/api/company/region", methods=["GET"])
@require_login
def get_company_region(company_id):
    """이 회사(매장)가 어느 지역에 있는지 — 지역 기념일(Anniversary Day) 자동계산에
    씁니다. 설정 안 했으면 "none"을 돌려줍니다."""
    state = load_state(company_id)
    return jsonify({"region": state.get("region") or "none", "regions": list(REGIONS)})


@schedule_bp.route("/api/company/region", methods=["POST"])
@require_login
def set_company_region(company_id):
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 작업은 사장 또는 매니저만 할 수 있습니다."}), 403
    payload = request.get_json(silent=True) or {}
    region = payload.get("region")
    if region not in REGIONS:
        return jsonify({"error": "올바르지 않은 지역입니다."}), 400
    state = load_state(company_id)
    state["region"] = region
    save_state(company_id, state)
    return jsonify({"region": region})


@schedule_bp.route("/api/public-holidays/preview", methods=["GET"])
@require_login
def preview_public_holidays(company_id):
    """사장/매니저 전용: 특정 연도의 공휴일을 계산해서 미리보기로 보여줍니다(아직
    저장 안 함) — 전국 공휴일(법으로 확정, needs_confirmation:false)과, 회사에
    지역이 설정되어 있으면 그 지역의 기념일(needs_confirmation:true, 사장님이 한 번
    확인해야 함)을 같이 계산합니다. 이미 등록된 날짜는 already_registered:true로
    표시해서, 확정 등록 화면에서 중복 추가를 피할 수 있게 합니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    try:
        year = int(request.args.get("year"))
    except (TypeError, ValueError):
        return jsonify({"error": "year 파라미터가 필요합니다."}), 400
    if not (2022 <= year <= 2040):
        return jsonify({"error": "2022~2040년만 자동계산을 지원합니다(그 이후는 Matariki 날짜표가 아직 없어서, 수동으로 등록해주세요)."}), 400

    state = load_state(company_id)
    existing_dates = {h["date"] for h in state["public_holidays"]}
    items = _national_holidays_for_year(year)
    region = state.get("region") or "none"
    if region != "none":
        regional = _regional_anniversary_for_year(region, year)
        if regional:
            items.append(regional)
            items.sort(key=lambda x: x["date"])
    for item in items:
        item["already_registered"] = item["date"] in existing_dates
    return jsonify({"year": year, "region": region, "items": items})


@schedule_bp.route("/api/public-holidays/auto-populate", methods=["POST"])
@require_login
def auto_populate_public_holidays(company_id):
    """사장/매니저 전용: preview에서 보여준 목록 중, 사장님이 실제로 선택한 것들만
    한 번에 등록합니다. body: {holidays: [{date, name}, ...]} — 이미 같은 날짜가
    등록되어 있으면 건너뜁니다(중복 방지)."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 작업은 사장 또는 매니저만 할 수 있습니다."}), 403
    payload = request.get_json(silent=True) or {}
    holidays = payload.get("holidays") or []
    if not isinstance(holidays, list):
        return jsonify({"error": "holidays는 배열이어야 합니다."}), 400

    state = load_state(company_id)
    existing_dates = {h["date"] for h in state["public_holidays"]}
    added = []
    for h in holidays:
        h_date = h.get("date")
        name = h.get("name", "")
        if not h_date or h_date in existing_dates:
            continue
        entry = {"date": h_date, "name": name}
        # Mondayisation 짝 정보(원래날짜/옮겨진날짜)가 있으면 같이 저장합니다 — 급여
        # 계산에서 "이 직원한텐 어느 쪽이 진짜 공휴일인지" 판단하는 데 씁니다.
        if h.get("pair_date"):
            entry["pair_date"] = h["pair_date"]
            entry["mondayised"] = True
        state["public_holidays"].append(entry)
        existing_dates.add(h_date)
        added.append(entry)
    if added:
        _log_audit(state, "public_holidays_auto_populated",
                   f"{len(added)}개 공휴일 자동 등록: " + ", ".join(f"{h['date']}({h['name']})" for h in added))
        save_state(company_id, state)
    return jsonify({"added": added})


def _carry_in_streak(state, employee_id, week_key):
    """이 주(week_key)가 시작되기 바로 전날부터 거꾸로 하루씩 확인하며, 쉬지 않고
    연속으로 근무한 날 수를 셉니다. 쉰 날(또는 기록이 없는 날)을 만나면 그 즉시 멈춥니다.
    최대 MAX_CONSECUTIVE_DAYS만큼만 셉니다(그 이상 정확히 셀 필요가 없으므로)."""
    y, m, d = map(int, week_key.split("-"))
    monday = date(y, m, d)
    streak = 0
    cursor = monday - timedelta(days=1)
    for _ in range(CARRY_IN_LOOKBACK_DAYS):
        cursor_monday = cursor - timedelta(days=cursor.weekday())
        cursor_week_key = cursor_monday.isoformat()
        day_name = DAYS[cursor.weekday()]
        if _worked_that_weekday(state, employee_id, day_name, cursor_week_key):
            streak += 1
            if streak >= MAX_CONSECUTIVE_DAYS:
                break
            cursor -= timedelta(days=1)
        else:
            break
    return streak

def _recent_shift_counts(state, employee_id, week_key, window=RECENT_FAIRNESS_WINDOW_WEEKS):
    """이 주(week_key) 이전 최근 window주 동안, 이 직원이 마감(is_closing) 근무를 몇 번,
    주말(토/일) 근무를 몇 번 했는지 셉니다. 이번 주 자체는 포함하지 않습니다(아직 계산 전이므로)."""
    closing_ids = {s["id"] for s in state.get("shift_types", []) if s.get("is_closing")}
    y, m, d = map(int, week_key.split("-"))
    base_monday = date(y, m, d)
    night_count = 0
    weekend_count = 0
    for i in range(1, window + 1):
        wk_key = (base_monday - timedelta(weeks=i)).isoformat()
        week = state["weeks"].get(wk_key)
        if not week:
            continue
        assignments = (week.get("schedule") or {}).get("assignments") or []
        for a in assignments:
            if a["employee_id"] != employee_id:
                continue
            if a["shift_type"] in closing_ids:
                night_count += 1
            if a["day"] in ("sat", "sun"):
                weekend_count += 1
    return night_count, weekend_count

@schedule_bp.route("/api/weeks/<week_key>/weekday-frequency", methods=["GET"])
@require_login
def get_weekday_frequency(company_id, week_key):
    """이 주(week_key)를 기준으로, 각 직원이 각 요일에 "몇 주 연속으로" 근무했는지
    (근무유형은 상관없이) 셉니다. 이번 주부터 거슬러 올라가며 세다가, 그 요일에
    근무하지 않은(쉬거나 배정이 없는) 주를 만나면 그 즉시 스트릭이 끊깁니다.
    최대 8주까지만 셉니다. 순전히 참고용 정보이며, 스케줄 생성 로직에는 전혀
    영향을 주지 않습니다(하드 규칙도 소프트 규칙도 아님 — 화면에 숫자로만 표시)."""
    state = load_state(company_id)
    y, m, d = map(int, week_key.split("-"))
    base_monday = date(y, m, d)

    this_week = state["weeks"].get(week_key)
    this_week_assignments = (this_week.get("schedule") or {}).get("assignments") if this_week else None
    pairs = set()
    if this_week_assignments:
        for a in this_week_assignments:
            pairs.add((a["employee_id"], a["day"]))

    counts = {}  # employee_id -> {day: streak}
    for (emp_id, day) in pairs:
        streak = 0
        for i in range(FREQUENCY_WINDOW_WEEKS):
            wk_key = (base_monday - timedelta(weeks=i)).isoformat()
            if _worked_that_weekday(state, emp_id, day, wk_key):
                streak += 1
            else:
                break
        counts.setdefault(emp_id, {})[day] = streak

    return jsonify({"window": FREQUENCY_WINDOW_WEEKS, "highlight_at": FREQUENCY_HIGHLIGHT_THRESHOLD, "counts": counts})

@schedule_bp.route("/api/pattern-suggestions", methods=["GET"])
@require_login
def get_pattern_suggestions(company_id):
    state = load_state(company_id)
    return jsonify(_analyze_edit_patterns(state))
