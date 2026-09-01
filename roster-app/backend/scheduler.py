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
  2단계: 연속근무 7일 제한 최대한 지키기 — 전주에서 넘어온 연속근무일수(carry_in_streak)까지
         포함해서 계산합니다. 1단계 결과를 해치지 않는 선에서 최적화 (완전한 하드는 아님 —
         인력이 정말 부족하면 예외적으로 뚫릴 수 있고, 그 경우 진단에 안내됩니다)
  3단계: 선호 오프요일 최대한 지키기 — 1·2단계 결과를 해치지 않는 선에서 최적화
  4단계: 나머지(목표근무일수·휴무패턴·근무선호도·공정성) — 1·2·3단계 결과를 해치지 않는 선에서
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


def _hours_from_range(start: str, end: str) -> float:
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    return (eh * 60 + em - sh * 60 - sm) / 60


def _hours(shift: str) -> float:
    s, e = SHIFT_TIME_RANGES[shift]
    return _hours_from_range(s, e)


SHIFT_HOURS = {s: _hours(s) for s in SHIFT_TYPES}

# 마감(is_closing) 근무 다음날은, blocked_after_closing=True로 표시된 근무유형 배정 금지.
# SHIFT_DEFS에 정의된 플래그로 자동 계산되므로, 새 부서를 추가할 때 이 부분은 손댈 필요 없습니다.
FORBIDDEN_CONSECUTIVE = {
    (prev, nxt)
    for prev in SHIFT_TYPES if SHIFT_DEFS[prev]["is_closing"]
    for nxt in SHIFT_TYPES if SHIFT_DEFS[nxt]["blocked_after_closing"]
}

# 연속근무 최대 일수 (2단계 소프트 규칙 — 인력이 정말 부족하면 예외적으로 뚫릴 수 있음)
MAX_CONSECUTIVE_DAYS = 7


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
    preferred_off_days: list[str] = field(default_factory=list)  # 소프트(3단계, 최우선급): 선호 오프요일
    carry_in_streak: int = 0  # 전주 끝자락부터 이어져온 연속 근무일수 (2단계 계산의 시작점)
    recent_night_count: int = 0
    recent_weekend_count: int = 0
    credited_off_hours: float = 0.0  # 유급/병가 리브로 이미 "채운 걸로 인정"할 시간
    # (하드: min_hours_per_week 계산 시 실제 배정 시간에 이만큼을 더한 걸로 칩니다 —
    # 유급으로 쉰 시간은 회사가 이미 지급을 약속한 시간이라, 나머지 요일에 억지로
    # 근무를 몰아넣지 않아도 되게 하기 위함입니다. 무급 리브/수동 Off는 여기 안 들어갑니다.)


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
    pattern_issues: list[dict] = field(default_factory=list)
    preference_issues: list[dict] = field(default_factory=list)
    fairness_issues: list[dict] = field(default_factory=list)
    consecutive_issues: list[dict] = field(default_factory=list)


