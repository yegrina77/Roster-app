"""
근무 스케줄링 엔진 (OR-Tools CP-SAT 기반)

부서(Department) 체계:
- Kitchen: Opening / Helper / Closing1 / Closing2
- Sushi: 스시1 / 스시2 / 스시3
- Cashier: C.Opening / C.Helper / C.Closing1 / C.Closing2
(나중에 Hall 등 추가 가능 — SHIFT_DEFS에 항목만 추가하면 됩니다)

하드(절대 규칙, 항상 지켜짐):
- 하루 1교대만
- 근무유형 전체 금지 설정
- 캘린더에서 지정한 휴무일(forced_off_days) - 그 날은 아무 교대도 배정 안 함
- 주당 최소 근무시간 (5 / 30 / 40시간 중 선택, 그 이상 배정)
- 마감(Closing) 근무 다음날은 그 사람에게 오프닝류 근무 배정 금지 (Kitchen·Cashier 적용, SHIFT_DEFS의
  is_closing / blocked_after_closing 플래그로 자동 계산됨 — 부서 상관없이 같은 사람 기준)
- 필요인원은 상한(그 이상 넣지 않음) — 단, 못 채우는 것 자체는 허용(shortfall)
- 자동 생성 시 직원은 자신의 소속 부서 근무유형에만 배정됨 (타 부서 교차배정 금지)
  → 단, 사용자가 캘린더에서 직접 수동으로 배치(pin)한 경우는 이 규칙의 예외입니다.

우선순위 기반 단계별(lexicographic) 최적화:
  1단계: 필요인원 최대한 채우기 (shortfall 최소화) — 항상 최우선
  2단계: 선호 오프요일 최대한 지키기 — 1단계 결과를 해치지 않는 선에서 최적화
  3단계: 나머지(목표근무일수·휴무패턴·근무선호도·공정성) — 1·2단계 결과를 해치지 않는 선에서
         기존 가중치 방식으로 최적화

세밀한 조정은 생성 후 화면에서 드래그(스왑) · 빈 칸 클릭(추가/휴무 지정) ·
블록 클릭(시간수정/삭제)으로 수동으로 하시면 됩니다.
"""

from dataclasses import dataclass, field
from typing import Optional
from ortools.sat.python import cp_model


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABEL_KO = {"mon": "월", "tue": "화", "wed": "수", "thu": "목", "fri": "금", "sat": "토", "sun": "일"}

# ---------------------------------------------------------------------------
# 부서 / 근무유형 정의 — 여기에 항목을 추가하면 시스템 전체에 자동 반영됩니다.
# ---------------------------------------------------------------------------

DEPARTMENTS = ["kitchen", "sushi", "cashier", "management", "training"]
DEPARTMENT_LABEL_KO = {"kitchen": "Kitchen", "sushi": "스시", "cashier": "Cashier", "management": "Management", "training": "Training"}

