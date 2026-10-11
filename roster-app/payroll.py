"""
RosterFlow 급여/리브/공휴일 모듈 — Employment Leave Act 2026(2028-08-06 시행) 기준.

주간 인건비 계산(LCP 12.5%, OWD 공휴일, 가장 낮은 시급 기준 리브 지급), 애뉴얼 리브/
병가 시간 적립(0.0769/0.0385 × 표준시간, 병가 상한 160시간), 대체휴일(Alternative
Holiday) 1:1 적립, 계약시간 초과 알림, 퇴사 정산 추정을 담당합니다. helpers.py에만 의존합니다.
(구법 Holidays Act 2003의 OWP/AWE, Section 23, 8%, 기념일 부여, Lieu Day는 제거되었습니다.)
"""
from datetime import date, timedelta, datetime, timezone
from flask import request, jsonify, g, Response
from scheduler import DAYS
import csv
import io

from helpers import (
    NZ_TZ, load_state, save_state, require_login, require_owner, _log_audit,
    FREQUENCY_WINDOW_WEEKS, HOLIDAY_OWD_RATIO, DEFAULT_PAYROLL_ROUNDING_MINUTES, ALLOWED_ROUNDING_MINUTES,
    ANNUAL_ACCRUAL_PER_HOUR, SICK_ACCRUAL_PER_HOUR, SICK_LEAVE_CAP_HOURS, LCP_RATE,
    PH_WORKED_OWD_MULTIPLIER, PH_WORKED_NON_OWD_MULTIPLIER,
    _effective_shift_times, _actual_hours_for_entry, _assignment_duration_hours,
    _week_dates, _week_key_for_date, _is_paid_leave, _leave_info_by_day, _leave_hours_by_day,
    _leave_hours_explicit_by_day, _worked_that_weekday,
    _average_day_hours, _contract_hours, _lowest_hourly_rate, _weekly_salary,
    _effective_hourly_rate_for_salary, empty_week,
)

from flask import Blueprint
payroll_bp = Blueprint("payroll", __name__)

