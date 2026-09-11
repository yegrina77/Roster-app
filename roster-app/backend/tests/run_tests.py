"""
RosterFlow 핵심 로직 회귀 테스트

이 파일은 지금까지 만들어온 모든 핵심 계산(급여, 리브 잔액, 타임존, 지오펜싱,
스케줄러 라우트 무결성 등)을 한 번에 실행해서 검증합니다. 새 기능을 추가하거나
기존 코드를 수정한 뒤, 배포 전에 이 스크립트를 돌려서 "예전에 잘 되던 게 이번
변경으로 깨지지 않았는지"를 빠르게 확인하기 위한 용도입니다.

실행 방법:
    cd roster-app/backend
    python3 tests/run_tests.py

주의: app.py는 scheduler.py의 OR-Tools 관련 이름들을 임포트합니다. OR-Tools가
설치되어 있지 않은 환경(예: 이 스크립트를 만든 개발 샌드박스)에서는, 같은
이름들을 흉내만 내는 가벼운 스텁(stub) scheduler 모듈이 필요합니다. 이 파일은
OR-Tools가 실제로 설치되어 있으면 진짜 scheduler.py를 그대로 쓰고, 없으면
자동으로 스텁을 만들어 씁니다 — 실제 스케줄 계산 자체는 이 테스트의 범위가
아니라서(그건 OR-Tools 자체의 정확성 문제), 스텁으로도 이 테스트들의 목적(급여,
리브, 날짜 등 app.py 자체 로직 검증)에는 충분합니다.
"""

import sys
import os
import types
import importlib
from datetime import date, timedelta, datetime, timezone

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

# ---------------------------------------------------------------------------
# scheduler 모듈 준비 — 실제 것이 있으면(OR-Tools 설치됨) 그대로 쓰고, 없으면 스텁.
# ---------------------------------------------------------------------------
try:
    import ortools  # noqa: F401
    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False

if not HAS_ORTOOLS:
    stub = types.ModuleType("scheduler")

    from dataclasses import dataclass
    from typing import Optional, List, Set

    stub.DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    stub.SHIFT_TYPES = ["opening", "closing"]
    stub.DEPARTMENTS = ["kitchen", "foh", "management"]
    stub.DEPARTMENT_LABEL_KO = {}
    stub.DEPARTMENT_SHIFTS = {}
    stub.SHIFT_LABEL_KO = {}
    stub.SHIFT_TIME_RANGES = {}
    stub.SHIFT_DEFS = []
    stub.MAX_CONSECUTIVE_DAYS = 7

    @dataclass
    class Employee:
        id: str
        name: str
        department: str
        min_hours_per_week: float = 0
        target_days_per_week: Optional[int] = None
        forced_off_days: Optional[Set[str]] = None
        blocked_shift_types: Optional[List[str]] = None
        day_off_pattern: Optional[str] = None
        preferred: Optional[list] = None
        preferred_off_days: Optional[List[str]] = None
        recent_night_count: int = 0
        recent_weekend_count: int = 0
        credited_off_hours: float = 0
        carry_in_streak: int = 0

    @dataclass
    class ShiftRequirement:
        day: str
        shift_type: str
        required_count: int

    @dataclass
    class ScheduleResult:
        status: str
        assignments: list
        unmet_requirements: list
        diagnostics: list

    def solve_schedule(*args, **kwargs):
        return ScheduleResult(status="OPTIMAL", assignments=[], unmet_requirements=[], diagnostics=[])

    stub.Employee = Employee
    stub.ShiftRequirement = ShiftRequirement
    stub.ScheduleResult = ScheduleResult
    stub.solve_schedule = solve_schedule
    sys.modules["scheduler"] = stub

import app as A  # noqa: E402
import helpers as H  # noqa: E402
import payroll as P  # noqa: E402

ALL_MODULE_FILES = ["app.py", "helpers.py", "payroll.py", "employees.py", "schedule.py", "time_tracking.py"]

# ---------------------------------------------------------------------------
# 아주 가벼운 테스트 프레임워크 — pytest 없이 순수 assert 기반으로 돌립니다.
# ---------------------------------------------------------------------------
_results = []


def test(name):
    def decorator(fn):
        _results.append((name, fn))
        return fn
    return decorator


def run_all():
    passed, failed = 0, []
    for name, fn in _results:
        try:
            fn()
            passed += 1
            print(f"  ✅ {name}")
        except AssertionError as e:
            failed.append((name, str(e)))
            print(f"  ❌ {name} — {e}")
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  💥 {name} — {type(e).__name__}: {e}")
    print()
    print(f"결과: {passed}/{len(_results)} 통과" + (f", {len(failed)}개 실패" if failed else ""))
    if failed:
        print("\n실패 목록:")
        for name, msg in failed:
            print(f"  - {name}: {msg}")
    return len(failed) == 0


def approx(a, b, tol=0.01):
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# 1. 타임존 / 날짜 계산
# ---------------------------------------------------------------------------

@test("NZ_TZ 변환이 UTC 자정 부근 날짜를 정확히 뉴질랜드 날짜로 바꾼다")
def _():
    # NZ 시간 9월 8일 오전 9시 -> UTC로는 9월 7일 저녁 (NZDT, UTC+13 가정 시)
    nz_local = datetime(2026, 9, 8, 9, 0, tzinfo=H.NZ_TZ)
    utc_sent = nz_local.astimezone(timezone.utc)
    fixed_date = utc_sent.astimezone(H.NZ_TZ).date().isoformat()
    assert fixed_date == "2026-09-08", f"기대: 2026-09-08, 실제: {fixed_date}"


@test("_week_key_for_date가 월요일 기준으로 정확히 계산된다")
def _():
    assert H._week_key_for_date(date(2026, 9, 7)) == "2026-09-07"  # 월요일
    assert H._week_key_for_date(date(2026, 9, 11)) == "2026-09-07"  # 금요일 -> 같은 주 월요일
    assert H._week_key_for_date(date(2026, 9, 6)) == "2026-08-31"  # 일요일 -> 이전 주 월요일


