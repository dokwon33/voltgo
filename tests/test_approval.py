"""
HITL 승인 검증 (설계서 3.2 / C002 · C016 · C018)

외부 API 도 실제 모델도 쓰지 않는다. 정해진 tool_call 을 돌려주는 가짜 모델과
더미 도구만으로 "승인 전 실행 0건 -> 승인 후 1회 실행" 을 증명한다.
"""
from typing import Any, Optional

import pytest
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from voltgo.agent.approval import approval_middleware, resume_command

CALLS = {"confirm": 0, "save": 0, "delete": 0}


@tool
def confirm_plan(plan_id: str, version: int = 1) -> str:
    """사용자가 선택한 계획을 확정한다. 별도 승인 없이 실행한다."""
    CALLS["confirm"] += 1
    return f"confirmed {plan_id}"


@tool
def save_preferences(category: str = "cafe") -> str:
    """선호를 저장한다. 승인 후에만 실행되어야 한다."""
    CALLS["save"] += 1
    return f"saved {category}"


@tool
def delete_preferences() -> str:
    """선호를 삭제한다. 승인 없이 진행한다(설계서 interrupt_on=False)."""
    CALLS["delete"] += 1
    return "deleted"


class ScriptedModel(BaseChatModel):
    """미리 정한 AIMessage 를 순서대로 돌려주는 가짜 모델."""

    script: list = []
    idx: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):     # create_agent 가 호출한다
        return self

    def _generate(self, messages, stop=None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        i = min(self.idx, len(self.script) - 1)
        msg = self.script[i]
        object.__setattr__(self, "idx", self.idx + 1)
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _ai(tool_calls):
    return AIMessage(content="", tool_calls=tool_calls)


def _tc(name, args, tid):
    return {"name": name, "args": args, "id": tid, "type": "tool_call"}


def _build(script):
    for k in CALLS:
        CALLS[k] = 0
    model = ScriptedModel(script=script)
    return create_agent(
        model=model,
        tools=[confirm_plan, save_preferences, delete_preferences],
        middleware=[approval_middleware()],
        checkpointer=InMemorySaver(),
    )


def _cfg(tid):
    return {"configurable": {"thread_id": tid}}


def _interrupts(result):
    return result.get("__interrupt__", []) if isinstance(result, dict) else []


# ---------------------------------------------------------------- C002
def test_계획_확정은_별도_승인_없이_실행된다():
    agent = _build([
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": 1}, "t1")]),
        AIMessage(content="확정했습니다."),
    ])
    out = agent.invoke({"messages": [("user", "A 계획으로 확정")]}, _cfg("hitl-plan"))
    assert not _interrupts(out)
    assert CALLS["confirm"] == 1


def test_선호_저장은_승인_전에_실행되지_않고_승인_후_한_번만_실행된다():
    agent = _build([
        _ai([_tc("save_preferences", {"category": "cafe"}, "t1")]),
        AIMessage(content="저장했습니다."),
    ])
    out = agent.invoke({"messages": [("user", "카페 선호 기억해줘")]}, _cfg("hitl-save"))
    assert _interrupts(out)
    assert CALLS["save"] == 0
    agent.invoke(resume_command("approve"), _cfg("hitl-save"))
    assert CALLS["save"] == 1


# ---------------------------------------------------------------- C016
def test_선호_저장을_거절하면_실행되지_않는다():
    agent = _build([
        _ai([_tc("save_preferences", {"category": "cafe"}, "t1")]),
        AIMessage(content="저장하지 않았습니다."),
    ])
    agent.invoke({"messages": [("user", "카페 기억해줘")]}, _cfg("hitl-2"))
    assert CALLS["save"] == 0
    agent.invoke(resume_command("reject", "사용자가 취소"), _cfg("hitl-2"))
    assert CALLS["save"] == 0


# ---------------------------------------------------------------- 복수 승인
def test_확정과_저장이_함께_와도_선호_저장만_승인_대상이다():
    agent = _build([
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": 1}, "t1"),
             _tc("save_preferences", {"category": "cafe"}, "t2")]),
        AIMessage(content="처리했습니다."),
    ])
    out = agent.invoke({"messages": [("user", "확정하고 카페 기억해줘")]}, _cfg("hitl-3"))
    assert _interrupts(out)
    assert CALLS["confirm"] == 0 and CALLS["save"] == 0

    requests = _interrupts(out)[0].value["action_requests"]
    assert [r["name"] for r in requests] == ["save_preferences"]
    agent.invoke(resume_command("reject"), _cfg("hitl-3"))
    assert CALLS["confirm"] == 1, "계획 확정은 별도 승인 대상이 아니다"
    assert CALLS["save"] == 0, "선호 저장을 거절하면 저장하지 않는다"


# ---------------------------------------------------------------- 읽기/삭제 도구
def test_승인_대상이_아닌_도구는_바로_실행된다():
    agent = _build([
        _ai([_tc("delete_preferences", {}, "t1")]),
        AIMessage(content="삭제했습니다."),
    ])
    out = agent.invoke({"messages": [("user", "기억 삭제해줘")]}, _cfg("hitl-4"))
    assert not _interrupts(out), "delete_preferences 는 승인 없이 진행"
    assert CALLS["delete"] == 1