# is_closing: 이 근무를 하면, 다음날 blocked_after_closing=True인 근무유형은 배정 금지됩니다.
# (부서 상관없이 같은 사람 기준으로 적용 — 실제로 늦게까지 일한 사람의 휴식시간을 보호하는 규칙입니다)
SHIFT_DEFS = {
    "opening":            {"dept": "kitchen",    "label": "Opening",            "time": ("06:00", "15:00"), "is_closing": False, "blocked_after_closing": True},
    "helper":             {"dept": "kitchen",    "label": "Helper",             "time": ("08:00", "17:00"), "is_closing": False, "blocked_after_closing": True},
    "closing1":           {"dept": "kitchen",    "label": "Closing1",           "time": ("11:00", "20:30"), "is_closing": True,  "blocked_after_closing": False},
    "closing2":           {"dept": "kitchen",    "label": "Closing2",           "time": ("11:30", "20:30"), "is_closing": True,  "blocked_after_closing": False},
    "sushi1":             {"dept": "sushi",      "label": "스시1",               "time": ("05:00", "14:00"), "is_closing": False, "blocked_after_closing": False},
    "sushi2":             {"dept": "sushi",      "label": "스시2",               "time": ("05:00", "14:00"), "is_closing": False, "blocked_after_closing": False},
    "sushi3":             {"dept": "sushi",      "label": "스시3",               "time": ("10:00", "19:00"), "is_closing": False, "blocked_after_closing": False},
    "c_opening":          {"dept": "cashier",    "label": "C.Opening",          "time": ("06:00", "15:00"), "is_closing": False, "blocked_after_closing": True},
    "c_helper":           {"dept": "cashier",    "label": "C.Helper",           "time": ("10:00", "19:00"), "is_closing": False, "blocked_after_closing": True},
    "c_closing1":         {"dept": "cashier",    "label": "C.Closing1",         "time": ("11:00", "20:30"), "is_closing": True,  "blocked_after_closing": False},
    "c_closing2":         {"dept": "cashier",    "label": "C.Closing2",         "time": ("12:00", "20:30"), "is_closing": True,  "blocked_after_closing": False},
    "management":         {"dept": "management", "label": "Management",         "time": ("09:00", "18:00"), "is_closing": False, "blocked_after_closing": False},
    "management_cashier": {"dept": "management", "label": "Management & Cashier", "time": ("10:00", "19:00"), "is_closing": False, "blocked_after_closing": False},
    # Training은 사람마다 매번 시간이 달라서 고정 기본값이 의미가 없습니다. 아래 시간은 그냥
    # 임시 표시값일 뿐이고, "근무유형 기본 시간" 설정이나 블록별 수동 수정으로 실제 시간을
    # 매번 새로 지정해주세요.
    "training":           {"dept": "training",   "label": "Training",           "time": ("09:00", "17:00"), "is_closing": False, "blocked_after_closing": False},
}

SHIFT_TYPES = list(SHIFT_DEFS.keys())
SHIFT_LABEL_KO = {s: v["label"] for s, v in SHIFT_DEFS.items()}
SHIFT_TIME_RANGES = {s: v["time"] for s, v in SHIFT_DEFS.items()}
SHIFT_DEPARTMENT = {s: v["dept"] for s, v in SHIFT_DEFS.items()}
DEPARTMENT_SHIFTS = {dept: [s for s, v in SHIFT_DEFS.items() if v["dept"] == dept] for dept in DEPARTMENTS}


def _hours(shift: str) -> float:
    s, e = SHIFT_TIME_RANGES[shift]
    sh, sm = map(int, s.split(":"))
    eh, em = map(int, e.split(":"))
    return (eh * 60 + em - sh * 60 - sm) / 60


SHIFT_HOURS = {s: _hours(s) for s in SHIFT_TYPES}

# 마감(is_closing) 근무 다음날은, blocked_after_closing=True로 표시된 근무유형 배정 금지.
# SHIFT_DEFS에 정의된 플래그로 자동 계산되므로, 새 부서를 추가할 때 이 부분은 손댈 필요 없습니다.
FORBIDDEN_CONSECUTIVE = {
    (prev, nxt)
    for prev in SHIFT_TYPES if SHIFT_DEFS[prev]["is_closing"]
    for nxt in SHIFT_TYPES if SHIFT_DEFS[nxt]["blocked_after_closing"]
}


@dataclass
class Employee:
    id: str
    name: str
    department: str = "kitchen"  # 하드: 자동 생성 시 이 부서의 근무유형에만 배정됨 (수동 배치는 예외)
    min_hours_per_week: int = 30  # 하드: 이 시간 이상 배정 (5 / 30 / 40 중 선택)
    target_days_per_week: Optional[int] = None  # 소프트(3단계): 벗어나면 페널티
    forced_off_days: list[str] = field(default_factory=list)  # 하드: 캘린더에서 지정한 휴무일
    blocked_shift_types: list[str] = field(default_factory=list)  # 하드
    day_off_pattern: Optional[str] = None  # "consecutive" | "split" | None (소프트, 3단계)
    preferred: list[tuple[str, str]] = field(default_factory=list)  # 소프트(3단계): (day, shift_type)
    preferred_off_days: list[str] = field(default_factory=list)  # 소프트(2단계, 최우선급): 선호 오프요일
    recent_night_count: int = 0
    recent_weekend_count: int = 0


