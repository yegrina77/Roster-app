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
"""

import sys
import time
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import requests
from datetime import date, timedelta, datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, session

from scheduler import (
    Employee, ShiftRequirement, solve_schedule, DAYS, SHIFT_TYPES,
    DEPARTMENTS, DEPARTMENT_LABEL_KO, DEPARTMENT_SHIFTS, SHIFT_LABEL_KO, SHIFT_TIME_RANGES,
    SHIFT_DEFS, MAX_CONSECUTIVE_DAYS,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "..", "data")
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

# Render 등 클라우드 호스팅에서 print() 로그가 실시간으로 안 보이고 쌓여있다가 늦게
# 나오는 문제를 막기 위해, 표준출력을 줄 단위로 즉시 흘려보내도록 강제합니다.
try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
# SECRET_KEY는 로그인 세션(쿠키)을 암호학적으로 서명하는 데 씁니다. 실제 운영 환경에서는
# 반드시 환경변수로 별도 지정해주세요(Render 환경변수에 SECRET_KEY 추가). 로컬 개발 중에는
# 지정 안 해도 임시값으로 자동 동작합니다.
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SECRET_KEY") is not None,  # 운영(SECRET_KEY 지정됨)에서는 HTTPS 전용 쿠키
)

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


# ---------------------------------------------------------------------------
# 비밀번호 재설정 토큰 관리 (auth 저장소 안에 함께 보관)
# ---------------------------------------------------------------------------

RESET_TOKEN_VALID_MINUTES = 60


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


# ---------------------------------------------------------------------------
# 이메일 발송 (Resend). RESEND_API_KEY 환경변수가 없으면 실제 발송 없이
# 콘솔에만 링크를 출력합니다(로컬 개발 중에도 기능을 테스트할 수 있도록).
# ---------------------------------------------------------------------------

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")


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


def _send_signup_request_email(admin_email, requester_name, requester_email):
    return _send_email(
        admin_email,
        f"New Signup Request: {requester_name}",
        (
            f"<p>A new company has requested access to your roster system:</p>"
            f"<p><b>Company:</b> {requester_name}<br><b>Email:</b> {requester_email}</p>"
            f"<p>Log in and open the Admin page to approve or reject this request.</p>"
        ),
    )


def _send_signup_approved_email(to_email):
    return _send_email(
        to_email,
        "Your account has been approved",
        "<p>Your signup request has been approved. You can now log in with the email and password you registered with.</p>",
    )


def _send_signup_rejected_email(to_email):
    return _send_email(
        to_email,
        "Your signup request was not approved",
        "<p>Unfortunately, your signup request was not approved. If you believe this is a mistake, please contact the administrator directly.</p>",
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
    완전히 분리됩니다 — 로그인 안 하면 어떤 데이터도 못 보고 못 바꿉니다."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        company_id = session.get("company_id")
        if not company_id:
            return jsonify({"error": "Login required."}), 401
        _last_active[company_id] = time.time()
        return f(company_id, *args, **kwargs)
    return wrapper


def require_admin(f):
    """관리자(맨 처음 가입한 계정)만 접근 가능한 API에 붙입니다. 다른 회사 데이터를
    직접 다루지 않고, 가입자 통계만 볼 수 있게 하는 용도입니다."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        company_id = session.get("company_id")
        if not company_id:
            return jsonify({"error": "Login required."}), 401
        auth = load_auth()
        company = auth["companies"].get(company_id)
        if not company or not company.get("is_admin"):
            return jsonify({"error": "Admin access only."}), 403
        return f(*args, **kwargs)
    return wrapper


def _default_departments():
    """새 회사가 가입할 때, 그리고 이 기능이 생기기 전에 이미 있던 회사에 채워주는 기본값입니다.
    회사는 이후 자유롭게 이름을 바꾸거나 추가·삭제할 수 있습니다 — 이건 코드에 고정된 값이 아니라
    '시작할 때 미리 채워주는 예시'일 뿐입니다."""
    return [
        {"id": "kitchen", "name": "Kitchen"},
        {"id": "sushi", "name": "Sushi"},
        {"id": "cashier", "name": "Cashier"},
        {"id": "management", "name": "Management"},
        {"id": "training", "name": "Training"},
    ]


