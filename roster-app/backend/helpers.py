"""
RosterFlow 공용 헬퍼 모듈.

저장소 읽기/쓰기(Upstash/로컬파일), 인증 데코레이터(require_login 등), 이메일 발송,
급여/리브/지오펜싱/날짜 계산처럼 여러 라우트 모듈(payroll.py, employees.py,
schedule.py, time_tracking.py, app.py)이 공통으로 쓰는 순수 로직을 모아둔 곳입니다.

⚠️ 이 파일은 다른 커스텀 모듈(app, payroll, employees, schedule, time_tracking)을
import하지 않습니다 — 의존관계의 가장 아래 계층이라, 순환 참조를 막기 위해 항상
이 규칙을 지켜야 합니다. 새 헬퍼를 추가할 때도 이 원칙을 유지해주세요.
"""
import sys
import time
import hashlib
import hmac
import html
import json
import math
import os
import random
import re
import secrets
import requests
from datetime import date, timedelta, datetime, timezone
try:
    from zoneinfo import ZoneInfo
    NZ_TZ = ZoneInfo("Pacific/Auckland")
except Exception:
    # 일부 최소 구성 서버 환경엔 시간대 데이터(tzdata)가 없을 수 있습니다 — 그런 경우,
    # 뉴질랜드 서머타임 기간(대략 9월~4월, UTC+13)에 맞춘 고정 오프셋으로 대체합니다.
    # (완벽하진 않지만, 겨울철 UTC+12 구간엔 최대 1시간 오차가 생길 수 있습니다.)
    NZ_TZ = timezone(timedelta(hours=13))
from functools import wraps
from flask import request, jsonify, session, g
from scheduler import DAYS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "..", "data")

# ---------------------------------------------------------------------------
# 원시 저장소 계층 (키-값 하나 읽기/쓰기)
# 환경변수 UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN 이 설정되어 있으면
# Upstash(무료, 영구 저장)에 저장합니다. 설정이 없으면(예: 로컬 개발) 로컬 파일
# (data/<키이름>.json)에 저장합니다.
# ---------------------------------------------------------------------------

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

_upstash_redis = None
if UPSTASH_URL and UPSTASH_TOKEN:
    from upstash_redis import Redis as _UpstashRedis
    _upstash_redis = _UpstashRedis(url=UPSTASH_URL, token=UPSTASH_TOKEN)

AUTH_KEY = "roster_auth_companies"
LEGACY_STATE_KEY = "roster_state"  # 로그인 기능 도입 이전, 단일 매장이던 시절의 데이터 (마이그레이션용)
TOGGLABLE_FEATURES = {
    "public_holiday": "Public Holiday (NZ labour law calculator)",
    "leave_request": "Leave Request",
    "shift_time_settings": "Default Shift Time settings",
    "week_lock": "Week Lock",
    "pattern_suggestions": "Rule suggestions from past edits",
    "weekday_frequency": "Consecutive weekday (N/8) badge",
}
TOGGLABLE_FEATURES = {
    "public_holiday": "Public Holiday (NZ labour law calculator)",
    "leave_request": "Leave Request",
    "shift_time_settings": "Default Shift Time settings",
    "week_lock": "Week Lock",
    "pattern_suggestions": "Rule suggestions from past edits",
    "weekday_frequency": "Consecutive weekday (N/8) badge",
}
RESET_TOKEN_VALID_MINUTES = 60
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")


def _raw_get(key):
    if _upstash_redis is not None:
        return _upstash_redis.get(key)
    path = os.path.join(DATA_DIR, f"{key}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def _raw_set(key, value_str):
    if _upstash_redis is not None:
        _upstash_redis.set(key, value_str)
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(value_str)

def _raw_delete(key):
    if _upstash_redis is not None:
        _upstash_redis.delete(key)
        return
    path = os.path.join(DATA_DIR, f"{key}.json")
    if os.path.exists(path):
        os.remove(path)

def _default_features():
    return {key: True for key in TOGGLABLE_FEATURES}

def load_auth():
    raw = _raw_get(AUTH_KEY)
    if not raw:
        return {"companies": {}, "reset_tokens": {}}
    data = json.loads(raw)
    data.setdefault("companies", {})
    data.setdefault("reset_tokens", {})
    data.setdefault("pending_signups", {})
    data.setdefault("support_requests", {})

    # 관리자 기능이 생기기 전에 이미 가입된 계정들은 is_admin 표시가 없을 수 있습니다.
    # 그런 경우, 가장 먼저 가입한(created_at이 가장 이른) 계정을 자동으로 관리자로 지정합니다.
    needs_save = False
    if data["companies"] and not any(c.get("is_admin") for c in data["companies"].values()):
        oldest = min(data["companies"].values(), key=lambda c: c.get("created_at") or "")
        oldest["is_admin"] = True
        needs_save = True

    # 기능 토글(enabled_features)이 생기기 전에 가입된 계정에는, 전부 켜진 기본값을 채워줍니다.
    for c in data["companies"].values():
        if "enabled_features" not in c:
            c["enabled_features"] = _default_features()
            needs_save = True

    if needs_save:
        save_auth(data)

    return data

def save_auth(auth):
    _raw_set(AUTH_KEY, json.dumps(auth, ensure_ascii=False))

def _create_reset_token(auth, company_id):
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_VALID_MINUTES)).isoformat()
    auth.setdefault("reset_tokens", {})[token] = {"company_id": company_id, "expires_at": expires_at}
    return token

def _consume_reset_token(auth, token):
    """토큰이 유효하면(존재하고 아직 안 만료됐으면) company_id를 반환하고, 토큰을 즉시 삭제합니다
    (한 번 쓰면 재사용 불가). 유효하지 않으면 None을 반환합니다."""
    entry = auth.get("reset_tokens", {}).pop(token, None)
    if not entry:
        return None
    try:
        expires_at = datetime.fromisoformat(entry["expires_at"])
    except ValueError:
        return None
    if datetime.now(timezone.utc) > expires_at:
        return None
    return entry["company_id"]

def _send_email(to_email, subject, html):
    """Resend를 통해 이메일을 보냅니다. RESEND_API_KEY가 없으면 콘솔에만 출력합니다.
    (True, None) 또는 (False, 에러메시지)를 돌려줍니다."""
    key_status = f"len={len(RESEND_API_KEY)}, {RESEND_API_KEY[:6]}..." if RESEND_API_KEY else "not set (empty)"
    print(f"[email debug] RESEND_API_KEY status: {key_status}", flush=True)
    if not RESEND_API_KEY:
        print(f"[email - sending not configured, console only] to={to_email} subject={subject}", flush=True)
        return True, None
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": RESEND_FROM_EMAIL, "to": [to_email], "subject": subject, "html": html},
            timeout=10,
        )
        print(f"[email debug] Resend response: status={resp.status_code} body={resp.text[:300]}", flush=True)
        if resp.status_code < 300:
            return True, None
        print(f"[email send failed] status={resp.status_code} body={resp.text}", flush=True)
        return False, resp.text
    except requests.RequestException as e:
        print(f"[email send exception] {e}", flush=True)
        return False, str(e)

