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
from flask import Flask, request, jsonify, send_from_directory

from scheduler import Employee, ShiftRequirement, solve_schedule, DAYS, SHIFT_TYPES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "..", "data", "state.json")
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")


def load_state():
    if not os.path.exists(DATA_PATH):
        return {"employees": [], "weeks": {}}
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
        state.setdefault("employees", [])
        state.setdefault("weeks", {})
        return state


def save_state(state):
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def empty_week():
    return {"requirements": [], "off_days": {}, "schedule": None, "auto_assignments": []}


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/api/meta", methods=["GET"])
def get_meta():
    return jsonify({"days": DAYS, "shift_types": SHIFT_TYPES})


# ---------------------------------------------------------------------------
# 직원 (여러 주 공통)
# ---------------------------------------------------------------------------

@app.route("/api/employees", methods=["GET"])
def list_employees():
    state = load_state()
    return jsonify(state["employees"])


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
        "min_hours_per_week": payload.get("min_hours_per_week", 30),
        "target_days_per_week": payload.get("target_days_per_week"),
        "blocked_shift_types": payload.get("blocked_shift_types", []),
        "day_off_pattern": payload.get("day_off_pattern"),
        "preferred": payload.get("preferred", []),
        "preferred_off_days": payload.get("preferred_off_days", []),
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
    """새 주차를 만듭니다. copy_from에 기존 주차 키를 주면 그 주의 근무요건/휴무지정을 복사합니다."""
    state = load_state()
    payload = request.get_json()
    week_key = payload.get("week_key")
    copy_from = payload.get("copy_from")

    if not week_key:
        return jsonify({"error": "week_key가 필요합니다."}), 400

    if week_key in state["weeks"]:
        return jsonify(state["weeks"][week_key])

    if copy_from and copy_from in state["weeks"]:
        source = state["weeks"][copy_from]
        new_week = {
            "requirements": [dict(r) for r in source["requirements"]],
            "off_days": {k: list(v) for k, v in source["off_days"].items()},
            "schedule": None,
            "auto_assignments": [],
        }
    else:
        new_week = empty_week()

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


# ---------------------------------------------------------------------------
# 주차별 근무요건
# ---------------------------------------------------------------------------

@app.route("/api/weeks/<week_key>/requirements", methods=["POST"])
def set_week_requirements(week_key):
    state = load_state()
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

    if not state["employees"]:
        return jsonify({"error": "등록된 직원이 없습니다."}), 400
    if not week["requirements"]:
        return jsonify({"error": "이번 주 근무 요건이 설정되지 않았습니다."}), 400

    off_days_map = week.get("off_days", {})

    employees = [
        Employee(
            id=e["id"],
            name=e["name"],
            min_hours_per_week=e.get("min_hours_per_week", 30),
            target_days_per_week=e.get("target_days_per_week"),
            forced_off_days=off_days_map.get(e["id"], []),
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
    payload = request.get_json(silent=True) or {}
    exclude_raw = payload.get("exclude", [])
    exclude_solutions = [
        [(a["employee_id"], a["day"], a["shift_type"]) for a in sol]
        for sol in exclude_raw
    ]
    random_seed = random.randint(1, 10_000_000) if exclude_solutions else None

    result = solve_schedule(
        employees, requirements,
        exclude_solutions=exclude_solutions or None,
        random_seed=random_seed,
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


@app.route("/api/pattern-suggestions", methods=["GET"])
def get_pattern_suggestions():
    state = load_state()
    return jsonify(_analyze_edit_patterns(state))


if __name__ == "__main__":
    app.run(debug=False, port=5000)
