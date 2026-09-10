"""
RosterFlow 급여/리브/공휴일 정책 모듈.

주간 급여 계산, OWP/AWE(평소 주급/평균소득) 비교, Section 23(1년 미만 퇴사) 정산,
기념일 이후 8% 정산, 애뉴얼 리브 자동 적립·기념일 발생, 공휴일 카테고리 판정 등을
담당합니다. helpers.py에만 의존합니다.
"""
from datetime import date, timedelta, datetime, timezone
from flask import request, jsonify, g
from scheduler import DAYS

from helpers import (
    NZ_TZ, load_state, save_state, require_login, require_owner, _log_audit, HOLIDAY_OWD_THRESHOLD,
    FREQUENCY_WINDOW_WEEKS, DEFAULT_PAYROLL_ROUNDING_MINUTES, ALLOWED_ROUNDING_MINUTES,
    _effective_shift_times, _actual_hours_for_entry, _assignment_duration_hours,
    _week_dates, _week_key_for_date, _is_paid_leave, _leave_info_by_day, _worked_that_weekday,
    _average_day_hours, _weekly_salary, _effective_hourly_rate_for_salary,
)



from flask import Blueprint
payroll_bp = Blueprint("payroll", __name__)

def _compute_week_payroll(state, week_key):
    """이 주(week_key)의 인건비(Labour Cost)를, 요일별/직원별로 계산합니다.
    "스케줄 기준"(로스터에 배정된 시간)과 "실제 기준"(클락인/아웃 기록, 회사가 설정한
    반올림 단위 적용) 둘 다 계산해서 같이 보여주고, 그 차액도 계산합니다.

      - 공휴일이 낀 날은 _public_holiday_category()로 A/B/C/D를 판정해서(스케줄 기준으로
        판정 — 공휴일 근무 의무는 "실제로 찍었는지"가 아니라 "로스터상 근무일이었는지"가
        법적 기준이므로) 그에 맞는 배율을 스케줄/실제 시간 양쪽에 똑같이 적용합니다:
        A(1)/C(3)=1.5배, B(2)=하루치 평균급여, D(4)=0
      - 공휴일이 아닌 날에 유급(paid) 또는 병가(sick) Leave Request가 있으면, 실제
        배정/근무 시간이 없어도 하루치 평균급여를 지급합니다. 무급(unpaid) Leave
        Request나 수동 Off는 원래대로 $0입니다.

    ⚠️ "하루치 평균급여"는 실제 과거 지급 내역(relevant daily pay)이 아니라, 이 직원의
    주당 최소시간(min_hours_per_week)을 목표 근무일수(target_days_per_week)로 나눈
    추정값입니다 — 정확한 금액은 회계/노무 담당자 확인을 권장합니다.
    """
    shift_times = _effective_shift_times(state)
    y, m, d = map(int, week_key.split("-"))
    monday = date(y, m, d)
    week_dates = [monday + timedelta(days=i) for i in range(7)]
    date_iso_by_day = {DAYS[i]: week_dates[i].isoformat() for i in range(7)}
    rounding_minutes = state.get("payroll_rounding_minutes", DEFAULT_PAYROLL_ROUNDING_MINUTES)

    holiday_by_day = {}
    for h in state["public_holidays"]:
        try:
            hd = date.fromisoformat(h["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if hd in week_dates:
            holiday_by_day[DAYS[week_dates.index(hd)]] = h

    week = state["weeks"].get(week_key)
    assignments = ((week.get("schedule") or {}).get("assignments") if week else None) or []
    policy = state["public_holiday_policy"]

    assignments_by_emp_day = {}
    for a in assignments:
        assignments_by_emp_day.setdefault((a["employee_id"], a["day"]), []).append(a)

    entries_by_emp_date = {}
    for te in state["time_entries"]:
        entries_by_emp_date.setdefault((te["employee_id"], te.get("date")), []).append(te)

    day_totals = {
        d: {
            "scheduled_hours": 0.0, "scheduled_cost": 0.0,
            "actual_hours": 0.0, "actual_cost": 0.0,
            "is_public_holiday": d in holiday_by_day,
        }
        for d in DAYS
    }
    employee_rows = []
    total_scheduled_cost = 0.0
    total_actual_cost = 0.0

    for e in state["employees"]:
        is_salary = e.get("pay_type") == "salary"
        if is_salary:
            wage = None
            salary_rate = _effective_hourly_rate_for_salary(e)  # 공휴일 추가수당 계산용 환산 시급
            base_weekly = _weekly_salary(e)
            has_wage = e.get("annual_salary") is not None
        else:
            wage = e.get("hourly_wage")
            salary_rate = None
            base_weekly = None
            has_wage = wage is not None
        emp_id = e["id"]
        weekly_hours = 0.0
        weekly_pay = 0.0
        weekly_actual_hours = 0.0
        weekly_actual_pay = 0.0
        has_open_entry = False
        per_day = {}
        avg_day_hours = _average_day_hours(e)
        leave_info = _leave_info_by_day(e, week_key)
        # 애뉴얼 리브(annual_leave) 급여는 시급제일 때 OWP/AWE 중 더 큰 쪽으로 계산합니다
        # (Holidays Act 2003 21조) — 그 외 유급/병가/Lieu Day는 그대로 현재 시급을 씁니다.
        # 주당 한 번만 계산해서 요일 루프 안에서 재활용합니다(매일 다시 계산할 필요 없음).
        annual_leave_hourly_rate = None
        if not is_salary:
            applicable_weekly_rate, _rate_info = _applicable_leave_rate(state, e)
            weekly_hours_for_rate = e.get("min_hours_per_week") or 0
            if applicable_weekly_rate is not None and weekly_hours_for_rate > 0:
                annual_leave_hourly_rate = applicable_weekly_rate / weekly_hours_for_rate

        for day in DAYS:
            day_assignments = assignments_by_emp_day.get((emp_id, day), [])
            hours = sum(_assignment_duration_hours(a, shift_times) for a in day_assignments)
            worked = len(day_assignments) > 0
            category = None
            leave_type = leave_info.get(day)

            day_entries = entries_by_emp_date.get((emp_id, date_iso_by_day[day]), [])
            actual_hours = sum(
                h for h in (_actual_hours_for_entry(te, rounding_minutes) for te in day_entries) if h is not None
            )
            day_has_open_entry = any(not te.get("clock_out") for te in day_entries)
            has_open_entry = has_open_entry or day_has_open_entry

            if day in holiday_by_day:
                category, _count, _is_usual = _public_holiday_category(state, policy, emp_id, day, week_key, worked)
                if category in (1, 3):
                    if is_salary:
                        # 연봉제는 기본급이 이미 고정 주급에 포함되어 있으므로, 공휴일에
                        # 일한 시간만큼 "0.5배 추가수당"만 더 얹어줍니다(1.5배 전체가
                        # 아니라 0.5배만 — 나머지 1배는 이미 주급에 포함되어 있으므로).
                        pay = 0.5 * salary_rate * hours if salary_rate is not None else None
                        actual_pay = 0.5 * salary_rate * actual_hours if salary_rate is not None else None
                    else:
                        pay = hours * wage * 1.5 if wage is not None else None
                        actual_pay = actual_hours * wage * 1.5 if wage is not None else None
                elif category == 2:
                    # 평소 근무일인데 안 일함 — 시급제는 하루치 평균급여를 별도 지급하지만,
                    # 연봉제는 어차피 고정 주급에 이미 포함되어 있으므로 추가 지급이 없습니다.
                    pay = (0.0 if has_wage else None) if is_salary else (avg_day_hours * wage if wage is not None else None)
                    actual_pay = pay
                else:
                    pay = 0.0 if has_wage else None
                    actual_pay = 0.0 if has_wage else None
            elif leave_type and _is_paid_leave(leave_type) and not worked:
                # 유급/병가/애뉴얼 리브/Lieu Day — 시급제는 하루치 평균급여를 지급하지만,
                # 연봉제는 이미 고정 주급에 포함되어 있으므로 추가 지급이 없습니다.
                # 애뉴얼 리브만 예외로, OWP/AWE 중 더 큰 쪽으로 계산된 단가를 씁니다.
                if is_salary:
                    pay = 0.0 if has_wage else None
                elif leave_type == "annual_leave" and annual_leave_hourly_rate is not None:
                    pay = avg_day_hours * annual_leave_hourly_rate
                else:
                    pay = avg_day_hours * wage if wage is not None else None
                actual_pay = pay
            else:
                if is_salary:
                    # 평범한 근무일 — 연봉제는 몇 시간을 일했든 요일별로 추가 지급이 없고
                    # (기본급은 주급으로 한 번에 반영), 시급제만 시간×시급으로 계산합니다.
                    pay = 0.0 if has_wage else None
                    actual_pay = 0.0 if has_wage else None
                else:
                    pay = hours * wage if wage is not None else None
                    actual_pay = actual_hours * wage if wage is not None else None

            per_day[day] = {
                "hours": round(hours, 2),
                "pay": round(pay, 2) if pay is not None else None,
                "actual_hours": round(actual_hours, 2),
                "actual_pay": round(actual_pay, 2) if actual_pay is not None else None,
                "has_open_entry": day_has_open_entry,
                "is_public_holiday": day in holiday_by_day,
                "category": category,
                "leave_type": leave_type,
            }
            weekly_hours += hours
            weekly_actual_hours += actual_hours
            day_totals[day]["scheduled_hours"] += hours
            day_totals[day]["actual_hours"] += actual_hours
            if pay is not None:
                weekly_pay += pay
                day_totals[day]["scheduled_cost"] += pay
            if actual_pay is not None:
                weekly_actual_pay += actual_pay
                day_totals[day]["actual_cost"] += actual_pay

        # 연봉제는 고정 주급을 요일 하나에 귀속시키지 않고, 주간 합계에만 한 번 더해줍니다
        # (그래서 위 요일별 표(day_totals)에는 연봉제 직원의 기본급이 잡히지 않고, 공휴일
        # 추가수당만 그날에 표시됩니다 — 기본급은 특정 요일에 "발생"하는 비용이 아니므로).
        if is_salary and has_wage:
            weekly_pay += base_weekly
            weekly_actual_pay += base_weekly

        employee_rows.append({
            "employee_id": emp_id, "employee_name": e["name"],
            "pay_type": "salary" if is_salary else "hourly",
            "hourly_wage": wage, "annual_salary": e.get("annual_salary"),
            "has_wage_set": has_wage,
            "weekly_hours": round(weekly_hours, 2),
            "weekly_pay": round(weekly_pay, 2) if has_wage else None,
            "weekly_actual_hours": round(weekly_actual_hours, 2),
            "weekly_actual_pay": round(weekly_actual_pay, 2) if has_wage else None,
            "pay_difference": round(weekly_actual_pay - weekly_pay, 2) if has_wage else None,
            "has_open_entry": has_open_entry,
            "per_day": per_day,
        })
        if has_wage:
            total_scheduled_cost += weekly_pay
            total_actual_cost += weekly_actual_pay

    for day in DAYS:
        day_totals[day]["scheduled_hours"] = round(day_totals[day]["scheduled_hours"], 2)
        day_totals[day]["scheduled_cost"] = round(day_totals[day]["scheduled_cost"], 2)
        day_totals[day]["actual_hours"] = round(day_totals[day]["actual_hours"], 2)
        day_totals[day]["actual_cost"] = round(day_totals[day]["actual_cost"], 2)

    return {
        "week_key": week_key,
        "has_public_holiday": bool(holiday_by_day),
        "rounding_minutes": rounding_minutes,
        "public_holidays": [
            {"date": h["date"], "name": h.get("name", ""), "day": day}
            for day, h in holiday_by_day.items()
        ],
        "days": day_totals,
        "employees": employee_rows,
        "total_scheduled_cost": round(total_scheduled_cost, 2),
        "total_actual_cost": round(total_actual_cost, 2),
    }

def _record_earnings_history(state, week_key, payroll):
    """이 주가 완전히 지난 주(오늘이 그 주의 일요일보다 뒤)라면, 각 직원의 "실제
    기준" 주급을 급여 이력에 기록해둡니다 — OWP(변동시간 근무자)와 AWE 계산은 지난
    몇 주간의 실제 지급액이 필요해서, 조회할 때마다 조용히 이렇게 쌓아갑니다. 아직
    안 지난 주는 기록하지 않습니다(그 주 클락아웃이 다 끝나지 않아 실제 금액이
    아직 확정이 아니므로). 이미 기록된 주는 최신 값으로 덮어씁니다(그 사이 클락인
    기록을 수정했을 수도 있으므로).

    각 주에서 "애뉴얼 리브로 지급된 금액"도 따로 같이 기록해둡니다 — Holidays Act
    2003 23조(1년 미만 퇴사 시 정산)는 "총소득의 8%에서 이미 당겨쓴 만큼 지급된
    금액을 뺀다"는 공식이라, 이 값을 분리해서 갖고 있어야 나중에 정확히 계산할 수
    있습니다."""
    try:
        week_dates = _week_dates(week_key)
        week_sunday = week_dates[-1]
    except (ValueError, IndexError):
        return
    today = datetime.now(NZ_TZ).date()
    if week_sunday >= today:
        return
    history = state.setdefault("earnings_history", {})
    for row in payroll["employees"]:
        if row["weekly_actual_pay"] is None:
            continue
        key = f"{row['employee_id']}|{week_key}"
        annual_leave_pay = sum(
            (d.get("actual_pay") or 0.0)
            for d in row["per_day"].values()
            if d.get("leave_type") == "annual_leave"
        )
        history[key] = {
            "employee_id": row["employee_id"], "week_key": week_key,
            "gross_pay": row["weekly_actual_pay"], "hours": row["weekly_actual_hours"],
            "annual_leave_pay": round(annual_leave_pay, 2),
        }

@payroll_bp.route("/api/weeks/<week_key>/payroll", methods=["GET"])
@require_login
def get_week_payroll(company_id, week_key):
    """이 주의 예상 인건비(Labour Cost)를 요일별/직원별로 계산해서 보여줍니다.
    사장 또는 매니저만 볼 수 있습니다 — 급여 정보라 민감하기 때문에, 나중에 직원 본인
    로그인이 생기더라도 이 페이지는 계속 사장/매니저 전용으로 남아야 합니다.

    아직 스케줄 자체가 없는 주(예: 급여창에서 다음 주로 이동했는데 그 주가 아직
    생성 안 된 경우)도 에러 없이 "전부 0원"인 정상 응답으로 돌려줍니다 —
    _compute_week_payroll이 이미 이런 상황을 안전하게 처리하도록 짜여 있어서,
    여기서 굳이 404로 막을 이유가 없었습니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    state = load_state(company_id)
    payroll = _compute_week_payroll(state, week_key)
    _record_earnings_history(state, week_key, payroll)
    save_state(company_id, state)
    return jsonify(payroll)

def _owp_for_employee(state, employee):
    """OWP(평소 주급, Ordinary Weekly Pay)를 계산합니다 — Holidays Act 2003 기준.
      - 근무시간이 고정("fixed", 기본값)이면: 계약 주당 시간 × 현재 시급
      - 변동("variable")이면: 최근 4주간의 실지급액 평균(earnings_history 기준)
    돌려주는 값: (OWP 금액|None, 계산에 쓰인 주 수). 변동시간인데 데이터가 아직
    없으면 (None, 0)을 돌려줍니다 — 시급이 없는 연봉제 직원도 대상이 아닙니다
    (연봉제는 애초에 주급이 고정이라 이 계산 자체가 필요 없습니다)."""
    wage = employee.get("hourly_wage")
    if wage is None:
        return None, 0
    if employee.get("hours_type") != "variable":
        return round((employee.get("min_hours_per_week") or 0) * wage, 2), None
    history = state.get("earnings_history", {})
    entries = [h for h in history.values() if h["employee_id"] == employee["id"]]
    entries.sort(key=lambda h: h["week_key"], reverse=True)
    recent = entries[:4]
    if not recent:
        return None, 0
    return round(sum(h["gross_pay"] for h in recent) / len(recent), 2), len(recent)

def _awe_for_employee(state, employee, prorated=False):
    """AWE(평균소득, Average Weekly Earnings)를 계산합니다.
      - prorated=False(기본): 최근 52주 실지급액 평균 — 정식 발생한(입사 12개월 이후)
        애뉴얼 리브에 적용합니다.
      - prorated=True: 입사일부터 지금까지의 전체 실지급액을, 52주가 아니라 "입사 후
        지난 주 수"로 나눈 값입니다 — Holidays Act 2003 22조에 따라, 아직 만 1년이
        안 된 직원이 리브를 당겨쓸 때는 52주로 나누면 안 되고 실제 근속 기간으로
        나눠야 합니다.
    데이터가 부족하면 있는 만큼만으로 평균 냅니다 — 정확한 법정 계산이 아니라
    "지금까지 RosterFlow가 기록한 데이터 기준" 추정치이니, 사용하는 쪽에서 데이터
    주 수를 같이 확인해야 합니다."""
    history = state.get("earnings_history", {})
    entries = [h for h in history.values() if h["employee_id"] == employee["id"]]
    if prorated:
        hire_date_str = employee.get("hire_date")
        if not hire_date_str:
            return None, 0
        try:
            hire = date.fromisoformat(hire_date_str)
        except ValueError:
            return None, 0
        hire_week_key = _week_key_for_date(hire)
        relevant = [h for h in entries if h["week_key"] >= hire_week_key]
        if not relevant:
            return None, 0
        weeks_elapsed = max(1, (date.today() - hire).days // 7)
        return round(sum(h["gross_pay"] for h in relevant) / weeks_elapsed, 2), weeks_elapsed
    entries.sort(key=lambda h: h["week_key"], reverse=True)
    recent = entries[:52]
    if not recent:
        return None, 0
    return round(sum(h["gross_pay"] for h in recent) / len(recent), 2), len(recent)

def _is_before_first_anniversary(employee, as_of=None):
    """이 직원이 아직 입사 후 첫 12개월(첫 기념일)을 맞지 않았으면 True입니다.
    이 기간에 쓰는 애뉴얼 리브는 전부 "당겨쓰기"이므로, AWE를 52주가 아니라 실제
    근속 주 수로 나눠서 계산해야 합니다(Holidays Act 2003 22조). 입사일을 모르면
    판단할 수 없으므로 False(=일반 케이스로 취급)를 돌려줍니다."""
    hire_date_str = employee.get("hire_date")
    if not hire_date_str:
        return False
    try:
        hire = date.fromisoformat(hire_date_str)
    except ValueError:
        return False
    return (as_of or date.today()) < hire + timedelta(days=365)

def _eight_percent_since(state, employee, since_date):
    """이 직원의 since_date 이후 총소득의 8%에서, 그 사이 이미 당겨써서 지급받은
    애뉴얼 리브 금액을 뺀 값을 계산합니다 — Holidays Act 2003상 "아직 정식으로
    발생 안 한(또는 마지막 기념일 이후로 새로 쌓이고 있는) 리브"에 대한 정산
    공식입니다. 이 값이 마이너스가 나올 수도 있습니다(당겨쓴 게 8% 적립분보다
    많으면, 직원이 사장에게 돌려줘야 하는 상황).
    돌려주는 값: (정산액|None, 계산에 쓰인 세부 내역 dict). earnings_history에
    since_date 이후 데이터가 아예 없으면 0으로 채워진 내역을 돌려줍니다."""
    since_week_key = _week_key_for_date(since_date)
    history = state.get("earnings_history", {})
    relevant = [
        h for h in history.values()
        if h["employee_id"] == employee["id"] and h["week_key"] >= since_week_key
    ]
    if not relevant:
        return 0.0, {"weeks_used": 0, "gross_earnings": 0.0, "advance_leave_paid": 0.0, "eight_percent": 0.0}

    gross_earnings = sum(h["gross_pay"] for h in relevant)
    advance_leave_paid = sum(h.get("annual_leave_pay", 0.0) for h in relevant)
    eight_percent = gross_earnings * 0.08
    settlement = round(eight_percent - advance_leave_paid, 2)
    return settlement, {
        "weeks_used": len(relevant),
        "gross_earnings": round(gross_earnings, 2),
        "advance_leave_paid": round(advance_leave_paid, 2),
        "eight_percent": round(eight_percent, 2),
    }

def _section23_settlement(state, employee):
    """Holidays Act 2003 23조: 입사 12개월이 되기 전에 퇴사하는 경우의 애뉴얼 리브
    정산액을 계산합니다.

        정산액 = 입사일부터 지금까지 총소득의 8% − 이미 당겨써서 지급받은 금액

    이 값이 마이너스가 나올 수도 있습니다 — 8% 적립분보다 더 많이 당겨썼으면,
    법적으로 그 차액을 직원이 사장에게 돌려줘야 하는 상황입니다(실제 회수 방법은
    별도 법적 절차가 필요할 수 있으니 회계사 확인이 필요합니다).

    돌려주는 값: (정산액|None, 계산에 쓰인 세부 내역 dict). 입사일이 없으면
    None을 돌려줍니다."""
    hire_date_str = employee.get("hire_date")
    if not hire_date_str:
        return None, None
    try:
        hire = date.fromisoformat(hire_date_str)
    except ValueError:
        return None, None
    return _eight_percent_since(state, employee, hire)

def _last_anniversary_date(employee, as_of=None):
    """이 직원의 "가장 최근에 이미 지난" 근속 기념일 날짜를 돌려줍니다(아직 한 번도
    기념일을 맞지 않았으면 None). 퇴사 정산 시 "마지막 기념일 이후 8%"를 계산할
    기준점으로 씁니다."""
    hire_date_str = employee.get("hire_date")
    if not hire_date_str:
        return None
    try:
        hire = date.fromisoformat(hire_date_str)
    except ValueError:
        return None
    as_of = as_of or date.today()
    last = None
    n = 1
    while True:
        anniversary_date = hire + timedelta(days=365 * n)
        if anniversary_date > as_of:
            break
        last = anniversary_date
        n += 1
        if n > 60:
            break
    return last

def _applicable_leave_rate(state, employee, prorated=None):
    """이 직원이 애뉴얼 리브를 쓸 때 적용할 "주급 단가"를 계산합니다 — OWP와 AWE 중
    더 높은 쪽(Holidays Act 2003 21조: "whichever is greater")입니다. prorated를
    지정하지 않으면, 아직 첫 기념일 전(당겨쓰기 상황)인지를 자동으로 판단해서
    AWE 계산 방식을 알맞게 고릅니다."""
    if prorated is None:
        prorated = _is_before_first_anniversary(employee)
    owp, owp_weeks = _owp_for_employee(state, employee)
    awe, awe_weeks = _awe_for_employee(state, employee, prorated=prorated)
    candidates = [v for v in (owp, awe) if v is not None]
    applied = max(candidates) if candidates else None
    return applied, {
        "owp": owp, "awe": awe, "owp_weeks_used": owp_weeks, "awe_weeks_used": awe_weeks,
        "prorated": prorated,
    }

def _process_annual_leave_anniversary(employee, state):
    """입사일(hire_date)이 등록되어 있으면, 지금까지 지난 12개월 기념일마다 애뉴얼
    리브를 정리합니다 — Holidays Act상 "1년 근속 시 4주 정식 발생"을 반영한
    것입니다. 이미 처리된 기념일은 annual_leave_anniversaries_granted에 기록해서
    중복 처리를 막습니다. 돌려주는 값: 잔액이 실제로 바뀌었으면 True.

    ⚠️ 8% 자동 적립(annual_leave_auto_accrual_enabled)과 겹치지 않게 주의해서
    처리합니다 — 실제 payroll 자료에 따르면 "매주 8%씩 쌓인 것이 12개월 시점에
    그대로 4주 정식 발생분으로 전환되고, 적립 카운터가 리셋"되는 구조입니다(8%라는
    숫자 자체가 4주÷52주를 올림한 값이라, 1년 내내 쌓으면 자연스럽게 4주 분량에
    도달합니다). 그래서 **첫 번째 기념일**에서, 자동 적립이 켜져 있었다면(그 1년
    동안 이미 8%로 쌓였을 것이므로) 4주를 "추가로 더 얹지" 않고, 이미 쌓인 걸 그대로
    그 해의 정식 발생분으로 인정합니다 — 안 그러면 8%로 쌓인 것 위에 4주를 또
    더해서 거의 두 배로 부풀려집니다. 두 번째 기념일부터는 자동 적립이 이미 멈춘
    뒤(연속 적립은 첫 기념일 전까지만 동작)라 겹칠 일이 없으므로, 정상적으로 4주씩
    더합니다. 자동 적립을 아예 안 켜두신 회사는 첫 기념일에도 정상적으로 4주가
    더해집니다(쌓인 게 없으니 더해야 함)."""
    hire_date_str = employee.get("hire_date")
    if not hire_date_str:
        return False
    try:
        hire = date.fromisoformat(hire_date_str)
    except ValueError:
        return False
    today = date.today()
    granted = set(employee.get("annual_leave_anniversaries_granted") or [])
    changed = False
    anniversary_num = 1
    while True:
        # N번째 기념일 날짜 = 입사일 + N*365일 (윤년 등으로 인한 하루이틀 오차는
        # 감안한 근사치입니다 — 실무적으로는 문제없는 수준입니다).
        anniversary_date = hire + timedelta(days=365 * anniversary_num)
        if anniversary_date > today:
            break
        key = str(anniversary_num)
        if key not in granted:
            is_first_and_already_accrued = anniversary_num == 1 and state.get("annual_leave_auto_accrual_enabled")
            if not is_first_and_already_accrued:
                weekly_hours = employee.get("min_hours_per_week") or 0
                employee["annual_leave_balance_hours"] = round(
                    (employee.get("annual_leave_balance_hours") or 0.0) + weekly_hours * 4, 2
                )
            granted.add(key)
            changed = True
        anniversary_num += 1
        if anniversary_num > 60:  # 방어적 상한(60년치) — 무한루프 방지용
            break
    employee["annual_leave_anniversaries_granted"] = list(granted)
    return changed

@payroll_bp.route("/api/employees/<employee_id>/adjust-annual-leave", methods=["POST"])
@require_login
def adjust_annual_leave(company_id, employee_id):
    """사장/매니저 전용: 애뉴얼 리브 잔액(시간)을 직접 조정합니다. 뉴질랜드 Holidays Act의
    정확한 법정 적립 계산(OWP/AWE 비교 등)은 과거 소득 이력 전체가 필요해서 여기서는
    하지 않고, 사장이 직접 관리하는 참고용 추정치로 취급합니다. body에 delta_hours(더하거나
    뺄 시간, 음수 가능)를 주거나, set_hours(절대값으로 바로 설정)를 줄 수 있습니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 작업은 사장 또는 매니저만 할 수 있습니다."}), 403
    payload = request.get_json(silent=True) or {}
    state = load_state(company_id)
    employee = next((e for e in state["employees"] if e["id"] == employee_id), None)
    if not employee:
        return jsonify({"error": "Employee not found."}), 404

    if "set_hours" in payload:
        try:
            new_balance = float(payload["set_hours"])
        except (TypeError, ValueError):
            return jsonify({"error": "set_hours must be a number."}), 400
    else:
        try:
            delta = float(payload.get("delta_hours", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "delta_hours must be a number."}), 400
        new_balance = employee.get("annual_leave_balance_hours", 0.0) + delta

    if new_balance < 0:
        return jsonify({"error": "애뉴얼 리브 잔액은 0보다 작을 수 없습니다."}), 400

    employee["annual_leave_balance_hours"] = round(new_balance, 2)
    _log_audit(state, "annual_leave_adjusted",
               f"{employee['name']}의 애뉴얼 리브 잔액을 {employee['annual_leave_balance_hours']}시간으로 조정",
               {"employee_id": employee_id, "new_balance": employee["annual_leave_balance_hours"]})
    save_state(company_id, state)
    return jsonify({"annual_leave_balance_hours": employee["annual_leave_balance_hours"]})

@payroll_bp.route("/api/employees/<employee_id>/final-payment-estimate", methods=["GET"])
@require_login
def final_payment_estimate(company_id, employee_id):
    """사장/매니저 전용: 퇴사 시 지급해야 할 것으로 추정되는 금액을 계산합니다.
      - 입사 12개월 이상: 정식 발생한 애뉴얼 리브 잔액 × (OWP와 AWE 중 더 높은 쪽,
        Holidays Act 2003 21조) + 마지막 기념일 이후로 일한 기간에 대한 8%(아직
        정식 발생 전인, 새로 쌓이고 있는 중인 부분)
      - 입사 12개월 미만: 총소득의 8% − 이미 당겨써서 지급받은 금액(23조) — 이
        값은 마이너스가 나올 수 있고, 그 경우 직원이 사장에게 돌려줘야 하는
        금액입니다.
    두 경우 다 RosterFlow가 그동안 기록해온 실지급 이력에 기반한 추정치라, 이력이
    짧으면 실제 법정 금액과 다를 수 있습니다 — 데이터 주 수를 같이 표시하니 참고하세요.

    ⚠️ 이건 참고용 추정치입니다. 실제 지급 전 반드시 회계사/노무사 확인을 받으세요."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    state = load_state(company_id)
    employee = next((e for e in state["employees"] if e["id"] == employee_id), None)
    if not employee:
        return jsonify({"error": "Employee not found."}), 404

    is_salary = employee.get("pay_type") == "salary"
    weekly_hours = employee.get("min_hours_per_week") or 0
    annual_leave_hours = employee.get("annual_leave_balance_hours", 0.0)
    lieu_days = employee.get("lieu_day_balance", 0.0)
    avg_day_hours = _average_day_hours(employee)

    rate_breakdown = None
    section23_info = None
    post_anniversary_info = None
    if is_salary:
        # 연봉제는 주급이 애초에 고정이라 OWP/AWE 비교가 사실상 의미가 없어서, 지금까지
        # 쓰던 환산시급 방식을 그대로 씁니다.
        hourly_rate = _effective_hourly_rate_for_salary(employee)
        applied_hourly_rate = hourly_rate
        annual_leave_payout = annual_leave_hours * applied_hourly_rate if applied_hourly_rate is not None else None
    else:
        hourly_rate = employee.get("hourly_wage")
        applied_weekly_rate, rate_breakdown = _applicable_leave_rate(state, employee)
        applied_hourly_rate = (applied_weekly_rate / weekly_hours) if (applied_weekly_rate is not None and weekly_hours > 0) else None
        annual_leave_payout = annual_leave_hours * applied_hourly_rate if applied_hourly_rate is not None else None

        if _is_before_first_anniversary(employee):
            # 입사 12개월 미만 퇴사 — Holidays Act 2003 23조: "총소득의 8% - 이미
            # 당겨써서 지급받은 금액"으로 계산합니다. 이 값이 마이너스면, 직원이
            # 오히려 사장에게 돈을 돌려줘야 하는 상황입니다(실제로 흔한 일은
            # 아니지만, 법적으로 인정되는 계산 결과입니다).
            section23_settlement, section23_info = _section23_settlement(state, employee)
            if section23_settlement is not None:
                annual_leave_payout = section23_settlement
        else:
            # 입사 12개월 이상 퇴사 — 남은 정식 리브 잔액(위에서 이미 계산됨)에 더해,
            # "마지막 기념일 이후로 일한 기간"에 대한 8%도 추가로 지급해야 합니다.
            # 이 기간은 아직 정식으로 4주가 발생하지 않은, 새로 쌓이고 있는 중인
            # 부분이기 때문입니다(다음 기념일이 와야 정식 발생함).
            last_anniversary = _last_anniversary_date(employee)
            if last_anniversary:
                post_settlement, post_anniversary_info = _eight_percent_since(state, employee, last_anniversary)
                if post_anniversary_info:
                    post_anniversary_info["since_date"] = last_anniversary.isoformat()
                if post_settlement is not None and annual_leave_payout is not None:
                    annual_leave_payout = round(annual_leave_payout + post_settlement, 2)

    lieu_day_payout = (lieu_days * avg_day_hours * hourly_rate) if hourly_rate is not None else None
    total = None
    if annual_leave_payout is not None and lieu_day_payout is not None:
        total = round(annual_leave_payout + lieu_day_payout, 2)

    return jsonify({
        "employee_id": employee_id, "employee_name": employee["name"],
        "pay_type": employee.get("pay_type", "hourly"),
        "hourly_rate_used": round(hourly_rate, 2) if hourly_rate is not None else None,
        "applied_hourly_rate": round(applied_hourly_rate, 2) if applied_hourly_rate is not None else None,
        "rate_breakdown": rate_breakdown,
        "section23": section23_info,
        "post_anniversary_eight_percent": post_anniversary_info,
        "annual_leave_balance_hours": round(annual_leave_hours, 2),
        "annual_leave_payout_estimate": round(annual_leave_payout, 2) if annual_leave_payout is not None else None,
        "lieu_day_balance": round(lieu_days, 2),
        "lieu_day_payout_estimate": round(lieu_day_payout, 2) if lieu_day_payout is not None else None,
        "total_estimate": total,
    })

@payroll_bp.route("/api/payroll-rounding", methods=["GET"])
@require_owner
def get_payroll_rounding(company_id):
    state = load_state(company_id)
    return jsonify({"rounding_minutes": state.get("payroll_rounding_minutes", DEFAULT_PAYROLL_ROUNDING_MINUTES)})

@payroll_bp.route("/api/payroll-rounding", methods=["POST"])
@require_owner
def set_payroll_rounding(company_id):
    payload = request.get_json(silent=True) or {}
    try:
        minutes = int(payload.get("rounding_minutes"))
    except (TypeError, ValueError):
        return jsonify({"error": "rounding_minutes must be a number."}), 400
    if minutes not in ALLOWED_ROUNDING_MINUTES:
        return jsonify({"error": f"rounding_minutes must be one of {ALLOWED_ROUNDING_MINUTES}."}), 400
    state = load_state(company_id)
    state["payroll_rounding_minutes"] = minutes
    _log_audit(state, "payroll_rounding_changed", f"급여 계산 단위를 {minutes}분으로 변경", {"rounding_minutes": minutes})
    save_state(company_id, state)
    return jsonify({"rounding_minutes": minutes})

@payroll_bp.route("/api/annual-leave-accrual-settings", methods=["GET"])
@require_owner
def get_annual_leave_accrual_settings(company_id):
    state = load_state(company_id)
    return jsonify({"enabled": bool(state.get("annual_leave_auto_accrual_enabled"))})

@payroll_bp.route("/api/annual-leave-accrual-settings", methods=["POST"])
@require_owner
def set_annual_leave_accrual_settings(company_id):
    """사장 전용: 매주 스케줄 퍼블리시 시 시급제 직원의 애뉴얼 리브 잔액에 그 주
    급여의 8%를 자동으로 적립할지 여부를 켜고 끕니다. 연봉제 직원은 이 자동 적립
    대상이 아닙니다(보통 연봉에 이미 포함된 구조이므로)."""
    payload = request.get_json(silent=True) or {}
    state = load_state(company_id)
    enabled = bool(payload.get("enabled"))
    state["annual_leave_auto_accrual_enabled"] = enabled
    _log_audit(state, "annual_leave_accrual_toggled", f"애뉴얼 리브 자동 적립(8%) {'켜짐' if enabled else '꺼짐'}", {"enabled": enabled})
    save_state(company_id, state)
    return jsonify({"enabled": enabled})

def _accrue_annual_leave_for_week(state, week_key):
    """이번 주 스케줄을 퍼블리시할 때, (자동 적립이 켜져 있으면) 시급제 직원들에게
    그 주 예상(스케줄 기준) 급여의 8%만큼을 애뉴얼 리브 잔액(시간)으로 자동 적립합니다.
    연봉제는 보통 애뉴얼 리브가 이미 연봉에 포함된 구조라 이 자동 적립 대상에서
    제외합니다. 입사일이 등록되어 있고 이미 첫 기념일(12개월)이 지난 직원은 여기서
    적립을 멈춥니다 — 그 이후는 _process_annual_leave_anniversary가 주는 "기념일마다
    4주 한 번에 발생" 방식으로 넘어가야 하고, 두 방식이 동시에 계속 쌓이면 중복
    적립이 되기 때문입니다. 같은 주에 대해 같은 직원이 중복 적립되지 않도록
    state["annual_leave_accrual_credits"]에 기록해둡니다."""
    if not state.get("annual_leave_auto_accrual_enabled"):
        return
    payroll = _compute_week_payroll(state, week_key)
    credited = set(state.setdefault("annual_leave_accrual_credits", []))
    employees_by_id = {e["id"]: e for e in state["employees"]}

    for row in payroll["employees"]:
        emp_id = row["employee_id"]
        employee = employees_by_id.get(emp_id)
        if not employee or employee.get("pay_type") == "salary":
            continue
        # 입사일이 등록되어 있고, 이미 첫 기념일(12개월)이 지났으면 여기서 8% 주간
        # 적립을 멈춥니다 — 그 이후부터는 _process_annual_leave_anniversary가 주는
        # "기념일마다 4주 한 번에 발생"으로 넘어가야 하고, 두 방식을 동시에 계속
        # 쌓으면 잔액이 실제보다 부풀려지는 중복 적립이 됩니다. 입사일이 아직 등록
        # 안 된 직원은 근속 기간을 판단할 수 없으므로, 예전처럼 계속 8%를 적립합니다
        # (등록해두시면 그때부터 정확히 전환됩니다).
        if employee.get("hire_date") and not _is_before_first_anniversary(employee):
            continue
        credit_key = f"{emp_id}|{week_key}"
        if credit_key in credited:
            continue
        wage = employee.get("hourly_wage")
        weekly_pay = row.get("weekly_pay")
        if not wage or not weekly_pay or wage <= 0:
            continue
        accrued_hours = (weekly_pay * 0.08) / wage
        employee["annual_leave_balance_hours"] = round(employee.get("annual_leave_balance_hours", 0.0) + accrued_hours, 2)
        credited.add(credit_key)
    state["annual_leave_accrual_credits"] = list(credited)

def _credit_lieu_days_for_week(state, week_key):
    """이 주에 공휴일이 있고, 어떤 직원이 카테고리 A(평소 근무일+일함 → 1.5배+Lieu Day)에
    해당하면 그 직원의 Lieu Day 잔액에 +1을 크레딧합니다. 같은 공휴일에 대해 중복으로
    크레딧되지 않도록, 이미 크레딧한 (직원, 날짜) 조합을 state["lieu_day_credits"]에
    기록해둡니다 — 이 주를 여러 번 퍼블리시해도 같은 공휴일로 두 번 크레딧되지 않습니다."""
    week = state["weeks"].get(week_key)
    if not week:
        return
    y, m, d = map(int, week_key.split("-"))
    monday = date(y, m, d)
    week_dates = [monday + timedelta(days=i) for i in range(7)]

    holiday_by_day = {}
    for h in state["public_holidays"]:
        try:
            hd = date.fromisoformat(h["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if hd in week_dates:
            holiday_by_day[DAYS[week_dates.index(hd)]] = h["date"]
    if not holiday_by_day:
        return

    assignments = (week.get("schedule") or {}).get("assignments") or []
    worked_by_emp_day = {(a["employee_id"], a["day"]) for a in assignments}

    policy = state["public_holiday_policy"]
    credited = set(state.setdefault("lieu_day_credits", []))
    for e in state["employees"]:
        for day, holiday_date in holiday_by_day.items():
            worked = (e["id"], day) in worked_by_emp_day
            category, _count, _is_usual = _public_holiday_category(state, policy, e["id"], day, week_key, worked)
            if category == 1:
                credit_key = f"{e['id']}|{holiday_date}"
                if credit_key not in credited:
                    e["lieu_day_balance"] = round(e.get("lieu_day_balance", 0.0) + 1, 2)
                    credited.add(credit_key)
    state["lieu_day_credits"] = list(credited)

def _validate_public_holiday_policy(payload):
    """회사가 보낸 공휴일 정책 설정값을 검증하고, 정리된 딕셔너리를 돌려줍니다.
    문제가 있으면 (None, 에러메시지)를 돌려줍니다."""
    method = payload.get("method")
    if method not in ("threshold", "actual_only"):
        return None, "method는 'threshold' 또는 'actual_only'여야 합니다."

    policy = {"method": method}
    if method == "threshold":
        try:
            window_weeks = int(payload.get("window_weeks"))
            min_weeks_worked = int(payload.get("min_weeks_worked"))
        except (TypeError, ValueError):
            return None, "window_weeks와 min_weeks_worked는 숫자여야 합니다."
        if window_weeks < 1 or window_weeks > 26:
            return None, "window_weeks는 1~26 사이여야 합니다."
        if min_weeks_worked < 1 or min_weeks_worked > window_weeks:
            return None, "min_weeks_worked는 1 이상, window_weeks 이하여야 합니다."
        policy["window_weeks"] = window_weeks
        policy["min_weeks_worked"] = min_weeks_worked
    # method == "actual_only"인 경우 추가 필드가 없습니다 — 일했으면 항상 1.5배+대체휴무로
    # 고정 처리합니다 (get_public_holiday_info 참고).

    return policy, None

@payroll_bp.route("/api/public-holiday-policy", methods=["GET"])
@require_login
def get_public_holiday_policy(company_id):
    state = load_state(company_id)
    return jsonify(state["public_holiday_policy"])

@payroll_bp.route("/api/public-holiday-policy", methods=["POST"])
@require_login
def set_public_holiday_policy(company_id):
    payload = request.get_json(silent=True) or {}
    policy, error = _validate_public_holiday_policy(payload)
    if error:
        return jsonify({"error": error}), 400
    state = load_state(company_id)
    state["public_holiday_policy"] = policy
    _log_audit(state, "public_holiday_policy_changed", "공휴일 판정 정책 변경", {"policy": policy})
    save_state(company_id, state)
    return jsonify(policy)

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

def _public_holiday_category(state, policy, emp_id, day, week_key, worked):
    """공휴일 요일(day)에 이 직원이 A/B/C/D 중 어느 카테고리에 해당하는지 계산합니다
    (1=A: 평소근무일+일함, 2=B: 평소근무일+안일함, 3=C: 평소근무일아님+일함, 4=D: 해당없음).
    get_public_holiday_info와 payroll 계산에서 공통으로 씁니다 — 두 군데서 서로 다른
    기준으로 계산되면 안 되기 때문에 반드시 이 함수 하나만 씁니다."""
    if policy["method"] == "threshold":
        count = _weekday_total_count(state, emp_id, day, week_key, window=policy["window_weeks"])
        is_usual_day = count >= policy["min_weeks_worked"]
        if is_usual_day and worked:
            category = 1
        elif is_usual_day and not worked:
            category = 2
        elif not is_usual_day and worked:
            category = 3
        else:
            category = 4
    else:  # actual_only — 과거 근무 기록을 보지 않고, 이 공휴일에 실제로 일했는지만 봅니다.
        # 일했으면 항상 1.5배+대체휴무(카테고리 1), 아니면 해당 없음(카테고리 4)으로 고정합니다.
        count = None
        is_usual_day = None
        category = 1 if worked else 4
    return category, count, is_usual_day

@payroll_bp.route("/api/weeks/<week_key>/public-holiday-info", methods=["GET"])
@require_login
def get_public_holiday_info(company_id, week_key):
    """이 주(week_key)에 Public Holiday가 포함되어 있으면, 이 회사가 설정한 정책
    (Settings > 공휴일 정책)에 따라 직원별 적용 항목(1.5배+Lieu / 평소급여만 / 1.5배만 /
    해당없음)을 계산해서 돌려줍니다.

    ⚠️ 이 계산은 사용자가 정의한 규칙을 그대로 옮긴 것으로, 실제 급여 지급 전에는
    회계/노무 담당자 확인을 권장합니다."""
    state = load_state(company_id)
    policy = state["public_holiday_policy"]
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
        return jsonify({"holidays": [], "categories": {}, "policy": policy})

    week = state["weeks"].get(week_key)
    assignments = ((week.get("schedule") or {}).get("assignments") if week else None) or []
    worked_today = {(a["employee_id"], a["day"]) for a in assignments}

    categories = {}
    for holiday in holidays_this_week:
        day = holiday["day"]
        rows = []
        for e in state["employees"]:
            emp_id = e["id"]
            worked = (emp_id, day) in worked_today
            category, count, is_usual_day = _public_holiday_category(state, policy, emp_id, day, week_key, worked)

            rows.append({
                "employee_id": emp_id, "employee_name": e["name"],
                "occurrence_count": count, "is_usual_working_day": is_usual_day,
                "worked_on_holiday": worked, "category": category,
            })
        categories[day] = rows

    return jsonify({"holidays": holidays_this_week, "categories": categories, "policy": policy})