def _explain_shortfall(req: "ShiftRequirement", employees: list["Employee"], shift_department: dict) -> dict:
    day, shift = req.day, req.shift_type
    req_dept = shift_department.get(shift)
    reasons = {"forced_off": 0, "blocked_shift_type": 0, "different_department": 0}
    blocked_count = 0

    for e in employees:
        blocked = False
        if day in e.forced_off_days:
            reasons["forced_off"] += 1
            blocked = True
        if shift in e.blocked_shift_types:
            reasons["blocked_shift_type"] += 1
            blocked = True
        if req_dept is not None and e.department != req_dept:
            reasons["different_department"] += 1
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
    shift_types: Optional[list[str]] = None,
    shift_defs: Optional[dict] = None,
    departments: Optional[list[str]] = None,
    relax: Optional[set] = None,
    relax_min_hours_employee_ids: Optional[set] = None,
    time_limit_seconds: float = 10.0,
) -> ScheduleResult:
    """shift_types/shift_defs/departments를 안 넘기면 코드에 기본 내장된 구성(Kitchen/Sushi/...)을
    씁니다. 회사가 직접 만든 커스텀 부서·근무유형이 있으면, app.py에서 그 데이터를 이 형태로
    변환해서 넘겨줍니다 — 이 함수 자체는 "부서/근무유형이 뭐가 있는지" 전혀 모르는 채로,
    넘겨받은 목록만 갖고 계산합니다.

    relax: 이 세트에 이름을 넣으면 그 하드 규칙을 이번 계산에서만 꺼둡니다. 정상적으로
    스케줄을 만들 때는 항상 비워둡니다 — INFEASIBLE이 났을 때, app.py가 "어떤 규칙 하나를
    빼면 풀리는지"를 자동으로 찾아내서 사용자에게 정확한 원인을 알려주는 진단 용도로만
    씁니다. 가능한 이름: "min_hours", "forbidden_consecutive", "blocked_shift_types",
    "cross_department".

    relax_min_hours_employee_ids: "min_hours"가 원인으로 의심될 때, 이 세트에 담긴
    직원 id들만 콕 집어서 최소시간 규칙을 꺼봅니다 — "규칙 자체"가 아니라 "정확히 어느
    직원 때문인지"까지 찾아내는 2단계 진단용입니다."""
    relax = relax or frozenset()
    shift_types = shift_types if shift_types is not None else SHIFT_TYPES
    shift_defs = shift_defs if shift_defs is not None else SHIFT_DEFS
    departments = departments if departments is not None else DEPARTMENTS

    shift_department = {s: shift_defs[s]["dept"] for s in shift_types if s in shift_defs}
    forbidden_consecutive = {
        (prev, nxt)
        for prev in shift_types if shift_defs.get(prev, {}).get("is_closing")
        for nxt in shift_types if shift_defs.get(nxt, {}).get("blocked_after_closing")
    }
    default_hours = {}
    for s in shift_types:
        d = shift_defs.get(s, {})
        t = d.get("time")
        default_hours[s] = _hours_from_range(*t) if t else 8.0

    model = cp_model.CpModel()
    pinned_set = set(pinned or [])
    hours_map = shift_hours or default_hours  # 관리자가 근무유형 기본 시간을 조정했으면 그 값을 사용

    x = {}
    for e in employees:
        for day in DAYS:
            for shift in shift_types:
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
            model.Add(sum(x[(e.id, day, s)] for s in shift_types) <= 1)

    for e in employees:
        for day in e.forced_off_days:
            for shift in shift_types:
                model.Add(x[(e.id, day, shift)] == 0)

    for e in employees:
        for shift in e.blocked_shift_types:
            for day in DAYS:
                if "blocked_shift_types" in relax:
                    continue
                model.Add(x[(e.id, day, shift)] == 0)

    # ---- 하드: 자동 생성 시 타 부서 근무유형 배정 금지 (수동 고정 배치는 예외) ----
    for e in employees:
        for shift in shift_types:
            if shift_department[shift] == e.department:
                continue
            for day in DAYS:
                if (e.id, day, shift) in pinned_set:
                    continue  # 수동 배치는 부서 제한의 예외
                if "cross_department" in relax:
                    continue
                model.Add(x[(e.id, day, shift)] == 0)

    for e in employees:
        # 이번 주에 강제 휴무일(캘린더 직접 지정 또는 Leave Request)이 있으면, 평소의
        # 주당 최소시간(min_hours_per_week)을 두 단계로 조정합니다:
        #   1) 유급/병가 리브(credited_off_hours)는 이미 채운 시간으로 그대로 인정합니다.
        #   2) 그러고도 남는 목표치는, 무급 리브/수동 Off 등으로 못 나오는 날이 있으면
        #      "남은 근무 가능 일수 비율"을 상한으로 삼아 더 줄여줍니다 — 안 그러면
        #      무급으로 여러 날 못 나온 직원 한 명 때문에 전체 스케줄 자체가 통째로
        #      INFEASIBLE(생성 불가) 처리되는 문제가 있었습니다.
        if "min_hours" in relax or e.id in (relax_min_hours_employee_ids or set()):
            continue
        available_days = 7 - len(e.forced_off_days)
        target_after_credit = max(0.0, e.min_hours_per_week - e.credited_off_hours)
        if available_days <= 0 or target_after_credit <= 0:
            effective_min_hours = 0.0
        else:
            proportional_cap = e.min_hours_per_week * available_days / 7
            effective_min_hours = min(target_after_credit, proportional_cap)
        total_hours_x1000 = sum(
            x[(e.id, day, shift)] * round(hours_map[shift] * 1000)
            for day in DAYS for shift in shift_types
        )
        model.Add(total_hours_x1000 >= round(effective_min_hours * 1000))

    for e in employees:
        if "forbidden_consecutive" in relax:
            break
        for i in range(len(DAYS) - 1):
            today, tomorrow = DAYS[i], DAYS[i + 1]
            for (prev_shift, next_shift) in forbidden_consecutive:
                model.Add(
                    x[(e.id, today, prev_shift)] + x[(e.id, tomorrow, next_shift)] <= 1
                )

    shortfall = {}
    for idx, req in enumerate(requirements):
        eligible = [x[(e.id, req.day, req.shift_type)] for e in employees]
        shortfall[idx] = model.NewIntVar(0, req.required_count, f"shortfall_{idx}")
        model.Add(sum(eligible) + shortfall[idx] >= req.required_count)
        model.Add(sum(eligible) <= req.required_count)

    # ---- 2단계용: 연속근무 7일 제한 (전주에서 이어져온 연속근무일수 포함) ----
    worked_vars = {}
    streak_vars = {}
    consecutive_viol_vars = {}
    for e in employees:
        worked_vars[e.id] = {}
        streak_vars[e.id] = {}
        consecutive_viol_vars[e.id] = {}
        for i, day in enumerate(DAYS):
            worked = model.NewBoolVar(f"worked_{e.id}_{day}")
            model.Add(sum(x[(e.id, day, s)] for s in shift_types) == worked)
            worked_vars[e.id][day] = worked

            streak = model.NewIntVar(0, MAX_CONSECUTIVE_DAYS + len(DAYS), f"streak_{e.id}_{day}")
            model.Add(streak == 0).OnlyEnforceIf(worked.Not())
            if i == 0:
                model.Add(streak == e.carry_in_streak + 1).OnlyEnforceIf(worked)
            else:
                model.Add(streak == streak_vars[e.id][DAYS[i - 1]] + 1).OnlyEnforceIf(worked)
            streak_vars[e.id][day] = streak

            viol = model.NewBoolVar(f"streakviol_{e.id}_{day}")
            model.Add(streak > MAX_CONSECUTIVE_DAYS).OnlyEnforceIf(viol)
            model.Add(streak <= MAX_CONSECUTIVE_DAYS).OnlyEnforceIf(viol.Not())
            consecutive_viol_vars[e.id][day] = viol

    total_consecutive_violation = sum(
        v for byday in consecutive_viol_vars.values() for v in byday.values()
    )

    # ---- 3단계용: 선호 오프요일 위반 여부 ----
    pref_off_violation_terms = []
    for e in employees:
        for day in e.preferred_off_days:
            for shift in shift_types:
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
        # "힘든 근무"는 부서·이름이 아니라 근무유형 속성(is_closing)으로 판단합니다.
        # 이러면 Kitchen뿐 아니라 Cashier 등 마감이 있는 모든 부서에 공평하게 적용됩니다.
        closing_assignments = sum(
            x[(e.id, day, s)] for day in DAYS for s in shift_types if shift_defs[s]["is_closing"]
        )
        weekend_assignments = sum(
            x[(e.id, day, s)] for day in ["sat", "sun"] for s in shift_types
        )
        fairness_penalty_terms.append(closing_assignments * e.recent_night_count)
        fairness_penalty_terms.append(weekend_assignments * e.recent_weekend_count)

    day_count_dev_vars = {}
    for e in employees:
        if e.target_days_per_week is not None:
            actual_days = sum(x[(e.id, day, s)] for day in DAYS for s in shift_types)
            dev = model.NewIntVar(0, 7, f"devdays_{e.id}")
            model.AddAbsEquality(dev, actual_days - e.target_days_per_week)
            day_count_dev_vars[e.id] = dev

    pattern_violation_vars = []
    pattern_violation_by_employee = {}
    for e in employees:
        if e.day_off_pattern in ("consecutive", "split"):
            off_vars_local = {}
            for day in DAYS:
                off = model.NewBoolVar(f"off_{e.id}_{day}")
                model.Add(sum(x[(e.id, day, s)] for s in shift_types) + off == 1)
                off_vars_local[day] = off

            emp_pattern_vars = []
            if e.day_off_pattern == "split":
                for i in range(len(DAYS) - 1):
                    viol = model.NewBoolVar(f"splitviol_{e.id}_{i}")
                    model.Add(off_vars_local[DAYS[i]] + off_vars_local[DAYS[i + 1]] - 1 <= viol)
                    pattern_violation_vars.append(viol)
                    emp_pattern_vars.append(viol)
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
                emp_pattern_vars.append(extra_blocks)

            pattern_violation_by_employee[e.id] = emp_pattern_vars

    total_shortfall = sum(shortfall.values())
    total_pref_off_violation = sum(pref_off_violation_terms) if pref_off_violation_terms else 0
    total_preference = sum(preference_terms) if preference_terms else 0
    total_fairness_penalty = sum(fairness_penalty_terms) if fairness_penalty_terms else 0
    total_day_count_penalty = sum(day_count_dev_vars.values()) if day_count_dev_vars else 0
    total_pattern_penalty = sum(pattern_violation_vars) if pattern_violation_vars else 0

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
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
                "Could not find a schedule that satisfies all constraints. Check whether "
                "day-off marks, blocked shift types, minimum hours, or department "
                "assignments are set too strictly for multiple employees at once.",
            ],
        )

    shortfall_min = solver.Value(total_shortfall)
    model.Add(total_shortfall <= shortfall_min)

    # ---- 2단계: 연속근무 7일 제한 최대한 지키기 (1단계 결과는 그대로 유지) ----
    if consecutive_viol_vars:
        model.Minimize(total_consecutive_violation)
        status2 = solver.Solve(model)
        if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            status_name = solver.StatusName(status2)
            consecutive_min = solver.Value(total_consecutive_violation)
            model.Add(total_consecutive_violation <= consecutive_min)
        # 2단계가 실패해도(이론상 거의 없음) 1단계 결과는 이미 하드로 고정되어 있어 안전합니다.

    # ---- 3단계: 선호 오프요일 최대한 지키기 (1·2단계 결과는 그대로 유지) ----
    if pref_off_violation_terms:
        model.Minimize(total_pref_off_violation)
        status3a = solver.Solve(model)
        if status3a in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            status_name = solver.StatusName(status3a)
            pref_off_min = solver.Value(total_pref_off_violation)
            model.Add(total_pref_off_violation <= pref_off_min)

    # ---- 4단계: 나머지 소프트 규칙 (1·2·3단계 결과는 그대로 유지) ----
    model.Minimize(
        total_day_count_penalty * day_count_weight
        + total_pattern_penalty * pattern_weight
        - total_preference * preference_weight
        + total_fairness_penalty * fairness_weight
    )
    status4 = solver.Solve(model)
    if status4 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        status_name = solver.StatusName(status4)
    # 이 단계가 실패해도 이전 단계에서 이미 찾은 값이 model에 마지막으로 반영된 해로 남아있으므로
    # solver.Value(...)는 계속 그 직전 최선의 해를 반환합니다.

    assignments = []
    for e in employees:
        for day in DAYS:
            for shift in shift_types:
                if solver.Value(x[(e.id, day, shift)]) == 1:
                    assignments.append(
                        {"employee_id": e.id, "employee_name": e.name, "day": day, "shift_type": shift}
                    )

    unmet = []
    diagnostics = []
    for idx, req in enumerate(requirements):
        gap = solver.Value(shortfall[idx])
        if gap > 0:
            explanation = _explain_shortfall(req, employees, shift_department)
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

    consecutive_issues = []
    for e in employees:
        max_streak = 0
        for day in DAYS:
            val = solver.Value(streak_vars[e.id][day])
            if val > max_streak:
                max_streak = val
        if max_streak > MAX_CONSECUTIVE_DAYS:
            consecutive_issues.append({
                "employee_id": e.id, "employee_name": e.name,
                "streak_days": max_streak, "carry_in": e.carry_in_streak,
            })

    pattern_issues = []
    for e in employees:
        if e.id in pattern_violation_by_employee:
            total_viol = sum(solver.Value(v) for v in pattern_violation_by_employee[e.id])
            if total_viol > 0:
                pattern_issues.append({
                    "employee_id": e.id, "employee_name": e.name,
                    "pattern": e.day_off_pattern,
                })

    preference_issues = []
    for e in employees:
        if e.preferred:
            fulfilled = sum(
                1 for (day, shift) in e.preferred
                if (e.id, day, shift) in x and solver.Value(x[(e.id, day, shift)]) == 1
            )
            total_pref = len(e.preferred)
            if fulfilled < total_pref:
                preference_issues.append({
                    "employee_id": e.id, "employee_name": e.name,
                    "fulfilled": fulfilled, "total": total_pref,
                })

    fairness_issues = []
    FAIRNESS_ALERT_THRESHOLD = 3  # 최근 집계 중 이 값(포함) 이상이면 "몰림" 경고 대상
    for e in employees:
        got_closing = any(
            a["employee_id"] == e.id and shift_defs[a["shift_type"]]["is_closing"] for a in assignments
        )
        got_weekend = any(
            a["employee_id"] == e.id and a["day"] in ("sat", "sun") for a in assignments
        )
        if got_closing and e.recent_night_count >= FAIRNESS_ALERT_THRESHOLD:
            fairness_issues.append({
                "employee_id": e.id, "employee_name": e.name,
                "type": "night", "recent_count": e.recent_night_count,
            })
        if got_weekend and e.recent_weekend_count >= FAIRNESS_ALERT_THRESHOLD:
            fairness_issues.append({
                "employee_id": e.id, "employee_name": e.name,
                "type": "weekend", "recent_count": e.recent_weekend_count,
            })

    if not diagnostics:
        diagnostics.append("All required staffing needs have been met.")

    return ScheduleResult(
        status=status_name,
        assignments=assignments,
        unmet_requirements=unmet,
        diagnostics=diagnostics,
        day_count_issues=day_count_issues,
        preferred_off_issues=preferred_off_issues,
        pattern_issues=pattern_issues,
        preference_issues=preference_issues,
        fairness_issues=fairness_issues,
        consecutive_issues=consecutive_issues,
    )
