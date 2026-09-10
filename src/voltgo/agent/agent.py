"""
VoltGo 에이전트 조립 (설계서 2.3, 2.5)

노트북 [4] 의 create_agent + ToolStrategy, [5] 의 middleware / checkpointer / context_schema 조합.
"""
import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver

from voltgo.agent.approval import approval_middleware, resume_command
from voltgo.agent.assembler import assemble, error_response
from voltgo.agent.middleware import (
    input_validation, model_budget, output_integrity, preference_prompt, tool_policy,
)
from voltgo.agent.prompts import SYSTEM_PROMPT
from voltgo.agent.schemas import ModelDecision, VoltGoResponse
from voltgo.agent.state import Context
from voltgo.agent.tools import TOOLS

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
            approval_middleware(),  # after_model (HITL)
            output_integrity,       # after_agent
        ],
        checkpointer=checkpointer or InMemorySaver(),   # 단기 메모리 (thread_id 별)
        context_schema=Context,
    )
    return agent


def _config(thread_id: str):
    return {"configurable": {"thread_id": thread_id}}


def ask(agent, text: str, context: Context, thread_id: str) -> VoltGoResponse:
    """사용자 입력 1턴 -> VoltGoResponse"""
    try:
        result = agent.invoke({"messages": [{"role": "user", "content": text}]},
                              _config(thread_id), context=context)
    except RuntimeError as e:
        if str(e) == "MODEL_BUDGET_EXCEEDED":
            return error_response(context, "LIMIT_EXCEEDED", "모델 호출 한도(8회)에 도달해 중단했습니다.")
        raise
    return assemble(result, context)


def decide(agent, decision: str, context: Context, thread_id: str, reason: str = "") -> VoltGoResponse:
    """승인 대기 중인 thread 를 approve / reject 로 재개"""
    result = agent.invoke(resume_command(decision, reason), _config(thread_id), context=context)
    return assemble(result, context)
