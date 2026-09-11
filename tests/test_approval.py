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
    """계획을 확정한다. 승인 후에만 실행되어야 한다."""
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
def test_확정은_승인_전에_실행되지_않고_승인_후_한_번만_실행된다():
    agent = _build([
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": 1}, "t1")]),
        AIMessage(content="확정했습니다."),
    ])
    out = agent.invoke({"messages": [("user", "A 계획으로 확정")]}, _cfg("hitl-1"))

    assert _interrupts(out), "승인 요청(interrupt)이 발생해야 한다"
    assert CALLS["confirm"] == 0, "승인 전에는 실행되면 안 된다"

    agent.invoke(resume_command("approve"), _cfg("hitl-1"))
    assert CALLS["confirm"] == 1, "승인 후 정확히 1회 실행"


# ---------------------------------------------------------------- C016
def test_거절하면_실행되지_않는다():
    agent = _build([
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": 1}, "t1")]),
        AIMessage(content="확정하지 않았습니다."),
    ])
    agent.invoke({"messages": [("user", "A 계획으로 확정")]}, _cfg("hitl-2"))
    assert CALLS["confirm"] == 0

    agent.invoke(resume_command("reject", "사용자가 취소"), _cfg("hitl-2"))
    assert CALLS["confirm"] == 0, "거절했는데 실행되면 안 된다"


# ---------------------------------------------------------------- 복수 승인
def test_확정과_저장이_함께_오면_결정을_각각_적용한다():
    agent = _build([
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": 1}, "t1"),
             _tc("save_preferences", {"category": "cafe"}, "t2")]),
        AIMessage(content="처리했습니다."),
    ])
    out = agent.invoke({"messages": [("user", "확정하고 카페 기억해줘")]}, _cfg("hitl-3"))
    assert _interrupts(out)
    assert CALLS["confirm"] == 0 and CALLS["save"] == 0

    # 계획은 승인, 선호 저장은 거절 - 요청 순서대로 대응
    agent.invoke(resume_command(["approve", "reject"]), _cfg("hitl-3"))
    assert CALLS["confirm"] == 1, "승인한 확정은 실행"
    assert CALLS["save"] == 0, "거절한 저장은 미실행 (계획 승인으로 자동 승인되면 안 된다)"


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
def test_승인_화면에_계획_ID_와_선호_내용이_보인다():
    from voltgo.agent.approval import _describe
    assert "A" in _describe(_tc("confirm_plan", {"plan_id": "A", "version": 2}, "t1"))
    assert "cafe" in _describe(_tc("save_preferences", {"category": "cafe"}, "t2"))


def test_잘못된_결정값은_거부한다():
    with pytest.raises(ValueError):
        resume_command("yes")
