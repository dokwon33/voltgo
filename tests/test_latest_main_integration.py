"""최신 main 검색과 A의 실제 에이전트 실행을 연결하는 회귀 검증 (모델/API는 Mock)."""
from types import SimpleNamespace

import pytest

from voltgo.agent import tools
from voltgo.agent.agent import ask, build_agent
from tests.test_approval import ScriptedModel, _ai, _tc, _plan_ready
from tests.test_decide_entrypoint import _tool_results


def decision(action, ids=(), tid="final"):
    return _ai([_tc("ModelDecision", {
        "candidate_ids": list(ids), "next_action": action, "explanation": "확인 결과입니다."
    }, tid)])


def test_체류_단축으로_재검색한_새_후보를_왕복_판정하고_확정한다(context):
    model = ScriptedModel(script=[
        _ai([_tc("get_charging_status", {}, "charging")]),
        _ai([_tc("calculate_time_budget", {}, "budget")]),
        _ai([_tc("search_nearby_places", {"category": "cafe"}, "default-search")]),
        decision("clarify", tid="default-result"),
        _ai([_tc("search_nearby_places", {"category": "cafe", "dwell_min": 5}, "short-search")]),
        _ai([_tc("get_walking_routes", {"poi_ids": ["D"]}, "routes")]),
        _ai([_tc("select_feasible_plans", {"dwell_min": 5}, "select")]),
        decision("choose", ["D"], tid="short-result"),
    ])
    agent = build_agent(model=model)
    thread = "adaptive-dwell"
    ask(agent, "충전 중에 카페에 가고 싶어", context, thread)
    initial = _tool_results(agent, thread, "search_nearby_places")[0]
    assert "540m" in initial["message"]
    assert "D" not in context.session.places

    response = ask(agent, "카페에서는 5분만 있을게", context, thread)
    searches = _tool_results(agent, thread, "search_nearby_places")
    assert len(searches) == 2 and "810m" in searches[-1]["message"]
    assert "D" in {p["poi_id"] for p in searches[-1]["data"]}
    assert response.status == "ok" and response.request_id is None
    assert [p.plan_id for p in response.candidates] == ["D"]
    plan = response.candidates[0]
    assert (plan.outbound_sec, plan.dwell_sec, plan.inbound_sec) == (600, 300, 640)
    assert plan.total_sec == 1540
    assert plan.return_at < response.return_deadline

    model.script.extend([
        _ai([_tc("confirm_plan", {"plan_id": "D", "version": plan.version}, "confirm")]),
        decision("done", ["D"], tid="confirmed-result"),
    ])
    response = ask(agent, "D로 확정해줘", context, thread)
    assert response.status == "confirmed" and response.selected_plan_id == "D"
    assert response.request_id is None
    assert _tool_results(agent, thread, "confirm_plan")[0]["status"] == "ok"


@pytest.mark.parametrize("category", ["mart", "convenience"], ids=["empty", "nonempty"])
def test_새_검색은_이전_확정과_재확정_캐시도_무효화한다(context, category):
    version = _plan_ready(context)
    runtime = SimpleNamespace(context=context)
    assert tools.confirm_plan.func(runtime, plan_id="A", version=version)["status"] == "ok"
    assert context.session.confirmed_by_plan
    agent = build_agent(model=ScriptedModel(script=[
        _ai([_tc("search_nearby_places", {"category": category}, "new-search")]),
        decision("choose"),
    ]))

    response = ask(agent, "다른 업종으로 다시 찾아줘", context, "new-category")
    result = _tool_results(agent, "new-category", "search_nearby_places")[0]
    assert result["status"] == "ok"
    assert bool(result["data"]) == (category == "convenience")
    assert context.session.condition_version == version + 1
    assert context.session.confirmed is None
    assert context.session.confirmed_by_plan == {}
    assert context.session.candidates == {} and context.session.routes == {}
    assert response.status != "confirmed" and response.selected_plan_id is None
    assert tools.confirm_plan.func(runtime, plan_id="A", version=version)["error_code"] == "PRECONDITION_FAILED"


def test_충전소_후보를_사용자가_선택한_ID로_확정한다(context):
    context.session.origin = None
    agent = build_agent(model=ScriptedModel(script=[
        _ai([_tc("find_station", {"keyword": "역삼역"}, "find")]),
        decision("clarify", tid="ask-station"),
        _ai([_tc("find_station", {"keyword": "역삼역", "station_id": "S2"}, "choose-station")]),
        decision("clarify", tid="ask-intent"),
    ]))
    response = ask(agent, "역삼역 충전소야", context, "station")
    assert response.status == "need_input"
    assert context.session.origin is None
    assert set(context.session.station_candidates) == {"S1", "S2"}
    ask(agent, "2주차장 충전소로 선택할게", context, "station")
    assert context.session.origin.poi_id == "S2"
    assert context.session.station_candidates == {}
