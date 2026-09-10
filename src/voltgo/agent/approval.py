"""
Human-in-the-loop (설계서 3.2 HumanInTheLoopMiddleware)

노트북 [5] 2-1 ③ 패턴. confirm_plan / save_preferences 두 도구만 사람 승인을 거친다.
읽기 도구는 승인 없이 진행. 승인 상태를 true 로 주는 모델 인자는 없다 - 재개 결정으로만 생긴다.
"""
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langgraph.types import Command


def _describe(request):
    call = request.tool_call
    if call["name"] == "confirm_plan":
        return f"계획 {call['args'].get('plan_id')} 을(를) 확정할까요?"
    return f"선호를 저장할까요? {call['args']}"


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


def resume_command(decision: str, reason: str = "") -> Command:
    """approve / reject 를 LangGraph 재개 명령으로 바꾼다"""
    if decision not in ("approve", "reject"):
        raise ValueError("decision 은 approve 또는 reject")
    d = {"type": decision}
    if decision == "reject" and reason:
        d["message"] = reason
    return Command(resume={"decisions": [d]})
