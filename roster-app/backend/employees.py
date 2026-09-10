"""
RosterFlow 직원 관리 모듈.

직원 등록/수정/삭제, PIN 조회·재발급, 비자·자격증 만료 알림을 담당합니다.
helpers.py와 payroll.py(애뉴얼 리브 기념일 처리)에 의존합니다.
"""
from datetime import date, timedelta, datetime, timezone
from flask import request, jsonify, g, Blueprint

from helpers import (
    load_state, save_state, require_login, _log_audit,
    _generate_pin, _employee_id_in_use_globally, _apply_leave_balance_diff,
    _prune_expired_leave_requests, _sanitize_wage, _sanitize_annual_salary,
    _sanitize_documents, _sanitize_date, _ensure_employee_limit,
)
from payroll import _process_annual_leave_anniversary

employees_bp = Blueprint("employees", __name__)

@employees_bp.route("/api/employees", methods=["GET"])
@require_login
def list_employees(company_id):
    state = load_state(company_id)
    # 조회할 때마다, 입사일이 지난 기념일(12개월 단위)이 있으면 애뉴얼 리브 4주를
    # 자동으로 지급합니다 — 별도 예약 작업(cron) 없이도, 누군가 직원 목록을 볼 때마다
    # 자연스럽게 체크되는 구조입니다.
    any_changed = False
    for e in state["employees"]:
        if _process_annual_leave_anniversary(e, state):
            any_changed = True
    if any_changed:
        save_state(company_id, state)
    out = []
    for e in state["employees"]:
        e2 = dict(e)
        e2["leave_requests"] = _prune_expired_leave_requests(e.get("leave_requests", []))
        # PIN은 여기서 절대 내려주지 않습니다 — 화면에 뒤섞여 노출되지 않도록, 전용
        # 엔드포인트(/api/employees/pins)로만 조회할 수 있게 분리해뒀습니다.
        e2.pop("pin", None)
        e2.pop("pin_failed_attempts", None)
        e2.pop("pin_locked_until", None)
        out.append(e2)
    return jsonify(out)

