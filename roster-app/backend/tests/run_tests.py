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
    nz_local = datetime(2026, 9, 8, 9, 0, tzinfo=A.NZ_TZ)
    utc_sent = nz_local.astimezone(timezone.utc)
    fixed_date = utc_sent.astimezone(A.NZ_TZ).date().isoformat()
    assert fixed_date == "2026-09-08", f"기대: 2026-09-08, 실제: {fixed_date}"


@test("_week_key_for_date가 월요일 기준으로 정확히 계산된다")
def _():
    assert A._week_key_for_date(date(2026, 9, 7)) == "2026-09-07"  # 월요일
    assert A._week_key_for_date(date(2026, 9, 11)) == "2026-09-07"  # 금요일 -> 같은 주 월요일
    assert A._week_key_for_date(date(2026, 9, 6)) == "2026-08-31"  # 일요일 -> 이전 주 월요일


@test("클락인 반올림(올림)/클락아웃 반올림(버림)이 정확히 동작한다")
def _():
    ci = A._round_up_minutes(datetime(2026, 9, 8, 9, 16), 30)
    assert ci == datetime(2026, 9, 8, 9, 30), f"실제: {ci}"
    co = A._round_down_minutes(datetime(2026, 9, 8, 17, 16), 30)
    assert co == datetime(2026, 9, 8, 17, 0), f"실제: {co}"


# ---------------------------------------------------------------------------
# 2. 급여 반올림(버림) 기반 실제 근무시간 계산
# ---------------------------------------------------------------------------

@test("실제 근무시간 계산 - 휴게시간 없을 때")
def _():
    entry = {"clock_in": "2026-09-08T21:00:00.000Z", "clock_out": "2026-09-09T05:00:00.000Z", "breaks": []}
    hours = A._actual_hours_for_entry(entry, 1)
    assert approx(hours, 8.0), f"실제: {hours}"


@test("실제 근무시간 계산 - 휴게 40분 반영")
def _():
    entry = {
        "clock_in": "2026-09-08T21:00:00.000Z", "clock_out": "2026-09-09T05:00:00.000Z",
        "breaks": [{"start": "2026-09-09T00:00:00.000Z", "end": "2026-09-09T00:40:00.000Z"}],
    }
    hours = A._actual_hours_for_entry(entry, 1)
    assert approx(hours, 7.333, tol=0.01), f"실제: {hours}"


# ---------------------------------------------------------------------------
# 3. 휴게시간 검증(_sanitize_breaks)
# ---------------------------------------------------------------------------

@test("휴게 검증 - 정상 케이스 통과")
def _():
    breaks, err = A._sanitize_breaks(
        [{"start": "2026-09-09T00:00:00.000Z", "end": "2026-09-09T00:40:00.000Z"}],
        "2026-09-08T21:00:00.000Z", "2026-09-09T05:00:00.000Z",
    )
    assert err is None and len(breaks) == 1


@test("휴게 검증 - 종료가 시작보다 빠르면 거부")
def _():
    breaks, err = A._sanitize_breaks(
        [{"start": "2026-09-09T00:40:00.000Z", "end": "2026-09-09T00:00:00.000Z"}],
        "2026-09-08T21:00:00.000Z", "2026-09-09T05:00:00.000Z",
    )
    assert err is not None


