"""
ask() / decide() 진입점 검증 — demo.py 가 실제로 부르는 경로.

build_agent 에 가짜 모델을 넣고 실제 도구·미들웨어·HITL·checkpointer 를 그대로 쓴다.
"""
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from voltgo.agent import memory, tools
from voltgo.agent.agent import ask, build_agent, decide
from voltgo.agent.middleware import MAX_MODEL_CALLS

from test_approval import ScriptedModel, _ai, _tc


def _rt(context):
    return SimpleNamespace(context=context)


def _plan_ready(context):
    tools.get_charging_status.func(_rt(context))
    tools.calculate_time_budget.func(_rt(context), user_limit_min=30)
    tools.search_nearby_places.func(_rt(context), category="meal")
    tools.get_walking_routes.func(_rt(context), poi_ids=["A", "B"])
    tools.select_feasible_plans.func(_rt(context), dwell_min=12)
    return context.session.candidates["A"].version


def _agent(script):
    return build_agent(model=ScriptedModel(script=script))


def test_승인_없이_decide_하면_오류로_안내한다(context):
    agent = _agent([AIMessage(content="안녕하세요")])
    r = decide(agent, "approve", context, "d-0")
    assert r.status == "error" and "NO_PENDING_APPROVAL" in r.message


def test_ask_가_승인을_기다리고_decide_approve_로_확정된다(context):
    v = _plan_ready(context)
    agent = _agent([
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": v}, "t1")]),
        AIMessage(content="확정했습니다."),
    ])
    r = ask(agent, "A 계획으로 확정해줘", context, "d-1")
    assert r.status == "awaiting_approval"
    assert context.session.confirmed is None

    r2 = decide(agent, "approve", context, "d-1")
    assert context.session.confirmed is not None and context.session.confirmed.plan_id == "A"
    assert r2.status != "error"


def test_확정과_저장이_함께_오면_화면에_둘_다_보이고_결정_하나로_전부_거절된다(context):
    v = _plan_ready(context)
    agent = _agent([
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": v}, "t1"),
             _tc("save_preferences", {"category": "cafe"}, "t2")]),
        AIMessage(content="처리했습니다."),
    ])
    r = ask(agent, "확정하고 카페 기억해줘", context, "d-2")
    assert r.status == "awaiting_approval"
    assert "저장" in r.message, "두 번째 승인 요청(선호 저장)도 화면에 보여야 한다"

    r2 = decide(agent, "reject", context, "d-2")
    assert context.session.confirmed is None
    assert memory.load_preferences(context.user_id) is None
    assert r2.status != "error"


def test_결정을_리스트로_주면_요청_순서대로_적용된다(context):
    v = _plan_ready(context)
    agent = _agent([
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": v}, "t1"),
             _tc("save_preferences", {"category": "cafe"}, "t2")]),
        AIMessage(content="처리했습니다."),
    ])
    ask(agent, "확정하고 카페 기억해줘", context, "d-3")
    decide(agent, ["approve", "reject"], context, "d-3")
    assert context.session.confirmed is not None, "확정은 승인"
    assert memory.load_preferences(context.user_id) is None, "저장은 거절"


def test_결정_개수가_다르면_거부한다(context):
    v = _plan_ready(context)
    agent = _agent([_ai([_tc("confirm_plan", {"plan_id": "A", "version": v}, "t1")]), AIMessage(content="x")])
    ask(agent, "확정", context, "d-4")
    r = decide(agent, ["approve", "reject"], context, "d-4")
    assert r.status == "error" and "DECISION_COUNT_MISMATCH" in r.message
    assert context.session.confirmed is None


def test_재개_중_모델_한도에_걸리면_오류_응답으로_돌려준다(context):
    v = _plan_ready(context)
    agent = _agent([_ai([_tc("confirm_plan", {"plan_id": "A", "version": v}, "t1")]), AIMessage(content="x")])
    ask(agent, "확정", context, "d-5")
    context.session.counters["model"] = MAX_MODEL_CALLS     # 남은 여력 0
    r = decide(agent, "approve", context, "d-5")
    assert r.status == "error" and "LIMIT_EXCEEDED" in r.message