@employees_bp.route("/api/employees", methods=["POST"])
@require_login
def add_employee(company_id):
    state = load_state(company_id)
    payload = request.get_json()

    for f in ["id", "name"]:
        if f not in payload:
            return jsonify({"error": f"'{f}' 필드가 필요합니다."}), 400

    if any(e["id"] == payload["id"] for e in state["employees"]):
        return jsonify({"error": f"An employee with this id already exists: {payload['id']}"}), 400

    # 직원 로그인이 ID+PIN만으로 이루어지기 때문에, 직원 ID는 전체 시스템에서 고유해야
    # 합니다(다른 회사가 먼저 쓴 ID는 못 씀).
    auth = load_auth()
    if _employee_id_in_use_globally(auth, payload["id"], exclude_company_id=company_id):
        return jsonify({"error": f"This employee ID is already in use by another company: {payload['id']}. Please choose a different ID."}), 400

    # 가입할 때 신고한 예상 직원 수를 넘어서 등록하지 못하도록 막습니다.
    company = auth["companies"].get(company_id)
    if company is not None:
        current_count = len(state["employees"])
        limit = _ensure_employee_limit(auth, company, current_count)
        save_auth(auth)  # _ensure_employee_limit이 방금 기본값을 채워 넣었을 수 있으므로 저장
        if current_count >= limit:
            return jsonify({
                "error": (
                    f"등록 가능한 직원 수({limit}명)를 초과했습니다. "
                    "더 많은 인원이 필요하시면 관리자에게 한도 증가를 요청해주세요."
                ),
                "error_code": "employee_limit_exceeded",
                "employee_limit": limit,
                "employee_count": current_count,
            }), 400

    employee = {
        "id": payload["id"],
        "name": payload["name"],
        "department": payload.get("department", "kitchen"),
        "min_hours_per_week": payload.get("min_hours_per_week", 30),
        "target_days_per_week": payload.get("target_days_per_week"),
        "blocked_shift_types": payload.get("blocked_shift_types", []),
        "day_off_pattern": payload.get("day_off_pattern"),
        "preferred": payload.get("preferred", []),
        "preferred_off_days": payload.get("preferred_off_days", []),
        "leave_requests": _prune_expired_leave_requests(payload.get("leave_requests", [])),
        "recent_night_count": payload.get("recent_night_count", 0),
        "recent_weekend_count": payload.get("recent_weekend_count", 0),
        # 급여 방식: "hourly"(시급제, 기본값) 또는 "salary"(연봉제). 연봉제면 hourly_wage
        # 대신 annual_salary를 씁니다 — 둘 다 저장은 해두되, 급여 계산은 pay_type을
        # 보고 어느 쪽을 쓸지 결정합니다.
        "pay_type": payload.get("pay_type") if payload.get("pay_type") in ("hourly", "salary") else "hourly",
        # 시급. 급여/Labour Cost 계산에 쓰입니다 — 설정 안 하면 None(급여 계산에서 제외).
        "hourly_wage": _sanitize_wage(payload.get("hourly_wage")),
        # 연봉(연봉제 직원용). 설정 안 하면 None.
        "annual_salary": _sanitize_annual_salary(payload.get("annual_salary")),
        # 입사일 — 애뉴얼 리브 기념일(12개월마다 4주 자동 발생) 계산의 기준점입니다.
        "hire_date": _sanitize_date(payload.get("hire_date")),
        # 근무시간 고정("fixed") 또는 변동("variable") — OWP(평소 주급) 계산 방식을
        # 결정합니다. 고정이면 계약시간×현재시급, 변동이면 최근 4주 평균 실지급액을 씁니다.
        "hours_type": payload.get("hours_type") if payload.get("hours_type") in ("fixed", "variable") else "fixed",
        # 직원 로그인용 PIN — 등록 시 자동으로 무작위 생성됩니다. 관리자/매니저가
        # "직원 PIN 조회" 화면에서 확인하거나 재발급할 수 있습니다.
        "pin": _generate_pin(),
        "pin_failed_attempts": 0,
        "pin_locked_until": None,
        # Lieu Day 잔액(일 단위)과 애뉴얼 리브 잔액(시간 단위) — 둘 다 0에서 시작합니다.
        # Lieu Day는 공휴일 근무(카테고리 A) 시 자동으로 쌓이고, 애뉴얼 리브는 기념일마다
        # 자동으로 4주씩 발생하거나(입사일이 등록된 경우) 사장이 직접 조정합니다.
        "lieu_day_balance": 0.0,
        "annual_leave_balance_hours": 0.0,
        # 이미 지급된(정산된) 애뉴얼 리브 기념일 목록 — 같은 기념일에 중복으로 4주가
        # 또 발생하지 않도록 기록해둡니다.
        "annual_leave_anniversaries_granted": [],
        # 비자·자격증 등 만료일이 있는 문서 목록 — 각 항목은 {id, doc_type, label, expiry_date}.
        "documents": [],
    }
    state["employees"].append(employee)
    _log_audit(state, "employee_added", f"직원 추가: {employee['name']} ({employee['id']})", {"employee_id": employee["id"]})
    save_state(company_id, state)
    return jsonify(employee), 201

