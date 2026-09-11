"""실제 build_agent graph와 InMemorySaver를 통과하는 대체 활동 multi-turn 테스트."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from voltgo.agent.agent import ask, build_agent
from voltgo.agent.alternative_agent import assess_time_shortage_alternatives
from voltgo.agent.schemas import Place, RoundTrip
from voltgo.clients import ClientError
from tests.test_approval import ScriptedModel, _ai, _tc

pytestmark = pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")


def _decision(action, text, ids=None, tag="decision"):
    return _ai([_tc("ModelDecision", {
        "candidate_ids": ids or [], "next_action": action, "explanation": text,
    }, tag)])


def _shortage_turn(prefix="one", limit=20, dwell=20):
    return [
        _ai([_tc("get_charging_status", {}, f"{prefix}-charging")]),
        _ai([_tc("calculate_time_budget", {"user_limit_min": limit}, f"{prefix}-budget")]),
        _ai([_tc("search_nearby_places", {"category": "meal"}, f"{prefix}-meal")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["A", "B", "C"]}, f"{prefix}-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": dwell}, f"{prefix}-select")]),
        _ai([_tc("assess_time_shortage_alternatives", {}, f"{prefix}-assess")]),
        _decision("clarify", "식사는 어렵습니다. 편의점이나 카페를 찾아볼까요?", tag=f"{prefix}-decision"),
    ]


def _config(thread_id):
    return {"configurable": {"thread_id": thread_id}}


def _messages(agent, thread_id):
    return agent.get_state(_config(thread_id)).values["messages"]


def _results(agent, thread_id, name):
    return [json.loads(message.content) for message in _messages(agent, thread_id)
            if isinstance(message, ToolMessage) and message.name == name]


def _search_categories(agent, thread_id):
    return [(call.get("args") or {}).get("category") for message in _messages(agent, thread_id)
            if isinstance(message, AIMessage) for call in message.tool_calls
            if call.get("name") == "search_nearby_places"]


def _call_args(agent, thread_id, name):
    return [call.get("args") or {} for message in _messages(agent, thread_id)
            if isinstance(message, AIMessage) for call in message.tool_calls if call.get("name") == name]


def test_demo1_same_thread_waits_then_replans_convenience(context):
    script = _shortage_turn() + [
        _ai([_tc("search_nearby_places", {"category": "convenience"}, "two-search")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["E"]}, "two-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 10}, "two-select")]),
        _decision("choose", "편의점 계획입니다.", ["E"], "two-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))

    turn1 = ask(agent, "20분 안에 밥 먹을 수 있을까?", context, "alternative-demo-1")
    assert turn1.status == "need_input"
    assert _search_categories(agent, "alternative-demo-1") == ["meal"]
    assert _results(agent, "alternative-demo-1", "assess_time_shortage_alternatives")[0]["data"] == {
        "reason": "time_insufficient", "original_category": "meal",
        "alternatives": ["convenience", "cafe"],
        "dwell_min_by_category": {"convenience": 10, "cafe": 15},
    }
    snapshot = deepcopy(context.session)
    assess_time_shortage_alternatives.func(
        SimpleNamespace(context=context, state={"messages": _messages(agent, "alternative-demo-1")}),
    )
    assert context.session == snapshot

    turn2 = ask(agent, "응, 편의점 좋아.", context, "alternative-demo-1")
    assert turn2.status == "ok"
    assert [candidate.category for candidate in turn2.candidates] == ["convenience"]
    assert _search_categories(agent, "alternative-demo-1") == ["meal", "convenience"]
    assert _call_args(agent, "alternative-demo-1", "select_feasible_plans") == [
        {"dwell_min": 20}, {"dwell_min": 10},
    ]


def test_same_turn_automatic_alternative_search_is_blocked_in_real_graph(context):
    script = _shortage_turn(prefix="early")[:-1] + [
        _ai([_tc("search_nearby_places", {"category": "convenience"}, "early-search")]),
        _decision("clarify", "먼저 대체 활동을 선택해 주세요.", tag="early-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))

    response = ask(agent, "20분 안에 밥 먹을 수 있을까?", context, "same-turn")

    assert response.status == "need_input"
    assert context.places_client.calls == 1
    blocked = _results(agent, "same-turn", "search_nearby_places")[-1]
    assert blocked["error_code"] == "ALTERNATIVE_CONSENT_REQUIRED"


def test_exact_ten_minute_demo_replans_but_does_not_invent_feasibility(context):
    script = _shortage_turn(limit=10) + [
        _ai([_tc("search_nearby_places", {"category": "convenience"}, "ten-search")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["E"]}, "ten-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 10}, "ten-select")]),
        _ai([_tc("assess_time_shortage_alternatives", {}, "ten-assess")]),
        _decision("stop", "대체 활동도 현재 시간에는 어렵습니다.", tag="ten-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))

    first = ask(agent, "10분밖에 없어. 밥 먹을 수 있을까?", context, "ten-minute")
    assert first.status == "need_input"
    assert _search_categories(agent, "ten-minute") == ["meal"]
    second = ask(agent, "응, 편의점 좋아.", context, "ten-minute")

    assert second.status == "no_feasible"
    assert _search_categories(agent, "ten-minute") == ["meal", "convenience"]
    assert _results(agent, "ten-minute", "assess_time_shortage_alternatives")[-1]["data"]["reason"] \
        == "alternative_exhausted"


def test_demo2_rejection_and_ambiguous_reply_call_no_search(context):
    for reply, action, thread in [
        ("아니, 그냥 차에 있을게.", "stop", "reject"),
        ("음...", "clarify", "ambiguous"),
    ]:
        script = _shortage_turn(prefix=thread) + [
            _decision(action, "알겠습니다." if action == "stop" else "어떤 활동을 원하시나요?",
                      tag=f"{thread}-reply"),
        ]
        agent = build_agent(model=ScriptedModel(script=script))
        ask(agent, "20분 안에 밥 먹을 수 있을까?", context, thread)
        ask(agent, reply, context, thread)
        assert _search_categories(agent, thread) == ["meal"]


def test_demo3_explicit_cafe_selection_is_not_changed(context):
    script = _shortage_turn(prefix="cafe") + [
        _ai([_tc("search_nearby_places", {"category": "cafe", "max_dist_m": 1000}, "cafe-search")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["D"]}, "cafe-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 15}, "cafe-select")]),
        _ai([_tc("assess_time_shortage_alternatives", {}, "cafe-assess")]),
        _decision("stop", "가까운 카페를 찾지 못했습니다.", tag="cafe-end"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))
    ask(agent, "20분 안에 밥 먹을 수 있을까?", context, "cafe-demo")
    ask(agent, "카페로 찾아줘.", context, "cafe-demo")

    assert _search_categories(agent, "cafe-demo") == ["meal", "cafe"]
    assert _call_args(agent, "cafe-demo", "select_feasible_plans") == [
        {"dwell_min": 20}, {"dwell_min": 15},
    ]
    assert context.places_client.calls == 2


def test_explicit_dwell_is_kept_when_category_changes(context):
    script = _shortage_turn(prefix="explicit", limit=25, dwell=12) + [
        _ai([_tc("search_nearby_places", {"category": "convenience"}, "explicit-search")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["E"]}, "explicit-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 12}, "explicit-select")]),
        _decision("choose", "지정한 체류시간을 반영했습니다.", ["E"], "explicit-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))
    ask(agent, "25분 안에 밥은 12분 동안 먹을래.", context, "explicit-dwell")
    response = ask(agent, "응, 편의점 좋아.", context, "explicit-dwell")

    assert response.status == "ok"
    assert _call_args(agent, "explicit-dwell", "select_feasible_plans") == [
        {"dwell_min": 12}, {"dwell_min": 12},
    ]


def test_user_limit_is_not_reused_as_cafe_dwell(context):
    script = [
        _ai([_tc("get_charging_status", {}, "limit-charging")]),
        _ai([_tc("calculate_time_budget", {"user_limit_min": 20}, "limit-budget")]),
        _ai([_tc("search_nearby_places", {"category": "cafe", "max_dist_m": 1000}, "limit-cafe")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["D"]}, "limit-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 15}, "limit-select")]),
        _ai([_tc("assess_time_shortage_alternatives", {}, "limit-assess")]),
        _decision("clarify", "카페보다 짧은 활동을 찾아볼까요?", tag="limit-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))
    ask(agent, "20분 안에 카페 다녀올 수 있을까?", context, "limit-vs-dwell")

    assert _call_args(agent, "limit-vs-dwell", "calculate_time_budget") == [{"user_limit_min": 20}]
    assert _call_args(agent, "limit-vs-dwell", "select_feasible_plans") == [{"dwell_min": 15}]


def test_explicit_convenience_dwell_overrides_default(context):
    script = [
        _ai([_tc("get_charging_status", {}, "store-charging")]),
        _ai([_tc("calculate_time_budget", {"user_limit_min": 20}, "store-budget")]),
        _ai([_tc("search_nearby_places", {"category": "convenience"}, "store-search")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["E"]}, "store-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 12}, "store-select")]),
        _ai([_tc("assess_time_shortage_alternatives", {}, "store-assess")]),
        _decision("stop", "현재 시간에는 어렵습니다.", tag="store-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))
    ask(agent, "20분 안에 편의점 가서 12분 정도 있을래.", context, "explicit-store-dwell")

    assert _call_args(agent, "explicit-store-dwell", "calculate_time_budget") == [{"user_limit_min": 20}]
    assert _search_categories(agent, "explicit-store-dwell") == ["convenience"]
    assert _call_args(agent, "explicit-store-dwell", "select_feasible_plans") == [{"dwell_min": 12}]


def test_demo4_original_meal_is_feasible_and_assessment_is_not_called(context):
    script = [
        _ai([_tc("get_charging_status", {}, "ok-charging")]),
        _ai([_tc("calculate_time_budget", {"user_limit_min": 40}, "ok-budget")]),
        _ai([_tc("search_nearby_places", {"category": "meal"}, "ok-meal")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["A", "B", "C"]}, "ok-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 20}, "ok-select")]),
        _decision("choose", "식사 계획입니다.", ["A"], "ok-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))
    response = ask(agent, "40분 동안 밥 먹고 싶어.", context, "meal-ok")

    assert response.status == "ok"
    assert response.candidates[0].category == "meal"
    assert _results(agent, "meal-ok", "assess_time_shortage_alternatives") == []


def test_demo5_other_thread_has_no_alternative_context(context):
    script = _shortage_turn(prefix="thread-a") + [
        _ai([_tc("calculate_time_budget", {"user_limit_min": 40}, "thread-b-budget")]),
        _ai([_tc("search_nearby_places", {"category": "meal"}, "thread-b-meal")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["A", "B", "C"]}, "thread-b-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 20}, "thread-b-select")]),
        _decision("choose", "식사 계획입니다.", ["A"], "thread-b-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))
    ask(agent, "20분 안에 밥 먹고 싶어.", context, "thread-a")
    response = ask(agent, "40분 동안 밥 먹고 싶어.", context, "thread-b")

    assert response.status == "ok"
    assert _search_categories(agent, "thread-b") == ["meal"]
    assert _results(agent, "thread-b", "assess_time_shortage_alternatives") == []


def test_demo6_route_api_error_keeps_existing_error_flow(context):
    class FailingRoutes:
        def round_trip(self, origin, place):
            raise ClientError("ROUTE_PARSE", "경로 오류")

    context.routes_client = FailingRoutes()
    script = [
        _ai([_tc("get_charging_status", {}, "err-charging")]),
        _ai([_tc("calculate_time_budget", {"user_limit_min": 20}, "err-budget")]),
        _ai([_tc("search_nearby_places", {"category": "meal"}, "err-meal")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["A", "B", "C"]}, "err-routes")]),
        _decision("stop", "경로 조회 오류로 계획할 수 없습니다.", tag="err-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))
    ask(agent, "20분 안에 밥 먹고 싶어.", context, "api-error")

    assert _results(agent, "api-error", "get_walking_routes")[0]["status"] == "error"
    assert _results(agent, "api-error", "assess_time_shortage_alternatives") == []


def test_no_place_and_partial_route_are_not_time_shortage(context):
    no_place_script = [
        _ai([_tc("get_charging_status", {}, "none-charging")]),
        _ai([_tc("calculate_time_budget", {"user_limit_min": 20}, "none-budget")]),
        _ai([_tc("search_nearby_places", {"category": "mart"}, "none-mart")]),
        _decision("stop", "주변 마트를 찾지 못했습니다.", tag="none-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=no_place_script))
    ask(agent, "마트 찾아줘.", context, "no-place")
    assert _results(agent, "no-place", "assess_time_shortage_alternatives") == []

    class PartialRoutes:
        def round_trip(self, origin, place):
            if place.poi_id == "C":
                raise ClientError("ROUTE_PARSE", "경로 오류")
            return RoundTrip(poi_id=place.poi_id, outbound_sec=600, inbound_sec=600,
                             route_source="mock")

    context.routes_client = PartialRoutes()
    partial_script = [
        _ai([_tc("search_nearby_places", {"category": "meal"}, "partial-meal")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["A", "B", "C"]}, "partial-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 20}, "partial-select")]),
        _ai([_tc("assess_time_shortage_alternatives", {}, "partial-assess")]),
        _decision("stop", "일부 경로를 평가하지 못했습니다.", tag="partial-decision"),
    ]
    partial_agent = build_agent(model=ScriptedModel(script=partial_script))
    ask(partial_agent, "밥 먹고 싶어.", context, "partial")
    assessment = _results(partial_agent, "partial", "assess_time_shortage_alternatives")[0]
    assert assessment["data"]["reason"] == "not_time_shortage"


def test_new_time_and_new_category_take_priority(context):
    time_script = _shortage_turn(prefix="time") + [
        _ai([_tc("calculate_time_budget", {"user_limit_min": 40}, "new-budget")]),
        _ai([_tc("search_nearby_places", {"category": "meal"}, "new-meal")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["A", "B", "C"]}, "new-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 20}, "new-select")]),
        _decision("choose", "새 시간으로 다시 계산했습니다.", ["A"], "new-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=time_script))
    ask(agent, "20분 안에 밥 먹고 싶어.", context, "new-time")
    response = ask(agent, "생각보다 40분 남았어. 그래도 밥 먹을래.", context, "new-time")
    assert response.status == "ok"
    assert _search_categories(agent, "new-time") == ["meal", "meal"]

    category_script = _shortage_turn(prefix="new-category") + [
        _ai([_tc("search_nearby_places", {"category": "cafe"}, "new-cafe")]),
        _decision("stop", "카페 결과입니다.", tag="new-category-decision"),
    ]
    category_agent = build_agent(model=ScriptedModel(script=category_script))
    ask(category_agent, "20분 안에 밥 먹고 싶어.", context, "new-category")
    ask(category_agent, "아니, 그냥 카페 찾아줘.", context, "new-category")
    assert _search_categories(category_agent, "new-category") == ["meal", "cafe"]


def test_new_mart_request_uses_mart_default_without_old_alternative(context):
    base_places = context.places_client
    base_routes = context.routes_client

    class MartPlaces:
        def search_around(self, origin, category, radius_km=1, count=20):
            if category != "mart":
                return base_places.search_around(origin, category, radius_km, count)
            return [Place(poi_id="M", name="동네마트", category="mart", latitude=origin.latitude,
                          longitude=origin.longitude, distance_m=10, poi_source="mock")]

    class MartRoutes:
        def round_trip(self, origin, place):
            if place.poi_id != "M":
                return base_routes.round_trip(origin, place)
            return RoundTrip(poi_id="M", outbound_sec=60, inbound_sec=60, route_source="mock")

    context.places_client = MartPlaces()
    context.routes_client = MartRoutes()
    script = _shortage_turn(prefix="mart") + [
        _ai([_tc("search_nearby_places", {"category": "mart"}, "mart-search")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["M"]}, "mart-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 25}, "mart-select")]),
        _ai([_tc("assess_time_shortage_alternatives", {}, "mart-assess")]),
        _decision("clarify", "마트보다 짧은 활동을 선택해 주세요.", tag="mart-decision"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))
    ask(agent, "20분 안에 밥 먹고 싶어.", context, "new-mart")
    ask(agent, "아니, 그냥 마트 찾아줘.", context, "new-mart")

    assert _search_categories(agent, "new-mart") == ["meal", "mart"]
    assert _call_args(agent, "new-mart", "select_feasible_plans") == [
        {"dwell_min": 20}, {"dwell_min": 25},
    ]


def test_alternative_plan_can_be_confirmed_on_third_turn(context):
    script = _shortage_turn(prefix="confirm") + [
        _ai([_tc("search_nearby_places", {"category": "convenience"}, "confirm-search")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["E"]}, "confirm-routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 10}, "confirm-select")]),
        _decision("choose", "편의점 계획입니다.", ["E"], "confirm-choice"),
        _ai([_tc("confirm_plan", {"plan_id": "E", "version": 1}, "confirm-plan")]),
        _decision("done", "편의점 계획을 확정했습니다.", ["E"], "confirm-done"),
    ]
    agent = build_agent(model=ScriptedModel(script=script))
    ask(agent, "20분 안에 밥 먹고 싶어.", context, "confirm-alternative")
    recommended = ask(agent, "편의점 좋아.", context, "confirm-alternative")
    confirmed = ask(agent, "네.", context, "confirm-alternative")

    assert recommended.status == "ok"
    assert confirmed.status == "confirmed"
    assert confirmed.selected_plan_id == "E"
    assert _results(agent, "confirm-alternative", "confirm_plan")[-1]["status"] == "ok"


def test_assessment_tool_is_read_only(context):
    s = context.session
    before = repr(s)
    result = assess_time_shortage_alternatives.func(
        SimpleNamespace(context=context, state={"messages": []}),
    )
    assert result["data"]["reason"] == "not_verified"
    assert repr(s) == before
