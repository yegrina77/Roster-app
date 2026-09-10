"""
로스터 관리 웹앱 - Flask 백엔드 (멀티테넌시 / 로그인 지원)

실행 방법:
    pip install flask ortools upstash-redis
    python app.py
    -> http://localhost:5000 접속

데이터 구조 (회사 계정별로 완전히 분리됨):
- roster_auth_companies: 로그인 계정(회사) 목록 - {company_id: {name, email, password_hash, created_at}}
- roster_state:<company_id>: 그 회사(매장) 하나의 데이터
  - employees: 직원 정보(이름, 최소시간, 목표근무일, 선호도 등) - 여러 주에 걸쳐 공통으로 재사용되는 정보
  - weeks: { "YYYY-MM-DD"(그 주 월요일 날짜): { requirements, off_days, schedule } } - 주차별로 따로 관리되는 정보
  - public_holidays, shift_time_overrides: 회사 전체에 적용되는 설정

--------------------------------------------------------------------------------
파일 구조 (기능별로 분리되어 있습니다):
  app.py           - 이 파일. Flask 앱 생성, 로그인/인증, 관리자 패널, 매니저 관리
  helpers.py       - 공용 헬퍼(저장소, 인증 데코레이터, 이메일, 날짜/급여 기초 계산)
  payroll.py       - 급여 계산, OWP/AWE, 애뉴얼 리브 자동적립/기념일, 공휴일 정책
  employees.py     - 직원 등록/수정/삭제, PIN, 비자·자격증 관리
  schedule.py       - 스케줄 생성/수정/퍼블리시, 근무요건, 부서/근무유형, 공휴일 등록
  time_tracking.py - 클락인/아웃, 휴게시간, 지오펜싱
각 파일은 helpers.py에 의존하고, helpers.py는 다른 커스텀 모듈에 의존하지 않습니다
(순환 참조 방지). 자세한 내용은 각 파일 맨 위 설명을 참고하세요.
"""
import os
import secrets
import time
from datetime import date, timedelta, datetime, timezone
from flask import Flask, request, jsonify, send_from_directory, session, g

from scheduler import DAYS, SHIFT_TYPES, DEPARTMENTS, DEPARTMENT_LABEL_KO, DEPARTMENT_SHIFTS, SHIFT_LABEL_KO, SHIFT_TIME_RANGES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
# SECRET_KEY는 로그인 세션(쿠키)을 암호학적으로 서명하는 데 씁니다. 실제 운영 환경에서는
# 반드시 환경변수로 별도 지정해주세요(Render 환경변수에 SECRET_KEY 추가). 로컬 개발 중에는
# 지정 안 해도 임시값으로 자동 동작합니다.
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SECRET_KEY") is not None,  # 운영(SECRET_KEY 지정됨)에서는 HTTPS 전용 쿠키
)

from helpers import (
    LEGACY_STATE_KEY, TOGGLABLE_FEATURES, _last_active, ONLINE_THRESHOLD_SECONDS,
    FREQUENCY_WINDOW_WEEKS, DEFAULT_EMPLOYEE_LIMIT_BUFFER, PIN_LOCKOUT_MINUTES, PIN_MAX_FAILED_ATTEMPTS,
    _consume_reset_token, _create_reset_token, _default_features, _effective_shift_times,
    _email_in_use, _ensure_employee_limit, _hash_password, _log_audit, _password_error,
    _raw_delete, _raw_get, _raw_set, _send_account_deleted_email, _send_email,
    _send_employee_limit_request_email, _send_password_reset_email, _send_signup_approved_email,
    _send_signup_rejected_email, _send_signup_request_email, _send_support_request_email,
    _valid_email, _verify_password, load_auth, load_state, require_admin,
    require_employee_login, require_login, require_owner, save_auth, save_state,
)

from payroll import payroll_bp
from employees import employees_bp
from schedule import schedule_bp
from time_tracking import time_tracking_bp

app.register_blueprint(payroll_bp)
app.register_blueprint(employees_bp)
app.register_blueprint(schedule_bp)
app.register_blueprint(time_tracking_bp)

