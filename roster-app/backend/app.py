"""
로스터 관리 웹앱 - Flask 백엔드

실행 방법:
    pip install flask ortools
    python app.py
    -> http://localhost:5000 접속

데이터 구조:
- employees: 직원 정보(이름, 최소시간, 목표근무일, 선호도 등) - 여러 주에 걸쳐 공통으로 재사용되는 정보
- weeks: { "YYYY-MM-DD"(그 주 월요일 날짜): { requirements, off_days, schedule } } - 주차별로 따로 관리되는 정보
  - requirements: 그 주의 요일×근무유형별 필요인원
  - off_days: { employee_id: [day, ...] } - 그 주에 한해 적용되는 휴무 지정 (캘린더에서 클릭)
  - schedule: 그 주의 생성/수동조정된 스케줄 결과
"""

import json
import os
import random
from datetime import date, timedelta
from flask import Flask, request, jsonify, send_from_directory

from scheduler import (
    Employee, ShiftRequirement, solve_schedule, DAYS, SHIFT_TYPES,
    DEPARTMENTS, DEPARTMENT_LABEL_KO, DEPARTMENT_SHIFTS, SHIFT_LABEL_KO, SHIFT_TIME_RANGES,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "..", "data", "state.json")
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")

# ---------------------------------------------------------------------------
# 데이터 저장소
# 환경변수 UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN 이 설정되어 있으면
# Upstash(무료, 영구 저장)에 저장합니다. 설정이 없으면(예: 로컬 개발) 예전처럼
# 로컬 파일(data/state.json)에 저장합니다.
# ---------------------------------------------------------------------------

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
STATE_KEY = "roster_state"

_upstash_redis = None
if UPSTASH_URL and UPSTASH_TOKEN:
    from upstash_redis import Redis as _UpstashRedis
    _upstash_redis = _UpstashRedis(url=UPSTASH_URL, token=UPSTASH_TOKEN)


def load_state():
    if _upstash_redis is not None:
        raw = _upstash_redis.get(STATE_KEY)
        if not raw:
            return {"employees": [], "weeks": {}, "public_holidays": [], "shift_time_overrides": {}}
        state = json.loads(raw)
        state.setdefault("employees", [])
        state.setdefault("weeks", {})
        state.setdefault("public_holidays", [])
        state.setdefault("shift_time_overrides", {})
        return state

    if not os.path.exists(DATA_PATH):
        return {"employees": [], "weeks": {}, "public_holidays": [], "shift_time_overrides": {}}
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
        state.setdefault("employees", [])
        state.setdefault("weeks", {})
        state.setdefault("public_holidays", [])
        state.setdefault("shift_time_overrides", {})
        return state


def save_state(state):
    if _upstash_redis is not None:
        _upstash_redis.set(STATE_KEY, json.dumps(state, ensure_ascii=False))
        return

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _effective_shift_times(state):
    """근무유형별 실제 적용되는 시작/종료 시간. 관리자가 조정해둔 값(shift_time_overrides)이
    있으면 그걸 쓰고, 없으면 코드에 정의된 기본값(SHIFT_TIME_RANGES)을 씁니다."""
    times = dict(SHIFT_TIME_RANGES)
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
# 직원 (여러 주 공통)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 근무유형별 기본 시작/종료 시간 (바쁜 시기가 아니면 30분씩 당기거나 늦추는 식으로
# 관리자가 상황에 맞게 조정할 수 있습니다. 개별 배치를 따로 수정해둔 것(custom_start/end)에는
# 영향을 주지 않습니다.)
# ---------------------------------------------------------------------------

@app.route("/api/shift-time-settings", methods=["GET"])
def get_shift_time_settings():
    state = load_state()
    times = _effective_shift_times(state)
    overrides = state.get("shift_time_overrides", {})
    return jsonify({
        shift: {"start": s, "end": e, "is_default": shift not in overrides}
        for shift, (s, e) in times.items()
    })


@app.route("/api/shift-time-settings", methods=["POST"])
def set_shift_time_setting():
    """body: {shift_type, start, end}"""
    state = load_state()
    payload = request.get_json()
    shift = payload.get("shift_type")
    start = payload.get("start")
    end = payload.get("end")
    if shift not in SHIFT_TYPES:
        return jsonify({"error": "존재하지 않는 근무유형입니다."}), 400
    if not start or not end:
        return jsonify({"error": "start, end가 필요합니다."}), 400
    state.setdefault("shift_time_overrides", {})[shift] = {"start": start, "end": end}
    save_state(state)
    return jsonify({"shift_type": shift, "start": start, "end": end})