@test("클락인 반올림(올림)/클락아웃 반올림(버림)이 정확히 동작한다")
def _():
    ci = H._round_up_minutes(datetime(2026, 9, 8, 9, 16), 30)
    assert ci == datetime(2026, 9, 8, 9, 30), f"실제: {ci}"
    co = H._round_down_minutes(datetime(2026, 9, 8, 17, 16), 30)
    assert co == datetime(2026, 9, 8, 17, 0), f"실제: {co}"


# ---------------------------------------------------------------------------
# 2. 급여 반올림(버림) 기반 실제 근무시간 계산
# ---------------------------------------------------------------------------

@test("실제 근무시간 계산 - 휴게시간 없을 때")
def _():
    entry = {"clock_in": "2026-09-08T21:00:00.000Z", "clock_out": "2026-09-09T05:00:00.000Z", "breaks": []}
    hours = H._actual_hours_for_entry(entry, 1)
    assert approx(hours, 8.0), f"실제: {hours}"


@test("실제 근무시간 계산 - 휴게 40분 반영")
def _():
    entry = {
        "clock_in": "2026-09-08T21:00:00.000Z", "clock_out": "2026-09-09T05:00:00.000Z",
        "breaks": [{"start": "2026-09-09T00:00:00.000Z", "end": "2026-09-09T00:40:00.000Z"}],
    }
    hours = H._actual_hours_for_entry(entry, 1)
    assert approx(hours, 7.333, tol=0.01), f"실제: {hours}"


# ---------------------------------------------------------------------------
# 3. 휴게시간 검증(_sanitize_breaks)
# ---------------------------------------------------------------------------

@test("휴게 검증 - 정상 케이스 통과")
def _():
    breaks, err = H._sanitize_breaks(
        [{"start": "2026-09-09T00:00:00.000Z", "end": "2026-09-09T00:40:00.000Z"}],
        "2026-09-08T21:00:00.000Z", "2026-09-09T05:00:00.000Z",
    )
    assert err is None and len(breaks) == 1


@test("휴게 검증 - 종료가 시작보다 빠르면 거부")
def _():
    breaks, err = H._sanitize_breaks(
        [{"start": "2026-09-09T00:40:00.000Z", "end": "2026-09-09T00:00:00.000Z"}],
        "2026-09-08T21:00:00.000Z", "2026-09-09T05:00:00.000Z",
    )
    assert err is not None


@test("휴게 검증 - 근무 범위 밖이면 거부")
def _():
    breaks, err = H._sanitize_breaks(
        [{"start": "2026-09-09T06:00:00.000Z", "end": "2026-09-09T06:30:00.000Z"}],
        "2026-09-08T21:00:00.000Z", "2026-09-09T05:00:00.000Z",
    )
    assert err is not None


# ---------------------------------------------------------------------------
# 4. 리브 신청 잔액 diff (Lieu Day / 애뉴얼 리브)
# ---------------------------------------------------------------------------

@test("Lieu Day 잔액 - 신청 시 정확히 차감된다")
def _():
    emp = {"lieu_day_balance": 3.0, "annual_leave_balance_hours": 0, "min_hours_per_week": 30,
           "target_days_per_week": 5, "leave_requests": []}
    new_reqs = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-16", "leave_type": "lieu_day"}]
    result, err = H._apply_leave_balance_diff(emp, new_reqs)
    assert err is None
    assert result["lieu_day_balance"] == 1.0, f"실제: {result}"


@test("Lieu Day 잔액 - 취소하면 환불된다")
def _():
    emp = {"lieu_day_balance": 1.0, "annual_leave_balance_hours": 0, "min_hours_per_week": 30,
           "target_days_per_week": 5,
           "leave_requests": [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-16", "leave_type": "lieu_day"}]}
    result, err = H._apply_leave_balance_diff(emp, [])
    assert err is None
    assert result["lieu_day_balance"] == 3.0, f"실제: {result}"


@test("Lieu Day 잔액 - 부족하면 거부된다")
def _():
    emp = {"lieu_day_balance": 1.0, "annual_leave_balance_hours": 0, "min_hours_per_week": 30,
           "target_days_per_week": 5, "leave_requests": []}
    new_reqs = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-19", "leave_type": "lieu_day"}]
    result, err = H._apply_leave_balance_diff(emp, new_reqs)
    assert result is None and err is not None


@test("Lieu Day 잔액 - 이미 지난(만료된) 신청은 환불되지 않는다")
def _():
    emp = {"lieu_day_balance": 2.0, "annual_leave_balance_hours": 0, "min_hours_per_week": 30,
           "target_days_per_week": 5,
           "leave_requests": [{"id": "r1", "start_date": "2020-08-01", "end_date": "2020-08-02", "leave_type": "lieu_day"}]}
    result, err = H._apply_leave_balance_diff(emp, [])
    assert err is None
    assert result["lieu_day_balance"] == 2.0, f"실제: {result} (환불되면 안 됨)"


@test("애뉴얼 리브 - 당겨쓰기 동의 없으면 마이너스 거부, 있으면 허용")
def _():
    emp = {"lieu_day_balance": 0, "annual_leave_balance_hours": 0, "min_hours_per_week": 35,
           "target_days_per_week": 5, "leave_requests": []}
    req_no_consent = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-15", "leave_type": "annual_leave", "allow_advance": False}]
    result, err = H._apply_leave_balance_diff(emp, req_no_consent)
    assert result is None and err is not None

    req_with_consent = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-15", "leave_type": "annual_leave", "allow_advance": True}]
    result2, err2 = H._apply_leave_balance_diff(emp, req_with_consent)
    assert err2 is None and result2["annual_leave_balance_hours"] < 0


@test("리브 신청 - 날짜별 시간 직접입력(daily_hours)이 평균 대신 정확히 반영된다")
def _():
    emp = {"lieu_day_balance": 0, "annual_leave_balance_hours": 0, "min_hours_per_week": 30,
           "target_days_per_week": 5, "leave_requests": []}
    new_reqs = [{
        "id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-17", "leave_type": "annual_leave",
        "status": "approved", "allow_advance": True,
        "daily_hours": {"2026-09-15": 8, "2026-09-16": 6, "2026-09-17": 4},
    }]
    result, err = H._apply_leave_balance_diff(emp, new_reqs)
    assert err is None
    assert result["annual_leave_balance_hours"] == -18.0, f"실제: {result}"  # 8+6+4


@test("리브 신청 - '대기중(pending)' 상태는 잔액에 전혀 영향을 주지 않는다")
def _():
    emp = {"lieu_day_balance": 0, "annual_leave_balance_hours": 0, "min_hours_per_week": 30,
           "target_days_per_week": 5, "leave_requests": []}
    new_reqs = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-17",
                 "leave_type": "annual_leave", "status": "pending"}]
    result, err = H._apply_leave_balance_diff(emp, new_reqs)
    assert err is None
    assert result["annual_leave_balance_hours"] == 0.0, f"실제: {result} (대기중인데 차감되면 안 됨)"


