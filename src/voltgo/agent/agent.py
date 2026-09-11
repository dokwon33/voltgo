"""
VoltGo 에이전트 조립 (설계서 2.3, 2.5)

노트북 [4] 의 create_agent + ToolStrategy, [5] 의 middleware / checkpointer / context_schema 조합.
"""
import json
import logging
import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver

from voltgo.agent.approval import approval_middleware, mark_approval_requested, resume_command
from voltgo.agent.assembler import error_response
from voltgo.agent.middleware import (
    input_validation, model_budget, output_integrity, preference_prompt, tool_policy,
)
from voltgo.agent.prompts import SYSTEM_PROMPT
from voltgo.agent.requests import (
    approval_payload, bind_session, config_for, finish_request, present_result, request_error,
)
from voltgo.agent.schemas import ModelDecision, VoltGoResponse
from voltgo.agent.state import Context
from voltgo.agent.tools import TOOLS

logger = logging.getLogger(__name__)
DEFAULT_MODEL = "gpt-4.1-mini"


def build_model(model_name: str = None):
    load_dotenv()
    model_name = model_name or os.getenv("MODEL_NAME", DEFAULT_MODEL)
    kwargs = {"timeout": 10.0, "max_tokens": 1500}
    if not model_name.startswith("gpt-5"):      # gpt-5 계열은 temperature 를 못 바꾼다
        kwargs["temperature"] = 0.1
    return init_chat_model(model_name, **kwargs)


def build_agent(model=None, checkpointer=None):
    if model is None or isinstance(model, str):
        model = build_model(model)

    agent = create_agent(
        model=model,
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        # 최종 산출물은 ModelDecision 으로 강제. 숫자는 코드가 채운다.
        response_format=ToolStrategy(ModelDecision),
        middleware=[
            input_validation,       # before_agent
            preference_prompt,      # wrap_model_call
            model_budget,           # wrap_model_call
            tool_policy,            # wrap_tool_call
            approval_middleware(),  # after_model: 선호 저장만 승인 대기
            mark_approval_requested,  # after_model은 등록 역순: HITL 중단 전에 시각 기록
            output_integrity,       # after_agent
        ],
        checkpointer=checkpointer or InMemorySaver(),   # 단기 메모리 (thread_id 별)
        context_schema=Context,
    )
    return agent


def ask(agent, text: str, context: Context, thread_id: str) -> VoltGoResponse:
    """사용자 입력 1턴 -> VoltGoResponse. 승인 요청 ID는 실행 래퍼가 발급한다."""
    scope_error = bind_session(agent, context, thread_id)
    if scope_error is not None:
        return scope_error
    try:
        result = agent.invoke({"messages": [{"role": "user", "content": text}]},
                              config_for(context.user_id, thread_id), context=context)
    except RuntimeError as e:
        if str(e) == "MODEL_BUDGET_EXCEEDED":
            return error_response(context, "LIMIT_EXCEEDED", "모델 호출 한도(8회)에 도달해 중단했습니다.")
        raise
    return present_result(result, context)


def decide(agent, decision, context: Context, thread_id: str, reason: str = "",
           *, request_id: str = None) -> VoltGoResponse:
    """승인 ID에 결정을 적용한다. 같은 ID/내용의 재전송은 실행 없이 기존 응답을 반환한다.

    문자열 결정은 배치 전체에, 리스트는 요청 순서대로 적용한다.
    ID 생략은 기존 CLI 호출 호환용이며 현재 대기 요청만 가리킨다.
    재전송하는 호출자는 승인 화면의 request_id를 반드시 보관해야 한다.
    """
    scope_error = bind_session(agent, context, thread_id)
    if scope_error is not None:
        return scope_error
    session = context.session
    if request_id is None:
        request_id = session.pending_request_id
    if request_id is None:
        return error_response(context, "NO_PENDING_APPROVAL", "승인을 기다리는 요청이 없습니다.")
    if not isinstance(request_id, str) or not request_id:
        return error_response(context, "INVALID_REQUEST_ID", "승인 화면의 요청 ID를 전달하세요.")
    request = session.approval_requests.get(request_id)
    if request is None:
        return request_error(context, request_id, "UNKNOWN_REQUEST_ID",
                             "현재 대화에서 발급한 승인 요청 ID가 아닙니다.")
    if isinstance(decision, (list, tuple)) and len(decision) != request.action_count:
        return request_error(context, request_id, "DECISION_COUNT_MISMATCH",
                             f"대기 중인 요청은 {request.action_count}개인데 결정은 {len(decision)}개입니다.")
    try:
        if not isinstance(reason, str):
            raise ValueError("reason must be a string")
        command = resume_command(decision, reason, count=request.action_count)
    except (ValueError, TypeError):
        return request_error(context, request_id, "INVALID_DECISION", "approve 또는 reject로 응답하세요.")
    payload = json.dumps(command.resume, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if request.decision_payload is not None:
        if payload != request.decision_payload:
            return request_error(context, request_id, "REQUEST_ID_CONFLICT",
                                 "같은 요청 ID에 다른 결정을 보낼 수 없습니다.")
        if request.response is not None:
            return request.response.model_copy(deep=True)
        return request_error(context, request_id, "REQUEST_IN_PROGRESS", "이 승인 요청을 처리하고 있습니다.")

    config = config_for(context.user_id, thread_id)
    pending_payload, interrupt_ids, _ = approval_payload(agent.get_state(config).interrupts)
    if (session.pending_request_id != request_id or pending_payload != request.payload
            or interrupt_ids != request.interrupt_ids):
        return request_error(context, request_id, "REQUEST_ID_CONFLICT",
                             "승인 대상이 달라졌습니다. 현재 승인 화면의 요청 ID를 사용하세요.")

    # 부수 효과가 발생한 뒤 모델이 실패할 수도 있으므로 실행 전 요청을 선점한다.
    request.decision_payload = payload
    session.approval_requested_at = request.requested_at
    try:
        result = agent.invoke(command, config, context=context)
        response = present_result(result, context)
    except Exception as e:
        if isinstance(e, RuntimeError) and str(e) == "MODEL_BUDGET_EXCEEDED":
            response = request_error(context, request_id, "LIMIT_EXCEEDED",
                                     "모델 호출 한도(8회)에 도달해 중단했습니다.")
        else:
            logger.error("Approval execution failed: request_id=%s type=%s", request_id, type(e).__name__)
            response = request_error(context, request_id, "APPROVAL_EXECUTION_FAILED",
                                     "승인 처리 중 오류가 발생했습니다. 중복 실행을 막기 위해 재실행하지 않습니다.")
    return finish_request(context, request_id, response)