@app.route("/api/shift-time-settings/<shift_type>", methods=["DELETE"])
def reset_shift_time_setting(shift_type):
    """이 근무유형의 시간을 코드에 정의된 원래 기본값으로 되돌립니다."""
    state = load_state()
    state.get("shift_time_overrides", {}).pop(shift_type, None)
    save_state(state)
    default_start, default_end = SHIFT_TIME_RANGES.get(shift_type, (None, None))
    return jsonify({"shift_type": shift_type, "start": default_start, "end": default_end})


@app.route("/api/employees", methods=["GET"])
def list_employees():
    state = load_state()
    out = []
    for e in state["employees"]:
        e2 = dict(e)
        e2["leave_requests"] = _prune_expired_leave_requests(e.get("leave_requests", []))
        out.append(e2)
    return jsonify(out)


@app.route("/api/employees", methods=["POST"])
def add_employee():
    state = load_state()
    payload = request.get_json()

    for f in ["id", "name"]:
        if f not in payload:
            return jsonify({"error": f"'{f}' 필드가 필요합니다."}), 400

    if any(e["id"] == payload["id"] for e in state["employees"]):
        return jsonify({"error": f"이미 존재하는 직원 id입니다: {payload['id']}"}), 400

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
    save_state(state)
    return jsonify(employee), 201


@app.route("/api/employees/<employee_id>", methods=["PUT"])
def update_employee(employee_id):
    state = load_state()
    payload = request.get_json()
    if "leave_requests" in payload:
        payload["leave_requests"] = _prune_expired_leave_requests(payload["leave_requests"])
    for i, e in enumerate(state["employees"]):
        if e["id"] == employee_id:
            state["employees"][i].update(payload)
            save_state(state)
            return jsonify(state["employees"][i])
    return jsonify({"error": "직원을 찾을 수 없습니다."}), 404


@app.route("/api/employees/<employee_id>", methods=["DELETE"])
def delete_employee(employee_id):
    state = load_state()
    state["employees"] = [e for e in state["employees"] if e["id"] != employee_id]
    save_state(state)
    return "", 204


# ---------------------------------------------------------------------------
# 주차 목록/생성
# ---------------------------------------------------------------------------

@app.route("/api/weeks", methods=["GET"])
def list_weeks():
    state = load_state()
    return jsonify(sorted(state["weeks"].keys()))


@app.route("/api/weeks", methods=["POST"])
def create_week():
    """새 주차를 만듭니다.
    근무요건은 선택(copy_from)과 상관없이, 캘린더 기준 바로 전주 데이터가 있으면
    항상 자동으로 이어받습니다 (매주 똑같은 근무요건을 반복 입력하는 번거로움을 없애기 위함).
    휴무(Off) 지정은 copy_from을 넘긴 경우에만(즉 "전주와 동일하게 시작"을 선택한 경우에만)
    그 주로부터 복사됩니다."""
    state = load_state()
    payload = request.get_json()
    week_key = payload.get("week_key")
    copy_from = payload.get("copy_from")

    if not week_key:
        return jsonify({"error": "week_key가 필요합니다."}), 400

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
    save_state(state)
    return jsonify(new_week), 201


@app.route("/api/weeks/<week_key>", methods=["GET"])
def get_week(week_key):
    state = load_state()
    week = state["weeks"].get(week_key)
    if week is None:
        return jsonify(None)
    return jsonify(week)


@app.route("/api/weeks/<week_key>", methods=["DELETE"])
def delete_week(week_key):
    state = load_state()
    state["weeks"].pop(week_key, None)
    save_state(state)
    return "", 204


@app.route("/api/weeks/<week_key>/lock", methods=["POST"])
def set_week_lock(week_key):
    """body: {locked: true/false} - 이 주차를 잠그거나 풉니다. 잠긴 동안엔 이 주의
    근무요건/휴무지정/스케줄 생성·수동조정이 모두 서버에서도 거부됩니다."""
    state = load_state()
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    payload = request.get_json(silent=True) or {}
    state["weeks"][week_key]["locked"] = bool(payload.get("locked", False))
    save_state(state)
    return jsonify({"locked": state["weeks"][week_key]["locked"]})