@test("리브 신청 - status 필드가 없는 옛날 데이터는 승인된 것으로 취급된다(하위 호환)")
def _():
    emp = {"lieu_day_balance": 0, "annual_leave_balance_hours": 0, "min_hours_per_week": 30,
           "target_days_per_week": 5, "leave_requests": []}
    new_reqs = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-15", "leave_type": "annual_leave", "allow_advance": True}]
    result, err = H._apply_leave_balance_diff(emp, new_reqs)
    assert err is None
    assert result["annual_leave_balance_hours"] == -6.0, f"실제: {result} (30/5=6시간 차감되어야 함)"


@test("리브 신청 - '대기중' 신청은 _leave_info_by_day(스케줄/급여)에 반영되지 않는다")
def _():
    emp = {
        "leave_requests": [
            {"id": "r1", "start_date": "2026-09-07", "end_date": "2026-09-07", "leave_type": "annual_leave", "status": "pending"},
            {"id": "r2", "start_date": "2026-09-08", "end_date": "2026-09-08", "leave_type": "sick", "status": "approved"},
        ],
    }
    info = H._leave_info_by_day(emp, "2026-09-07")  # 이 주의 월요일=9/7, 화요일=9/8
    assert "mon" not in info, f"대기중 신청이 반영됨: {info}"
    assert info.get("tue") == "sick", f"승인된 신청이 안 보임: {info}"


@test("리브 신청 - _stamp_new_leave_requests가 새 항목에만 메타데이터(신청자/시각)를 채운다")
def _():
    old = [{"id": "r1", "start_date": "2026-09-01", "end_date": "2026-09-01", "leave_type": "sick", "status": "approved", "submitted_by_name": "기존"}]
    new = old + [{"id": "r2", "start_date": "2026-09-15", "end_date": "2026-09-15", "leave_type": "annual_leave"}]
    stamped, err = H._stamp_new_leave_requests(old, new, "employee", "Jenny", "pending")
    assert err is None
    r1 = next(r for r in stamped if r["id"] == "r1")
    r2 = next(r for r in stamped if r["id"] == "r2")
    assert r1["submitted_by_name"] == "기존", "기존 항목이 덮어써짐"
    assert r2["status"] == "pending" and r2["submitted_by_name"] == "Jenny", f"새 항목 메타데이터 오류: {r2}"


# ---------------------------------------------------------------------------
# 4-2. 병가(Sick Leave) 잔액 — 일(day) 단위 관리, 부분 조퇴 급여 계산
# ---------------------------------------------------------------------------

@test("병가 기념일 - 입사 7개월차, 첫 부여로 10일 발생한다")
def _():
    hire = date.today() - timedelta(days=210)
    emp = {"hire_date": hire.isoformat(), "sick_leave_balance_days": 0.0, "sick_leave_anniversaries_granted": []}
    P._process_sick_leave_anniversary(emp)
    assert emp["sick_leave_balance_days"] == 10.0, f"실제: {emp['sick_leave_balance_days']}"


@test("병가 기념일 - 20개월차, 두 번 부여되어도 20일(누적 상한)을 넘지 않는다")
def _():
    hire = date.today() - timedelta(days=610)
    emp = {"hire_date": hire.isoformat(), "sick_leave_balance_days": 0.0, "sick_leave_anniversaries_granted": []}
    P._process_sick_leave_anniversary(emp)
    assert emp["sick_leave_balance_days"] == 20.0, f"실제: {emp['sick_leave_balance_days']}"


@test("병가 기념일 - 이미 일부 사용한 상태(5일 남음)에서 2번째 기념일 -> 20일 안 넘으면 그대로 더해진다")
def _():
    hire = date.today() - timedelta(days=610)
    emp = {"hire_date": hire.isoformat(), "sick_leave_balance_days": 5.0, "sick_leave_anniversaries_granted": ["1"]}
    P._process_sick_leave_anniversary(emp)
    assert emp["sick_leave_balance_days"] == 15.0, f"실제: {emp['sick_leave_balance_days']}"


@test("병가 잔액 - 부분 시간(3시간, 평균 8시간) 신청 시 0.375일만 정확히 차감된다")
def _():
    emp = {"sick_leave_balance_days": 10.0, "lieu_day_balance": 0, "annual_leave_balance_hours": 0,
           "min_hours_per_week": 40, "target_days_per_week": 5, "leave_requests": []}
    new_reqs = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-15", "leave_type": "sick",
                 "status": "approved", "daily_hours": {"2026-09-15": 3}}]
    result, err = H._apply_leave_balance_diff(emp, new_reqs)
    assert err is None
    assert result["sick_leave_balance_days"] == 9.62, f"실제: {result} (기대: 10 - 3/8 = 9.625 -> 반올림 9.62)"


@test("병가 잔액 - 부족하면 거부, allow_sick_override 있으면 마이너스 허용")
def _():
    emp = {"sick_leave_balance_days": 0.2, "lieu_day_balance": 0, "annual_leave_balance_hours": 0,
           "min_hours_per_week": 40, "target_days_per_week": 5, "leave_requests": []}
    no_override = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-15", "leave_type": "sick", "status": "approved"}]
    result, err = H._apply_leave_balance_diff(emp, no_override)
    assert result is None and err is not None

    with_override = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-15", "leave_type": "sick", "status": "approved", "allow_sick_override": True}]
    result2, err2 = H._apply_leave_balance_diff(emp, with_override)
    assert err2 is None and result2["sick_leave_balance_days"] < 0