def _send_password_reset_email(to_email, reset_link):
    return _send_email(
        to_email,
        "Password Reset Request",
        (
            f"<p>Click the link below to reset your password. This link is valid "
            f"for {RESET_TOKEN_VALID_MINUTES} minutes.</p>"
            f'<p><a href="{reset_link}">{reset_link}</a></p>'
            f"<p>If you didn't request this, you can safely ignore this email.</p>"
        ),
    )

def _send_signup_request_email(admin_email, requester_name, requester_email, contact_name=None, phone=None):
    contact_line = f"<br><b>Contact:</b> {contact_name}" if contact_name else ""
    phone_line = f"<br><b>Phone:</b> {phone}" if phone else ""
    return _send_email(
        admin_email,
        f"New Signup Request: {requester_name}",
        (
            f"<p>A new company has requested access to your roster system:</p>"
            f"<p><b>Company:</b> {requester_name}<br><b>Email:</b> {requester_email}{contact_line}{phone_line}</p>"
            f"<p>Log in and open the Admin page to approve or reject this request.</p>"
        ),
    )

def _send_signup_approved_email(to_email):
    return _send_email(
        to_email,
        "Your account has been approved",
        "<p>Your signup request has been approved. You can now log in with the email and password you registered with.</p>",
    )

def _send_employee_limit_request_email(admin_email, company_name, company_email, current_count, current_limit, requested_limit):
    return _send_email(
        admin_email,
        f"Employee Limit Increase Requested: {company_name}",
        (
            f"<p><b>{company_name}</b> ({company_email}) has requested a higher employee registration limit.</p>"
            f"<p>Currently using {current_count} of {current_limit} slots"
            f"{f', requesting up to {requested_limit}' if requested_limit else ''}.</p>"
            f"<p>Log in and open the Admin page to update their limit.</p>"
        ),
    )

def _send_account_deleted_email(admin_email, company_name, company_email, reasons, other_text):
    # reasons/other_text는 사용자가 자유롭게 입력하는 값이라, 이메일 본문에 넣기 전에
    # HTML 이스케이프를 거쳐서 이메일 클라이언트에서 깨지거나 악용되지 않도록 합니다.
    safe_reasons = ", ".join(html.escape(r) for r in reasons) if reasons else "(no reason selected)"
    other_line = f"<p><b>Other:</b> {html.escape(other_text)}</p>" if other_text else ""
    return _send_email(
        admin_email,
        f"Account Deleted: {company_name}",
        (
            f"<p><b>{html.escape(company_name)}</b> ({html.escape(company_email)}) has deleted their account "
            f"and all associated roster data.</p>"
            f"<p><b>Reason(s):</b> {safe_reasons}</p>"
            f"{other_line}"
        ),
    )

def _send_signup_rejected_email(to_email):
    return _send_email(
        to_email,
        "Your signup request was not approved",
        "<p>Unfortunately, your signup request was not approved. If you believe this is a mistake, please contact the administrator directly.</p>",
    )

def _send_support_request_email(admin_email, company_name, sender_name, sender_role, message):
    # message는 사용자가 자유롭게 입력하는 값이라, 이메일 본문에 넣기 전에 반드시
    # HTML 이스케이프를 거쳐서 이메일 클라이언트에서 깨지거나 악용되지 않도록 합니다.
    return _send_email(
        admin_email,
        f"Support Request: {company_name}",
        (
            f"<p><b>{html.escape(company_name)}</b>의 {html.escape(sender_name)}"
            f"({'사장' if sender_role == 'owner' else '매니저'})님이 지원 요청을 보냈습니다:</p>"
            f"<p style='white-space:pre-wrap; border-left:3px solid #ccc; padding-left:10px;'>"
            f"{html.escape(message)}</p>"
            f"<p>관리자 패널에서 전체 요청 목록을 확인할 수 있습니다.</p>"
        ),
    )

def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
    return f"{salt}${digest}"

def _verify_password(password, stored_hash):
    try:
        salt, digest = stored_hash.split("$")
    except (ValueError, AttributeError):
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
    return hmac.compare_digest(check, digest)

def _valid_email(email):
    return bool(email) and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) is not None

def _email_in_use(auth, email):
    """이 이메일이 이미 어떤 회사의 사장(owner) 계정이거나, 어떤 회사의 매니저 계정으로
    쓰이고 있는지 전체 시스템에서 확인합니다. 이메일은 로그인 시 유일한 식별자로 쓰이기
    때문에, 사장이든 매니저든 전체에서 겹치면 안 됩니다."""
    for c in auth["companies"].values():
        if c["email"] == email:
            return True
        for u in (c.get("users") or {}).values():
            if u["email"] == email:
                return True
    return False