def _compute_week_payroll(state, week_key):
    """[Employment Leave Act 2026 기준] 이 주(week_key)의 인건비(Labour Cost)를, 요일별/직원별로 계산합니다.
    "스케줄 기준"(로스터에 배정된 시간)과 "실제 기준"(클락인/아웃 기록, 회사가 설정한
    반올림 단위 적용) 둘 다 계산해서 같이 보여주고, 그 차액도 계산합니다.

      - 공휴일이 낀 날은 _public_holiday_category()로 1~4를 판정합니다(스케줄 기준):
        1=OWD+일함 → 1.5배(+대체휴일 1:1 적립, 별도), 2=OWD+안 일함 → 하루치(표준시간
        근로자만, 캐주얼은 0), 3=OWD 아님+일함 → 1.0배(가정, helpers 상수), 4=해당없음 0
      - 유급(paid)/병가/애뉴얼/대체휴일 리브는 **가장 낮은 시급**으로 하루치를 지급합니다
        (신법: OWP/AWE 비교 없음). 무급(unpaid)은 $0.
      - LCP(Leave Compensation Payment) 12.5%: 캐주얼은 일한 모든 시간에, 표준시간
        근로자는 계약 주당시간을 넘긴 "추가 시간"에 더합니다(그 시간엔 리브가 적립 안 되므로).

    ⚠️ "하루치 평균급여"는 실제 과거 지급 내역(relevant daily pay)이 아니라, 이 직원의
    계약 표준시간(없으면 로스터 최소시간)을 목표 근무일수(target_days_per_week)로 나눈
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
        leave_hours_info = _leave_hours_by_day(e, week_key)
        leave_hours_explicit_info = _leave_hours_explicit_by_day(e, week_key)
        # 계약 최소시간보다 실제 배정이 부족했는데, "사업 사정"이라 판단해서 부족분을
        # 급여로 보전해주기로 한 기록 — {day: {hours, reason, ...}} 형태입니다.
        emp_topups = ((state.get("weeks", {}).get(week_key) or {}).get("shortfall_topups") or {}).get(emp_id, {})
        is_casual = e.get("employment_type") == "casual"
        contract_h = _contract_hours(e)

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
                category, _count, _is_usual = _public_holiday_category(
                    state, policy, emp_id, day, week_key, worked,
                    pair_date=holiday_by_day[day].get("pair_date"),
                )
                if category in (1, 3):
                    mult = PH_WORKED_OWD_MULTIPLIER if category == 1 else PH_WORKED_NON_OWD_MULTIPLIER
                    if is_salary:
                        # 연봉제는 기본급이 고정 주급에 포함 — 공휴일 근무분은 (배율-1)배만 추가.
                        pay = (mult - 1) * salary_rate * hours if salary_rate is not None else None
                        actual_pay = (mult - 1) * salary_rate * actual_hours if salary_rate is not None else None
                    else:
                        pay = hours * wage * mult if wage is not None else None
                        actual_pay = actual_hours * wage * mult if wage is not None else None
                elif category == 2:
                    # OWD인데 안 일함 — 표준시간 시급제는 하루치(가장 낮은 시급×평균 하루시간),
                    # 연봉제는 주급에 포함이라 추가 없음, 캐주얼은 지급 없음(가정).
                    if is_salary:
                        pay = 0.0 if has_wage else None
                    elif is_casual:
                        pay = 0.0 if has_wage else None
                    else:
                        pay = avg_day_hours * wage if wage is not None else None
                    actual_pay = pay
                else:
                    pay = 0.0 if has_wage else None
                    actual_pay = 0.0 if has_wage else None
            elif leave_type and _is_paid_leave(leave_type) and not worked:
                # 유급/병가/애뉴얼/대체휴일 — 시급제는 하루치를 "가장 낮은 시급"으로 지급하고,
                # 연봉제는 고정 주급에 이미 포함되어 추가 지급이 없습니다. 하루치 시간은
                # 신청 시 직접 입력한 시간이 있으면 그 값, 없으면 평균 하루시간입니다.
                leave_day_hours = leave_hours_info.get(day, avg_day_hours)
                if is_salary:
                    pay = 0.0 if has_wage else None
                else:
                    pay = leave_day_hours * wage if wage is not None else None
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
                    # 스케줄은 있었지만(worked=True) 이날 유급/병가/애뉴얼 리브도 걸려있는
                    # 경우 — 부분 조퇴(예: 8시간 근무 중 5시간만 일하고 3시간은 아파서
                    # 조퇴)를 처리합니다. 날짜별로 직접 입력한 시간(daily_hours)이
                    # 있으면 "그 시간만큼 그대로" 추가합니다(예: 3시간 입력했으면
                    # 실제 일한 5시간에 3시간을 그냥 더해서 8시간분). 직접 입력이
                    # 없으면(평균 폴백) "평균 하루시간에서 이미 일한 시간을 뺀
                    # 부족분"만 추가합니다 — 이 경우는 얼마나 일했는지 모르니
                    # 추정치라, 이미 평균만큼 일했으면 더 얹을 게 없어야 합니다.
                    if leave_type and _is_paid_leave(leave_type):
                        explicit_hours = leave_hours_explicit_info.get(day)
                        if explicit_hours is not None:
                            addon_hours = explicit_hours
                        else:
                            addon_hours = max(0.0, avg_day_hours - actual_hours)
                        if addon_hours > 0:
                            if wage is not None and actual_pay is not None:
                                actual_pay = round(actual_pay + addon_hours * wage, 2)

            # 이 요일에 "사업 사정 부족분 보전(topup)" 기록이 있으면, 실제 지급액에
            # 그만큼 추가로 더합니다 — 리브와는 별개 개념이라(직원 사정이 아니라
            # 사업 사정), 위의 리브 관련 분기들과 상관없이 항상 마지막에 적용합니다.
            topup = emp_topups.get(day)
            topup_hours = 0.0
            if topup and not is_salary and wage is not None:
                topup_hours = float(topup.get("hours") or 0)
                if topup_hours > 0 and actual_pay is not None:
                    actual_pay = round(actual_pay + topup_hours * wage, 2)

            per_day[day] = {
                "hours": round(hours, 2),
                "pay": round(pay, 2) if pay is not None else None,
                "actual_hours": round(actual_hours, 2),
                "actual_pay": round(actual_pay, 2) if actual_pay is not None else None,
                "has_open_entry": day_has_open_entry,
                "is_public_holiday": day in holiday_by_day,
                "category": category,
                "leave_type": leave_type,
                "topup_hours": round(topup_hours, 2) if topup_hours else 0.0,
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

        # LCP 12.5% — 캐주얼: 일한 모든 시간 / 표준시간 근로자: 계약시간 초과분(추가 시간)
        base_rate = _lowest_hourly_rate(e)
        if is_casual:
            add_sched, add_actual = weekly_hours, weekly_actual_hours
        elif contract_h > 0:
            add_sched = max(0.0, weekly_hours - contract_h)
            add_actual = max(0.0, weekly_actual_hours - contract_h)
        else:
            add_sched = add_actual = 0.0
        lcp_pay = lcp_actual_pay = 0.0
        if has_wage and base_rate is not None:
            lcp_pay = round(add_sched * base_rate * LCP_RATE, 2)
            lcp_actual_pay = round(add_actual * base_rate * LCP_RATE, 2)
            weekly_pay += lcp_pay
            weekly_actual_pay += lcp_actual_pay
        over_contract = (not is_casual) and contract_h > 0 and weekly_hours > contract_h + 0.01

        employee_rows.append({
            "employee_id": emp_id, "employee_name": e["name"],
            "pay_type": "salary" if is_salary else "hourly",
            "employment_type": "casual" if is_casual else "standard",
            "contract_hours": round(contract_h, 2),
            "additional_hours": round(add_sched, 2),
            "lcp_pay": lcp_pay, "lcp_actual_pay": lcp_actual_pay,
            "over_contract": over_contract,
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
    save_state(company_id, state)
    return jsonify(payroll)


def _build_payroll_export_rows(state, employee_id, start, end):
    """이 직원의 start~end 기간 급여 기록을 요일별 행으로 만듭니다(CSV 변환 전
    순수 데이터) — 라우트 함수와 분리해서, HTTP 요청 없이도 이 로직만 바로
    테스트할 수 있게 합니다."""
    rows = []
    cur_monday = start - timedelta(days=start.weekday())
    while cur_monday <= end:
        week_key = cur_monday.isoformat()
        payroll = _compute_week_payroll(state, week_key)
        emp_row = next((r for r in payroll["employees"] if r["employee_id"] == employee_id), None)
        if emp_row:
            for i, day in enumerate(DAYS):
                the_date = cur_monday + timedelta(days=i)
                if not (start <= the_date <= end):
                    continue
                d = emp_row["per_day"].get(day, {})
                rows.append((
                    the_date.isoformat(), day,
                    d.get("hours", 0), d.get("pay"),
                    d.get("actual_hours", 0), d.get("actual_pay"),
                    d.get("leave_type") or "", "Yes" if d.get("is_public_holiday") else "",
                    d.get("topup_hours") or "",
                ))
        cur_monday += timedelta(days=7)
    return rows


@payroll_bp.route("/api/employees/<employee_id>/payroll-export", methods=["GET"])
@require_login
def export_employee_payroll_csv(company_id, employee_id):
    """사장/매니저 전용: 이 직원의 급여·근무 기록을 CSV로 내보냅니다. 뉴질랜드
    Employment Leave Act 2026/Employment Relations Act상, 임금·시간·리브 기록은 최소 7년간
    보관해야 하고 노동청(Labour Inspector)이 요청하면 즉시 제출할 수 있어야 합니다
    — 이 파일을 받아서 따로 보관해두시면 그 대비가 됩니다. 실제 급여 지급은 사장님이
    쓰시는 정식 페이롤 시스템(PAYE·KiwiSaver 계산 포함)에서 이뤄지는 게 맞고, 이
    CSV는 어디까지나 "그 지급이 어떤 근거로 계산됐는지"를 보여주는 참고 자료입니다.

    쿼리 파라미터: start_date, end_date (YYYY-MM-DD, 생략하면 입사일~오늘 전체.
    입사일도 없으면 2020-01-01부터로 간주합니다)."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    state = load_state(company_id)
    employee = next((e for e in state["employees"] if e["id"] == employee_id), None)
    if not employee:
        return jsonify({"error": "Employee not found."}), 404

    start_str = request.args.get("start_date")
    end_str = request.args.get("end_date")
    try:
        if start_str:
            start = date.fromisoformat(start_str)
        elif employee.get("hire_date"):
            start = date.fromisoformat(employee["hire_date"])
        else:
            start = date(2020, 1, 1)
        end = date.fromisoformat(end_str) if end_str else date.today()
    except ValueError:
        return jsonify({"error": "날짜 형식이 올바르지 않습니다."}), 400
    if start > end:
        return jsonify({"error": "시작일이 종료일보다 늦을 수 없습니다."}), 400
    if (end - start).days > 366 * 8:
        return jsonify({"error": "한 번에 최대 8년치까지만 내보낼 수 있습니다. 기간을 나눠서 요청해주세요."}), 400

    rows = _build_payroll_export_rows(state, employee_id, start, end)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Employee Name", "Employee ID", "Date", "Day",
        "Scheduled Hours", "Scheduled Pay ($)", "Actual Hours", "Actual Pay ($)",
        "Leave Type", "Public Holiday", "Business Shortfall Top-up Hours",
    ])
    for r in rows:
        writer.writerow([employee["name"], employee_id, *r])
    csv_text = output.getvalue()

    safe_name = "".join(c if c.isalnum() else "_" for c in employee["name"])
    filename = f"{safe_name}_payroll_{start.isoformat()}_to_{end.isoformat()}.csv"
    return Response(
        csv_text, mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@payroll_bp.route("/api/employees/<employee_id>/leave-history-export", methods=["GET"])
@require_login
def export_employee_leave_history_csv(company_id, employee_id):
    """사장/매니저 전용: 이 직원의 리브 신청 이력 전체(대기중/승인됨/거절됨 다
    포함)를 CSV로 내보냅니다 — 누가 언제 신청했고, 누가 언제 승인/거절했는지까지
    전부 기록되어 있어서, 나중에 "그때 승인 안 했다/신청 안 했다" 같은 분쟁이
    생겼을 때 근거 자료로 쓸 수 있습니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    state = load_state(company_id)
    employee = next((e for e in state["employees"] if e["id"] == employee_id), None)
    if not employee:
        return jsonify({"error": "Employee not found."}), 404

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Employee Name", "Start Date", "End Date", "Leave Type", "Status", "Reason",
        "Submitted By", "Submitted At", "Reviewed By", "Reviewed At", "Reject Reason",
    ])
    for lr in sorted(employee.get("leave_requests") or [], key=lambda r: r.get("start_date", "")):
        writer.writerow([
            employee["name"], lr.get("start_date", ""), lr.get("end_date", ""),
            lr.get("leave_type", ""), lr.get("status", "approved"), lr.get("reason", ""),
            lr.get("submitted_by_name", ""), lr.get("submitted_at", ""),
            lr.get("reviewed_by_name", ""), lr.get("reviewed_at", ""), lr.get("reject_reason", ""),
        ])
    csv_text = output.getvalue()

    safe_name = "".join(c if c.isalnum() else "_" for c in employee["name"])
    filename = f"{safe_name}_leave_history.csv"
    return Response(
        csv_text, mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@payroll_bp.route("/api/weeks/<week_key>/hours-shortfall", methods=["GET"])
@require_login
def get_week_hours_shortfall(company_id, week_key):
    """사장/매니저 전용: 이 주에 "계약상 주당 최소시간"보다 실제 배정된 시간이
    부족한 직원들을 찾아서 보여줍니다 — 계약서에 최소시간이 명시되어 있는데 그보다
    적게 로스터되면, 그게 사업 사정일 경우 그 차액을 지급해야 할 의무가 있을 수
    있습니다(뉴질랜드 ERA 판례 기준 — 직원의 병가/리브 등 "직원 사정"일 때만
    예외입니다). 연봉제는 애초에 시간 단위 최소시간 개념이 급여에 안 걸리므로
    제외합니다. 이미 사업사정으로 보전(topup) 처리됐거나 리브로 등록된 만큼은
    "실제 시간"에 이미 반영되어 있으므로, 여기서 다시 부족으로 잡히지 않습니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    state = load_state(company_id)
    payroll = _compute_week_payroll(state, week_key)
    employees_by_id = {e["id"]: e for e in state["employees"]}
    results = []
    for row in payroll["employees"]:
        emp = employees_by_id.get(row["employee_id"])
        if not emp or emp.get("pay_type") == "salary":
            continue
        min_hours = emp.get("min_hours_per_week") or 0
        actual = row.get("weekly_actual_hours")
        if actual is None or min_hours <= 0:
            continue
        if actual < min_hours - 0.01:
            results.append({
                "employee_id": row["employee_id"], "employee_name": row["employee_name"],
                "min_hours_per_week": min_hours, "actual_hours": round(actual, 2),
                "shortfall_hours": round(min_hours - actual, 2),
            })
    return jsonify(results)


@payroll_bp.route("/api/weeks/<week_key>/shortfall-topup", methods=["POST"])
@require_login
def create_shortfall_topup(company_id, week_key):
    """사장/매니저 전용: "계약 최소시간보다 부족한 게 사업 사정 때문"이라고 판단해서,
    그 부족분만큼 급여를 그냥 보전해주는 걸 기록합니다. body: {employee_id, day,
    hours, reason}. day는 DAYS(mon~sun) 중 하나여야 하고, 이미 그 요일에 등록된
    보전 기록이 있으면 덮어씁니다(수정 용도)."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 작업은 사장 또는 매니저만 할 수 있습니다."}), 403
    payload = request.get_json(silent=True) or {}
    employee_id = payload.get("employee_id")
    day = payload.get("day")
    try:
        hours = float(payload.get("hours"))
    except (TypeError, ValueError):
        return jsonify({"error": "시간을 올바르게 입력해주세요."}), 400
    reason = str(payload.get("reason") or "").strip()[:200]
    if day not in DAYS:
        return jsonify({"error": "요일이 올바르지 않습니다."}), 400
    if hours <= 0:
        return jsonify({"error": "0보다 큰 시간을 입력해주세요."}), 400

    state = load_state(company_id)
    emp = next((e for e in state["employees"] if e["id"] == employee_id), None)
    if not emp:
        return jsonify({"error": "Employee not found."}), 404
    week = state["weeks"].setdefault(week_key, empty_week())
    topups = week.setdefault("shortfall_topups", {})
    emp_topups = topups.setdefault(employee_id, {})
    emp_topups[day] = {
        "hours": round(hours, 2), "reason": reason,
        "recorded_by": g.user_name, "recorded_by_role": g.role,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    _log_audit(state, "shortfall_topup_recorded",
               f"{emp['name']}님의 {week_key} 주 {day} 부족분({hours}시간) 사업사정 보전 등록",
               {"employee_id": employee_id, "week_key": week_key, "day": day, "hours": hours})
    save_state(company_id, state)
    return jsonify(emp_topups[day])



# ---------------------------------------------------------------------------
# 리브 잔액 수동 조정 (모두 시간 단위)
# ---------------------------------------------------------------------------

def _adjust_balance(company_id, employee_id, field, label, audit_action, cap=None):
    """사장/매니저 전용 공통 처리: body의 delta_hours(증감) 또는 set_hours(절대값)로
    employee[field](시간)를 조정합니다. 음수 불가, cap이 있으면 그 이상 불가."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 작업은 사장 또는 매니저만 할 수 있습니다."}), 403
    payload = request.get_json(silent=True) or {}
    state = load_state(company_id)
    employee = next((e for e in state["employees"] if e["id"] == employee_id), None)
    if not employee:
        return jsonify({"error": "Employee not found."}), 404
    try:
        if "set_hours" in payload:
            new_balance = float(payload["set_hours"])
        else:
            new_balance = (employee.get(field) or 0.0) + float(payload.get("delta_hours", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "시간은 숫자여야 합니다."}), 400
    if new_balance < 0:
        return jsonify({"error": f"{label} 잔액은 0보다 작을 수 없습니다."}), 400
    if cap is not None and new_balance > cap:
        return jsonify({"error": f"{label} 잔액은 최대 {cap:g}시간까지만 쌓을 수 있습니다."}), 400
    employee[field] = round(new_balance, 2)
    _log_audit(state, audit_action, f"{employee['name']}의 {label} 잔액을 {employee[field]}시간으로 조정",
               {"employee_id": employee_id, "new_balance": employee[field]})
    save_state(company_id, state)
    return jsonify({field: employee[field]})


