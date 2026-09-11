# 담당 D - 후보 선별 (C005, C006, C010)
from datetime import timedelta

from voltgo.core.feasibility import recheck_plan, select_feasible_plans


def test_design_example_a_b_pass_c_excluded(places, routes, budget, now):
    # 설계서 2.2 예시: A 21분(여유 4), B 25분(경계값), C 37분(제외)
    plans = select_feasible_plans(places, routes, budget, now, version=1,
                                  dwell_overrides={"A": 12, "B": 16, "C": 12})
    ids = [p.plan_id for p in plans]
    assert ids == ["A", "B"]            # 여유 큰 순

    a = plans[0]
    assert a.total_sec == 21 * 60
    assert a.return_at == now + timedelta(minutes=21)
    assert a.leave_by == now + timedelta(minutes=20)   # 마감 14:25 - 복귀 5분
    assert a.slack_sec == 4 * 60

    b = plans[1]
    assert b.return_at == budget.return_deadline
    assert b.slack_sec == 0


def test_c005_boundary_1500_vs_1501(places, routes, budget, now):
    # 가용 1,500초. A 왕복 540초 -> 체류 960초면 딱 1,500 (통과), 961초면 초과
    only_a = {"A": places["A"]}
    r = {"A": routes["A"]}
    r["A"] = r["A"].model_copy(update={"outbound_sec": 240, "inbound_sec": 300})

    ok = select_feasible_plans(only_a, r, budget, now, version=1, dwell_overrides={"A": 16})   # 16분 = 960초
    assert [p.plan_id for p in ok] == ["A"]

    over = r["A"].model_copy(update={"inbound_sec": 301})
    none = select_feasible_plans(only_a, {"A": over}, budget, now, version=1, dwell_overrides={"A": 16})
    assert none == []


def test_c006_recheck_after_5_minutes(places, routes, budget, now):
    # 14:05 에 승인하면 A 복귀 14:26 > 마감 14:25 -> 확정하지 않는다
    plan = select_feasible_plans(places, routes, budget, now, version=1, dwell_overrides={"A": 12})[0]
    assert recheck_plan(plan, budget, now) is not None
    approval_time = now + timedelta(minutes=5)
    assert budget.available_sec == 1500                 # 14:00 계산 당시 값은 그대로 남아 있어도
    assert recheck_plan(plan, budget, approval_time) is None  # 14:05 현재 시각으로 다시 판정한다
    assert select_feasible_plans(
        places, routes, budget, approval_time, version=1, dwell_overrides={"A": 12}
    ) == []


def test_c010_missing_inbound_route_excluded(places, routes, budget, now):
    # A 는 경로가 없고(한 방향 실패로 등록 안 됨) B 만 양방향 -> B 만
    r = {"B": routes["B"]}
    plans = select_feasible_plans(places, r, budget, now, version=1, dwell_overrides={"A": 12, "B": 12})
    assert [p.plan_id for p in plans] == ["B"]


def test_sort_prefers_saved_category(places, routes, budget, now):
    places = dict(places)
    places["B"] = places["B"].model_copy(update={"category": "cafe"})
    plans = select_feasible_plans(places, routes, budget, now, version=1,
                                  dwell_overrides={"A": 12, "B": 12}, preferred_category="cafe")
    assert plans[0].plan_id == "B"