@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/api/admin/companies", methods=["GET"])
@require_admin
def list_admin_companies():
    """관리자 전용: 지금까지 가입한 회사(매장) 목록과, 각 회사의 등록 직원 수를 보여줍니다.
    다른 회사의 직원/스케줄 데이터 자체는 절대 보여주지 않고, 딱 '몇 명 등록되어 있는지'
    개수만 보여줍니다 (다른 회사의 개인정보를 침해하지 않기 위함)."""
    auth = load_auth()
    now = time.time()
    companies = []
    auth_dirty = False
    for c in auth["companies"].values():
        try:
            state = load_state(c["id"])
            employee_count = len(state.get("employees", []))
            week_count = len(state.get("weeks", {}))
        except Exception:
            employee_count = 0
            week_count = 0
        if c.get("employee_limit") is None:
            _ensure_employee_limit(auth, c, employee_count)
            auth_dirty = True
        last_active = _last_active.get(c["id"])
        companies.append({
            "id": c["id"],
            "name": c["name"],
            "contact_name": c.get("contact_name", ""),
            "phone": c.get("phone", ""),
            "email": c["email"],
            "created_at": c.get("created_at"),
            "is_admin": bool(c.get("is_admin")),
            "employee_count": employee_count,
            "employee_limit": c.get("employee_limit"),
            "week_count": week_count,
            "is_online": bool(last_active) and (now - last_active) < ONLINE_THRESHOLD_SECONDS,
            "last_active_seconds_ago": int(now - last_active) if last_active else None,
        })
    if auth_dirty:
        save_auth(auth)
    companies.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    return jsonify({"total": len(companies), "companies": companies, "online_threshold_seconds": ONLINE_THRESHOLD_SECONDS})

@app.route("/api/admin/companies/<company_id>/employee-limit", methods=["POST"])
@require_admin
def set_employee_limit(company_id):
    """관리자 전용: 특정 회사의 직원 등록 상한을 직접 수정합니다."""
    payload = request.get_json(silent=True) or {}
    try:
        new_limit = int(payload.get("employee_limit"))
    except (TypeError, ValueError):
        return jsonify({"error": "employee_limit must be a whole number."}), 400
    if new_limit < 1:
        return jsonify({"error": "employee_limit must be at least 1."}), 400

    auth = load_auth()
    company = auth["companies"].get(company_id)
    if not company:
        return jsonify({"error": "Company not found."}), 404
    company["employee_limit"] = new_limit
    save_auth(auth)
    return jsonify({"id": company_id, "employee_limit": new_limit})

@app.route("/api/admin/companies/<company_id>", methods=["DELETE"])
@require_admin
def admin_delete_company(company_id):
    """관리자 전용: 특정 회사(매장) 계정을 강제로 탈퇴(삭제)시킵니다. 계정과 저장된
    로스터 데이터를 전부 지우는 되돌릴 수 없는 작업입니다. 관리자 계정은 이 API로
    지울 수 없게 막아둡니다(관리자가 하나도 안 남는 상황을 막기 위함 — 필요하다면
    먼저 다른 계정에 관리자 권한을 넘긴 뒤 그 계정을 지워야 합니다)."""
    auth = load_auth()
    company = auth["companies"].get(company_id)
    if not company:
        return jsonify({"error": "Company not found."}), 404
    if company.get("is_admin"):
        return jsonify({"error": "관리자 계정은 이 기능으로 삭제할 수 없습니다."}), 400

    del auth["companies"][company_id]
    save_auth(auth)
    _raw_delete(f"roster_state:{company_id}")
    return "", 204

@app.route("/api/admin/companies/<company_id>/features", methods=["GET"])
@require_admin
def get_company_features(company_id):
    """관리자 전용: 특정 회사에 어떤 기능이 켜져있는지 조회합니다."""
    auth = load_auth()
    company = auth["companies"].get(company_id)
    if not company:
        return jsonify({"error": "Company not found."}), 404
    return jsonify({
        "company_id": company_id,
        "company_name": company["name"],
        "features": {key: TOGGLABLE_FEATURES[key] for key in TOGGLABLE_FEATURES},
        "enabled_features": company.get("enabled_features") or _default_features(),
    })

@app.route("/api/admin/companies/<company_id>/features", methods=["POST"])
@require_admin
def set_company_features(company_id):
    """관리자 전용: 특정 회사의 기능 켜기/끄기를 저장합니다. body: {feature_key: true/false, ...}"""
    auth = load_auth()
    company = auth["companies"].get(company_id)
    if not company:
        return jsonify({"error": "Company not found."}), 404
    payload = request.get_json(silent=True) or {}
    current = company.get("enabled_features") or _default_features()
    for key in TOGGLABLE_FEATURES:
        if key in payload:
            current[key] = bool(payload[key])
    company["enabled_features"] = current
    save_auth(auth)
    return jsonify({"company_id": company_id, "enabled_features": current})