def _default_shift_types():
    return [
        {"id": "opening", "name": "Opening", "department_id": "kitchen", "start": "06:00", "end": "15:00", "is_closing": False, "blocked_after_closing": True},
        {"id": "helper", "name": "Helper", "department_id": "kitchen", "start": "08:00", "end": "17:00", "is_closing": False, "blocked_after_closing": True},
        {"id": "closing1", "name": "Closing1", "department_id": "kitchen", "start": "11:00", "end": "20:30", "is_closing": True, "blocked_after_closing": False},
        {"id": "closing2", "name": "Closing2", "department_id": "kitchen", "start": "11:30", "end": "20:30", "is_closing": True, "blocked_after_closing": False},
        {"id": "sushi1", "name": "Sushi 1", "department_id": "sushi", "start": "05:00", "end": "14:00", "is_closing": False, "blocked_after_closing": False},
        {"id": "sushi2", "name": "Sushi 2", "department_id": "sushi", "start": "05:00", "end": "14:00", "is_closing": False, "blocked_after_closing": False},
        {"id": "sushi3", "name": "Sushi 3", "department_id": "sushi", "start": "10:00", "end": "19:00", "is_closing": False, "blocked_after_closing": False},
        {"id": "c_opening", "name": "C.Opening", "department_id": "cashier", "start": "06:00", "end": "15:00", "is_closing": False, "blocked_after_closing": True},
        {"id": "c_helper", "name": "C.Helper", "department_id": "cashier", "start": "10:00", "end": "19:00", "is_closing": False, "blocked_after_closing": True},
        {"id": "c_closing1", "name": "C.Closing1", "department_id": "cashier", "start": "11:00", "end": "20:30", "is_closing": True, "blocked_after_closing": False},
        {"id": "c_closing2", "name": "C.Closing2", "department_id": "cashier", "start": "12:00", "end": "20:30", "is_closing": True, "blocked_after_closing": False},
        {"id": "management", "name": "Management", "department_id": "management", "start": "09:00", "end": "18:00", "is_closing": False, "blocked_after_closing": False},
        {"id": "management_cashier", "name": "Management & Cashier", "department_id": "management", "start": "10:00", "end": "19:00", "is_closing": False, "blocked_after_closing": False},
        {"id": "training", "name": "Training", "department_id": "training", "start": "09:00", "end": "17:00", "is_closing": False, "blocked_after_closing": False},
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
    return state


def save_state(company_id, state):
    _raw_set(f"roster_state:{company_id}", json.dumps(state, ensure_ascii=False))


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
    return {"requirements": [], "off_days": {}, "schedule": None, "auto_assignments": [], "locked": False}


def _prune_expired_leave_requests(leave_requests):
    """이미 끝난(오늘보다 종료일이 이른) Leave Request는 걸러냅니다."""
    today = date.today()
    result = []
    for lr in (leave_requests or []):
        try:
            end = date.fromisoformat(lr["end_date"])
        except (KeyError, ValueError, TypeError):
            continue
        if end >= today:
            result.append(lr)
    return result


def _week_dates(week_key):
    """week_key(그 주 월요일, YYYY-MM-DD)를 기준으로 DAYS 순서에 맞는 실제 날짜 7개를 돌려줍니다."""
    y, m, d = map(int, week_key.split("-"))
    monday = date(y, m, d)
    return [monday + timedelta(days=i) for i in range(7)]


def _leave_forced_days(employee_dict, week_key):
    """이 직원의 Leave Request 중, 이 주(week_key)의 날짜와 겹치는 요일들을 반환합니다."""
    week_dates = _week_dates(week_key)
    forced = []
    for i, day in enumerate(DAYS):
        the_date = week_dates[i]
        for lr in employee_dict.get("leave_requests", []):
            try:
                start = date.fromisoformat(lr["start_date"])
                end = date.fromisoformat(lr["end_date"])
            except (KeyError, ValueError, TypeError):
                continue
            if start <= the_date <= end:
                forced.append(day)
                break
    return forced


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
    for c in auth["companies"].values():
        try:
            state = load_state(c["id"])
            employee_count = len(state.get("employees", []))
            week_count = len(state.get("weeks", {}))
        except Exception:
            employee_count = 0
            week_count = 0
        last_active = _last_active.get(c["id"])
        companies.append({
            "id": c["id"],
            "name": c["name"],
            "email": c["email"],
            "created_at": c.get("created_at"),
            "is_admin": bool(c.get("is_admin")),
            "employee_count": employee_count,
            "week_count": week_count,
            "is_online": bool(last_active) and (now - last_active) < ONLINE_THRESHOLD_SECONDS,
            "last_active_seconds_ago": int(now - last_active) if last_active else None,
        })
    companies.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    return jsonify({"total": len(companies), "companies": companies, "online_threshold_seconds": ONLINE_THRESHOLD_SECONDS})


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


@app.route("/api/admin/pending-signups", methods=["GET"])
@require_admin
def list_pending_signups():
    """관리자 전용: 승인 대기 중인 가입 요청 목록을 보여줍니다."""
    auth = load_auth()
    pending = list(auth.get("pending_signups", {}).values())
    pending.sort(key=lambda r: r.get("requested_at") or "")
    return jsonify({
        "pending": [
            {"id": r["id"], "name": r["name"], "email": r["email"], "requested_at": r.get("requested_at")}
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
        "email": req["email"],
        "password_hash": req["password_hash"],
        "created_at": date.today().isoformat(),
        "is_admin": False,
        "enabled_features": _default_features(),
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


# ---------------------------------------------------------------------------
# 회사별 커스텀 부서 (Department)
# 매장마다 부서 구성이 다를 수 있어서(예: Larder/Protein/Dessert/FOH 등),
# 더 이상 코드에 고정된 목록이 아니라 각 회사가 직접 만들고 관리하는 데이터입니다.
# ---------------------------------------------------------------------------

@app.route("/api/departments", methods=["GET"])
@require_login
def list_departments(company_id):
    state = load_state(company_id)
    return jsonify(state["departments"])


@app.route("/api/departments", methods=["POST"])
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
    save_state(company_id, state)
    return jsonify(dept), 201


@app.route("/api/departments/<dept_id>", methods=["PUT"])
@require_login
def rename_department(company_id, dept_id):
    state = load_state(company_id)
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Department name is required."}), 400
    for d in state["departments"]:
        if d["id"] == dept_id:
            d["name"] = name
            save_state(company_id, state)
            return jsonify(d)
    return jsonify({"error": "Department not found."}), 404


@app.route("/api/departments/<dept_id>", methods=["DELETE"])
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
    save_state(company_id, state)
    return "", 204


# ---------------------------------------------------------------------------
# 회사별 커스텀 근무유형 (Shift Type)
# ---------------------------------------------------------------------------

@app.route("/api/shift-types", methods=["GET"])
@require_login
def list_shift_types(company_id):
    state = load_state(company_id)
    return jsonify(state["shift_types"])


@app.route("/api/shift-types", methods=["POST"])
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
    save_state(company_id, state)
    return jsonify(shift), 201


@app.route("/api/shift-types/<shift_id>", methods=["PUT"])
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
            save_state(company_id, state)
            return jsonify(s)
    return jsonify({"error": "Shift type not found."}), 404


@app.route("/api/shift-types/<shift_id>", methods=["DELETE"])
@require_login
def delete_shift_type(company_id, shift_id):
    state = load_state(company_id)
    state["shift_types"] = [s for s in state["shift_types"] if s["id"] != shift_id]
    state.get("shift_time_overrides", {}).pop(shift_id, None)
    for e in state["employees"]:
        e["blocked_shift_types"] = [s for s in e.get("blocked_shift_types", []) if s != shift_id]
        e["preferred"] = [p for p in e.get("preferred", []) if p[1] != shift_id]
    save_state(company_id, state)
    return "", 204


# ---------------------------------------------------------------------------
# 인증 (회사/매장 단위 로그인 — 회사 하나당 계정 하나)
# ---------------------------------------------------------------------------

@app.route("/api/auth/register", methods=["POST"])
def register():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""

    if not name or not email or not password:
        return jsonify({"error": "Please enter company/store name, email, and password."}), 400
    if not _valid_email(email):
        return jsonify({"error": "Please enter a valid email address."}), 400
    pw_error = _password_error(password)
    if pw_error:
        return jsonify({"error": pw_error}), 400

    auth = load_auth()
    if any(c["email"] == email for c in auth["companies"].values()):
        return jsonify({"error": "This email is already registered."}), 400
    if any(r["email"] == email for r in auth.get("pending_signups", {}).values()):
        return jsonify({"error": "A request with this email is already pending approval."}), 400

    is_first_company = len(auth["companies"]) == 0

    if is_first_company:
        # 맨 처음 가입하는 계정(개발자 본인)은 승인 절차 없이 즉시 생성되고, 자동으로 관리자가 됩니다.
        # (그래야 승인해줄 관리자가 아무도 없는 상태를 피할 수 있습니다.)
        company_id = secrets.token_hex(8)
        auth["companies"][company_id] = {
            "id": company_id,
            "name": name,
            "email": email,
            "password_hash": _hash_password(password),
            "created_at": date.today().isoformat(),
            "is_admin": True,
            "enabled_features": _default_features(),
        }
        save_auth(auth)

        # 로그인 기능이 생기기 전 단일 매장이던 시절의 데이터를 그대로 이어받습니다.
        legacy_raw = _raw_get(LEGACY_STATE_KEY)
        if legacy_raw:
            _raw_set(f"roster_state:{company_id}", legacy_raw)

        session["company_id"] = company_id
        session.permanent = True
        return jsonify({
            "id": company_id, "name": name, "email": email,
            "is_admin": True, "enabled_features": _default_features(),
        }), 201

    # 두 번째 가입자부터는 관리자 승인이 필요합니다. 계정을 바로 만들지 않고
    # "승인 대기" 상태로 저장한 뒤, 관리자(들)에게 이메일로 알립니다.
    request_id = secrets.token_hex(8)
    auth.setdefault("pending_signups", {})[request_id] = {
        "id": request_id,
        "name": name,
        "email": email,
        "password_hash": _hash_password(password),
        "requested_at": date.today().isoformat(),
    }
    save_auth(auth)

    admin_emails = [c["email"] for c in auth["companies"].values() if c.get("is_admin")]
    for admin_email in admin_emails:
        _send_signup_request_email(admin_email, name, email)

    return jsonify({
        "status": "pending",
        "message": "Your request has been submitted for approval. You'll receive an email once it's reviewed.",
    }), 202


@app.route("/api/auth/login", methods=["POST"])
def login():
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""

    auth = load_auth()
    match = None
    for c in auth["companies"].values():
        if c["email"] == email:
            match = c
            break

    if not match or not _verify_password(password, match["password_hash"]):
        return jsonify({"error": "Incorrect email or password."}), 401

    session["company_id"] = match["id"]
    session.permanent = True
    return jsonify({
        "id": match["id"], "name": match["name"], "email": match["email"],
        "is_admin": bool(match.get("is_admin")), "enabled_features": match.get("enabled_features") or _default_features(),
    })


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.pop("company_id", None)
    return "", 204


@app.route("/api/auth/me", methods=["GET"])
def me():
    company_id = session.get("company_id")
    if not company_id:
        return jsonify(None)
    auth = load_auth()
    company = auth["companies"].get(company_id)
    if not company:
        session.pop("company_id", None)
        return jsonify(None)
    return jsonify({
        "id": company["id"], "name": company["name"], "email": company["email"],
        "is_admin": bool(company.get("is_admin")), "enabled_features": company.get("enabled_features") or _default_features(),
    })


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


# ---------------------------------------------------------------------------
# 직원 (여러 주 공통)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 근무유형별 기본 시작/종료 시간 (바쁜 시기가 아니면 30분씩 당기거나 늦추는 식으로
# 관리자가 상황에 맞게 조정할 수 있습니다. 개별 배치를 따로 수정해둔 것(custom_start/end)에는
# 영향을 주지 않습니다.)
# ---------------------------------------------------------------------------

@app.route("/api/shift-time-settings", methods=["GET"])
@require_login
def get_shift_time_settings(company_id):
    state = load_state(company_id)
    times = _effective_shift_times(state)
    overrides = state.get("shift_time_overrides", {})
    return jsonify({
        shift: {"start": s, "end": e, "is_default": shift not in overrides}
        for shift, (s, e) in times.items()
    })


@app.route("/api/shift-time-settings", methods=["POST"])
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


@app.route("/api/shift-time-settings/<shift_type>", methods=["DELETE"])
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


@app.route("/api/employees", methods=["GET"])
@require_login
def list_employees(company_id):
    state = load_state(company_id)
    out = []
    for e in state["employees"]:
        e2 = dict(e)
        e2["leave_requests"] = _prune_expired_leave_requests(e.get("leave_requests", []))
        out.append(e2)
    return jsonify(out)


@app.route("/api/employees", methods=["POST"])
@require_login
def add_employee(company_id):
    state = load_state(company_id)
    payload = request.get_json()

    for f in ["id", "name"]:
        if f not in payload:
            return jsonify({"error": f"'{f}' 필드가 필요합니다."}), 400

    if any(e["id"] == payload["id"] for e in state["employees"]):
        return jsonify({"error": f"An employee with this id already exists: {payload['id']}"}), 400

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
    }
    state["employees"].append(employee)
    save_state(company_id, state)
    return jsonify(employee), 201


@app.route("/api/employees/<employee_id>", methods=["PUT"])
@require_login
def update_employee(company_id, employee_id):
    state = load_state(company_id)
    payload = request.get_json()
    if "leave_requests" in payload:
        payload["leave_requests"] = _prune_expired_leave_requests(payload["leave_requests"])
    for i, e in enumerate(state["employees"]):
        if e["id"] == employee_id:
            state["employees"][i].update(payload)
            save_state(company_id, state)
            return jsonify(state["employees"][i])
    return jsonify({"error": "Employee not found."}), 404


@app.route("/api/employees/<employee_id>", methods=["DELETE"])
@require_login
def delete_employee(company_id, employee_id):
    state = load_state(company_id)
    state["employees"] = [e for e in state["employees"] if e["id"] != employee_id]
    save_state(company_id, state)
    return "", 204


# ---------------------------------------------------------------------------
# 주차 목록/생성
# ---------------------------------------------------------------------------

@app.route("/api/weeks", methods=["GET"])
@require_login
def list_weeks(company_id):
    state = load_state(company_id)
    return jsonify(sorted(state["weeks"].keys()))


@app.route("/api/weeks", methods=["POST"])
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
        }
    else:
        new_week = empty_week()
        new_week["requirements"] = carried_requirements

    state["weeks"][week_key] = new_week
    save_state(company_id, state)
    return jsonify(new_week), 201


@app.route("/api/weeks/<week_key>", methods=["GET"])
@require_login
def get_week(company_id, week_key):
    state = load_state(company_id)
    week = state["weeks"].get(week_key)
    if week is None:
        return jsonify(None)
    return jsonify(week)


@app.route("/api/weeks/<week_key>", methods=["DELETE"])
@require_login
def delete_week(company_id, week_key):
    state = load_state(company_id)
    state["weeks"].pop(week_key, None)
    save_state(company_id, state)
    return "", 204


@app.route("/api/weeks/<week_key>/lock", methods=["POST"])
@require_login
def set_week_lock(company_id, week_key):
    """body: {locked: true/false} - 이 주차를 잠그거나 풉니다. 잠긴 동안엔 이 주의
    근무요건/휴무지정/스케줄 생성·수동조정이 모두 서버에서도 거부됩니다."""
    state = load_state(company_id)
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    payload = request.get_json(silent=True) or {}
    state["weeks"][week_key]["locked"] = bool(payload.get("locked", False))
    save_state(company_id, state)
    return jsonify({"locked": state["weeks"][week_key]["locked"]})


def _week_locked(state, week_key):
    week = state["weeks"].get(week_key)
    return bool(week and week.get("locked"))


# ---------------------------------------------------------------------------
# 주차별 근무요건
# ---------------------------------------------------------------------------

@app.route("/api/weeks/<week_key>/requirements", methods=["POST"])
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
    save_state(company_id, state)
    return jsonify(state["weeks"][week_key]["requirements"])


# ---------------------------------------------------------------------------
# 주차별 휴무(Off) 지정
# ---------------------------------------------------------------------------

@app.route("/api/weeks/<week_key>/off-days", methods=["POST"])
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


# ---------------------------------------------------------------------------
# 주차별 스케줄 생성/수동조정
# ---------------------------------------------------------------------------

@app.route("/api/weeks/<week_key>/generate-schedule", methods=["POST"])
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
    save_state(company_id, state)

    return jsonify(result_dict)


@app.route("/api/weeks/<week_key>/manual-adjust", methods=["POST"])
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
    save_state(company_id, state)
    return jsonify(schedule)


@app.route("/api/weeks/<week_key>/reset-schedule", methods=["POST"])
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
    save_state(company_id, state)
    return jsonify({"status": "reset"})


MIN_PATTERN_OCCURRENCES = 3


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


# ---------------------------------------------------------------------------
# Public Holiday (연간 공휴일 등록)
# ---------------------------------------------------------------------------

@app.route("/api/public-holidays", methods=["GET"])
@require_login
def list_public_holidays(company_id):
    state = load_state(company_id)
    return jsonify(sorted(state["public_holidays"], key=lambda h: h["date"]))


@app.route("/api/public-holidays", methods=["POST"])
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


@app.route("/api/public-holidays/<h_date>", methods=["DELETE"])
@require_login
def delete_public_holiday(company_id, h_date):
    state = load_state(company_id)
    state["public_holidays"] = [h for h in state["public_holidays"] if h["date"] != h_date]
    save_state(company_id, state)
    return "", 204


# ---------------------------------------------------------------------------
# 참고용 근무 빈도 (연속 스트릭 / 총 횟수)
# ---------------------------------------------------------------------------

FREQUENCY_WINDOW_WEEKS = 8
FREQUENCY_HIGHLIGHT_THRESHOLD = 5  # 이 값(포함) 이상이면 화면에서 강조 표시 (강제 배정 제한 아님)


def _worked_that_weekday(state, employee_id, day, week_key):
    week = state["weeks"].get(week_key)
    if not week:
        return False
    assignments = (week.get("schedule") or {}).get("assignments") or []
    return any(a["employee_id"] == employee_id and a["day"] == day for a in assignments)


CARRY_IN_LOOKBACK_DAYS = 14  # 전주 끝자락부터 최대 이만큼(2주)까지만 거슬러 올라가며 연속근무를 셉니다.


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


RECENT_FAIRNESS_WINDOW_WEEKS = 4  # 공정성 판단에 참고하는 "최근" 기간


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


@app.route("/api/weeks/<week_key>/weekday-frequency", methods=["GET"])
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


HOLIDAY_OWD_THRESHOLD = 5  # 지난 8주(이번 주 포함) 중 이 값(포함) 이상 일했으면 "평소 근무 요일"로 간주


def _weekday_total_count(state, employee_id, day, week_key, window=FREQUENCY_WINDOW_WEEKS):
    """연속 스트릭이 아니라, 지난 window주(이번 주 포함) 동안 그 요일에 일한 '총 횟수'입니다.
    Public Holiday 노동법 판정에는 연속 여부가 아니라 총 횟수를 씁니다."""
    y, m, d = map(int, week_key.split("-"))
    base_monday = date(y, m, d)
    count = 0
    for i in range(window):
        wk_key = (base_monday - timedelta(weeks=i)).isoformat()
        if _worked_that_weekday(state, employee_id, day, wk_key):
            count += 1
    return count


@app.route("/api/weeks/<week_key>/public-holiday-info", methods=["GET"])
@require_login
def get_public_holiday_info(company_id, week_key):
    """이 주(week_key)에 Public Holiday가 포함되어 있으면, 사용자가 정의한 뉴질랜드
    노동법 4가지 기준에 따라 직원별 적용 항목(1.5배+Lieu / 평소급여만 / 1.5배만 / 해당없음)을
    계산해서 돌려줍니다.

    기준: 지난 8주(이번 주 포함) 중 5주 이상 그 요일에 근무했으면 "평소 근무 요일"로 간주합니다.
    ⚠️ 이 계산은 사용자가 정의한 규칙을 그대로 옮긴 것으로, 실제 급여 지급 전에는
    회계/노무 담당자 확인을 권장합니다."""
    state = load_state(company_id)
    y, m, d = map(int, week_key.split("-"))
    monday = date(y, m, d)
    week_dates = [monday + timedelta(days=i) for i in range(7)]

    holidays_this_week = []
    for h in state["public_holidays"]:
        try:
            hd = date.fromisoformat(h["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if hd in week_dates:
            day_idx = week_dates.index(hd)
            holidays_this_week.append({"date": h["date"], "name": h.get("name", ""), "day": DAYS[day_idx]})

    if not holidays_this_week:
        return jsonify({"holidays": [], "categories": {}})

    week = state["weeks"].get(week_key)
    assignments = ((week.get("schedule") or {}).get("assignments") if week else None) or []
    worked_today = {(a["employee_id"], a["day"]) for a in assignments}

    categories = {}
    for holiday in holidays_this_week:
        day = holiday["day"]
        rows = []
        for e in state["employees"]:
            emp_id = e["id"]
            count = _weekday_total_count(state, emp_id, day, week_key)
            is_usual_day = count >= HOLIDAY_OWD_THRESHOLD
            worked = (emp_id, day) in worked_today
            if is_usual_day and worked:
                category = 1
            elif is_usual_day and not worked:
                category = 2
            elif not is_usual_day and worked:
                category = 3
            else:
                category = 4
            rows.append({
                "employee_id": emp_id, "employee_name": e["name"],
                "occurrence_count": count, "is_usual_working_day": is_usual_day,
                "worked_on_holiday": worked, "category": category,
            })
        categories[day] = rows

    return jsonify({"holidays": holidays_this_week, "categories": categories, "threshold": HOLIDAY_OWD_THRESHOLD, "window": FREQUENCY_WINDOW_WEEKS})


@app.route("/api/pattern-suggestions", methods=["GET"])
@require_login
def get_pattern_suggestions(company_id):
    state = load_state(company_id)
    return jsonify(_analyze_edit_patterns(state))


if __name__ == "__main__":
    # PORT 환경변수는 Render 같은 클라우드 호스팅이 실행 시 자동으로 지정해줍니다.
    # 로컬에서 그냥 python app.py로 실행하면 여전히 5000번 포트를 씁니다.
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