# ---------------------------------------------------------------- 승인 화면 문구
def test_승인_화면에_저장할_선호_내용이_보인다():
    from voltgo.agent.approval import _describe
    assert "cafe" in _describe(_tc("save_preferences", {"category": "cafe"}, "t2"))


def test_잘못된_결정값은_거부한다():
    with pytest.raises(ValueError):
        resume_command("yes")


# ================================================================
# 승인 대기 만료 vs 후보 신선도 (C016)  — 두 시각은 다른 것을 잰다
# ================================================================
from datetime import timedelta                                    # noqa: E402

from voltgo.agent import tools                                    # noqa: E402
from voltgo.agent.state import Context                            # noqa: E402


class _RT:
    """ToolRuntime 대역. 도구는 runtime.context 만 쓴다."""

    def __init__(self, context: Context):
        self.context = context


def _plan_ready(context):
    """C001 조건으로 후보 A 까지 만들어 둔다."""
    tools.get_charging_status.func(_RT(context))
    tools.calculate_time_budget.func(_RT(context), user_limit_min=30)
    tools.search_nearby_places.func(_RT(context), category="meal")
    tools.get_walking_routes.func(_RT(context), poi_ids=["A", "B"])
    tools.select_feasible_plans.func(_RT(context), dwell_min=12)
    return context.session.candidates["A"].version


def test_승인_요청_후_2분이_지나면_선호_저장을_거절한다(context):
    context.session.approval_requested_at = context.clock() - timedelta(seconds=121)
    r = tools.save_preferences.func(_RT(context), category="cafe")
    assert r["status"] == "error"
    assert r["error_code"] == "APPROVAL_EXPIRED"


def test_계획_확정은_선호_승인_시각에_영향받지_않는다(context):
    version = _plan_ready(context)
    s = context.session
    base = context.clock()
    s.candidates["A"].evaluated_at = base - timedelta(seconds=150)
    s.approval_requested_at = base - timedelta(seconds=121)
    r = tools.confirm_plan.func(_RT(context), plan_id="A", version=version)
    assert r["status"] == "ok"
    assert s.approval_requested_at == base - timedelta(seconds=121)


def test_후보가_너무_오래되면_따로_거절한다(context):
    version = _plan_ready(context)
    s = context.session
    base = context.clock()
    s.candidates["A"].evaluated_at = base - timedelta(seconds=400)
    s.approval_requested_at = base

    r = tools.confirm_plan.func(_RT(context), plan_id="A", version=version)
    assert r["status"] == "error"
    assert r["error_code"] == "STALE_CANDIDATE", "승인 만료와 다른 사유로 구분돼야 한다"


def test_승인_요청_시각은_승인_화면이_뜰_때_기록된다(context):
    from langchain_core.messages import AIMessage

    from voltgo.agent.approval import mark_approval_requested

    s = context.session
    assert s.approval_requested_at is None

    state = {"messages": [AIMessage(content="", tool_calls=[
        {"name": "save_preferences", "args": {"category": "cafe"}, "id": "t1", "type": "tool_call"}])]}
    mark_approval_requested.after_model(state, _RT(context))
    assert s.approval_requested_at == context.clock()


def test_읽기_도구만_제안되면_승인_시각을_기록하지_않는다(context):
    from langchain_core.messages import AIMessage

    from voltgo.agent.approval import mark_approval_requested

    state = {"messages": [AIMessage(content="", tool_calls=[
        {"name": "search_nearby_places", "args": {"category": "meal"}, "id": "t1", "type": "tool_call"}])]}
    mark_approval_requested.after_model(state, _RT(context))
    assert context.session.approval_requested_at is None


# ================================================================
# 확정 재검증이 추천보다 관대해지면 안 된다 (목표 SoC 불일치, C028 연계)
# ================================================================
def test_사용자_목표가_차량_목표보다_낮으면_재검증도_원문_잔여시간을_쓰지_않는다(context):
    version = _plan_ready(context)
    s = context.session
    base = context.clock()
    # 차량은 100% 까지 충전하도록 설정(원문 잔여 60분), 사용자 목표는 80%
    s.charging = s.charging.model_copy(update={"reported_target_soc_pct": 100, "reported_remaining_sec": 3600})
    context.charging_provider = None          # 재검증 때 새로 조회하지 않고 위 상태를 쓰게 한다

    r = tools.confirm_plan.func(_RT(context), plan_id="A", version=version)
    assert r["status"] == "ok"
    # 추정 경로: (80-40)% x 60kWh / 48kW = 30분 -> 완료 14:30, 버퍼 5분 -> 마감 14:25
    assert s.time_budget.return_deadline == base + timedelta(minutes=25), \
        "원문 잔여시간(100% 기준 60분)으로 마감이 14:55 처럼 늘어나면 안 된다"


def test_사용자_목표가_차량_목표보다_높으면_차량_목표에서_멈추는_것으로_재검증한다(context):
    version = _plan_ready(context)
    s = context.session
    s.charging = s.charging.model_copy(update={"reported_target_soc_pct": 80, "reported_remaining_sec": 1800})
    s.target_soc_pct = 100
    context.charging_provider = None

    r = tools.confirm_plan.func(_RT(context), plan_id="A", version=version)
    assert r["status"] == "ok"
    assert s.time_budget.finish_at == context.clock() + timedelta(seconds=1800), \
        "차량은 80% 에서 멈추므로 원문 잔여 30분 기준이어야 한다"