def _password_error(password):
    """비밀번호가 규칙(8자 이상, 숫자 포함, 특수문자 포함)을 만족하지 않으면 에러 메시지를,
    통과하면 None을 돌려줍니다."""
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[0-9]", password):
        return "Password must include at least one number."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must include at least one special character (e.g. !@#$%)."
    return None

# 회사별 "마지막 활동 시각"을 메모리에만 기록합니다 (서버 재시작하면 초기화되는데,
# 그건 상관없습니다 — 재시작 시점엔 어차피 아무도 접속 중이 아니었다는 뜻이니까요).
# 이 값은 저장소(Upstash/파일)에 안 쓰기 때문에, 매 요청마다 추가 비용이 거의 없습니다.
_last_active = {}
ONLINE_THRESHOLD_SECONDS = 90  # 이 시간 이내에 요청이 있었으면 "접속 중"으로 표시


def require_login(f):
    """이 데코레이터가 붙은 API는 로그인(세션에 company_id가 있는지)을 먼저 확인하고,
    통과하면 첫 번째 인자로 company_id를 넘겨줍니다. 이걸로 회사(매장)마다 데이터가
    완전히 분리됩니다 — 로그인 안 하면 어떤 데이터도 못 보고 못 바꿉니다.

    한 회사 안에는 사장(owner, company 레코드 자체의 이메일/비밀번호로 로그인) 한 명과
    매니저(manager, company["users"]에 별도 계정으로 등록) 여러 명이 있을 수 있습니다.
    둘 다 로그인하면 이 데코레이터를 통과하지만, 누가 로그인했는지는
    g.role("owner"|"manager"), g.user_id, g.user_name, g.user_email로 구분해서
    각 API 안에서 필요하면 추가로 권한을 확인할 수 있게 해둡니다."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        company_id = session.get("company_id")
        if not company_id:
            return jsonify({"error": "Login required."}), 401
        auth = load_auth()
        company = auth["companies"].get(company_id)
        if not company:
            session.clear()
            return jsonify({"error": "Login required."}), 401

        user_id = session.get("user_id")
        if user_id:
            user = (company.get("users") or {}).get(user_id)
            if not user:
                # 매니저 계정이 그 사이 삭제됐을 수 있습니다 — 세션을 정리하고 다시 로그인하게 합니다.
                session.clear()
                return jsonify({"error": "Login required."}), 401
            g.role = "manager"
            g.user_id = user_id
            g.user_name = user.get("name", "")
            g.user_email = user.get("email", "")
        else:
            g.role = "owner"
            g.user_id = None
            g.user_name = company.get("contact_name", "")
            g.user_email = company.get("email", "")
        g.company_id = company_id

        _last_active[company_id] = time.time()
        return f(company_id, *args, **kwargs)
    return wrapper

def require_owner(f):
    """require_login과 같지만, 사장(owner) 본인만 통과시킵니다. 매니저 계정 생성/삭제,
    회사 전체 탈퇴처럼 "매니저에게는 맡길 수 없는" 민감한 작업에 붙입니다."""
    @wraps(f)
    @require_login
    def wrapper(company_id, *args, **kwargs):
        if g.role != "owner":
            return jsonify({"error": "이 작업은 사장(owner) 계정만 할 수 있습니다."}), 403
        return f(company_id, *args, **kwargs)
    return wrapper

def require_employee_login(f):
    """사장/매니저(회사) 로그인과 완전히 별개인, 직원 전용 로그인 세션을 확인합니다.
    통과하면 (company_id, employee_dict)를 앞에 넘겨줍니다. 직원용 화면은 관리 기능이
    전혀 없는 완전히 다른 화면이라, 세션 키 자체를 session["emp_company_id"] /
    session["emp_employee_id"]로 분리해서 사장/매니저 세션과 절대 섞이지 않게 합니다."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        company_id = session.get("emp_company_id")
        employee_id = session.get("emp_employee_id")
        if not company_id or not employee_id:
            return jsonify({"error": "Login required."}), 401
        state = load_state(company_id)
        employee = next((e for e in state["employees"] if e["id"] == employee_id), None)
        if not employee:
            session.clear()
            return jsonify({"error": "Login required."}), 401
        return f(company_id, employee, *args, **kwargs)
    return wrapper

def require_admin(f):
    """관리자(맨 처음 가입한 계정)만 접근 가능한 API에 붙입니다. 다른 회사 데이터를
    직접 다루지 않고, 가입자 통계만 볼 수 있게 하는 용도입니다. 이 회사 소속 매니저
    계정은(사장이 아니라면) 여기 통과시키지 않습니다 — 이 패널은 전체 회사 데이터를
    다루는 민감한 영역이라, 그 회사의 사장 본인만 접근하게 제한합니다."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        company_id = session.get("company_id")
        if not company_id:
            return jsonify({"error": "Login required."}), 401
        if session.get("user_id"):
            return jsonify({"error": "Admin access only."}), 403
        auth = load_auth()
        company = auth["companies"].get(company_id)
        if not company or not company.get("is_admin"):
            return jsonify({"error": "Admin access only."}), 403
        _last_active[company_id] = time.time()  # 관리자 본인 접속도 온라인 상태에 반영
        return f(*args, **kwargs)
    return wrapper

def _default_departments():
    """새 회사가 가입할 때, 그리고 이 기능이 생기기 전에 이미 있던 회사에 채워주는 기본값입니다.
    회사는 이후 자유롭게 이름을 바꾸거나 추가·삭제할 수 있습니다 — 이건 코드에 고정된 값이 아니라
    '시작할 때 미리 채워주는 예시'일 뿐입니다. 특정 업종에 치우치지 않도록 중립적인 이름(고객
    응대/내부 업무/관리)으로 구성했습니다."""
    return [
        {"id": "front_of_house", "name": "Front of House"},
        {"id": "back_of_house", "name": "Back of House"},
        {"id": "management", "name": "Management"},
    ]

def _default_shift_types():
    return [
        {"id": "foh_opening", "name": "Opening", "department_id": "front_of_house", "start": "09:00", "end": "17:00", "is_closing": False, "blocked_after_closing": True},
        {"id": "foh_closing", "name": "Closing", "department_id": "front_of_house", "start": "13:00", "end": "21:00", "is_closing": True, "blocked_after_closing": False},
        {"id": "boh_opening", "name": "Opening", "department_id": "back_of_house", "start": "09:00", "end": "17:00", "is_closing": False, "blocked_after_closing": True},
        {"id": "boh_closing", "name": "Closing", "department_id": "back_of_house", "start": "13:00", "end": "21:00", "is_closing": True, "blocked_after_closing": False},
        {"id": "mgmt_opening", "name": "Opening", "department_id": "management", "start": "09:00", "end": "17:00", "is_closing": False, "blocked_after_closing": True},
        {"id": "mgmt_closing", "name": "Closing", "department_id": "management", "start": "13:00", "end": "21:00", "is_closing": True, "blocked_after_closing": False},
    ]

def _slugify_id(name, existing_ids):
    """사람이 입력한 이름(예: "Larder")에서 안전한 내부 id(예: "larder")를 만듭니다.
    이미 있는 id와 겹치면 뒤에 숫자를 붙여 구분합니다."""
    base = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "item"
    slug = base
    n = 2
    while slug in existing_ids:
        slug = f"{base}_{n}"
        n += 1
    return slug

def load_state(company_id):
    raw = _raw_get(f"roster_state:{company_id}")
    if not raw:
        return {
            "employees": [], "weeks": {}, "public_holidays": [], "shift_time_overrides": {},
            "departments": _default_departments(), "shift_types": _default_shift_types(),
            "public_holiday_policy": _default_public_holiday_policy(),
            "time_entries": [], "payroll_rounding_minutes": DEFAULT_PAYROLL_ROUNDING_MINUTES,
            "geofence": _default_geofence(), "audit_log": [], "lieu_day_credits": [],
            "annual_leave_auto_accrual_enabled": False, "annual_leave_accrual_credits": [],
            "earnings_history": {},
        }
    state = json.loads(raw)
    state.setdefault("employees", [])
    state.setdefault("weeks", {})
    state.setdefault("public_holidays", [])
    state.setdefault("shift_time_overrides", {})
    # 이 기능(커스텀 부서/근무유형)이 생기기 전에 이미 만들어진 회사에는, 지금까지 쓰던
    # 고정 부서/근무유형을 그대로 "이 회사의 데이터"로 한 번 채워 넣어줍니다. 이후로는
    # 이 회사가 자유롭게 수정·추가·삭제할 수 있는 자기 데이터가 됩니다.
    state.setdefault("departments", _default_departments())
    state.setdefault("shift_types", _default_shift_types())
    # 회사별 공휴일 급여 정책 커스터마이징 기능이 생기기 전에 가입한 회사에는, 지금까지
    # 모든 회사에 똑같이 적용되던 계산 기준(8주 중 5주 이상)을 그대로 자기 회사 설정값으로
    # 채워 넣어줍니다. 이후로는 이 회사가 Settings에서 자유롭게 바꿀 수 있는 자기 데이터입니다.
    state.setdefault("public_holiday_policy", _default_public_holiday_policy())
    # 클락인/아웃 기록(time_entries)과 급여 반올림 단위 — 이 기능이 생기기 전 회사에는
    # 빈 기록/기본 반올림 단위(15분)로 채워 넣습니다.
    state.setdefault("time_entries", [])
    state.setdefault("payroll_rounding_minutes", DEFAULT_PAYROLL_ROUNDING_MINUTES)
    # 지오펜싱(매장 위치 기반 클락인 검증) — 기본은 꺼진 상태이고, 사장이 설정해야 켜집니다.
    state.setdefault("geofence", _default_geofence())
    # 매니저/사장 활동 감사기록 — 이 기능이 생기기 전 회사는 빈 기록으로 시작합니다.
    state.setdefault("audit_log", [])
    # 이미 크레딧된 (직원,공휴일날짜) Lieu Day 기록 — 중복 크레딧 방지용.
    state.setdefault("lieu_day_credits", [])
    # 애뉴얼 리브 자동 적립(매주 급여의 8%) — 기본은 꺼짐. 사장이 명시적으로 켜야
    # 적용됩니다(잔액에 영향을 주는 자동화라, 조용히 켜져있지 않도록).
    state.setdefault("annual_leave_auto_accrual_enabled", False)
    state.setdefault("annual_leave_accrual_credits", [])
    # 직원별 주간 실지급액 이력 — OWP(변동시간)/AWE 계산에 씁니다. key는
    # "직원ID|week_key" 형태의 문자열이고, 지난 주가 완전히 끝난 뒤부터 하나씩 쌓입니다.
    state.setdefault("earnings_history", {})
    return state

def save_state(company_id, state):
    _raw_set(f"roster_state:{company_id}", json.dumps(state, ensure_ascii=False))

AUDIT_LOG_MAX_ENTRIES = 2000  # 너무 오래 쌓이지 않도록 상한을 두고, 오래된 것부터 잘라냅니다.


def _log_audit(state, action, description, details=None):
    """매니저/사장이 관리 작업을 할 때마다 "누가·언제·무엇을·왜"를 기록합니다.
    require_login이 채워둔 g.user_name/g.role을 그대로 씁니다 — 이 함수를 호출하는
    시점엔 항상 로그인된 사장/매니저 컨텍스트 안에 있어야 합니다. state를 메모리상
    에서만 수정하므로, 호출한 쪽에서 반드시 save_state()를 이어서 호출해야 실제로
    저장됩니다(이미 다른 이유로 save_state를 호출할 예정이라면 그걸로 충분합니다)."""
    entry = {
        "id": secrets.token_hex(8),
        "at": datetime.now(timezone.utc).isoformat(),
        "actor_name": getattr(g, "user_name", "") or "",
        "actor_role": getattr(g, "role", "") or "",
        "action": action,
        "description": description,
        "details": details or {},
    }
    log = state.setdefault("audit_log", [])
    log.append(entry)
    if len(log) > AUDIT_LOG_MAX_ENTRIES:
        del log[: len(log) - AUDIT_LOG_MAX_ENTRIES]

def _effective_shift_times(state):
    """근무유형별 실제 적용되는 시작/종료 시간. 관리자가 조정해둔 값(shift_time_overrides)이
    있으면 그걸 쓰고, 없으면 이 회사가 설정해둔 근무유형 기본값(shift_types)을 씁니다."""
    times = {s["id"]: (s["start"], s["end"]) for s in state.get("shift_types", [])}
    for shift, ov in state.get("shift_time_overrides", {}).items():
        if shift in times and ov.get("start") and ov.get("end"):
            times[shift] = (ov["start"], ov["end"])
    return times

def _effective_shift_hours(state):
    hours = {}
    for shift, (s, e) in _effective_shift_times(state).items():
        sh, sm = map(int, s.split(":"))
        eh, em = map(int, e.split(":"))
        hours[shift] = (eh * 60 + em - sh * 60 - sm) / 60
    return hours

ALLOWED_ROUNDING_MINUTES = (1, 10, 15, 30)
DEFAULT_PAYROLL_ROUNDING_MINUTES = 15


def _round_up_minutes(dt, minutes):
    """클락인 시각에 씁니다. 지정된 분 단위로 항상 올림합니다(예: 30분 단위면
    9:16 → 9:30). 일찍 출근하거나 아주 살짝 늦게 출근해도 그만큼 급여가 더 나가지
    않도록, 절대 직원에게 유리한 쪽으로 반올림하지 않습니다 — 언제나 다음 단위부터
    급여가 계산되기 시작합니다."""
    if minutes <= 1:
        return dt.replace(second=0, microsecond=0)
    dt = dt.replace(second=0, microsecond=0)
    remainder = dt.minute % minutes
    if remainder == 0:
        return dt
    return dt + timedelta(minutes=(minutes - remainder))

def _round_down_minutes(dt, minutes):
    """클락아웃 시각에 씁니다. 지정된 분 단위로 항상 내림(버림)합니다(예: 30분 단위면
    9:46 → 9:30). 조금 늦게 클락아웃을 찍어도 그 여분의 시간만큼 급여가 더 나가지
    않도록, 언제나 이전 단위까지만 급여로 인정합니다."""
    if minutes <= 1:
        return dt.replace(second=0, microsecond=0)
    dt = dt.replace(second=0, microsecond=0)
    remainder = dt.minute % minutes
    return dt - timedelta(minutes=remainder)

def _actual_break_hours(entry):
    """이 클락인/아웃 기록에 실제로 기록된 휴게시간 합계(시간 단위)입니다. 아직 끝나지
    않은(진행 중) 휴게는 계산에서 제외합니다(클락아웃 시점에 자동으로 닫히므로, 완료된
    기록에는 보통 열린 휴게가 남아있지 않습니다)."""
    total_minutes = 0.0
    for br in (entry.get("breaks") or []):
        if not br.get("start") or not br.get("end"):
            continue
        try:
            start = datetime.fromisoformat(br["start"])
            end = datetime.fromisoformat(br["end"])
        except (TypeError, ValueError):
            continue
        total_minutes += max(0.0, (end - start).total_seconds() / 60)
    return total_minutes / 60

def _sanitize_breaks(breaks, clock_in=None, clock_out=None):
    """관리자가 직접 입력한 휴게 시작/종료 목록을 검증합니다. 각 휴게는 시작<종료여야
    하고, 가능하면(클락인/아웃 시각이 주어졌으면) 그 근무 시간 범위 안에 있어야
    합니다. 잘못된 항목이 있으면 (None, 에러메시지)를 돌려주고, 성공하면
    (정리된 목록, None)을 돌려줍니다. 최대 20개까지만 허용합니다(방어적 상한)."""
    out = []
    try:
        clock_in_dt = datetime.fromisoformat(clock_in) if clock_in else None
        clock_out_dt = datetime.fromisoformat(clock_out) if clock_out else None
    except (TypeError, ValueError):
        clock_in_dt = clock_out_dt = None
    for br in (breaks or [])[:20]:
        br_start, br_end = br.get("start"), br.get("end")
        try:
            start_dt = datetime.fromisoformat(br_start) if br_start else None
            end_dt = datetime.fromisoformat(br_end) if br_end else None
        except (TypeError, ValueError):
            return None, "휴게 시간 형식이 올바르지 않습니다."
        if not start_dt or not end_dt:
            return None, "휴게 시작·종료 시각을 모두 입력해주세요."
        if end_dt <= start_dt:
            return None, "휴게 종료 시각이 시작 시각보다 빠르거나 같을 수 없습니다."
        if clock_in_dt and start_dt < clock_in_dt:
            return None, "휴게 시작 시각이 클락인 시각보다 빠를 수 없습니다."
        if clock_out_dt and end_dt > clock_out_dt:
            return None, "휴게 종료 시각이 클락아웃 시각보다 늦을 수 없습니다."
        out.append({"id": br.get("id") or secrets.token_hex(6), "start": br_start, "end": br_end})
    return out, None

def _actual_hours_for_entry(entry, rounding_minutes):
    """이 클락인/아웃 기록의 실제 근무시간을 계산합니다. 클락인은 올림(늦게 인정),
    클락아웃은 내림(일찍 인정)해서 — 어느 쪽으로도 직원에게 유리하게 반올림되지 않는,
    악용 방지용 "버림" 방식입니다. 그렇게 계산된 시간에서, 실제로 "휴게 시작/종료"
    버튼으로 기록된 휴게시간(무급으로 취급)을 뺍니다 — 스케줄 단계의 고정 1시간
    추정치와 달리, 실제 근무는 진짜 기록된 휴게시간만큼만 뺍니다. 휴게 기록이 아예
    없으면(직원이 버튼을 안 눌렀다면) 아무것도 빼지 않습니다 — 없는 휴게를 추측해서
    임의로 차감하지 않기 위함입니다.
    아직 클락아웃을 안 했으면(진행 중) None을 돌려줍니다."""
    if not entry.get("clock_in") or not entry.get("clock_out"):
        return None
    try:
        clock_in = datetime.fromisoformat(entry["clock_in"])
        clock_out = datetime.fromisoformat(entry["clock_out"])
    except (TypeError, ValueError):
        return None
    rounded_in = _round_up_minutes(clock_in, rounding_minutes)
    rounded_out = _round_down_minutes(clock_out, rounding_minutes)
    minutes = (rounded_out - rounded_in).total_seconds() / 60
    hours = max(0.0, minutes / 60)
    hours -= _actual_break_hours(entry)
    return max(0.0, hours)

PAYROLL_BREAK_HOURS = 1  # 프론트엔드 캘린더의 "요일별 총 근무시간" 표시와 반드시 같은 기준을


def _assignment_duration_hours(a, shift_times):
    """근무 배정(assignment) 하나의 실제 근무시간(휴게시간 차감)을 계산합니다.
    프론트엔드의 assignmentDurationHours()와 정확히 같은 로직이어야 합니다 — 캘린더
    상단에 뜨는 '요일별 총 근무시간'과 급여 계산의 기준 시간이 서로 다르면 안 되므로."""
    default_start, default_end = shift_times.get(a["shift_type"], ("09:00", "17:00"))
    start = a.get("custom_start") or default_start
    end = a.get("custom_end") or default_end
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    minutes = (eh * 60 + em) - (sh * 60 + sm)
    if minutes < 0:
        minutes += 24 * 60  # 자정을 넘어가는 근무 대비
    return max(0.0, minutes / 60 - PAYROLL_BREAK_HOURS)

def _scheduler_shift_defs(state):
    """이 회사가 만든 부서/근무유형(state["departments"], state["shift_types"])을,
    scheduler.py의 solve_schedule()이 이해하는 형태로 변환합니다. 이렇게 하면 스케줄
    계산 엔진은 "Kitchen이 뭔지" 전혀 몰라도, 이 회사가 정의한 목록만 갖고 계산합니다."""
    shift_types = [s["id"] for s in state.get("shift_types", [])]
    shift_defs = {
        s["id"]: {
            "dept": s["department_id"],
            "is_closing": bool(s.get("is_closing")),
            "blocked_after_closing": bool(s.get("blocked_after_closing")),
            "time": (s["start"], s["end"]),
        }
        for s in state.get("shift_types", [])
    }
    departments = [d["id"] for d in state.get("departments", [])]
    return shift_types, shift_defs, departments

def empty_week():
    return {
        "requirements": [], "off_days": {}, "schedule": None, "auto_assignments": [], "locked": False,
        # published: 이 주 스케줄이 직원들에게 공개(퍼블리시)됐는지. agreements: 직원별
        # {"agreed": bool, "agreed_at": ISO날짜|None} — 직원이 "확인" 버튼을 눌렀는지 추적합니다.
        # last_published_assignments: 지난번 퍼블리시 시점의 배정 스냅샷 — 재퍼블리시할 때
        # "누구 스케줄이 실제로 바뀌었는지" 비교하는 기준입니다(publish_week 참고).
        "published": False, "agreements": {}, "last_published_assignments": [],
    }

def _sanitize_wage(value):
    """시급 입력값을 검증합니다. 숫자가 아니거나 0 이하/비정상적으로 큰 값(시간당 $1000
    초과 — 오타 방지용 상한)이면 None(미설정)으로 취급합니다. None이면 급여 계산에서
    이 직원은 제외되고, 화면에 '시급 미설정'으로 표시됩니다."""
    try:
        wage = float(value)
    except (TypeError, ValueError):
        return None
    if wage <= 0 or wage > 1000:
        return None
    return round(wage, 2)

def _sanitize_annual_salary(value):
    """연봉 입력값을 검증합니다. 숫자가 아니거나 0 이하/비정상적으로 큰 값(연 $10,000,000
    초과 — 오타 방지용 상한)이면 None(미설정)으로 취급합니다."""
    try:
        salary = float(value)
    except (TypeError, ValueError):
        return None
    if salary <= 0 or salary > 10_000_000:
        return None
    return round(salary, 2)

DOCUMENT_TYPES = (
    "work_visa", "student_visa", "working_holiday_visa", "resident_visa",
    "citizenship", "food_handler_cert", "first_aid_cert", "other",
)

NO_EXPIRY_DOCUMENT_TYPES = ("resident_visa", "citizenship")


def _sanitize_documents(documents):
    """직원의 비자·자격증 등 "만료일이 있는 문서" 목록을 검증/정리합니다. 영주권/
    시민권이 아닌데 만료일이 없거나 형식이 잘못된 항목은 걸러냅니다. 한 직원당
    최대 20개까지만 허용합니다(방어적 제한 — 실제로 이 이상 필요한 경우는 거의
    없을 것입니다)."""
    out = []
    for d in (documents or [])[:20]:
        doc_type = d.get("doc_type") if d.get("doc_type") in DOCUMENT_TYPES else "other"
        expiry = d.get("expiry_date")
        if doc_type in NO_EXPIRY_DOCUMENT_TYPES and not expiry:
            expiry = None
        else:
            try:
                date.fromisoformat(expiry)
            except (TypeError, ValueError):
                continue
        out.append({
            "id": d.get("id") or secrets.token_hex(6),
            "doc_type": doc_type,
            "label": str(d.get("label") or "").strip()[:100],
            "expiry_date": expiry,
        })
    return out

def _sanitize_date(value):
    """날짜 입력값(YYYY-MM-DD)을 검증합니다. 형식이 잘못됐거나 미래 날짜(입사일이
    미래일 수는 없으므로)면 None으로 취급합니다."""
    try:
        d = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if d > date.today():
        return None
    return d.isoformat()

def _weekly_salary(employee_dict):
    """연봉을 52주로 나눈 고정 주급입니다. 연봉제 직원의 스케줄/실제 근무시간과
    무관하게 매주 동일하게 지급되는 기본급입니다."""
    salary = employee_dict.get("annual_salary")
    if salary is None:
        return None
    return round(salary / 52, 2)

def _effective_hourly_rate_for_salary(employee_dict):
    """연봉제 직원의 '환산 시급'입니다 — 공휴일 근무 시 추가수당(0.5배)을 계산하기
    위한 용도로만 씁니다. 연봉 ÷ 52주 ÷ 주당 계약시간(min_hours_per_week)으로
    계산합니다. 계약시간이 0이거나 없으면 계산할 수 없으므로 None을 돌려줍니다."""
    weekly = _weekly_salary(employee_dict)
    weekly_hours = employee_dict.get("min_hours_per_week") or 0
    if weekly is None or weekly_hours <= 0:
        return None
    return weekly / weekly_hours

DEFAULT_GEOFENCE_RADIUS_M = 100  # 디퓨티 등 실제 업체들이 "GPS 오차 감안 시 최소 권장값"으로


def _default_geofence():
    return {"enabled": False, "lat": None, "lng": None, "radius_m": DEFAULT_GEOFENCE_RADIUS_M}

def _haversine_meters(lat1, lng1, lat2, lng2):
    """두 GPS 좌표 사이의 실제 거리(미터)를 계산합니다(지구를 구로 근사)."""
    r = 6371000  # 지구 반지름(미터)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

def _geofence_check(state, lat, lng):
    """이 회사의 지오펜싱 설정 기준으로, 주어진 좌표가 허용 범위 안인지 확인합니다.
    지오펜싱이 꺼져있거나 매장 위치가 아직 설정 안 됐으면 항상 통과시킵니다(기능을
    켜지 않은 회사는 예전처럼 위치 체크 없이 그대로 씁니다).
    돌려주는 값: (허용여부: bool, 거리(m): float|None)"""
    geofence = state.get("geofence") or _default_geofence()
    if not geofence.get("enabled") or geofence.get("lat") is None or geofence.get("lng") is None:
        return True, None
    if lat is None or lng is None:
        return False, None
    distance = _haversine_meters(geofence["lat"], geofence["lng"], lat, lng)
    return distance <= geofence.get("radius_m", DEFAULT_GEOFENCE_RADIUS_M), round(distance, 1)

PIN_LENGTH = 6
PIN_MAX_FAILED_ATTEMPTS = 5
PIN_LOCKOUT_MINUTES = 15


def _generate_pin():
    """6자리 숫자 PIN을 무작위로 생성합니다(직원 로그인용)."""
    return f"{secrets.randbelow(10 ** PIN_LENGTH):0{PIN_LENGTH}d}"

def _employee_id_in_use_globally(auth, employee_id, exclude_company_id=None):
    """직원 로그인이 '회사 선택' 없이 직원 ID + PIN만으로 이루어지기 때문에, 직원 ID는
    이제 전체 시스템에서 고유해야 합니다. 모든 회사의 직원 목록을 뒤져서 이미 쓰이고
    있는 ID인지 확인합니다."""
    for c in auth["companies"].values():
        if exclude_company_id is not None and c["id"] == exclude_company_id:
            continue
        try:
            state = load_state(c["id"])
        except Exception:
            continue
        if any(e["id"] == employee_id for e in state.get("employees", [])):
            return True
    return False

FREQUENCY_WINDOW_WEEKS = 8
HOLIDAY_OWD_THRESHOLD = 5  # 지난 8주(이번 주 포함) 중 이 값(포함) 이상 일했으면 "평소 근무 요일"로 간주


LEAVE_TYPES = ("paid", "unpaid", "sick", "lieu_day", "annual_leave")  # sick/lieu_day/annual_leave도


def _is_paid_leave(leave_type):
    """유급(paid), 병가(sick), Lieu Day, 애뉴얼 리브면 True — 급여 계산과 스케줄러의
    최소시간 크레딧에서 "이미 지급이 약속된 시간"으로 취급합니다. 무급(unpaid)이거나
    값이 없으면 False."""
    return leave_type in ("paid", "sick", "lieu_day", "annual_leave")

def _leave_request_day_span(lr):
    """이 Leave Request가 며칠짜리인지(시작일~종료일 포함) 계산합니다. 날짜 형식이
    잘못됐으면 0을 돌려줍니다."""
    try:
        start = date.fromisoformat(lr["start_date"])
        end = date.fromisoformat(lr["end_date"])
    except (KeyError, ValueError, TypeError):
        return 0
    return max(0, (end - start).days + 1)


def _sanitize_daily_hours(daily_hours, start_date, end_date):
    """직원(또는 관리자)이 "이 날은 8시간, 저 날은 6시간" 식으로 날짜별 시간을 직접
    입력한 경우, 그 값을 검증합니다 — 각 날짜가 신청 기간(start_date~end_date) 안에
    있어야 하고, 시간은 0보다 크고 24 이하여야 합니다. 문제가 있으면 (None, 에러메시지)를,
    비어있거나 없으면 (None, None)을(=평균 자동계산으로 폴백), 정상이면
    (정리된 dict, None)을 돌려줍니다."""
    if not daily_hours:
        return None, None
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except (TypeError, ValueError):
        return None, "날짜 형식이 올바르지 않습니다."
    out = {}
    for day_str, hours in daily_hours.items():
        try:
            d = date.fromisoformat(day_str)
            h = float(hours)
        except (TypeError, ValueError):
            return None, f"날짜별 시간 형식이 올바르지 않습니다 ({day_str})."
        if not (start <= d <= end):
            return None, f"{day_str}은(는) 신청 기간 밖의 날짜입니다."
        if not (0 < h <= 24):
            return None, f"{day_str}의 시간은 0시간 초과 24시간 이하여야 합니다."
        out[day_str] = round(h, 2)
    return (out or None), None


def _leave_request_hours(lr, employee):
    """이 Leave Request의 총 시간을 계산합니다 — 날짜별로 직접 입력한 시간
    (daily_hours)이 있으면 그 합계를, 없으면 "평균 하루시간 × 일수"로 계산합니다.
    잔액 차감, 급여 계산 양쪽에서 재사용합니다."""
    daily_hours = lr.get("daily_hours")
    if daily_hours:
        return sum(float(h) for h in daily_hours.values())
    return _leave_request_day_span(lr) * _average_day_hours(employee)


def _leave_day_hours(lr, employee, the_date_iso):
    """이 Leave Request 중, 특정 하루(the_date_iso)에 해당하는 시간을 돌려줍니다 —
    daily_hours에 그 날짜가 있으면 그 값을, 없으면 평균 하루시간을 씁니다. 급여
    계산에서 "이 요일은 리브로 몇 시간 쳐줄지" 판단할 때 씁니다."""
    daily_hours = lr.get("daily_hours") or {}
    if the_date_iso in daily_hours:
        return float(daily_hours[the_date_iso])
    return _average_day_hours(employee)


def _apply_leave_balance_diff(employee, new_requests):
    """리브 신청 목록이 통째로 교체될 때(update_employee가 항상 이런 식으로 동작하므로),
    Lieu Day/애뉴얼 리브 잔액을 정확히 반영합니다. id가 있는 신청만 비교 대상으로
    삼습니다(id 없는 옛날 데이터는 잔액 계산에서 건너뜁니다 — 이 기능 이전에 만들어진
    신청까지 소급 적용하면 예상치 못하게 잔액이 깎일 수 있으므로).

    ⚠️ status가 "approved"인 신청만 잔액에 반영됩니다("pending"으로 새로 신청된
    건은 아직 잔액에 영향을 주지 않고, 관리자가 승인해서 approved로 바뀌는 순간
    이 함수가 다시 호출되면서 그제서야 차감됩니다 — approve_leave_request가 이
    방식을 그대로 재사용합니다). status 필드가 아예 없는 옛날 데이터는 이 기능
    이전에 만들어진 것이므로 approved로 간주합니다(하위 호환).

    새로 승인되거나 종류가 바뀐 Lieu Day/애뉴얼 리브 신청은 잔액에서 차감하고, 승인이
    취소되거나 종류가 바뀐 신청은 그만큼 잔액을 되돌립니다. 애뉴얼 리브는 날짜별로
    직접 입력한 시간(daily_hours)이 있으면 그 합계를, 없으면 평균 하루시간으로
    계산합니다. 잔액이 모자라면 (None, 에러메시지)를 돌려주고, 성공하면
    (갱신된 잔액 dict, None)을 돌려줍니다.

    애뉴얼 리브는 아직 정식 발생 전이라도(입사 12개월 전) 사장 동의하에 "당겨쓰기"가
    가능합니다(Holidays Act 2003 21A조) — 새로 승인되는 신청에 allow_advance:true가
    붙어있으면, 그 신청 때문에 잔액이 마이너스가 되는 것까지는 허용합니다(Lieu Day는
    당겨쓰기 개념이 없어서 이 예외가 적용되지 않습니다)."""
    old_requests = employee.get("leave_requests") or []
    old_by_id = {r.get("id"): r for r in old_requests if r.get("id") and r.get("status", "approved") == "approved"}
    new_by_id = {r.get("id"): r for r in (new_requests or []) if r.get("id") and r.get("status", "approved") == "approved"}

    lieu_delta_days = 0.0
    annual_delta_hours = 0.0
    today = date.today()
    advance_allowed = False
    for rid in set(old_by_id) | set(new_by_id):
        old_r, new_r = old_by_id.get(rid), new_by_id.get(rid)
        old_type = old_r.get("leave_type") if old_r else None
        new_type = new_r.get("leave_type") if new_r else None
        old_span = _leave_request_day_span(old_r) if old_r else 0
        new_span = _leave_request_day_span(new_r) if new_r else 0
        old_hours = _leave_request_hours(old_r, employee) if old_r else 0
        new_hours = _leave_request_hours(new_r, employee) if new_r else 0
        changed = (old_type, old_hours) != (new_type, new_hours)
        if old_r and (not new_r or changed):
            # 이미 기간이 지나서 자연스럽게 화면에서 사라진 신청(만료)은 "취소"가
            # 아니라 "이미 다 쓴 것"이므로, 잔액을 되돌리지 않습니다. 사용자가 실제로
            # 삭제하거나 승인을 취소한, 아직 기간이 지나지 않은 신청만 환불합니다.
            try:
                old_end = date.fromisoformat(old_r.get("end_date", ""))
            except (ValueError, TypeError):
                old_end = None
            still_active = old_end is None or old_end >= today
            if still_active:
                if old_type == "lieu_day":
                    lieu_delta_days += old_span
                elif old_type == "annual_leave":
                    annual_delta_hours += old_hours
        if new_r and (not old_r or changed):
            if new_type == "lieu_day":
                lieu_delta_days -= new_span
            elif new_type == "annual_leave":
                annual_delta_hours -= new_hours
                if new_r.get("allow_advance"):
                    advance_allowed = True

    new_lieu_balance = employee.get("lieu_day_balance", 0.0) + lieu_delta_days
    new_annual_balance = employee.get("annual_leave_balance_hours", 0.0) + annual_delta_hours

    if new_lieu_balance < -0.01:
        return None, f"Lieu Day 잔액이 부족합니다 (보유: {employee.get('lieu_day_balance', 0):.1f}일)."
    if new_annual_balance < -0.01 and not advance_allowed:
        return None, f"애뉴얼 리브 잔액이 부족합니다 (보유: {employee.get('annual_leave_balance_hours', 0):.1f}시간)."
    return {
        "lieu_day_balance": round(new_lieu_balance, 2),
        "annual_leave_balance_hours": round(new_annual_balance, 2),
    }, None

def _stamp_new_leave_requests(old_requests, new_requests, role, name, default_status):
    """새로 추가되는 Leave Request(기존 목록에 없던 id)에 메타데이터를 채웁니다 —
    누가/언제 신청했는지(submitted_by_role, submitted_by_name, submitted_at), 그리고
    status가 명시되어 있지 않으면 default_status를 붙입니다(관리자가 직접 등록하면
    "approved", 직원이 스스로 신청하면 "pending"). 이미 있던(id가 old에도 있는) 항목은
    건드리지 않습니다 — 수정 중에 신청 이력이 덮어써지면 안 되기 때문입니다.

    날짜별 시간(daily_hours)이 있으면 검증하고, 잘못됐으면 (None, 에러메시지)를
    돌려줍니다. 정상이면 (처리된 목록, None)을 돌려줍니다."""
    old_ids = {r.get("id") for r in (old_requests or []) if r.get("id")}
    now_iso = datetime.now(timezone.utc).isoformat()
    out = []
    for lr in (new_requests or []):
        lr = dict(lr)
        if lr.get("daily_hours"):
            cleaned, err = _sanitize_daily_hours(lr.get("daily_hours"), lr.get("start_date"), lr.get("end_date"))
            if err:
                return None, err
            lr["daily_hours"] = cleaned
        if lr.get("id") and lr["id"] not in old_ids:
            lr.setdefault("status", default_status)
            lr.setdefault("submitted_by_role", role)
            lr.setdefault("submitted_by_name", name)
            lr.setdefault("submitted_at", now_iso)
        out.append(lr)
    return out, None


def _prune_expired_leave_requests(leave_requests):
    """이미 끝난(오늘보다 종료일이 이른) Leave Request는 걸러내고, 사유(reason) 글자수도
    방어적으로 최대 200자로 제한하며, leave_type을 정해진 값으로 정규화합니다.
    leave_type이 없거나 잘못된 값이면 안전하게 "unpaid"로 취급합니다 — 잘못 입력됐다고
    실수로 급여가 더 나가면 안 되기 때문에, 애매하면 항상 무급 쪽으로 기웁니다."""
    today = date.today()
    result = []
    for lr in (leave_requests or []):
        try:
            end = date.fromisoformat(lr["end_date"])
        except (KeyError, ValueError, TypeError):
            continue
        if end >= today:
            lr = dict(lr)
            lr["reason"] = str(lr.get("reason") or "").strip()[:200]
            lr["leave_type"] = lr.get("leave_type") if lr.get("leave_type") in LEAVE_TYPES else "unpaid"
            result.append(lr)
    return result

def _week_dates(week_key):
    """week_key(그 주 월요일, YYYY-MM-DD)를 기준으로 DAYS 순서에 맞는 실제 날짜 7개를 돌려줍니다."""
    y, m, d = map(int, week_key.split("-"))
    monday = date(y, m, d)
    return [monday + timedelta(days=i) for i in range(7)]

def _week_key_for_date(d):
    """이 날짜(date)가 속한 주의 월요일을 week_key(YYYY-MM-DD) 형태로 돌려줍니다.
    클락인 시점에 "오늘이 속한 주"를 찾아서, 그 주 스케줄을 확인했는지 검사할 때 씁니다."""
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()

def _leave_info_by_day(employee_dict, week_key):
    """이 직원의 Leave Request 중, 이 주(week_key)의 날짜와 겹치는 요일들을
    {day: leave_type} 형태로 반환합니다. 하루에 여러 Leave Request가 겹치면 먼저
    찾은 것을 씁니다(정상적인 사용에서는 겹칠 일이 없습니다).

    ⚠️ "승인(approved)"된 신청만 반영합니다 — 아직 관리자 승인 전인 "대기중" 신청이
    스케줄 배정 제외나 급여 계산에 영향을 주면 안 되기 때문입니다(승인되기 전까지는
    확정된 게 아니므로). status 필드가 없는 옛날 데이터는 이 기능 이전에 만들어진
    것이므로 승인된 것으로 간주합니다(하위 호환)."""
    week_dates = _week_dates(week_key)
    info = {}
    for i, day in enumerate(DAYS):
        the_date = week_dates[i]
        for lr in employee_dict.get("leave_requests", []):
            if lr.get("status", "approved") != "approved":
                continue
            try:
                start = date.fromisoformat(lr["start_date"])
                end = date.fromisoformat(lr["end_date"])
            except (KeyError, ValueError, TypeError):
                continue
            if start <= the_date <= end:
                info[day] = lr.get("leave_type") if lr.get("leave_type") in LEAVE_TYPES else "unpaid"
                break
    return info


def _leave_hours_by_day(employee_dict, week_key):
    """이 직원의 (승인된) Leave Request 중, 이 주(week_key)의 날짜와 겹치는 요일들에
    대해 "그날 몇 시간을 리브로 쳐줄지"를 {day: 시간} 형태로 반환합니다 — 날짜별로
    직접 입력한 시간(daily_hours)이 있으면 그 값을, 없으면 평균 하루시간을 씁니다.
    급여 계산에서 avg_day_hours 대신 이 값을 쓰면, 날짜별로 다르게 입력한 시간이
    정확히 반영됩니다."""
    week_dates = _week_dates(week_key)
    out = {}
    for i, day in enumerate(DAYS):
        the_date = week_dates[i]
        the_date_iso = the_date.isoformat()
        for lr in employee_dict.get("leave_requests", []):
            if lr.get("status", "approved") != "approved":
                continue
            try:
                start = date.fromisoformat(lr["start_date"])
                end = date.fromisoformat(lr["end_date"])
            except (KeyError, ValueError, TypeError):
                continue
            if start <= the_date <= end:
                out[day] = _leave_day_hours(lr, employee_dict, the_date_iso)
                break
    return out

def _leave_forced_days(employee_dict, week_key):
    """이 직원의 Leave Request 중, 이 주(week_key)의 날짜와 겹치는 요일들을 반환합니다."""
    return list(_leave_info_by_day(employee_dict, week_key).keys())

def _average_day_hours(employee_dict):
    """이 직원의 '하루 평균 근무시간' 추정치입니다 — 주당 최소시간을 목표 근무일수로
    나눈 값이며, 실제 과거 지급 내역(relevant daily pay)이 아니라 추정치입니다.
    유급/병가 리브 하루치 크레딧, 공휴일 카테고리 B(평소 근무일인데 안 일함) 계산에
    공통으로 씁니다."""
    target_days = employee_dict.get("target_days_per_week") or 5
    return (employee_dict.get("min_hours_per_week") or 0) / target_days

def _credited_leave_hours(employee_dict, week_key):
    """이번 주에 유급/병가 리브로 인정되는 시간의 합계입니다(스케줄러의 최소시간
    크레딧, 급여 계산 양쪽에서 재사용)."""
    leave_info = _leave_info_by_day(employee_dict, week_key)
    avg_hours = _average_day_hours(employee_dict)
    paid_days = sum(1 for lt in leave_info.values() if _is_paid_leave(lt))
    return paid_days * avg_hours

DEFAULT_EMPLOYEE_LIMIT_BUFFER = 10  # 이 기능이 생기기 전에 가입한 회사는 한도가 없었으므로,


def _ensure_employee_limit(auth, company, employee_count):
    """회사 레코드에 employee_limit이 없으면(이 기능이 생기기 전에 가입한 회사),
    현재 등록된 직원 수 + 여유분으로 기본값을 채워 넣고 그 값을 반환합니다.
    호출한 쪽에서 auth를 이미 들고 있다면 이 함수 호출 후 save_auth(auth)를 해줘야 저장됩니다."""
    if company.get("employee_limit") is None:
        company["employee_limit"] = employee_count + DEFAULT_EMPLOYEE_LIMIT_BUFFER
    return company["employee_limit"]

def _week_locked(state, week_key):
    week = state["weeks"].get(week_key)
    return bool(week and week.get("locked"))

def _worked_that_weekday(state, employee_id, day, week_key):
    week = state["weeks"].get(week_key)
    if not week:
        return False
    assignments = (week.get("schedule") or {}).get("assignments") or []
    return any(a["employee_id"] == employee_id and a["day"] == day for a in assignments)

def _default_public_holiday_policy():
    """공휴일 급여 정책이 생기기 전부터 모든 회사에 똑같이 적용되던 계산 기준을
    그대로 기본값으로 씁니다: 지난 8주(이번 주 포함) 중 5주 이상 그 요일에 근무했으면
    '평소 근무 요일'로 간주. 회사는 이후 Settings에서 window_weeks/min_weeks_worked를
    자유롭게 바꾸거나, method 자체를 "actual_only"(과거 기록 없이 그날 근무 여부만 기준)로
    바꿀 수 있습니다."""
    return {
        "method": "threshold",  # "threshold" | "actual_only"
        "window_weeks": FREQUENCY_WINDOW_WEEKS,
        "min_weeks_worked": HOLIDAY_OWD_THRESHOLD,
    }