def _week_locked(state, week_key):
    week = state["weeks"].get(week_key)
    return bool(week and week.get("locked"))


# ---------------------------------------------------------------------------
# 주차별 근무요건
# ---------------------------------------------------------------------------

@app.route("/api/weeks/<week_key>/requirements", methods=["POST"])
def set_week_requirements(week_key):
    state = load_state()
    if _week_locked(state, week_key):
        return jsonify({"error": "이 주는 잠겨 있습니다. 먼저 잠금을 해제해주세요."}), 403
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    payload = request.get_json()
    if not isinstance(payload, list):
        return jsonify({"error": "요건 목록은 배열이어야 합니다."}), 400
    state["weeks"][week_key]["requirements"] = payload
    save_state(state)
    return jsonify(state["weeks"][week_key]["requirements"])


# ---------------------------------------------------------------------------
# 주차별 휴무(Off) 지정
# ---------------------------------------------------------------------------

@app.route("/api/weeks/<week_key>/off-days", methods=["POST"])
def set_week_off_days(week_key):
    """body: {employee_id, off_days: [day, ...]} - 그 직원의 그 주 휴무일 전체를 교체"""
    state = load_state()
    if _week_locked(state, week_key):
        return jsonify({"error": "이 주는 잠겨 있습니다. 먼저 잠금을 해제해주세요."}), 403
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    payload = request.get_json()
    employee_id = payload.get("employee_id")
    off_days = payload.get("off_days", [])
    if not employee_id:
        return jsonify({"error": "employee_id가 필요합니다."}), 400
    state["weeks"][week_key]["off_days"][employee_id] = off_days
    save_state(state)
    return jsonify(state["weeks"][week_key]["off_days"])


# ---------------------------------------------------------------------------
# 주차별 스케줄 생성/수동조정
# ---------------------------------------------------------------------------

@app.route("/api/weeks/<week_key>/generate-schedule", methods=["POST"])
def generate_week_schedule(week_key):
    state = load_state()
    week = state["weeks"].get(week_key)
    if week is None:
        return jsonify({"error": "존재하지 않는 주차입니다."}), 404
    if week.get("locked"):
        return jsonify({"error": "이 주는 잠겨 있습니다. 먼저 잠금을 해제해주세요."}), 403

    if not state["employees"]:
        return jsonify({"error": "등록된 직원이 없습니다."}), 400
    if not week["requirements"]:
        return jsonify({"error": "이번 주 근무 요건이 설정되지 않았습니다."}), 400

    off_days_map = week.get("off_days", {})

    employees = [
        Employee(
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
            recent_night_count=e.get("recent_night_count", 0),
            recent_weekend_count=e.get("recent_weekend_count", 0),
        )
        for e in state["employees"]
    ]

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

    result = solve_schedule(
        employees, requirements,
        exclude_solutions=exclude_solutions or None,
        random_seed=random_seed,
        pinned=pinned or None,
        shift_hours=_effective_shift_hours(state),
    )

    result_dict = {
        "status": result.status,
        "assignments": result.assignments,
        "unmet_requirements": result.unmet_requirements,
        "diagnostics": result.diagnostics,
        "day_count_issues": result.day_count_issues,
        "preferred_off_issues": result.preferred_off_issues,
    }

    state["weeks"][week_key]["schedule"] = result_dict
    # auto_assignments는 "자동 생성 직후"의 원본 스냅샷입니다. 이후 수동 조정(manual-adjust)이
    # 있어도 이 값은 덮어쓰지 않아서, 나중에 "사람이 뭘 얼마나 고쳤는지" 비교할 수 있습니다.
    state["weeks"][week_key]["auto_assignments"] = result.assignments
    save_state(state)

    return jsonify(result_dict)