@app.route("/api/support-request", methods=["POST"])
@require_login
def submit_support_request(company_id):
    """사장/매니저 전용: 개발자(시스템 관리자)에게 버그 제보·지원 요청을 보냅니다.
    작성한 내용이 관리자 패널의 "지원 요청" 목록에 쌓이고, 동시에 관리자에게
    이메일로도 알림이 갑니다."""
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "내용을 입력해주세요."}), 400
    message = message[:2000]

    auth = load_auth()
    company = auth["companies"].get(company_id)
    if not company:
        return jsonify({"error": "Company not found."}), 404

    request_id = secrets.token_hex(8)
    entry = {
        "id": request_id,
        "company_id": company_id,
        "company_name": company["name"],
        "sender_name": g.user_name, "sender_role": g.role, "sender_email": g.user_email,
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    }
    auth.setdefault("support_requests", {})[request_id] = entry
    save_auth(auth)

    admin_emails = [c["email"] for c in auth["companies"].values() if c.get("is_admin")]
    for admin_email in admin_emails:
        _send_support_request_email(admin_email, company["name"], g.user_name, g.role, message)

    return jsonify({"id": request_id}), 201

@app.route("/api/admin/support-requests", methods=["GET"])
@require_admin
def list_support_requests():
    """관리자(개발자) 전용: 모든 회사로부터 온 지원 요청을 최신순으로 보여줍니다."""
    auth = load_auth()
    requests_list = list(auth.get("support_requests", {}).values())
    requests_list.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return jsonify(requests_list)

@app.route("/api/admin/support-requests/<request_id>/resolve", methods=["POST"])
@require_admin
def resolve_support_request(request_id):
    """관리자(개발자) 전용: 지원 요청을 "처리 완료"로 표시합니다(삭제하지 않고, 목록에서
    구분만 해둡니다 — 나중에 어떤 문의들이 있었는지 계속 참고할 수 있도록)."""
    auth = load_auth()
    req = auth.get("support_requests", {}).get(request_id)
    if not req:
        return jsonify({"error": "Request not found."}), 404
    payload = request.get_json(silent=True) or {}
    req["status"] = "resolved" if payload.get("resolved", True) else "open"
    save_auth(auth)
    return jsonify(req)

@app.route("/api/admin/pending-signups", methods=["GET"])
@require_admin
def list_pending_signups():
    """관리자 전용: 승인 대기 중인 가입 요청 목록을 보여줍니다."""
    auth = load_auth()
    pending = list(auth.get("pending_signups", {}).values())
    pending.sort(key=lambda r: r.get("requested_at") or "")
    return jsonify({
        "pending": [
            {
                "id": r["id"], "name": r["name"],
                "contact_name": r.get("contact_name", ""), "phone": r.get("phone", ""),
                "email": r["email"], "requested_at": r.get("requested_at"),
                "employee_limit": r.get("employee_limit"),
            }
            for r in pending
        ]
    })

@app.route("/api/admin/pending-signups/<request_id>/approve", methods=["POST"])
@require_admin
def approve_pending_signup(request_id):
    """관리자 전용: 가입 요청을 승인해서 실제 계정으로 만듭니다."""
    auth = load_auth()
    req = auth.get("pending_signups", {}).pop(request_id, None)
    if not req:
        return jsonify({"error": "Request not found."}), 404

    company_id = secrets.token_hex(8)
    auth["companies"][company_id] = {
        "id": company_id,
        "name": req["name"],
        "contact_name": req.get("contact_name", ""),
        "phone": req.get("phone", ""),
        "email": req["email"],
        "password_hash": req["password_hash"],
        "created_at": date.today().isoformat(),
        "is_admin": False,
        "enabled_features": _default_features(),
        "employee_limit": req.get("employee_limit") or DEFAULT_EMPLOYEE_LIMIT_BUFFER,
    }
    save_auth(auth)
    _send_signup_approved_email(req["email"])
    return jsonify({"status": "approved", "company_id": company_id})

@app.route("/api/admin/pending-signups/<request_id>/reject", methods=["POST"])
@require_admin
def reject_pending_signup(request_id):
    """관리자 전용: 가입 요청을 거절합니다 (계정이 만들어지지 않습니다)."""
    auth = load_auth()
    req = auth.get("pending_signups", {}).pop(request_id, None)
    if not req:
        return jsonify({"error": "Request not found."}), 404
    save_auth(auth)
    _send_signup_rejected_email(req["email"])
    return jsonify({"status": "rejected"})

