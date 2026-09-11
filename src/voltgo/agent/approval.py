"""
Human-in-the-loop (설계서 3.2 HumanInTheLoopMiddleware)

노트북 [5] 2-1 ③ 패턴. 사람 승인을 거치는 도구는 save_preferences 하나다.
- 계획 확정(confirm_plan)은 사용자가 후보를 고른 발화 자체가 결정이라 승인 화면을 띄우지 않는다.
  대신 도구 안에서 현재 시각·최신 충전 상태로 다시 검증한 뒤 기록한다.
- 읽기 도구와 delete_preferences 는 승인 없이 진행한다.
승인 상태를 true 로 주는 모델 인자는 없다 - 재개 결정(Command(resume=...))으로만 생긴다.
"""
from langchain.agents.middleware import HumanInTheLoopMiddleware, after_model
from langgraph.types import Command

APPROVAL_TOOLS = ("save_preferences",)


def _describe(tool_call, state=None, runtime=None):
    """HITL 승인 화면 문구.

    langchain 의 HumanInTheLoopMiddleware 는 description 콜백을
    description(tool_call, state, runtime) 3 인자로 부른다.
    """
    args = tool_call.get("args", {})
    if tool_call["name"] == "save_preferences":
        items = ", ".join(f"{k}={v}" for k, v in args.items() if v is not None) or "없음"
        return f"선호를 저장할까요? ({items})"
    return f"{tool_call['name']} 을(를) 실행할까요? {args}"


@after_model
def mark_approval_requested(state, runtime):
    """승인 화면을 보여주기 직전에 그 시각을 남긴다.

    승인 만료(2분)를 재려면 '사용자에게 승인을 물어본 시각'이 필요하다.
    after_model 훅은 등록한 순서의 **역순**으로 실행되므로, 이 훅은 미들웨어 목록에서
    HumanInTheLoopMiddleware 보다 *뒤에* 등록해야 interrupt 로 멈추기 전에 먼저 돈다.
    승인 화면이 새로 뜰 때마다 덮어쓴다 (거절 뒤 다시 요청하면 그 시각부터 다시 잰다).
    """
    messages = state.get("messages") or []
    if not messages:
        return None
    calls = getattr(messages[-1], "tool_calls", None) or []
    if any(c.get("name") in APPROVAL_TOOLS for c in calls):
        runtime.context.session.approval_requested_at = runtime.context.clock()
    return None


def approval_middleware():
    return HumanInTheLoopMiddleware(
        interrupt_on={
            # 선호 저장은 명시 동의 뒤에만 (approve / reject 만)
            "save_preferences": {"allowed_decisions": ["approve", "reject"], "description": _describe},
            # 계획 확정은 승인 없이 진행 - 후보를 고른 발화가 곧 결정이고, 도구가 재검증한다
            "confirm_plan": False,
            # 삭제도 승인 없이 진행 (설계서 interrupt_on=False)
            "delete_preferences": False,
        }
    )


def resume_command(decision, reason: str = "", count: int = 1) -> Command:
    """approve / reject 를 LangGraph 재개 명령으로 바꾼다.

    한 응답에 승인 대상 도구가 여러 개면 결정도 같은 개수로 보내야 재개된다.
    - decision 이 문자열이면 count 개만큼 같은 결정을 반복한다.
    - decision 이 리스트면 요청 순서대로 하나씩 대응시킨다.
    """
    decisions = decision if isinstance(decision, (list, tuple)) else [decision] * max(count, 1)
    out = []
    for dec in decisions:
        if dec not in ("approve", "reject"):
            raise ValueError("decision 은 approve 또는 reject")
        d = {"type": dec}
        if dec == "reject" and reason:
            d["message"] = reason
        out.append(d)
    return Command(resume={"decisions": out})


def pending_approvals(agent, config) -> list[dict]:
    """checkpoint 에 남아 있는 승인 요청(action_requests) 목록. 없으면 빈 리스트."""
    try:
        state = agent.get_state(config)
    except Exception:
        return []
    out = []
    for it in getattr(state, "interrupts", None) or []:
        value = getattr(it, "value", None)
        if isinstance(value, dict):
            out.extend(value.get("action_requests") or [])
    return out
