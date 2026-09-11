# 담당 A - 도구 흐름 (C001 happy path, 선행 조건, 확정, 멱등)
# 도구는 runtime 만 있으면 에이전트 없이도 .func 로 직접 부를 수 있다.
from datetime import timedelta
from types import SimpleNamespace

from voltgo.agent import tools
from voltgo.agent.schemas import RoundTrip
from voltgo.clients import ClientError


def rt(context):
    return SimpleNamespace(context=context)


def test_precondition_order(context):
    r = tools.search_nearby_places.func(rt(context), category="meal")
    assert r["status"] == "error" and r["error_code"] == "PRECONDITION_FAILED"
    r = tools.calculate_time_budget.func(rt(context))
    assert r["error_code"] == "PRECONDITION_FAILED"


def test_c001_happy_path_and_confirm(context):
    s = context.session
    assert tools.get_charging_status.func(rt(context))["status"] == "ok"
    assert s.charging.source == "mock"

    # 잔여 40분(mock) vs 사용자 30분 -> 마감 14:25, 가용 1,500초
    r = tools.calculate_time_budget.func(rt(context), user_limit_min=30)
    assert r["status"] == "ok"
    assert s.time_budget.available_sec == 1500

    r = tools.search_nearby_places.func(rt(context), category="meal")
    assert [p["poi_id"] for p in r["data"]] == ["A", "B", "C"]

    r = tools.get_walking_routes.func(rt(context), poi_ids=["A", "B", "C"])
    assert r["status"] == "ok" and len(s.routes) == 3

    # 체류 12분: A/B 통과, C(12+12+13=37분) 제외
    r = tools.select_feasible_plans.func(rt(context), dwell_min=12)
    assert [p["plan_id"] for p in r["data"]] == ["A", "B"]

    # 확정 (승인은 미들웨어가 했다고 가정)
    version = s.candidates["A"].version
    r = tools.confirm_plan.func(rt(context), plan_id="A", version=version)
    assert r["status"] == "ok"
    assert s.confirmed.plan_id == "A"
    assert s.confirmed.leave_by == s.time_budget.return_deadline - timedelta(minutes=5)

    # C017 같은 요청 재전송 -> 새 기록 없이 이전 결과
    again = tools.confirm_plan.func(rt(context), plan_id="A", version=version)
    assert again["message"] == "이미 확정된 계획"


def test_c013_dwell_change_bumps_version_and_drops_approval(context):
    tools.get_charging_status.func(rt(context))
    tools.calculate_time_budget.func(rt(context), user_limit_min=30)
    tools.search_nearby_places.func(rt(context), category="meal")
    tools.get_walking_routes.func(rt(context), poi_ids=["A", "B"])
    tools.select_feasible_plans.func(rt(context), dwell_min=12)
    v1 = context.session.condition_version
    tools.confirm_plan.func(rt(context), plan_id="A", version=v1)

    # 체류 12 -> 20분: A(4+20+5=29분) 는 25분 안에 못 온다
    r = tools.select_feasible_plans.func(rt(context), dwell_min=20)
    assert r["data"] == []
    assert context.session.condition_version == v1 + 1
    assert context.session.confirmed is None
    # 옛 버전으로 확정 시도하면 거절
    r = tools.confirm_plan.func(rt(context), plan_id="A", version=v1)
    assert r["status"] == "error"


def test_c003_not_charging_stops(context):
    from voltgo.clients.mock_charging import MockChargingProvider
    context.charging_provider = MockChargingProvider("charging_done", clock=context.clock)
    tools.get_charging_status.func(rt(context))
    r = tools.calculate_time_budget.func(rt(context))
    assert r["error_code"] == "NOT_CHARGING"
    assert context.places_client.calls == 0       # 장소 API 호출 0


def test_unknown_poi_rejected(context):
    tools.get_charging_status.func(rt(context))
    tools.calculate_time_budget.func(rt(context))
    tools.search_nearby_places.func(rt(context), category="meal")
    r = tools.get_walking_routes.func(rt(context), poi_ids=["A", "X"])
    assert r["error_code"] == "PRECONDITION_FAILED"


def _prepare_candidate(context):
    tools.get_charging_status.func(rt(context))
    tools.calculate_time_budget.func(rt(context), user_limit_min=30)
    tools.search_nearby_places.func(rt(context), category="meal")
    tools.get_walking_routes.func(rt(context), poi_ids=["A"])
    tools.select_feasible_plans.func(rt(context), dwell_min=12)
    return context.session.candidates["A"]


def test_route_refresh_failure_invalidates_previous_candidate(context):
    old_plan = _prepare_candidate(context)

    class FailingRoutes:
        def round_trip(self, origin, place):
            raise ClientError("ROUTE_PARSE", "복귀 경로 없음")

    context.routes_client = FailingRoutes()
    result = tools.get_walking_routes.func(rt(context), poi_ids=["A"])

    assert result["status"] == "error"
    assert context.session.routes == {}
    assert context.session.candidates == {}
    assert context.session.selected_ran is False
    assert context.session.condition_version == old_plan.version + 1
    assert tools.confirm_plan.func(rt(context), plan_id="A", version=old_plan.version)["status"] == "error"


def test_longer_refreshed_route_requires_reselection(context):
    old_plan = _prepare_candidate(context)

    class SlowRoutes:
        def round_trip(self, origin, place):
            return RoundTrip(
                poi_id=place.poi_id,
                outbound_sec=1800,
                inbound_sec=1800,
                route_source="mock",
            )

    context.routes_client = SlowRoutes()
    assert tools.get_walking_routes.func(rt(context), poi_ids=["A"])["status"] == "ok"
    assert context.session.candidates == {}
    assert tools.confirm_plan.func(rt(context), plan_id="A", version=old_plan.version)["status"] == "error"

    selected = tools.select_feasible_plans.func(rt(context), dwell_min=12)
    assert selected["data"] == []
    assert context.session.candidates == {}


def test_route_refresh_invalidates_confirmed_plan(context):
    plan = _prepare_candidate(context)
    assert tools.confirm_plan.func(rt(context), plan_id="A", version=plan.version)["status"] == "ok"
    assert context.session.confirmed is not None
    assert context.session.confirmed_by_request

    result = tools.get_walking_routes.func(rt(context), poi_ids=["A"])

    assert result["status"] == "ok"
    assert context.session.confirmed is None
    assert context.session.confirmed_by_request == {}
    assert context.session.candidates == {}
    assert context.session.condition_version == plan.version + 1


def test_preferences_save_and_isolation(context):
    from voltgo.agent import memory
    r = tools.save_preferences.func(rt(context), category="cafe")
    assert r["status"] == "ok"
    assert memory.load_preferences("u1").preferred_category == "cafe"
    assert memory.load_preferences("u2") is None            # C015 다른 사용자 격리
    assert tools.delete_preferences.func(rt(context))["data"]["deleted"] is True
    assert memory.load_preferences("u1") is None
