"""
Human-in-the-loop (설계서 3.2 HumanInTheLoopMiddleware)

노트북 [5] 2-1 ③ 패턴. confirm_plan / save_preferences 두 도구만 사람 승인을 거친다.
읽기 도구는 승인 없이 진행. 승인 상태를 true 로 주는 모델 인자는 없다 - 재개 결정으로만 생긴다.
"""
from langchain.agents.middleware import HumanInTheLoopMiddleware, after_model
from langgraph.types import Command

APPROVAL_TOOLS = ("confirm_plan", "save_preferences")


def _describe(tool_call, state=None, runtime=None):
    """HITL 승인 화면 문구.

    langchain 의 HumanInTheLoopMiddleware 는 description 콜백을
    description(tool_call, state, runtime) 3 인자로 부른다.
    """
    args = tool_call.get("args", {})
    if tool_call["name"] == "confirm_plan":
        return f"계획 {args.get('plan_id')} (v{args.get('version')}) 을(를) 확정할까요?"
    if tool_call["name"] == "save_preferences":
        items = ", ".join(f"{k}={v}" for k, v in args.items() if v is not None) or "없음"
        return f"선호를 저장할까요? ({items})"
    return f"{tool_call['name']} 을(를) 실행할까요? {args}"


@after_model
def mark_approval_requested(state, runtime):
    """승인 화면을 보여주기 직전에 그 시각을 남긴다.

    확정 만료를 재려면 '후보를 만든 시각'이 아니라 '사용자에게 승인을 물어본 시각'이
    필요하다. HITL 미들웨어보다 먼저 등록해 interrupt 로 멈추기 전에 기록한다.
    """
    messages = state.get("messages") or []
    if not messages:
        return None
    calls = getattr(messages[-1], "tool_calls", None) or []
    if any(c.get("name") in APPROVAL_TOOLS for c in calls):
        session = runtime.context.session
        if session.approval_requested_at is None:
            session.approval_requested_at = runtime.context.clock()
    return None


def approval_middleware():
    return HumanInTheLoopMiddleware(
        interrupt_on={
            # 계획 확정 전에 사람 판단 개입 (approve / reject 만)
            "confirm_plan": {"allowed_decisions": ["approve", "reject"], "description": _describe},
            # 선호 저장도 명시 동의 뒤에만
            "save_preferences": {"allowed_decisions": ["approve", "reject"], "description": _describe},
            # 나머지는 자동 진행
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