@test("부분 조퇴 급여 - 날짜별 시간(3h) 직접입력 시, 실제 일한 시간에 그대로 더해진다 (5h+3h=$200)")
def _():
    state = {
        "employees": [{
            "id": "e1", "name": "T", "department": "kitchen", "pay_type": "hourly", "hourly_wage": 25.0,
            "hours_type": "fixed", "min_hours_per_week": 40, "target_days_per_week": 5, "hire_date": None,
            "leave_requests": [{
                "id": "lr1", "start_date": "2026-09-07", "end_date": "2026-09-07", "leave_type": "sick",
                "status": "approved", "daily_hours": {"2026-09-07": 3},
            }],
            "annual_leave_balance_hours": 0.0, "lieu_day_balance": 0.0, "sick_leave_balance_days": 10.0,
            "blocked_shift_types": [], "preferred": [], "preferred_off_days": [],
        }],
        "weeks": {"2026-09-07": {"schedule": {"assignments": [
            {"employee_id": "e1", "day": "mon", "shift_type": "opening", "custom_start": "09:00", "custom_end": "17:00"},
        ]}}},
        "public_holidays": [], "shift_time_overrides": {},
        "departments": [{"id": "kitchen", "name": "Kitchen"}],
        "shift_types": [{"id": "opening", "name": "Opening", "department_id": "kitchen", "start": "09:00", "end": "17:00", "is_closing": False, "blocked_after_closing": False}],
        "public_holiday_policy": H._default_public_holiday_policy(),
        # 실제로는 09:00~14:00만 찍음(5시간) — NZ 시간 09:00은 UTC 전날 20:00 근처
        "time_entries": [{"employee_id": "e1", "date": "2026-09-07", "clock_in": "2026-09-06T20:00:00.000Z", "clock_out": "2026-09-07T01:00:00.000Z", "breaks": []}],
        "payroll_rounding_minutes": 1, "earnings_history": {}, "lieu_day_credits": [],
        "annual_leave_auto_accrual_enabled": False, "annual_leave_accrual_credits": [],
    }
    result = P._compute_week_payroll(state, "2026-09-07")
    mon = result["employees"][0]["per_day"]["mon"]
    assert mon["actual_hours"] == 5.0, f"실제: {mon}"
    assert mon["actual_pay"] == 200.0, f"실제: {mon} (기대: 5*25 + 3*25 = 200)"


@test("부분 조퇴 급여 - 날짜별 시간 미입력(평균 폴백)이고 이미 평균만큼 일했으면 추가 지급이 없다")
def _():
    state = {
        "employees": [{
            "id": "e1", "name": "T", "department": "kitchen", "pay_type": "hourly", "hourly_wage": 25.0,
            "hours_type": "fixed", "min_hours_per_week": 40, "target_days_per_week": 5, "hire_date": None,
            "leave_requests": [{"id": "lr1", "start_date": "2026-09-07", "end_date": "2026-09-07", "leave_type": "sick", "status": "approved"}],
            "annual_leave_balance_hours": 0.0, "lieu_day_balance": 0.0, "sick_leave_balance_days": 10.0,
            "blocked_shift_types": [], "preferred": [], "preferred_off_days": [],
        }],
        "weeks": {"2026-09-07": {"schedule": {"assignments": [
            {"employee_id": "e1", "day": "mon", "shift_type": "opening", "custom_start": "09:00", "custom_end": "17:00"},
        ]}}},
        "public_holidays": [], "shift_time_overrides": {},
        "departments": [{"id": "kitchen", "name": "Kitchen"}],
        "shift_types": [{"id": "opening", "name": "Opening", "department_id": "kitchen", "start": "09:00", "end": "17:00", "is_closing": False, "blocked_after_closing": False}],
        "public_holiday_policy": H._default_public_holiday_policy(),
        # 20:00~05:00 = 9시간 (평균 8시간보다 이미 많음)
        "time_entries": [{"employee_id": "e1", "date": "2026-09-07", "clock_in": "2026-09-06T20:00:00.000Z", "clock_out": "2026-09-07T05:00:00.000Z", "breaks": []}],
        "payroll_rounding_minutes": 1, "earnings_history": {}, "lieu_day_credits": [],
        "annual_leave_auto_accrual_enabled": False, "annual_leave_accrual_credits": [],
    }
    result = P._compute_week_payroll(state, "2026-09-07")
    mon = result["employees"][0]["per_day"]["mon"]
    assert mon["actual_hours"] == 9.0, f"실제: {mon}"
    assert mon["actual_pay"] == 225.0, f"실제: {mon} (기대: 9*25 = 225, 추가지급 없어야 함)"


@test("부분 조퇴 급여 - 날짜별 시간 미입력(평균 폴백)이고 5시간만 일했으면 부족분(3h)만 추가된다")
def _():
    state = {
        "employees": [{
            "id": "e1", "name": "T", "department": "kitchen", "pay_type": "hourly", "hourly_wage": 25.0,
            "hours_type": "fixed", "min_hours_per_week": 40, "target_days_per_week": 5, "hire_date": None,
            "leave_requests": [{"id": "lr1", "start_date": "2026-09-07", "end_date": "2026-09-07", "leave_type": "sick", "status": "approved"}],
            "annual_leave_balance_hours": 0.0, "lieu_day_balance": 0.0, "sick_leave_balance_days": 10.0,
            "blocked_shift_types": [], "preferred": [], "preferred_off_days": [],
        }],
        "weeks": {"2026-09-07": {"schedule": {"assignments": [
            {"employee_id": "e1", "day": "mon", "shift_type": "opening", "custom_start": "09:00", "custom_end": "17:00"},
        ]}}},
        "public_holidays": [], "shift_time_overrides": {},
        "departments": [{"id": "kitchen", "name": "Kitchen"}],
        "shift_types": [{"id": "opening", "name": "Opening", "department_id": "kitchen", "start": "09:00", "end": "17:00", "is_closing": False, "blocked_after_closing": False}],
        "public_holiday_policy": H._default_public_holiday_policy(),
        "time_entries": [{"employee_id": "e1", "date": "2026-09-07", "clock_in": "2026-09-06T20:00:00.000Z", "clock_out": "2026-09-07T01:00:00.000Z", "breaks": []}],
        "payroll_rounding_minutes": 1, "earnings_history": {}, "lieu_day_credits": [],
        "annual_leave_auto_accrual_enabled": False, "annual_leave_accrual_credits": [],
    }
    result = P._compute_week_payroll(state, "2026-09-07")
    mon = result["employees"][0]["per_day"]["mon"]
    assert mon["actual_hours"] == 5.0, f"실제: {mon}"
    assert mon["actual_pay"] == 200.0, f"실제: {mon} (기대: 5*25 + (8-5)*25 = 200)"


