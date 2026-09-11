"""승인 요청 ID와 재전송 결과. CLI의 동일 Context/Session 수명 동안 보관한다."""
import json
from uuid import uuid4

from voltgo.agent.assembler import assemble, error_response
from voltgo.agent.schemas import VoltGoResponse
from voltgo.agent.state import ApprovalRequest


def config_for(user_id: str, thread_id: str) -> dict:
    # 같은 thread 이름을 쓰는 사용자도 checkpoint를 공유하지 않는다.
    key = "voltgo:" + json.dumps([user_id, thread_id], ensure_ascii=False)
    return {"configurable": {"thread_id": key}}


def bind_session(agent, context, thread_id: str):
    """Session 하나는 사용자/대화 한 쌍에만 연결한다. 새 Session의 자동 복원은 없다."""
    owner = (context.user_id, thread_id)
    session = context.session
    code = None
    if session.request_owner is not None and session.request_owner != owner:
        code = "SESSION_SCOPE_MISMATCH"
    elif session.request_owner is None:
        state = agent.get_state(config_for(*owner))
        if state.values:
            code = "SESSION_NOT_RESTORED"
        else:
            session.request_owner = owner
    if code:
        # 다른 사용자의 기존 Session 내용은 오류 응답에도 포함하지 않는다.
        return VoltGoResponse(
            status="error", generated_at=context.clock(),
            message=f"[{code}] 이 대화에서 사용하던 Context/Session으로 다시 요청하세요.")
    return None


def approval_payload(interrupts):
    actions = []
    ids = []
    for item in interrupts or []:
        value = item.value
        if isinstance(value, dict) and value.get("action_requests"):
            ids.append(item.id)
            actions.extend(value["action_requests"])
    # 표시 문구나 dict 삽입 순서가 달라도 실제 도구/인자가 같으면 같은 내용이다.
    payload = json.dumps(
        [{"name": a["name"], "args": a.get("args", {})} for a in actions],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return payload, tuple(ids), len(actions)


def present_result(result, context):
    """HITL 요청마다 ID를 발급한다. 같은 interrupt를 다시 표시할 때는 유지한다."""
    session = context.session
    payload, ids, count = approval_payload(result.get("__interrupt__"))
    request_id = None
    if count:
        previous = session.approval_requests.get(session.pending_request_id)
        if (previous and previous.interrupt_ids == ids and previous.payload == payload
                and previous.decision_payload is None):
            request = previous
        else:
            request_id = uuid4().hex
            request = ApprovalRequest(
                payload=payload, interrupt_ids=ids, action_count=count,
                requested_at=session.approval_requested_at or context.clock())
            session.approval_requests[request_id] = request
            session.pending_request_id = request_id
        request_id = session.pending_request_id
        session.approval_requested_at = request.requested_at
    else:
        session.pending_request_id = None
        session.approval_requested_at = None
    response = assemble(result, context)
    response.request_id = request_id
    return response


def request_error(context, request_id, code, message):
    response = error_response(context, code, message)
    response.request_id = request_id
    return response


def finish_request(context, request_id, response):
    """완료 응답의 사본을 보관한다. 재전송은 현재 Session을 되돌리지 않는다."""
    session = context.session
    if response.request_id is None:
        response.request_id = request_id
    session.approval_requests[request_id].response = response.model_copy(deep=True)
    if session.pending_request_id == request_id:
        session.pending_request_id = None
        session.approval_requested_at = None
    return response