@employees_bp.route("/api/employees/<employee_id>", methods=["PUT"])
@require_login
def update_employee(company_id, employee_id):
    state = load_state(company_id)
    payload = request.get_json()
    # id는 여러 곳(주차별 off_days, 스케줄 배치, leave_requests 등)에서 참조 키로 쓰이기
    # 때문에, 여기서 바뀌면 과거 데이터와 연결이 끊어집니다. 그래서 여기서 바꿀 수 있는
    # 필드를 화이트리스트로 명확히 제한합니다 (id는 절대 이 API로 바꿀 수 없음).
    ALLOWED_FIELDS = {
        "name", "department", "min_hours_per_week", "target_days_per_week",
        "blocked_shift_types", "day_off_pattern", "preferred", "preferred_off_days",
        "leave_requests", "recent_night_count", "recent_weekend_count", "hourly_wage",
        "pay_type", "annual_salary", "documents", "hire_date", "hours_type",
    }
    updates = {k: v for k, v in payload.items() if k in ALLOWED_FIELDS}
    if "leave_requests" in updates:
        updates["leave_requests"] = _prune_expired_leave_requests(updates["leave_requests"])
    if "hourly_wage" in updates:
        updates["hourly_wage"] = _sanitize_wage(updates["hourly_wage"])
    if "annual_salary" in updates:
        updates["annual_salary"] = _sanitize_annual_salary(updates["annual_salary"])
    if "pay_type" in updates and updates["pay_type"] not in ("hourly", "salary"):
        updates["pay_type"] = "hourly"
    if "documents" in updates:
        updates["documents"] = _sanitize_documents(updates["documents"])
    if "hire_date" in updates:
        updates["hire_date"] = _sanitize_date(updates["hire_date"])
    if "hours_type" in updates and updates["hours_type"] not in ("fixed", "variable"):
        updates["hours_type"] = "fixed"
    for i, e in enumerate(state["employees"]):
        if e["id"] == employee_id:
            if "leave_requests" in updates:
                new_balances, balance_error = _apply_leave_balance_diff(e, updates["leave_requests"])
                if balance_error:
                    return jsonify({"error": balance_error}), 400
                updates.update(new_balances)
            state["employees"][i].update(updates)
            _log_audit(state, "employee_updated", f"직원 정보 수정: {state['employees'][i]['name']} ({employee_id})",
                       {"employee_id": employee_id, "fields": list(updates.keys())})
            save_state(company_id, state)
            return jsonify(state["employees"][i])
    return jsonify({"error": "Employee not found."}), 404

@employees_bp.route("/api/employees/<employee_id>", methods=["DELETE"])
@require_login
def delete_employee(company_id, employee_id):
    state = load_state(company_id)
    removed = next((e for e in state["employees"] if e["id"] == employee_id), None)
    state["employees"] = [e for e in state["employees"] if e["id"] != employee_id]
    if removed:
        _log_audit(state, "employee_deleted", f"직원 삭제: {removed['name']} ({employee_id})", {"employee_id": employee_id})
    save_state(company_id, state)
    return "", 204

@employees_bp.route("/api/employees/pins", methods=["GET"])
@require_login
def list_employee_pins(company_id):
    """사장/매니저 전용: 직원들의 로그인 PIN을 조회합니다. 직원이 PIN을 잊어버렸을 때
    확인하는 용도입니다 — 일반 직원 목록 API에는 PIN이 절대 포함되지 않고, 이 전용
    엔드포인트로만 조회할 수 있습니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    state = load_state(company_id)
    return jsonify([
        {"id": e["id"], "name": e["name"], "pin": e.get("pin", "")}
        for e in state["employees"]
    ])

@employees_bp.route("/api/employees/<employee_id>/regenerate-pin", methods=["POST"])
@require_login
def regenerate_employee_pin(company_id, employee_id):
    """사장/매니저 전용: 이 직원의 PIN을 새로 발급하고, 잠금 상태도 같이 풀어줍니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    state = load_state(company_id)
    for e in state["employees"]:
        if e["id"] == employee_id:
            e["pin"] = _generate_pin()
            e["pin_failed_attempts"] = 0
            e["pin_locked_until"] = None
            save_state(company_id, state)
            return jsonify({"id": e["id"], "name": e["name"], "pin": e["pin"]})
    return jsonify({"error": "Employee not found."}), 404

@employees_bp.route("/api/expiring-documents", methods=["GET"])
@require_login
def get_expiring_documents(company_id):
    """사장/매니저 전용: 곧 만료되거나 이미 만료된 비자·자격증을 보여줍니다.
    쿼리파라미터 days(기본 30)로 "며칠 이내 만료"까지 포함할지 조절합니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    state = load_state(company_id)
    today = datetime.now(NZ_TZ).date()
    cutoff = today + timedelta(days=days)
    results = []
    for e in state["employees"]:
        for d in e.get("documents", []):
            try:
                expiry = date.fromisoformat(d["expiry_date"])
            except (KeyError, ValueError, TypeError):
                continue
            if expiry <= cutoff:
                results.append({
                    "employee_id": e["id"], "employee_name": e["name"],
                    "doc_id": d["id"], "doc_type": d["doc_type"], "label": d.get("label", ""),
                    "expiry_date": d["expiry_date"],
                    "days_remaining": (expiry - today).days,
                    "expired": expiry < today,
                })
    results.sort(key=lambda r: r["expiry_date"])
    return jsonify(results)