# ---------------------------------------------------------------------------
# 4-3. 계약 최소시간 부족분 — 사업사정 보전(topup)
# ---------------------------------------------------------------------------

@test("부족분 보전(topup) - 사업사정으로 등록한 시간만큼 실제 급여에 정확히 추가된다")
def _():
    state = {
        "employees": [{
            "id": "e1", "name": "T", "department": "kitchen", "pay_type": "hourly", "hourly_wage": 25.0,
            "hours_type": "fixed", "min_hours_per_week": 30, "target_days_per_week": 5, "hire_date": None,
            "leave_requests": [],
            "annual_leave_balance_hours": 0.0, "lieu_day_balance": 0.0, "sick_leave_balance_days": 10.0,
            "blocked_shift_types": [], "preferred": [], "preferred_off_days": [],
        }],
        "weeks": {"2026-09-07": {
            "schedule": {"assignments": []},
            "shortfall_topups": {"e1": {"mon": {"hours": 6.0, "reason": "한산해서 스케줄 안 짬", "recorded_by": "사장"}}},
        }},
        "public_holidays": [], "shift_time_overrides": {},
        "departments": [{"id": "kitchen", "name": "Kitchen"}],
        "shift_types": [{"id": "opening", "name": "Opening", "department_id": "kitchen", "start": "09:00", "end": "17:00", "is_closing": False, "blocked_after_closing": False}],
        "public_holiday_policy": H._default_public_holiday_policy(),
        "time_entries": [],
        "payroll_rounding_minutes": 1, "earnings_history": {}, "lieu_day_credits": [],
        "annual_leave_auto_accrual_enabled": False, "annual_leave_accrual_credits": [],
    }
    result = P._compute_week_payroll(state, "2026-09-07")
    mon = result["employees"][0]["per_day"]["mon"]
    assert mon["actual_pay"] == 150.0, f"실제: {mon} (기대: 6h x $25 = $150)"
    assert mon["topup_hours"] == 6.0, f"실제: {mon}"


# ---------------------------------------------------------------------------
# 4-4. 공휴일 자동 계산 — 전국 공휴일(Mondayisation 포함) + 지역 기념일
# ---------------------------------------------------------------------------

@test("공휴일 자동계산 - 2026년 전국 공휴일이 실제 공식 발표 날짜와 정확히 일치한다 (ANZAC/Boxing Day Mondayisation 포함)")
def _():
    items = H._national_holidays_for_year(2026)
    by_date = {h["date"]: h["name"] for h in items}
    # 실제 확인된 값: ANZAC Day는 원래 4/25(토)라 월요일(4/27)로, Boxing Day는
    # 원래 12/26(토)라 월요일(12/28)로 밀림 — Employment NZ 2026년 공식 표 기준.
    # Mondayisation이 발생한 공휴일은 원래날짜+옮겨진날짜가 각각 별도 항목으로 나옵니다.
    assert "2026-04-25" in by_date and "2026-04-27" in by_date, f"ANZAC Day 짝 누락: {by_date}"
    assert "2026-12-26" in by_date and "2026-12-28" in by_date, f"Boxing Day 짝 누락: {by_date}"
    assert by_date["2026-07-10"] == "Matariki", f"실제: {by_date}"
    assert len(items) == 13, f"11개 공휴일 중 2개(ANZAC/Boxing Day)가 짝으로 나뉘어 13개여야 함: {len(items)}"
    assert all(not h["needs_confirmation"] for h in items), "전국 공휴일에 확인 필요 표시가 붙어있음"


@test("공휴일 자동계산 - 지역 기념일이 실제 공식 발표 날짜와 정확히 일치한다 (2026/2027년)")
def _():
    cases = [
        ("auckland", 2026, "2026-01-26"), ("auckland", 2027, "2027-02-01"),
        ("wellington", 2026, "2026-01-19"), ("wellington", 2027, "2027-01-25"),
        ("nelson", 2026, "2026-02-02"), ("taranaki", 2026, "2026-03-09"),
        ("otago", 2026, "2026-03-23"), ("otago", 2027, "2027-03-22"),
        ("southland", 2026, "2026-04-07"), ("south_canterbury", 2026, "2026-09-28"),
    ]
    for region, year, expected in cases:
        result = H._regional_anniversary_for_year(region, year)
        assert result["date"] == expected, f"{region} {year}: 실제 {result['date']}, 기대 {expected}"
        assert result["needs_confirmation"] is True, f"{region}: 지역 기념일엔 확인 필요 표시가 있어야 함"


@test("공휴일 자동계산 - region이 'none'이거나 모르는 값이면 None을 돌려준다")
def _():
    assert H._regional_anniversary_for_year("none", 2026) is None
    assert H._regional_anniversary_for_year("mars", 2026) is None


@test("공휴일 자동계산 - Mondayisation 발생 시 원래날짜+옮겨진날짜가 서로 pair_date로 짝을 이룬다")
def _():
    items = H._national_holidays_for_year(2026)
    boxing = [h for h in items if "Boxing Day" in h["name"]]
    assert len(boxing) == 2, f"박싱데이가 2개(원래+옮겨진)여야 함: {boxing}"
    by_date = {h["date"]: h for h in boxing}
    assert by_date["2026-12-26"]["pair_date"] == "2026-12-28"
    assert by_date["2026-12-28"]["pair_date"] == "2026-12-26"
    assert all(h["mondayised"] for h in boxing)