@dataclass
class ShiftRequirement:
    day: str
    shift_type: str
    required_count: int


@dataclass
class ScheduleResult:
    status: str
    assignments: list[dict]
    unmet_requirements: list[dict]
    diagnostics: list[str]
    day_count_issues: list[dict] = field(default_factory=list)
    preferred_off_issues: list[dict] = field(default_factory=list)


def _explain_shortfall(req: "ShiftRequirement", employees: list["Employee"]) -> dict:
    day, shift = req.day, req.shift_type
    req_dept = SHIFT_DEPARTMENT.get(shift)
    reasons = {"휴무 지정": 0, "근무유형 전체 금지": 0, "다른 부서 소속": 0}
    blocked_count = 0

    for e in employees:
        blocked = False
        if day in e.forced_off_days:
            reasons["휴무 지정"] += 1
            blocked = True
        if shift in e.blocked_shift_types:
            reasons["근무유형 전체 금지"] += 1
            blocked = True
        if req_dept is not None and e.department != req_dept:
            reasons["다른 부서 소속"] += 1
            blocked = True
        if blocked:
            blocked_count += 1

    return {
        "pool_size": len(employees),
        "blocked_count": blocked_count,
        "reasons": {k: v for k, v in reasons.items() if v > 0},
    }


def solve_schedule(
    employees: list[Employee],
    requirements: list[ShiftRequirement],
    fairness_weight: int = 1,
    preference_weight: int = 3,
    day_count_weight: int = 50000,
    pattern_weight: int = 10,
    exclude_solutions: Optional[list[list[tuple]]] = None,
    random_seed: Optional[int] = None,
    pinned: Optional[list[tuple]] = None,
    shift_hours: Optional[dict] = None,
) -> ScheduleResult:
    model = cp_model.CpModel()
    pinned_set = set(pinned or [])
    hours_map = shift_hours or SHIFT_HOURS  # 관리자가 근무유형 기본 시간을 조정했으면 그 값을 사용

    x = {}
    for e in employees:
        for day in DAYS:
            for shift in SHIFT_TYPES:
                x[(e.id, day, shift)] = model.NewBoolVar(f"x_{e.id}_{day}_{shift}")

    # ---- 수동으로 미리 배치해둔 자리는 그대로 고정 (하드) ----
    for (emp_id, day, shift) in pinned_set:
        if (emp_id, day, shift) in x:
            model.Add(x[(emp_id, day, shift)] == 1)

    # ---- "다시 생성" 시 이전에 봤던 조합을 정확히 반복하지 않도록 배제 ----
    all_keys = list(x.keys())
    for sol in (exclude_solutions or []):
        sol_set = set(sol)
        lits = [x[k] if k in sol_set else x[k].Not() for k in all_keys]
        model.Add(sum(lits) <= len(all_keys) - 1)

    # ---- 하드 규칙들 (항상 적용) ----
    for e in employees:
        for day in DAYS:
            model.Add(sum(x[(e.id, day, s)] for s in SHIFT_TYPES) <= 1)

    for e in employees:
        for day in e.forced_off_days:
            for shift in SHIFT_TYPES:
                model.Add(x[(e.id, day, shift)] == 0)

    for e in employees:
        for shift in e.blocked_shift_types:
            for day in DAYS:
                model.Add(x[(e.id, day, shift)] == 0)

    # ---- 하드: 자동 생성 시 타 부서 근무유형 배정 금지 (수동 고정 배치는 예외) ----
    for e in employees:
        for shift in SHIFT_TYPES:
            if SHIFT_DEPARTMENT[shift] == e.department:
                continue
            for day in DAYS:
                if (e.id, day, shift) in pinned_set:
                    continue  # 수동 배치는 부서 제한의 예외
                model.Add(x[(e.id, day, shift)] == 0)

    for e in employees:
        total_hours_x1000 = sum(
            x[(e.id, day, shift)] * round(hours_map[shift] * 1000)
            for day in DAYS for shift in SHIFT_TYPES
        )
        model.Add(total_hours_x1000 >= round(e.min_hours_per_week * 1000))

    for e in employees:
        for i in range(len(DAYS) - 1):
            today, tomorrow = DAYS[i], DAYS[i + 1]
            for (prev_shift, next_shift) in FORBIDDEN_CONSECUTIVE:
                model.Add(
                    x[(e.id, today, prev_shift)] + x[(e.id, tomorrow, next_shift)] <= 1
                )

    shortfall = {}
    for idx, req in enumerate(requirements):
        eligible = [x[(e.id, req.day, req.shift_type)] for e in employees]
        shortfall[idx] = model.NewIntVar(0, req.required_count, f"shortfall_{idx}")
        model.Add(sum(eligible) + shortfall[idx] >= req.required_count)
        model.Add(sum(eligible) <= req.required_count)

    # ---- 2단계용: 선호 오프요일 위반 여부 ----
    pref_off_violation_terms = []
    for e in employees:
        for day in e.preferred_off_days:
            for shift in SHIFT_TYPES:
                if (e.id, day, shift) in x:
                    pref_off_violation_terms.append(x[(e.id, day, shift)])

    # ---- 3단계용: 소프트 규칙들 ----
    preference_terms = []
    for e in employees:
        for (day, shift) in e.preferred:
            if (e.id, day, shift) in x:
                preference_terms.append(x[(e.id, day, shift)])

    fairness_penalty_terms = []
    for e in employees:
        closing_assignments = sum(
            x[(e.id, day, s)] for day in DAYS for s in ("helper", "closing1", "closing2")
        )
        weekend_assignments = sum(
            x[(e.id, day, s)] for day in ["sat", "sun"] for s in SHIFT_TYPES
        )
        fairness_penalty_terms.append(closing_assignments * e.recent_night_count)
        fairness_penalty_terms.append(weekend_assignments * e.recent_weekend_count)

    day_count_dev_vars = {}
    for e in employees:
        if e.target_days_per_week is not None:
            actual_days = sum(x[(e.id, day, s)] for day in DAYS for s in SHIFT_TYPES)
            dev = model.NewIntVar(0, 7, f"devdays_{e.id}")
            model.AddAbsEquality(dev, actual_days - e.target_days_per_week)
            day_count_dev_vars[e.id] = dev

    pattern_violation_vars = []
    for e in employees:
        if e.day_off_pattern in ("consecutive", "split"):
            off_vars_local = {}
            for day in DAYS:
                off = model.NewBoolVar(f"off_{e.id}_{day}")
                model.Add(sum(x[(e.id, day, s)] for s in SHIFT_TYPES) + off == 1)
                off_vars_local[day] = off

            if e.day_off_pattern == "split":
                for i in range(len(DAYS) - 1):
                    viol = model.NewBoolVar(f"splitviol_{e.id}_{i}")
                    model.Add(off_vars_local[DAYS[i]] + off_vars_local[DAYS[i + 1]] - 1 <= viol)
                    pattern_violation_vars.append(viol)
            else:  # consecutive
                starts = [off_vars_local[DAYS[0]]]
                for i in range(1, len(DAYS)):
                    prev_off = off_vars_local[DAYS[i - 1]]
                    cur_off = off_vars_local[DAYS[i]]
                    start = model.NewBoolVar(f"start_{e.id}_{DAYS[i]}")
                    model.Add(start <= cur_off)
                    model.Add(start <= 1 - prev_off)
                    model.Add(start >= cur_off - prev_off)
                    starts.append(start)
                extra_blocks = model.NewIntVar(0, 7, f"extrablocks_{e.id}")
                model.Add(extra_blocks >= sum(starts) - 1)
                pattern_violation_vars.append(extra_blocks)

    total_shortfall = sum(shortfall.values())
    total_pref_off_violation = sum(pref_off_violation_terms) if pref_off_violation_terms else 0
    total_preference = sum(preference_terms) if preference_terms else 0
    total_fairness_penalty = sum(fairness_penalty_terms) if fairness_penalty_terms else 0
    total_day_count_penalty = sum(day_count_dev_vars.values()) if day_count_dev_vars else 0
    total_pattern_penalty = sum(pattern_violation_vars) if pattern_violation_vars else 0

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    solver.parameters.num_search_workers = 8
    if random_seed is not None:
        solver.parameters.random_seed = random_seed

    # ---- 1단계: 필요인원 최대한 채우기 ----
    model.Minimize(total_shortfall)
    status = solver.Solve(model)
    status_name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return ScheduleResult(
            status="INFEASIBLE",
            assignments=[],
            unmet_requirements=[],
            diagnostics=[
                "제약을 만족하는 스케줄을 찾지 못했습니다. 캘린더에서 지정한 휴무일이나 "
                "근무유형 전체 금지, 최소 근무시간, 부서 소속 설정이 여러 직원에게 동시에 "
                "너무 강하게 걸려있지 않은지 확인해보세요.",
            ],
        )

    shortfall_min = solver.Value(total_shortfall)
    model.Add(total_shortfall <= shortfall_min)

    # ---- 2단계: 선호 오프요일 최대한 지키기 (1단계 결과는 그대로 유지) ----
    if pref_off_violation_terms:
        model.Minimize(total_pref_off_violation)
        status2 = solver.Solve(model)
        if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            status_name = solver.StatusName(status2)
            pref_off_min = solver.Value(total_pref_off_violation)
            model.Add(total_pref_off_violation <= pref_off_min)
        # 2단계가 실패해도(이론상 거의 없음) 1단계 결과는 이미 하드로 고정되어 있어 안전합니다.

    # ---- 3단계: 나머지 소프트 규칙 (1·2단계 결과는 그대로 유지) ----
    model.Minimize(
        total_day_count_penalty * day_count_weight
        + total_pattern_penalty * pattern_weight
        - total_preference * preference_weight
        + total_fairness_penalty * fairness_weight
    )
    status3 = solver.Solve(model)
    if status3 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        status_name = solver.StatusName(status3)
    # 3단계가 실패해도 1·2단계에서 이미 찾은 값이 model에 마지막으로 반영된 해로 남아있으므로
    # solver.Value(...)는 계속 그 직전 최선의 해를 반환합니다.

    assignments = []
    for e in employees:
        for day in DAYS:
            for shift in SHIFT_TYPES:
                if solver.Value(x[(e.id, day, shift)]) == 1:
                    assignments.append(
                        {"employee_id": e.id, "employee_name": e.name, "day": day, "shift_type": shift}
                    )

    unmet = []
    diagnostics = []
    for idx, req in enumerate(requirements):
        gap = solver.Value(shortfall[idx])
        if gap > 0:
            explanation = _explain_shortfall(req, employees)
            reason_str = ", ".join(f"{k} {v}명" for k, v in explanation["reasons"].items())
            unmet.append({
                "day": req.day, "shift_type": req.shift_type, "missing_count": gap,
                "pool_size": explanation["pool_size"],
                "blocked_count": explanation["blocked_count"],
                "reasons": explanation["reasons"],
            })
            msg = (
                f"{req.day} {req.shift_type} 교대: 필요 인원 {req.required_count}명 중 {gap}명 부족. "
                f"(전체 직원 {explanation['pool_size']}명 중 "
                f"{explanation['blocked_count']}명이 규칙으로 배정 불가"
            )
            if reason_str:
                msg += f" — {reason_str}"
            msg += ")."
            diagnostics.append(msg)

    day_count_issues = []
    for e in employees:
        if e.target_days_per_week is not None:
            actual = sum(1 for a in assignments if a["employee_id"] == e.id)
            if actual != e.target_days_per_week:
                day_count_issues.append({
                    "employee_id": e.id, "employee_name": e.name,
                    "target": e.target_days_per_week, "actual": actual,
                })

    preferred_off_issues = []
    for e in employees:
        for day in e.preferred_off_days:
            worked = any(a["employee_id"] == e.id and a["day"] == day for a in assignments)
            if worked:
                preferred_off_issues.append({
                    "employee_id": e.id, "employee_name": e.name, "day": day,
                })

    if not diagnostics:
        diagnostics.append("모든 필수 인원 요건을 충족했습니다.")

    return ScheduleResult(
        status=status_name,
        assignments=assignments,
        unmet_requirements=unmet,
        diagnostics=diagnostics,
        day_count_issues=day_count_issues,
        preferred_off_issues=preferred_off_issues,
    )