@app.route("/api/weeks/<week_key>/manual-adjust", methods=["POST"])
def manual_adjust_week(week_key):
    state = load_state()
    if _week_locked(state, week_key):
        return jsonify({"error": "이 주는 잠겨 있습니다. 먼저 잠금을 해제해주세요."}), 403
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    payload = request.get_json()
    schedule = state["weeks"][week_key]["schedule"] or {
        "status": "MANUAL", "assignments": [], "unmet_requirements": [], "diagnostics": [], "day_count_issues": [], "preferred_off_issues": []
    }
    schedule["assignments"] = payload.get("assignments", [])
    schedule["status"] = "MANUAL"
    schedule["diagnostics"] = ["수동으로 재배치된 스케줄입니다."]
    state["weeks"][week_key]["schedule"] = schedule
    save_state(state)
    return jsonify(schedule)


@app.route("/api/weeks/<week_key>/reset-schedule", methods=["POST"])
def reset_week_schedule(week_key):
    """이 주의 스케줄(자동 생성 결과 + 수동 조정 결과)만 완전히 초기화합니다.
    근무요건, 휴무(Off) 지정, 잠금 상태는 건드리지 않습니다. Leave Request는
    직원별 전역 데이터라 애초에 이 주차 데이터에 포함되지 않으므로 영향을 받지 않습니다.
    트레이닝 실습용으로 스케줄을 새로 시작하고 싶을 때 사용합니다."""
    state = load_state()
    if _week_locked(state, week_key):
        return jsonify({"error": "이 주는 잠겨 있습니다. 먼저 잠금을 해제해주세요."}), 403
    if week_key not in state["weeks"]:
        state["weeks"][week_key] = empty_week()
    else:
        state["weeks"][week_key]["schedule"] = None
        state["weeks"][week_key]["auto_assignments"] = []
    save_state(state)
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
def list_public_holidays():
    state = load_state()
    return jsonify(sorted(state["public_holidays"], key=lambda h: h["date"]))


@app.route("/api/public-holidays", methods=["POST"])
def add_public_holiday():
    state = load_state()
    payload = request.get_json()
    h_date = payload.get("date")
    name = payload.get("name", "")
    if not h_date:
        return jsonify({"error": "date가 필요합니다."}), 400
    if any(h["date"] == h_date for h in state["public_holidays"]):
        return jsonify({"error": "이미 등록된 날짜입니다."}), 400
    holiday = {"date": h_date, "name": name}
    state["public_holidays"].append(holiday)
    save_state(state)
    return jsonify(holiday), 201


@app.route("/api/public-holidays/<h_date>", methods=["DELETE"])
def delete_public_holiday(h_date):
    state = load_state()
    state["public_holidays"] = [h for h in state["public_holidays"] if h["date"] != h_date]
    save_state(state)
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


@app.route("/api/weeks/<week_key>/weekday-frequency", methods=["GET"])
def get_weekday_frequency(week_key):
    """이 주(week_key)를 기준으로, 각 직원이 각 요일에 "몇 주 연속으로" 근무했는지
    (근무유형은 상관없이) 셉니다. 이번 주부터 거슬러 올라가며 세다가, 그 요일에
    근무하지 않은(쉬거나 배정이 없는) 주를 만나면 그 즉시 스트릭이 끊깁니다.
    최대 8주까지만 셉니다. 순전히 참고용 정보이며, 스케줄 생성 로직에는 전혀
    영향을 주지 않습니다(하드 규칙도 소프트 규칙도 아님 — 화면에 숫자로만 표시)."""
    state = load_state()
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
def get_public_holiday_info(week_key):
    """이 주(week_key)에 Public Holiday가 포함되어 있으면, 사용자가 정의한 뉴질랜드
    노동법 4가지 기준에 따라 직원별 적용 항목(1.5배+Lieu / 평소급여만 / 1.5배만 / 해당없음)을
    계산해서 돌려줍니다.

    기준: 지난 8주(이번 주 포함) 중 5주 이상 그 요일에 근무했으면 "평소 근무 요일"로 간주합니다.
    ⚠️ 이 계산은 사용자가 정의한 규칙을 그대로 옮긴 것으로, 실제 급여 지급 전에는
    회계/노무 담당자 확인을 권장합니다."""
    state = load_state()
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
def get_pattern_suggestions():
    state = load_state()
    return jsonify(_analyze_edit_patterns(state))


if __name__ == "__main__":
    # PORT 환경변수는 Render 같은 클라우드 호스팅이 실행 시 자동으로 지정해줍니다.
    # 로컬에서 그냥 python app.py로 실행하면 여전히 5000번 포트를 씁니다.
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