@test("Mondayisation 짝 처리 - 토요일만 평소 근무면 원래날짜(토)만 혜택, 옮겨진날짜(월)는 해당없음")
def _():
    policy = H._default_public_holiday_policy()

    def make_state(sat_weeks, mon_weeks):
        state = {"employees": [{"id": "e1", "name": "T"}], "weeks": {}}
        base_monday = date(2026, 12, 21)
        for i in range(8):
            wk_monday = base_monday - timedelta(days=7 * i)
            assignments = []
            if i < sat_weeks:
                assignments.append({"employee_id": "e1", "day": "sat"})
            if i < mon_weeks:
                assignments.append({"employee_id": "e1", "day": "mon"})
            state["weeks"][wk_monday.isoformat()] = {"schedule": {"assignments": assignments}}
        return state

    # 토요일만 평소 근무
    state = make_state(sat_weeks=8, mon_weeks=0)
    cat_sat, _, _ = P._public_holiday_category(state, policy, "e1", "sat", "2026-12-21", worked=True, pair_date="2026-12-28")
    cat_mon, _, _ = P._public_holiday_category(state, policy, "e1", "mon", "2026-12-28", worked=True, pair_date="2026-12-26")
    assert cat_sat == 1, f"토요일(원래날짜)은 혜택 받아야 함: {cat_sat}"
    assert cat_mon == 4, f"월요일(옮겨진날짜)은 해당없음이어야 함: {cat_mon}"

    # 월요일만 평소 근무 -> 반대로 적용
    state2 = make_state(sat_weeks=0, mon_weeks=8)
    cat_sat2, _, _ = P._public_holiday_category(state2, policy, "e1", "sat", "2026-12-21", worked=True, pair_date="2026-12-28")
    cat_mon2, _, _ = P._public_holiday_category(state2, policy, "e1", "mon", "2026-12-28", worked=True, pair_date="2026-12-26")
    assert cat_sat2 == 4, f"토요일 평소 근무 아니면 해당없음: {cat_sat2}"
    assert cat_mon2 == 1, f"월요일(평소 근무)이 혜택 받아야 함: {cat_mon2}"

    # 둘 다 평소 근무 -> 더 이른(원래) 날짜만 인정, 이중수령 방지
    state3 = make_state(sat_weeks=8, mon_weeks=8)
    cat_sat3, _, _ = P._public_holiday_category(state3, policy, "e1", "sat", "2026-12-21", worked=True, pair_date="2026-12-28")
    cat_mon3, _, _ = P._public_holiday_category(state3, policy, "e1", "mon", "2026-12-28", worked=True, pair_date="2026-12-26")
    assert cat_sat3 == 1, f"둘 다 근무해도 원래날짜만 인정: {cat_sat3}"
    assert cat_mon3 == 4, f"옮겨진날짜는 이중수령 방지로 해당없음이어야 함: {cat_mon3}"


# ---------------------------------------------------------------------------
# 4-5. 급여 CSV 내보내기
# ---------------------------------------------------------------------------

@test("급여 CSV 내보내기 - 여러 주에 걸친 요일별 데이터가 정확히 뽑힌다")
def _():
    state = {
        "employees": [{
            "id": "e1", "name": "Test", "department": "kitchen", "pay_type": "hourly", "hourly_wage": 25.0,
            "hours_type": "fixed", "min_hours_per_week": 30, "target_days_per_week": 5, "hire_date": None,
            "leave_requests": [], "annual_leave_balance_hours": 0.0, "lieu_day_balance": 0.0, "sick_leave_balance_days": 10.0,
            "blocked_shift_types": [], "preferred": [], "preferred_off_days": [],
        }],
        "weeks": {
            "2026-09-07": {"schedule": {"assignments": [
                {"employee_id": "e1", "day": "mon", "shift_type": "opening", "custom_start": "09:00", "custom_end": "17:00"},
            ]}},
            "2026-09-14": {"schedule": {"assignments": [
                {"employee_id": "e1", "day": "tue", "shift_type": "opening", "custom_start": "09:00", "custom_end": "17:00"},
            ]}},
        },
        "public_holidays": [], "shift_time_overrides": {},
        "departments": [{"id": "kitchen", "name": "Kitchen"}],
        "shift_types": [{"id": "opening", "name": "Opening", "department_id": "kitchen", "start": "09:00", "end": "17:00", "is_closing": False, "blocked_after_closing": False}],
        "public_holiday_policy": H._default_public_holiday_policy(),
        "time_entries": [], "payroll_rounding_minutes": 1, "earnings_history": {}, "lieu_day_credits": [],
        "annual_leave_auto_accrual_enabled": False, "annual_leave_accrual_credits": [],
    }
    rows = P._build_payroll_export_rows(state, "e1", date(2026, 9, 7), date(2026, 9, 20))
    assert len(rows) == 14, f"2주치 14일이어야 함: {len(rows)}"
    mon_row = next(r for r in rows if r[0] == "2026-09-07")
    assert mon_row[2] == 7.0, f"9/7 스케줄 시간이 7이어야 함: {mon_row}"
    tue_row = next(r for r in rows if r[0] == "2026-09-15")
    assert tue_row[2] == 7.0, f"9/15 스케줄 시간이 7이어야 함: {tue_row}"


# ---------------------------------------------------------------------------
# 5. 애뉴얼 리브 기념일 자동 발생 + 8% 자동 적립 중복 방지
# ---------------------------------------------------------------------------

@test("기념일 발생 - 자동적립 켜져있으면 첫 기념일엔 추가로 더하지 않는다")
def _():
    today = date.today()
    hire = today - timedelta(days=400)
    state = {"annual_leave_auto_accrual_enabled": True}
    emp = {"hire_date": hire.isoformat(), "min_hours_per_week": 40,
           "annual_leave_balance_hours": 158.0, "annual_leave_anniversaries_granted": []}
    P._process_annual_leave_anniversary(emp, state)
    assert emp["annual_leave_balance_hours"] == 158.0, f"실제: {emp['annual_leave_balance_hours']}"