@payroll_bp.route("/api/employees/<employee_id>/adjust-annual-leave", methods=["POST"])
@require_login
def adjust_annual_leave(company_id, employee_id):
    """애뉴얼 리브 잔액(시간) 수동 조정 — 상한 없음(신법)."""
    return _adjust_balance(company_id, employee_id, "annual_leave_balance_hours", "애뉴얼 리브", "annual_leave_adjusted")


@payroll_bp.route("/api/employees/<employee_id>/adjust-sick-leave", methods=["POST"])
@require_login
def adjust_sick_leave(company_id, employee_id):
    """병가 잔액(시간) 수동 조정 — 160시간 상한(신법)."""
    return _adjust_balance(company_id, employee_id, "sick_leave_balance_hours", "병가", "sick_leave_adjusted",
                           cap=SICK_LEAVE_CAP_HOURS)


@payroll_bp.route("/api/employees/<employee_id>/adjust-alt-leave", methods=["POST"])
@require_login
def adjust_alt_leave(company_id, employee_id):
    """대체휴일(Alternative Holiday) 잔액(시간) 수동 조정."""
    return _adjust_balance(company_id, employee_id, "alternative_leave_balance_hours", "대체휴일", "alt_leave_adjusted")


@payroll_bp.route("/api/employees/<employee_id>/final-payment-estimate", methods=["GET"])
@require_login
def final_payment_estimate(company_id, employee_id):
    """사장/매니저 전용: 퇴사 시 정산액 추정 — 신법에서는 남은 애뉴얼 리브 시간과 대체휴일
    시간을 "가장 낮은 시급"(연봉제는 연봉÷52÷표준시간)으로 곱한 값입니다. 병가는 정산하지
    않습니다. LCP는 근무할 때 이미 지급되었으므로 여기 포함되지 않습니다.
    ⚠️ 참고용 추정치입니다. 실제 지급 전 회계/노무 담당자 확인을 받으세요."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    state = load_state(company_id)
    employee = next((e for e in state["employees"] if e["id"] == employee_id), None)
    if not employee:
        return jsonify({"error": "Employee not found."}), 404
    rate = _lowest_hourly_rate(employee)
    annual_h = employee.get("annual_leave_balance_hours") or 0.0
    alt_h = employee.get("alternative_leave_balance_hours") or 0.0
    annual_payout = round(annual_h * rate, 2) if rate is not None else None
    alt_payout = round(alt_h * rate, 2) if rate is not None else None
    total = round(annual_payout + alt_payout, 2) if annual_payout is not None else None
    return jsonify({
        "employee_id": employee_id, "employee_name": employee["name"],
        "pay_type": employee.get("pay_type", "hourly"),
        "employment_type": employee.get("employment_type", "standard"),
        "lowest_hourly_rate": round(rate, 2) if rate is not None else None,
        "annual_leave_balance_hours": round(annual_h, 2),
        "annual_leave_payout_estimate": annual_payout,
        "alternative_leave_balance_hours": round(alt_h, 2),
        "alternative_leave_payout_estimate": alt_payout,
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
    return jsonify({"enabled": bool(state.get("annual_leave_auto_accrual_enabled", True))})

@payroll_bp.route("/api/annual-leave-accrual-settings", methods=["POST"])
@require_owner
def set_annual_leave_accrual_settings(company_id):
    """사장 전용: 스케줄 퍼블리시 시 리브(애뉴얼+병가) 시간 자동 적립을 켜고 끕니다."""
    payload = request.get_json(silent=True) or {}
    state = load_state(company_id)
    enabled = bool(payload.get("enabled"))
    state["annual_leave_auto_accrual_enabled"] = enabled
    _log_audit(state, "annual_leave_accrual_toggled", f"리브 자동 적립 {'켜짐' if enabled else '꺼짐'}", {"enabled": enabled})
    save_state(company_id, state)
    return jsonify({"enabled": enabled})


@payroll_bp.route("/api/weeks/<week_key>/over-contract-hours", methods=["GET"])
@require_login
def get_week_over_contract_hours(company_id, week_key):
    """사장/매니저 전용: 이 주 스케줄 시간이 계약 표준시간을 넘는 직원 목록(신법상 그 초과분은
    "추가 시간"으로 LCP 12.5%가 붙고 리브가 적립되지 않습니다). 캐주얼은 대상이 아닙니다."""
    if g.role not in ("owner", "manager"):
        return jsonify({"error": "이 페이지는 사장 또는 매니저만 볼 수 있습니다."}), 403
    state = load_state(company_id)
    payroll = _compute_week_payroll(state, week_key)
    return jsonify([
        {"employee_id": r["employee_id"], "employee_name": r["employee_name"],
         "contract_hours": r["contract_hours"], "scheduled_hours": r["weekly_hours"],
         "additional_hours": r["additional_hours"], "lcp_pay": r["lcp_pay"]}
        for r in payroll["employees"] if r["over_contract"]
    ])


def _accrual_base_hours(employee, week_key):
    """이 주에 리브 적립의 기준이 되는 표준 근무시간. 캐주얼은 0(LCP로 대체). 무급(비법정)
    리브로 쉰 날은 그 날의 평균 하루시간만큼 뺍니다(신법: 그 기간엔 적립 안 됨). 입사일이
    이 주 일요일보다 뒤면 0입니다."""
    if employee.get("employment_type") == "casual":
        return 0.0
    base = _contract_hours(employee)
    if base <= 0:
        return 0.0
    hire = employee.get("hire_date")
    if hire:
        try:
            if date.fromisoformat(hire) > _week_dates(week_key)[-1]:
                return 0.0
        except ValueError:
            pass
    unpaid_days = sum(1 for lt in _leave_info_by_day(employee, week_key).values() if lt == "unpaid")
    return max(0.0, base - unpaid_days * _average_day_hours(employee))


def _accrue_leave_for_week(state, week_key):
    """스케줄 퍼블리시 시, 각 표준시간 직원에게 그 주 표준시간 × 0.0769 시간의 애뉴얼 리브와
    × 0.0385 시간의 병가(잔액 160시간 상한, 도달 시 적립 중단)를 적립합니다. 같은 (직원,주)는
    state["leave_accrual_credits"]로 한 번만 적립합니다. 캐주얼은 적립 대신 LCP를 받습니다."""
    if not state.get("annual_leave_auto_accrual_enabled", True):
        return
    credits = state.setdefault("leave_accrual_credits", {})
    for e in state["employees"]:
        key = f"{e['id']}|{week_key}"
        if key in credits:
            continue
        base = _accrual_base_hours(e, week_key)
        if base <= 0:
            continue
        annual = round(base * ANNUAL_ACCRUAL_PER_HOUR, 4)
        sick_room = max(0.0, SICK_LEAVE_CAP_HOURS - (e.get("sick_leave_balance_hours") or 0.0))
        sick = round(min(base * SICK_ACCRUAL_PER_HOUR, sick_room), 4)
        e["annual_leave_balance_hours"] = round((e.get("annual_leave_balance_hours") or 0.0) + annual, 2)
        e["sick_leave_balance_hours"] = round((e.get("sick_leave_balance_hours") or 0.0) + sick, 2)
        credits[key] = {"annual": annual, "sick": sick}


def _credit_alt_holidays_for_week(state, week_key):
    """OWD인 공휴일에 일한 시간(스케줄 기준)만큼 대체휴일(Alternative Holiday)을 1:1로 시간
    적립합니다. (직원,공휴일날짜)별 적립 시간을 state["alt_leave_credits"]에 기록하므로,
    재퍼블리시로 근무 시간이나 OWD 판정이 바뀌면 차액만 반영됩니다."""
    week = state["weeks"].get(week_key)
    if not week:
        return
    week_dates = _week_dates(week_key)
    shift_times = _effective_shift_times(state)
    holiday_by_day = {}
    for h in state["public_holidays"]:
        try:
            hd = date.fromisoformat(h["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if hd in week_dates:
            holiday_by_day[DAYS[week_dates.index(hd)]] = h
    if not holiday_by_day:
        return
    assignments = (week.get("schedule") or {}).get("assignments") or []
    policy = state["public_holiday_policy"]
    credits = state.setdefault("alt_leave_credits", {})
    for e in state["employees"]:
        for day, h in holiday_by_day.items():
            day_as = [a for a in assignments if a["employee_id"] == e["id"] and a["day"] == day]
            hours = sum(_assignment_duration_hours(a, shift_times) for a in day_as)
            category, _c, _o = _public_holiday_category(
                state, policy, e["id"], day, week_key, bool(day_as), pair_date=h.get("pair_date"))
            new_h = round(hours, 2) if category == 1 else 0.0
            key = f"{e['id']}|{h['date']}"
            old_h = credits.get(key, 0.0)
            if abs(new_h - old_h) < 0.005:
                continue
            e["alternative_leave_balance_hours"] = round(
                max(0.0, (e.get("alternative_leave_balance_hours") or 0.0) + new_h - old_h), 2)
            credits[key] = new_h


@payroll_bp.route("/api/public-holiday-policy", methods=["GET"])
@require_login
def get_public_holiday_policy(company_id):
    """신법의 OWD 기준은 법정 고정값이라 회사가 바꿀 수 없습니다 — 표시용으로만 돌려줍니다."""
    return jsonify(_default_policy())

@payroll_bp.route("/api/public-holiday-policy", methods=["POST"])
@require_login
def set_public_holiday_policy(company_id):
    return jsonify({"error": "신법(Employment Leave Act 2026)의 OWD 판정 기준(직전 13주 중 50%)은 법정 고정값이라 변경할 수 없습니다."}), 400


def _default_policy():
    from helpers import _default_public_holiday_policy
    return _default_public_holiday_policy()


def _employee_by_id(state, emp_id):
    return next((x for x in state["employees"] if x["id"] == emp_id), None)


def _owd_ratio(state, employee, day, week_key):
    """직전 13주(이번 주 제외) 중 같은 요일에 일했거나(스케줄) 승인된 리브가 있던 주의 비율.
    입사 전 주는 분모에서 뺍니다(분모 = 입사 후 경과 주 수, 최대 13). 분모가 0이면 (0, 0)."""
    base_monday = _week_dates(week_key)[0]
    hire = None
    if employee.get("hire_date"):
        try:
            hire = date.fromisoformat(employee["hire_date"])
        except ValueError:
            hire = None
    hit = denom = 0
    for i in range(1, FREQUENCY_WINDOW_WEEKS + 1):
        monday = base_monday - timedelta(weeks=i)
        if hire and monday + timedelta(days=6) < hire:
            continue
        denom += 1
        wk = monday.isoformat()
        if _worked_that_weekday(state, employee["id"], day, wk) or day in _leave_info_by_day(employee, wk):
            hit += 1
    return hit, denom


def _is_owd(state, employee, day, week_key):
    """이 직원에게 이 요일이 OWD(Otherwise Working Day)인가. 합의된 근무요일(agreed_days)이
    있으면 그 요일이 곧 OWD, 없으면(캐주얼 포함) 직전 13주 중 50% 이상 근무했을 때 OWD입니다.
    돌려주는 값: (OWD 여부, 근거 횟수, 분모)."""
    agreed = employee.get("agreed_days") or []
    if agreed and employee.get("employment_type") != "casual":
        return day in agreed, None, None
    hit, denom = _owd_ratio(state, employee, day, week_key)
    return (denom > 0 and hit / denom >= HOLIDAY_OWD_RATIO), hit, denom


def _public_holiday_category(state, policy, emp_id, day, week_key, worked, pair_date=None):
    """공휴일 요일(day)의 카테고리: 1=OWD+일함, 2=OWD+안 일함, 3=OWD 아님+일함, 4=해당없음.
    돌려주는 값: (category, 근거 횟수, OWD 여부). policy 인자는 호환용(신법은 고정 기준).

    ⚠️ pair_date — Mondayisation으로 원래/옮겨진 날짜가 짝을 이룰 때, 혜택은 그 직원이 평소
    일하는 쪽에만 적용되고, 둘 다 OWD면 더 이른(원래) 날짜에만 인정됩니다(이중 수령 방지)."""
    employee = _employee_by_id(state, emp_id)
    if employee is None:
        return 4, None, None
    is_owd, count, _denom = _is_owd(state, employee, day, week_key)
    if is_owd and worked:
        category = 1
    elif is_owd:
        category = 2
    elif worked:
        category = 3
    else:
        category = 4

    if pair_date:
        this_date = _week_dates(week_key)[DAYS.index(day)]
        pair_dt = date.fromisoformat(pair_date)
        pair_day = DAYS[pair_dt.weekday()]
        pair_is_owd, _pc, _pd = _is_owd(state, employee, pair_day, _week_key_for_date(pair_dt))
        if not is_owd and pair_is_owd:
            category = 4
        elif is_owd and pair_is_owd and this_date > pair_dt:
            category = 4
    return category, count, is_owd

@payroll_bp.route("/api/weeks/<week_key>/public-holiday-info", methods=["GET"])
@require_login
def get_public_holiday_info(company_id, week_key):
    """이 주(week_key)에 Public Holiday가 포함되어 있으면, 이 회사가 설정한 정책
    (신법 OWD 기준)에 따라 직원별 적용 항목(1.5배+대체휴일 / 하루치 / 1.0배 /
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
            holidays_this_week.append({
                "date": h["date"], "name": h.get("name", ""), "day": DAYS[day_idx],
                "pair_date": h.get("pair_date"), "mondayised": bool(h.get("mondayised")),
            })

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
            category, count, is_usual_day = _public_holiday_category(
                state, policy, emp_id, day, week_key, worked,
                pair_date=holiday.get("pair_date"),
            )

            rows.append({
                "employee_id": emp_id, "employee_name": e["name"],
                "occurrence_count": count, "is_usual_working_day": is_usual_day,
                "worked_on_holiday": worked, "category": category,
            })
        categories[day] = rows

    return jsonify({"holidays": holidays_this_week, "categories": categories, "policy": policy})