@app.route("/api/meta", methods=["GET"])
def get_meta():
    return jsonify({
        "days": DAYS,
        "shift_types": SHIFT_TYPES,
        "departments": DEPARTMENTS,
        "department_labels": DEPARTMENT_LABEL_KO,
        "department_shifts": DEPARTMENT_SHIFTS,
        "shift_labels": SHIFT_LABEL_KO,
        "shift_times": SHIFT_TIME_RANGES,
    })

@app.route("/api/auth/register", methods=["POST"])
def register():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    contact_name = (payload.get("contact_name") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    # phone은 선택 입력이라 값이 없어도 통과시키되, 공백만 입력한 경우는 빈 값으로 취급합니다.
    phone = (payload.get("phone") or "").strip()

    if not name or not contact_name or not email or not password:
        return jsonify({"error": "Please enter company/store name, contact name, email, and password."}), 400
    try:
        employee_limit = int(payload.get("employee_limit"))
    except (TypeError, ValueError):
        return jsonify({"error": "Please enter the number of employees you expect to register."}), 400
    if employee_limit < 1:
        return jsonify({"error": "Number of employees must be at least 1."}), 400
    if not _valid_email(email):
        return jsonify({"error": "Please enter a valid email address."}), 400
    pw_error = _password_error(password)
    if pw_error:
        return jsonify({"error": pw_error}), 400

    auth = load_auth()
    if _email_in_use(auth, email):
        return jsonify({"error": "This email is already registered."}), 400
    if any(r["email"] == email for r in auth.get("pending_signups", {}).values()):
        return jsonify({"error": "A request with this email is already pending approval."}), 400

    is_first_company = len(auth["companies"]) == 0

    if is_first_company:
        # 저장 직전에 한 번 더 확인합니다 — 거의 동시에 두 명이 가입 요청을 보내면,
        # 둘 다 위에서 "회사가 0개"라고 읽었을 수 있습니다. 완벽한 잠금은 아니지만,
        # 이 재확인으로 그 창을 최대한 좁혀서 두 계정이 동시에 관리자가 되는 걸 막습니다.
        auth = load_auth()
        is_first_company = len(auth["companies"]) == 0

    if is_first_company:
        # 맨 처음 가입하는 계정(개발자 본인)은 승인 절차 없이 즉시 생성되고, 자동으로 관리자가 됩니다.
        # (그래야 승인해줄 관리자가 아무도 없는 상태를 피할 수 있습니다.)
        company_id = secrets.token_hex(8)
        auth["companies"][company_id] = {
            "id": company_id,
            "name": name,
            "contact_name": contact_name,
            "phone": phone,
            "email": email,
            "password_hash": _hash_password(password),
            "created_at": date.today().isoformat(),
            "is_admin": True,
            "enabled_features": _default_features(),
            "employee_limit": employee_limit,
        }
        save_auth(auth)

        # 로그인 기능이 생기기 전 단일 매장이던 시절의 데이터를 그대로 이어받습니다.
        legacy_raw = _raw_get(LEGACY_STATE_KEY)
        if legacy_raw:
            _raw_set(f"roster_state:{company_id}", legacy_raw)

        session["company_id"] = company_id
        session.permanent = True
        return jsonify({
            "id": company_id, "name": name, "contact_name": contact_name, "phone": phone, "email": email,
            "role": "owner", "is_admin": True, "enabled_features": _default_features(), "employee_limit": employee_limit,
        }), 201

    # 두 번째 가입자부터는 관리자 승인이 필요합니다. 계정을 바로 만들지 않고
    # "승인 대기" 상태로 저장한 뒤, 관리자(들)에게 이메일로 알립니다.
    request_id = secrets.token_hex(8)
    auth.setdefault("pending_signups", {})[request_id] = {
        "id": request_id,
        "name": name,
        "contact_name": contact_name,
        "phone": phone,
        "email": email,
        "password_hash": _hash_password(password),
        "requested_at": date.today().isoformat(),
        "employee_limit": employee_limit,
    }
    save_auth(auth)

    admin_emails = [c["email"] for c in auth["companies"].values() if c.get("is_admin")]
    for admin_email in admin_emails:
        _send_signup_request_email(admin_email, name, email, contact_name=contact_name, phone=phone)

    return jsonify({
        "status": "pending",
        "message": "Your request has been submitted for approval. You'll receive an email once it's reviewed.",
    }), 202

def _session_payload(company, role, user=None):
    """login()/me()에서 공통으로 쓰는, 로그인한 사용자 정보를 응답 JSON으로 만드는 헬퍼입니다.
    role이 'manager'면 user(그 매니저의 레코드)의 이름/이메일을 쓰고, 'owner'면 회사(사장)
    레코드 자체의 이름/이메일을 씁니다. is_admin(시스템 관리자 패널 접근 권한)은 사장 본인
    로그인일 때만 true가 될 수 있습니다 — 매니저는 그 회사가 시스템 관리자 회사여도
    시스템 관리자 패널에 접근할 수 없습니다."""
    if role == "manager" and user:
        display_name = user.get("name", "")
        display_email = user.get("email", "")
    else:
        display_name = company.get("contact_name", "")
        display_email = company.get("email", "")
    return {
        "id": company["id"], "name": company["name"],
        "contact_name": display_name, "phone": company.get("phone", ""),
        "email": display_email, "role": role,
        "is_admin": bool(company.get("is_admin")) and role == "owner",
        "enabled_features": company.get("enabled_features") or _default_features(),
        "employee_limit": company.get("employee_limit"),
    }

@app.route("/api/auth/login", methods=["POST"])
def login():
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""

    auth = load_auth()

    # 먼저 사장(owner) 계정으로 매칭을 시도합니다 — company 레코드 자체의 이메일입니다.
    for c in auth["companies"].values():
        if c["email"] == email:
            if not _verify_password(password, c["password_hash"]):
                return jsonify({"error": "Incorrect email or password."}), 401
            session.clear()
            session["company_id"] = c["id"]
            session.permanent = True
            return jsonify(_session_payload(c, "owner"))

    # 사장 계정 중엔 없었으니, 각 회사에 등록된 매니저 계정들도 찾아봅니다.
    for c in auth["companies"].values():
        for user_id, u in (c.get("users") or {}).items():
            if u["email"] == email:
                if not _verify_password(password, u["password_hash"]):
                    return jsonify({"error": "Incorrect email or password."}), 401
                session.clear()
                session["company_id"] = c["id"]
                session["user_id"] = user_id
                session.permanent = True
                return jsonify(_session_payload(c, "manager", u))

    return jsonify({"error": "Incorrect email or password."}), 401

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return "", 204

@app.route("/api/auth/me", methods=["GET"])
def me():
    company_id = session.get("company_id")
    if not company_id:
        return jsonify(None)
    auth = load_auth()
    company = auth["companies"].get(company_id)
    if not company:
        session.clear()
        return jsonify(None)

    user_id = session.get("user_id")
    if user_id:
        user = (company.get("users") or {}).get(user_id)
        if not user:
            session.clear()
            return jsonify(None)
        return jsonify(_session_payload(company, "manager", user))

    return jsonify(_session_payload(company, "owner"))

@app.route("/api/auth/account", methods=["DELETE"])
@require_owner
def delete_account(company_id):
    """회사(매장) 계정이 스스로 탈퇴합니다(사장 본인만 가능 — 매니저는 회사 전체를
    탈퇴시킬 수 없습니다). 탈퇴 사유(체크박스+기타 텍스트)를 받아서 관리자에게 이메일로
    통보한 뒤, 계정과 저장된 로스터 데이터를 전부 삭제합니다. 되돌릴 수 없는 작업이라,
    관리자 계정 본인은 이 API로 탈퇴할 수 없게 막아둡니다(관리자가 없어지면 아무도
    승인/관리를 못 하게 되므로)."""
    payload = request.get_json(silent=True) or {}
    reasons = payload.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = []
    reasons = [str(r).strip() for r in reasons if str(r).strip()][:10]  # 방어적으로 개수/타입 제한
    other_text = (payload.get("other_text") or "").strip()[:500]

    auth = load_auth()
    company = auth["companies"].get(company_id)
    if not company:
        return jsonify({"error": "Company not found."}), 404
    if company.get("is_admin"):
        return jsonify({
            "error": "관리자 계정은 이 기능으로 탈퇴할 수 없습니다. 다른 계정에 먼저 관리자 권한을 넘긴 뒤 다시 시도해주세요.",
        }), 400

    admin_emails = [c["email"] for c in auth["companies"].values() if c.get("is_admin")]
    company_name = company["name"]
    company_email = company["email"]

    del auth["companies"][company_id]
    save_auth(auth)
    _raw_delete(f"roster_state:{company_id}")
    session.clear()

    for admin_email in admin_emails:
        _send_account_deleted_email(admin_email, company_name, company_email, reasons, other_text)

    return "", 204

def _send_manager_created_email(manager_email, manager_name, company_name):
    # 비밀번호는 이메일 평문으로 보내지 않습니다 — 사장이 매니저에게 직접(전화, 대면 등)
    # 알려주는 방식이 안전하기 때문에, 이 메일은 "계정이 만들어졌다"는 안내만 담습니다.
    return _send_email(
        manager_email,
        f"{company_name} — Manager account created",
        (
            f"<p>Hi {manager_name},</p>"
            f"<p>A manager account for <b>{company_name}</b> on RosterFlow has been created for you "
            f"({manager_email}). Please contact your manager to get your login password.</p>"
        ),
    )

@app.route("/api/managers", methods=["GET"])
@require_owner
def list_managers(company_id):
    """사장 전용: 이 회사에 등록된 매니저 계정 목록을 보여줍니다(비밀번호는 당연히 뺍니다)."""
    auth = load_auth()
    company = auth["companies"].get(company_id)
    users = (company or {}).get("users") or {}
    managers = [
        {"id": u["id"], "email": u["email"], "name": u.get("name", ""), "created_at": u.get("created_at")}
        for u in users.values()
    ]
    managers.sort(key=lambda u: u.get("created_at") or "")
    return jsonify(managers)

@app.route("/api/managers", methods=["POST"])
@require_owner
def create_manager(company_id):
    """사장 전용: 이 회사에 새 매니저 계정을 만듭니다. 비밀번호는 사장이 직접 정해서
    매니저에게 따로(직접, 전화 등으로) 알려주는 방식입니다 — 이메일로 비밀번호를
    그대로 보내는 건 안전하지 않아서 하지 않습니다."""
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""

    if not name or not email or not password:
        return jsonify({"error": "이름, 이메일, 비밀번호를 모두 입력해주세요."}), 400
    if not _valid_email(email):
        return jsonify({"error": "이메일 형식이 올바르지 않습니다."}), 400
    pw_error = _password_error(password)
    if pw_error:
        return jsonify({"error": pw_error}), 400

    auth = load_auth()
    if _email_in_use(auth, email):
        return jsonify({"error": "이미 사용 중인 이메일입니다."}), 400

    company = auth["companies"].get(company_id)
    if not company:
        return jsonify({"error": "Company not found."}), 404

    user_id = secrets.token_hex(8)
    company.setdefault("users", {})[user_id] = {
        "id": user_id,
        "email": email,
        "name": name,
        "password_hash": _hash_password(password),
        "created_at": date.today().isoformat(),
    }
    save_auth(auth)
    state = load_state(company_id)
    _log_audit(state, "manager_created", f"매니저 계정 생성: {name} ({email})", {"manager_user_id": user_id})
    save_state(company_id, state)
    _send_manager_created_email(email, name, company["name"])
    return jsonify({"id": user_id, "email": email, "name": name}), 201

@app.route("/api/managers/<user_id>", methods=["DELETE"])
@require_owner
def delete_manager(company_id, user_id):
    """사장 전용: 매니저 계정을 삭제합니다. 그 매니저가 로그인해 있었다면, 다음 요청부터
    자동으로 세션이 무효화되고 다시 로그인해야 합니다(require_login이 매번 users
    딕셔너리에서 다시 확인하기 때문입니다)."""
    auth = load_auth()
    company = auth["companies"].get(company_id)
    if not company:
        return jsonify({"error": "Company not found."}), 404
    users = company.get("users") or {}
    if user_id not in users:
        return jsonify({"error": "Manager not found."}), 404
    removed_name = users[user_id].get("name", "")
    del users[user_id]
    save_auth(auth)
    state = load_state(company_id)
    _log_audit(state, "manager_deleted", f"매니저 계정 삭제: {removed_name}", {"manager_user_id": user_id})
    save_state(company_id, state)
    return "", 204

@app.route("/api/auth/forgot-password", methods=["POST"])
def forgot_password():
    """body: {email}. 보안상, 그 이메일이 실제로 등록되어 있는지 여부와 상관없이
    항상 똑같은 성공 메시지를 돌려줍니다(등록된 이메일 목록을 외부에 노출하지 않기 위함)."""
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()

    if email:
        auth = load_auth()
        match = next((c for c in auth["companies"].values() if c["email"] == email), None)
        if match:
            token = _create_reset_token(auth, match["id"])
            save_auth(auth)
            reset_link = f"{request.host_url.rstrip('/')}/?reset_token={token}"
            sent, err = _send_password_reset_email(email, reset_link)
            if not sent:
                print(f"[비밀번호 재설정] {email} 에게 메일 발송 실패 - {err}", flush=True)
        else:
            print(f"[비밀번호 재설정] 등록되지 않은 이메일로 요청됨: {email}", flush=True)

    return jsonify({"status": "ok"})

@app.route("/api/auth/reset-password", methods=["POST"])
def reset_password():
    """body: {token, password}"""
    payload = request.get_json(silent=True) or {}
    token = payload.get("token") or ""
    password = payload.get("password") or ""

    if not token:
        return jsonify({"error": "This reset link is invalid."}), 400
    pw_error = _password_error(password)
    if pw_error:
        return jsonify({"error": pw_error}), 400

    auth = load_auth()
    company_id = _consume_reset_token(auth, token)
    if not company_id or company_id not in auth["companies"]:
        save_auth(auth)  # 만료/사용된 토큰은 정리해서 저장
        return jsonify({"error": "This reset link has expired or already been used. Please request a new one."}), 400

    auth["companies"][company_id]["password_hash"] = _hash_password(password)
    save_auth(auth)
    return jsonify({"status": "ok"})

@app.route("/api/employee-auth/login", methods=["POST"])
def employee_login():
    payload = request.get_json(silent=True) or {}
    employee_id = (payload.get("employee_id") or "").strip()
    pin = (payload.get("pin") or "").strip()
    if not employee_id or not pin:
        return jsonify({"error": "직원 ID와 PIN을 입력해주세요."}), 400

    auth = load_auth()
    for c in auth["companies"].values():
        state = load_state(c["id"])
        employee = next((e for e in state["employees"] if e["id"] == employee_id), None)
        if employee is None:
            continue

        locked_until = employee.get("pin_locked_until")
        if locked_until:
            try:
                if datetime.fromisoformat(locked_until) > datetime.now(timezone.utc):
                    return jsonify({
                        "error": f"PIN을 너무 많이 틀려서 잠겼습니다. {PIN_LOCKOUT_MINUTES}분 후 다시 시도하거나, 관리자에게 문의해주세요.",
                    }), 423
                else:
                    # 잠금 시간이 지났으면, 실패 횟수도 같이 초기화해서 새로 5번의
                    # 기회를 줍니다 — 안 그러면 잠금이 풀린 직후 한 번만 더 틀려도
                    # 곧바로 재잠금되는 문제가 있었습니다.
                    employee["pin_failed_attempts"] = 0
                    employee["pin_locked_until"] = None
            except ValueError:
                pass

        if employee.get("pin") == pin:
            employee["pin_failed_attempts"] = 0
            employee["pin_locked_until"] = None
            save_state(c["id"], state)
            session.clear()
            session["emp_company_id"] = c["id"]
            session["emp_employee_id"] = employee_id
            session.permanent = True
            return jsonify({"id": employee["id"], "name": employee["name"], "company_name": c["name"]})

        employee["pin_failed_attempts"] = employee.get("pin_failed_attempts", 0) + 1
        if employee["pin_failed_attempts"] >= PIN_MAX_FAILED_ATTEMPTS:
            employee["pin_locked_until"] = (
                datetime.now(timezone.utc) + timedelta(minutes=PIN_LOCKOUT_MINUTES)
            ).isoformat()
        save_state(c["id"], state)
        return jsonify({"error": "직원 ID 또는 PIN이 올바르지 않습니다."}), 401

    return jsonify({"error": "직원 ID 또는 PIN이 올바르지 않습니다."}), 401

@app.route("/api/employee-auth/logout", methods=["POST"])
def employee_logout():
    session.clear()
    return "", 204

@app.route("/api/employee-auth/me", methods=["GET"])
def employee_me():
    company_id = session.get("emp_company_id")
    employee_id = session.get("emp_employee_id")
    if not company_id or not employee_id:
        return jsonify(None)
    state = load_state(company_id)
    employee = next((e for e in state["employees"] if e["id"] == employee_id), None)
    if not employee:
        session.clear()
        return jsonify(None)
    auth = load_auth()
    company = auth["companies"].get(company_id)
    return jsonify({
        "id": employee["id"], "name": employee["name"],
        "company_name": company["name"] if company else "",
    })

@app.route("/api/employee-auth/weeks/<week_key>", methods=["GET"])
@require_employee_login
def employee_view_week(company_id, employee, week_key):
    """직원 본인의 이번 주 로스터를 보여줍니다. 퍼블리시되지 않은 주는 아예 안
    보여줍니다 — 관리자가 아직 작업 중인 초안을 직원이 미리 볼 이유가 없습니다.
    본인 근무(shifts)뿐 아니라, 팀 전체가 이번 주에 언제 근무하는지(team_shifts)도
    같이 보여줍니다 — "이번 주에 누구랑 같이 일하는지" 알 수 있게 하기 위함입니다."""
    state = load_state(company_id)
    week = state["weeks"].get(week_key)
    if not week or not week.get("published"):
        return jsonify({"published": False, "shifts": [], "team_shifts": [], "agreed": False, "agreed_at": None})

    schedule = week.get("schedule") or {}
    assignments = schedule.get("assignments") or []
    shift_times = _effective_shift_times(state)
    shift_names = {s["id"]: s["name"] for s in state["shift_types"]}
    employee_names = {e["id"]: e["name"] for e in state["employees"]}

    my_shifts = []
    team_shifts = []
    for a in assignments:
        default_start, default_end = shift_times.get(a["shift_type"], ("", ""))
        row = {
            "day": a["day"], "shift_type": a["shift_type"],
            "shift_name": shift_names.get(a["shift_type"], a["shift_type"]),
            "start": a.get("custom_start") or default_start,
            "end": a.get("custom_end") or default_end,
        }
        if a["employee_id"] == employee["id"]:
            my_shifts.append(row)
        else:
            team_row = dict(row)
            team_row["employee_name"] = employee_names.get(a["employee_id"], "?")
            team_shifts.append(team_row)

    agreement = (week.get("agreements") or {}).get(employee["id"], {})
    return jsonify({
        "published": True,
        "shifts": my_shifts,
        "team_shifts": team_shifts,
        "agreed": bool(agreement.get("agreed")),
        "agreed_at": agreement.get("agreed_at"),
    })

@app.route("/api/employee-auth/weeks/<week_key>/agree", methods=["POST"])
@require_employee_login
def employee_agree_week(company_id, employee, week_key):
    state = load_state(company_id)
    week = state["weeks"].get(week_key)
    if not week or not week.get("published"):
        return jsonify({"error": "This week has not been published yet."}), 400
    week.setdefault("agreements", {})[employee["id"]] = {
        "agreed": True, "agreed_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(company_id, state)
    return jsonify({"agreed": True})

@app.route("/api/company/request-employee-limit-increase", methods=["POST"])
@require_login
def request_employee_limit_increase(company_id):
    """회사(매장) 계정이 등록 인원 한도를 늘려달라고 관리자에게 요청합니다.
    한도를 직접 바꾸지는 않고, 관리자에게 이메일 알림만 보냅니다 — 실제 한도 조정은
    관리자가 Admin 페이지에서 직접 승인해야 합니다."""
    payload = request.get_json(silent=True) or {}
    try:
        requested_limit = int(payload.get("requested_limit")) if payload.get("requested_limit") is not None else None
    except (TypeError, ValueError):
        requested_limit = None

    auth = load_auth()
    company = auth["companies"].get(company_id)
    if not company:
        return jsonify({"error": "Company not found."}), 404

    state = load_state(company_id)
    current_count = len(state.get("employees", []))
    current_limit = _ensure_employee_limit(auth, company, current_count)
    save_auth(auth)

    admin_emails = [c["email"] for c in auth["companies"].values() if c.get("is_admin")]
    for admin_email in admin_emails:
        _send_employee_limit_request_email(
            admin_email, company["name"], company["email"], current_count, current_limit, requested_limit,
        )

    return jsonify({"status": "requested"}), 202

@app.route("/api/audit-log", methods=["GET"])
@require_login
def get_audit_log(company_id):
    """사장/매니저 전용: 관리 작업 감사기록을 최신순으로 보여줍니다 — 누가, 언제,
    무엇을 했는지. 직원 추가/수정/삭제, 스케줄 생성/수동조정/초기화, 근무요건 변경,
    부서/근무유형 변경, 공휴일 정책 변경, 매니저 계정 생성/삭제, 주 잠금/퍼블리시,
    급여 계산 단위, 지오펜싱 설정 변경을 다룹니다. limit(기본 200)으로 최근 몇 건을
    가져올지 조절할 수 있습니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    try:
        limit = min(int(request.args.get("limit", 200)), 1000)
    except (TypeError, ValueError):
        limit = 200
    state = load_state(company_id)
    log = state.get("audit_log") or []
    return jsonify(list(reversed(log))[:limit])


if __name__ == "__main__":
    # PORT 환경변수는 Render 같은 클라우드 호스팅이 실행 시 자동으로 지정해줍니다.
    # 로컬에서 그냥 python app.py로 실행하면 여전히 5000번 포트를 씁니다.
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