@test("기념일 발생 - 자동적립 꺼져있으면 첫 기념일에 4주(160h)가 발생한다")
def _():
    today = date.today()
    hire = today - timedelta(days=400)
    state = {"annual_leave_auto_accrual_enabled": False}
    emp = {"hire_date": hire.isoformat(), "min_hours_per_week": 40,
           "annual_leave_balance_hours": 0.0, "annual_leave_anniversaries_granted": []}
    P._process_annual_leave_anniversary(emp, state)
    assert emp["annual_leave_balance_hours"] == 160.0, f"실제: {emp['annual_leave_balance_hours']}"


@test("기념일 발생 - 두 번째 기념일부터는 정상적으로 4주씩 누적된다")
def _():
    today = date.today()
    hire = today - timedelta(days=820)  # 2번째 기념일까지 지남
    state = {"annual_leave_auto_accrual_enabled": True}
    emp = {"hire_date": hire.isoformat(), "min_hours_per_week": 40,
           "annual_leave_balance_hours": 158.0, "annual_leave_anniversaries_granted": []}
    P._process_annual_leave_anniversary(emp, state)
    assert emp["annual_leave_balance_hours"] == 318.0, f"실제: {emp['annual_leave_balance_hours']}"
    assert set(emp["annual_leave_anniversaries_granted"]) == {"1", "2"}


@test("기념일 발생 - 같은 기념일에 중복 처리되지 않는다")
def _():
    today = date.today()
    hire = today - timedelta(days=400)
    state = {"annual_leave_auto_accrual_enabled": False}
    emp = {"hire_date": hire.isoformat(), "min_hours_per_week": 40,
           "annual_leave_balance_hours": 0.0, "annual_leave_anniversaries_granted": []}
    P._process_annual_leave_anniversary(emp, state)
    first_balance = emp["annual_leave_balance_hours"]
    changed_again = P._process_annual_leave_anniversary(emp, state)
    assert changed_again is False
    assert emp["annual_leave_balance_hours"] == first_balance


# ---------------------------------------------------------------------------
# 6. OWP/AWE 및 Section 23 / 기념일 이후 8% 정산
# ---------------------------------------------------------------------------

@test("OWP - 고정시간 직원은 계약시간 x 시급으로 계산된다")
def _():
    emp = {"hourly_wage": 25.0, "min_hours_per_week": 30, "hours_type": "fixed", "id": "e1"}
    owp, weeks = P._owp_for_employee({}, emp)
    assert owp == 750.0, f"실제: {owp}"
    assert weeks is None


@test("Section 23 정산 - 실제 법령 예시(총소득 $18,000, 8%=$1,440, 기지급 $500 -> $940)와 일치한다")
def _():
    hire_week = H._week_key_for_date(date.today() - timedelta(weeks=36))
    state = {"earnings_history": {
        f"e1|{hire_week}": {"employee_id": "e1", "week_key": hire_week, "gross_pay": 18000.0, "annual_leave_pay": 500.0},
    }}
    emp = {"id": "e1", "hire_date": (date.today() - timedelta(weeks=36)).isoformat()}
    settlement, info = P._section23_settlement(state, emp)
    assert approx(settlement, 940.0), f"실제: {settlement}"


@test("기념일 이후 8% 정산 - 13주 x $700 x 8% = $728")
def _():
    today = date.today()
    hire = today - timedelta(days=450)
    last_anniv = P._last_anniversary_date({"hire_date": hire.isoformat()})
    state = {"earnings_history": {}}
    cur = last_anniv
    for i in range(13):
        wk = H._week_key_for_date(cur)
        state["earnings_history"][f"e1|{wk}"] = {"employee_id": "e1", "week_key": wk, "gross_pay": 700.0, "annual_leave_pay": 0.0}
        cur = cur + timedelta(days=7)
    settlement, info = P._eight_percent_since(state, {"id": "e1"}, last_anniv)
    assert approx(settlement, 728.0), f"실제: {settlement}"


@test("공휴일 근무 시급 x 1.5배 계산이 정확하다")
def _():
    # _assignment_duration_hours와 동일한 방식으로 8시간 근무(휴게 1시간 차감) 가정
    hours = 7.0
    wage = 25.0
    assert approx(hours * wage * 1.5, 262.5)


# ---------------------------------------------------------------------------
# 7. 지오펜싱 거리 계산
# ---------------------------------------------------------------------------

@test("Haversine 거리 계산 - 가까운 두 지점(약 180m)")
def _():
    store = (-36.848461, 174.762582)
    nearby = (-36.848461, 174.764600)
    dist = H._haversine_meters(*store, *nearby)
    assert 170 <= dist <= 190, f"실제: {dist}"


@test("지오펜싱 - 꺼져있으면 항상 통과된다")
def _():
    state = {"geofence": {"enabled": False, "lat": -36.85, "lng": 174.76, "radius_m": 100}}
    ok, dist = H._geofence_check(state, -37.0, 175.0)  # 반경 훨씬 밖의 좌표
    assert ok is True


@test("지오펜싱 - 켜져있고 반경 밖이면 차단된다")
def _():
    state = {"geofence": {"enabled": True, "lat": -36.848461, "lng": 174.762582, "radius_m": 100}}
    ok, dist = H._geofence_check(state, -36.86, 174.78)  # 반경보다 훨씬 먼 좌표
    assert ok is False


# ---------------------------------------------------------------------------
# 8. 비자/자격증 문서 검증
# ---------------------------------------------------------------------------

@test("문서 검증 - 영주권은 만료일 없이 등록 가능하다")
def _():
    docs = H._sanitize_documents([{"doc_type": "resident_visa", "label": "PR", "expiry_date": None}])
    assert len(docs) == 1 and docs[0]["expiry_date"] is None


@test("문서 검증 - 일반 비자는 만료일 없으면 걸러진다")
def _():
    docs = H._sanitize_documents([{"doc_type": "work_visa", "label": "AEWV", "expiry_date": None}])
    assert len(docs) == 0