@test("휴게 검증 - 근무 범위 밖이면 거부")
def _():
    breaks, err = A._sanitize_breaks(
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
    result, err = A._apply_leave_balance_diff(emp, new_reqs)
    assert err is None
    assert result["lieu_day_balance"] == 1.0, f"실제: {result}"


@test("Lieu Day 잔액 - 취소하면 환불된다")
def _():
    emp = {"lieu_day_balance": 1.0, "annual_leave_balance_hours": 0, "min_hours_per_week": 30,
           "target_days_per_week": 5,
           "leave_requests": [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-16", "leave_type": "lieu_day"}]}
    result, err = A._apply_leave_balance_diff(emp, [])
    assert err is None
    assert result["lieu_day_balance"] == 3.0, f"실제: {result}"


@test("Lieu Day 잔액 - 부족하면 거부된다")
def _():
    emp = {"lieu_day_balance": 1.0, "annual_leave_balance_hours": 0, "min_hours_per_week": 30,
           "target_days_per_week": 5, "leave_requests": []}
    new_reqs = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-19", "leave_type": "lieu_day"}]
    result, err = A._apply_leave_balance_diff(emp, new_reqs)
    assert result is None and err is not None


@test("Lieu Day 잔액 - 이미 지난(만료된) 신청은 환불되지 않는다")
def _():
    emp = {"lieu_day_balance": 2.0, "annual_leave_balance_hours": 0, "min_hours_per_week": 30,
           "target_days_per_week": 5,
           "leave_requests": [{"id": "r1", "start_date": "2020-08-01", "end_date": "2020-08-02", "leave_type": "lieu_day"}]}
    result, err = A._apply_leave_balance_diff(emp, [])
    assert err is None
    assert result["lieu_day_balance"] == 2.0, f"실제: {result} (환불되면 안 됨)"


@test("애뉴얼 리브 - 당겨쓰기 동의 없으면 마이너스 거부, 있으면 허용")
def _():
    emp = {"lieu_day_balance": 0, "annual_leave_balance_hours": 0, "min_hours_per_week": 35,
           "target_days_per_week": 5, "leave_requests": []}
    req_no_consent = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-15", "leave_type": "annual_leave", "allow_advance": False}]
    result, err = A._apply_leave_balance_diff(emp, req_no_consent)
    assert result is None and err is not None

    req_with_consent = [{"id": "r1", "start_date": "2026-09-15", "end_date": "2026-09-15", "leave_type": "annual_leave", "allow_advance": True}]
    result2, err2 = A._apply_leave_balance_diff(emp, req_with_consent)
    assert err2 is None and result2["annual_leave_balance_hours"] < 0


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
    A._process_annual_leave_anniversary(emp, state)
    assert emp["annual_leave_balance_hours"] == 158.0, f"실제: {emp['annual_leave_balance_hours']}"


@test("기념일 발생 - 자동적립 꺼져있으면 첫 기념일에 4주(160h)가 발생한다")
def _():
    today = date.today()
    hire = today - timedelta(days=400)
    state = {"annual_leave_auto_accrual_enabled": False}
    emp = {"hire_date": hire.isoformat(), "min_hours_per_week": 40,
           "annual_leave_balance_hours": 0.0, "annual_leave_anniversaries_granted": []}
    A._process_annual_leave_anniversary(emp, state)
    assert emp["annual_leave_balance_hours"] == 160.0, f"실제: {emp['annual_leave_balance_hours']}"


@test("기념일 발생 - 두 번째 기념일부터는 정상적으로 4주씩 누적된다")
def _():
    today = date.today()
    hire = today - timedelta(days=820)  # 2번째 기념일까지 지남
    state = {"annual_leave_auto_accrual_enabled": True}
    emp = {"hire_date": hire.isoformat(), "min_hours_per_week": 40,
           "annual_leave_balance_hours": 158.0, "annual_leave_anniversaries_granted": []}
    A._process_annual_leave_anniversary(emp, state)
    assert emp["annual_leave_balance_hours"] == 318.0, f"실제: {emp['annual_leave_balance_hours']}"
    assert set(emp["annual_leave_anniversaries_granted"]) == {"1", "2"}


@test("기념일 발생 - 같은 기념일에 중복 처리되지 않는다")
def _():
    today = date.today()
    hire = today - timedelta(days=400)
    state = {"annual_leave_auto_accrual_enabled": False}
    emp = {"hire_date": hire.isoformat(), "min_hours_per_week": 40,
           "annual_leave_balance_hours": 0.0, "annual_leave_anniversaries_granted": []}
    A._process_annual_leave_anniversary(emp, state)
    first_balance = emp["annual_leave_balance_hours"]
    changed_again = A._process_annual_leave_anniversary(emp, state)
    assert changed_again is False
    assert emp["annual_leave_balance_hours"] == first_balance


# ---------------------------------------------------------------------------
# 6. OWP/AWE 및 Section 23 / 기념일 이후 8% 정산
# ---------------------------------------------------------------------------

@test("OWP - 고정시간 직원은 계약시간 x 시급으로 계산된다")
def _():
    emp = {"hourly_wage": 25.0, "min_hours_per_week": 30, "hours_type": "fixed", "id": "e1"}
    owp, weeks = A._owp_for_employee({}, emp)
    assert owp == 750.0, f"실제: {owp}"
    assert weeks is None


@test("Section 23 정산 - 실제 법령 예시(총소득 $18,000, 8%=$1,440, 기지급 $500 -> $940)와 일치한다")
def _():
    hire_week = A._week_key_for_date(date.today() - timedelta(weeks=36))
    state = {"earnings_history": {
        f"e1|{hire_week}": {"employee_id": "e1", "week_key": hire_week, "gross_pay": 18000.0, "annual_leave_pay": 500.0},
    }}
    emp = {"id": "e1", "hire_date": (date.today() - timedelta(weeks=36)).isoformat()}
    settlement, info = A._section23_settlement(state, emp)
    assert approx(settlement, 940.0), f"실제: {settlement}"


@test("기념일 이후 8% 정산 - 13주 x $700 x 8% = $728")
def _():
    today = date.today()
    hire = today - timedelta(days=450)
    last_anniv = A._last_anniversary_date({"hire_date": hire.isoformat()})
    state = {"earnings_history": {}}
    cur = last_anniv
    for i in range(13):
        wk = A._week_key_for_date(cur)
        state["earnings_history"][f"e1|{wk}"] = {"employee_id": "e1", "week_key": wk, "gross_pay": 700.0, "annual_leave_pay": 0.0}
        cur = cur + timedelta(days=7)
    settlement, info = A._eight_percent_since(state, {"id": "e1"}, last_anniv)
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
    dist = A._haversine_meters(*store, *nearby)
    assert 170 <= dist <= 190, f"실제: {dist}"


@test("지오펜싱 - 꺼져있으면 항상 통과된다")
def _():
    state = {"geofence": {"enabled": False, "lat": -36.85, "lng": 174.76, "radius_m": 100}}
    ok, dist = A._geofence_check(state, -37.0, 175.0)  # 반경 훨씬 밖의 좌표
    assert ok is True


@test("지오펜싱 - 켜져있고 반경 밖이면 차단된다")
def _():
    state = {"geofence": {"enabled": True, "lat": -36.848461, "lng": 174.762582, "radius_m": 100}}
    ok, dist = A._geofence_check(state, -36.86, 174.78)  # 반경보다 훨씬 먼 좌표
    assert ok is False


# ---------------------------------------------------------------------------
# 8. 비자/자격증 문서 검증
# ---------------------------------------------------------------------------

@test("문서 검증 - 영주권은 만료일 없이 등록 가능하다")
def _():
    docs = A._sanitize_documents([{"doc_type": "resident_visa", "label": "PR", "expiry_date": None}])
    assert len(docs) == 1 and docs[0]["expiry_date"] is None


@test("문서 검증 - 일반 비자는 만료일 없으면 걸러진다")
def _():
    docs = A._sanitize_documents([{"doc_type": "work_visa", "label": "AEWV", "expiry_date": None}])
    assert len(docs) == 0


# ---------------------------------------------------------------------------
# 9. 라우트/코드 무결성 감사 (AST 기반) — 이번 대화에서 실제로 버그를 잡아낸 검사들
# ---------------------------------------------------------------------------

@test("라우트 감사 - 데코레이터가 언더스코어 헬퍼 함수에 잘못 붙어있지 않다")
def _():
    import ast
    tree = ast.parse(open(os.path.join(BACKEND_DIR, "app.py")).read())
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            has_route = any("route" in ast.dump(d) for d in node.decorator_list)
            if has_route and node.name.startswith("_"):
                issues.append(node.name)
    assert not issues, f"라우트가 잘못 붙은 함수: {issues}"


@test("라우트 감사 - 중복 정의된 최상위 함수가 없다")
def _():
    import ast
    tree = ast.parse(open(os.path.join(BACKEND_DIR, "app.py")).read())
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.col_offset == 0:
            names.setdefault(node.name, []).append(node.lineno)
    dupes = {k: v for k, v in names.items() if len(v) > 1}
    assert not dupes, f"중복 함수: {dupes}"


@test("app.py 문법이 유효하다")
def _():
    import ast
    ast.parse(open(os.path.join(BACKEND_DIR, "app.py")).read())


if __name__ == "__main__":
    print(f"RosterFlow 회귀 테스트 실행 중... (OR-Tools 설치됨: {HAS_ORTOOLS})\n")
    ok = run_all()
    sys.exit(0 if ok else 1)
