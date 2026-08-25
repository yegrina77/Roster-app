print("1. 시작")
from scheduler import Employee, ShiftRequirement, solve_schedule
print("2. import 성공")

employees = [
    Employee(id="e1", name="테스트직원1", skills=[], max_hours_per_week=40),
    Employee(id="e2", name="테스트직원2", skills=[], max_hours_per_week=40),
]
requirements = [
    ShiftRequirement(day="mon", shift_type="morning", required_count=1),
]
print("3. 데이터 준비 완료, 솔버 실행 시작")

result = solve_schedule(employees, requirements)
print("4. 솔버 실행 완료!")
print(result.status)
print(result.assignments)