# ---------------------------------------------------------------------------
# 9. 라우트/코드 무결성 감사 (AST 기반) — 이번 대화에서 실제로 버그를 잡아낸 검사들
#    (app.py를 helpers/payroll/employees/schedule/time_tracking으로 나눈 뒤로는,
#    이 6개 파일 전부를 대상으로 검사합니다.)
# ---------------------------------------------------------------------------

@test("라우트 감사 - 데코레이터가 언더스코어 헬퍼 함수에 잘못 붙어있지 않다 (전체 6개 파일)")
def _():
    import ast
    issues = []
    for fname in ALL_MODULE_FILES:
        tree = ast.parse(open(os.path.join(BACKEND_DIR, fname), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                has_route = any("route" in ast.dump(d) for d in node.decorator_list)
                if has_route and node.name.startswith("_"):
                    issues.append(f"{fname}:{node.name}")
    assert not issues, f"라우트가 잘못 붙은 함수: {issues}"


@test("라우트 감사 - 같은 파일 안에 중복 정의된 최상위 함수가 없다 (전체 6개 파일)")
def _():
    import ast
    all_dupes = {}
    for fname in ALL_MODULE_FILES:
        tree = ast.parse(open(os.path.join(BACKEND_DIR, fname), encoding="utf-8").read())
        names = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.col_offset == 0:
                names.setdefault(node.name, []).append(node.lineno)
        dupes = {k: v for k, v in names.items() if len(v) > 1}
        if dupes:
            all_dupes[fname] = dupes
    assert not all_dupes, f"중복 함수: {all_dupes}"


@test("6개 파일 전부 문법이 유효하다")
def _():
    import ast
    for fname in ALL_MODULE_FILES:
        ast.parse(open(os.path.join(BACKEND_DIR, fname), encoding="utf-8").read())


@test("전체 앱 임포트 성공 + 등록된 라우트가 103개(=101개 + 급여/리브 CSV 내보내기 2개)")
def _():
    rules = list(A.app.url_map.iter_rules())
    assert len(rules) == 103, f"실제 라우트 개수: {len(rules)}"


@test("모듈을 6개로 나눈 뒤에도, 각 파일 안에서 정의되지 않고 임포트도 안 된 이름을 쓰는 곳이 없다 (import 누락 검사)")
def _():
    # 파일을 여러 개로 나누면서 가장 위험한 실수는 "함수는 옮겼는데 그 함수가 쓰는
    # 이름을 새 파일에 import하는 걸 깜빡하는 것"입니다 — 이 버그는 앱을 그냥
    # import만 해서는 절대 안 걸리고(파이썬은 함수 "본문" 안의 이름을 실제로 그
    # 함수가 "호출"되는 시점에만 확인하기 때문), 실제로 그 API를 요청했을 때만
    # NameError로 터집니다. symtable로 각 함수가 쓰는 이름 중 "이 함수 자기 자신의
    # 지역변수가 아닌 것"을 찾아서, 두 경우로 나눠 확인합니다:
    #   - is_free (클로저): 자신을 감싸는 함수들의 지역변수 중에 있는지 확인
    #   - is_global (그 외 대부분의 전역 참조): 모듈 최상위에 실제로 정의(할당/임포트)
    #     되어 있는지 확인 — "is_free만" 확인하면 이 케이스를 놓칩니다(실제로 이
    #     테스트를 처음 만들 때 이 부분을 놓쳐서, 배포 후에야 몇 개를 더 찾았습니다).
    import symtable
    import builtins
    BUILTIN_NAMES = set(dir(builtins))
    issues = []
    for fname in ALL_MODULE_FILES:
        source = open(os.path.join(BACKEND_DIR, fname), encoding="utf-8").read()
        table = symtable.symtable(source, fname, "exec")
        module_defined = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported()}

        def walk(tbl, ancestor_locals_stack):
            this_scope_names = {s.get_name() for s in tbl.get_symbols() if s.is_assigned() or s.is_parameter() or s.is_imported()}
            new_stack = ancestor_locals_stack + [this_scope_names]
            for child in tbl.get_children():
                if child.get_type() in ("function", "class"):
                    for sym in child.get_symbols():
                        name = sym.get_name()
                        if name in BUILTIN_NAMES:
                            continue
                        if sym.is_free():
                            if not any(name in s for s in new_stack):
                                issues.append(f"{fname}: {child.get_name()}() -> '{name}' (free)")
                        elif sym.is_global() and not (sym.is_assigned() or sym.is_parameter() or sym.is_imported()):
                            if name not in module_defined:
                                issues.append(f"{fname}: {child.get_name()}() -> '{name}' (global)")
                walk(child, new_stack)
        walk(table, [])
    assert not issues, "빠뜨린 import로 의심됨:\n    " + "\n    ".join(issues)


@test("모듈 간 의존관계에 순환참조가 없다 (helpers는 다른 커스텀 모듈에 의존하면 안 됨)")
def _():
    import ast
    file_of_module = {
        "app.py": "app", "helpers.py": "helpers", "payroll.py": "payroll",
        "employees.py": "employees", "schedule.py": "schedule", "time_tracking.py": "time_tracking",
    }
    deps = {}
    custom_modules = set(file_of_module.values())
    for fname, modname in file_of_module.items():
        tree = ast.parse(open(os.path.join(BACKEND_DIR, fname), encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in custom_modules:
                imported.add(node.module)
        deps[modname] = imported

    # helpers는 아무것도 의존하면 안 됨
    assert not deps.get("helpers"), f"helpers.py가 다른 모듈에 의존함: {deps['helpers']}"

    # A->B, B->A 동시에 있으면 순환
    cycles = []
    for a, targets in deps.items():
        for b in targets:
            if a in deps.get(b, set()):
                cycles.append((a, b))
    assert not cycles, f"순환참조 발견: {cycles}"


if __name__ == "__main__":
    print(f"RosterFlow 회귀 테스트 실행 중... (OR-Tools 설치됨: {HAS_ORTOOLS})\n")
    ok = run_all()
    sys.exit(0 if ok else 1)
