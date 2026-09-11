"""
Middleware / Guardrails (설계서 3.2, 3.3)

노트북 [5] 2-2 custom middleware 패턴 그대로.
  before_agent   : InputValidation  - 입력 길이·비밀값 검사, 카운터 초기화
  wrap_model_call: PreferencePrompt - 저장된 선호를 system prompt 뒤에 붙임
  wrap_model_call: ModelBudget      - 모델 호출 8회 제한, 일시 오류 1회 재시도
  wrap_tool_call : ToolPolicy       - 허용 도구·호출 한도 검사 + 읽기 도구 1회 재시도 (ToolResilience)
  after_agent    : OutputIntegrity  - candidate_ids 가 통과 후보 안에 있는지 검사
HITL 은 approval.py 에서 Built-in HumanInTheLoopMiddleware 로 붙인다.
"""
import json
import re

from langchain.agents.middleware import after_agent, before_agent, wrap_model_call, wrap_tool_call
from langchain.messages import AIMessage, SystemMessage, ToolMessage

from voltgo.agent import memory
from voltgo.agent.alternative_agent import guard_alternative_search
from voltgo.agent.prompts import preference_note
from voltgo.agent.schemas import tool_error
from voltgo.agent.tools import READ_TOOLS, TOOLS

MAX_INPUT_CHARS = 500
MAX_MODEL_CALLS = 8      # 설계서 2.5 시간 및 호출 상한
MAX_TOOL_CALLS = 12
MAX_API_CALLS = 24

ALLOWED_TOOLS = {t.name for t in TOOLS}
SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")   # OpenAI 키 형태 (노트북 [5] PII 예시)


def _text(message) -> str:
    c = message.content
    if isinstance(c, str):
        return c
    return " ".join(p.get("text", "") for p in c if isinstance(p, dict))


# ---------------------------------------------------------------
# before_agent : 입력 검사
# ---------------------------------------------------------------
@before_agent(can_jump_to=["end"])
def input_validation(state, runtime):
    s = runtime.context.session
    s.reset_counters()
    s.errors = []

    if not state["messages"]:
        return None
    last = state["messages"][-1]
    if last.type != "human":
        return None                      # HITL 재개 등 사람 입력이 아닌 경우
    text = _text(last)

    if not text.strip():
        return {"messages": [AIMessage(content="무엇을 도와드릴까요? 예: '30분 안에 간단히 밥 먹고 싶어'")],
                "jump_to": "end"}
    if len(text) > MAX_INPUT_CHARS:
        return {"messages": [AIMessage(content=f"입력이 너무 깁니다 ({MAX_INPUT_CHARS}자 이하로 줄여주세요).")],
                "jump_to": "end"}
    if SECRET_PATTERN.search(text):
        print("### input_validation : API 키 형태 감지 - 차단")
        return {"messages": [AIMessage(content="API 키 같은 비밀값은 입력하지 마세요. 요청만 다시 적어주세요.")],
                "jump_to": "end"}
    return None


# ---------------------------------------------------------------
# wrap_model_call : 선호 주입 + 모델 예산
# ---------------------------------------------------------------
@wrap_model_call
def preference_prompt(request, handler):
    # 노트북 [5] inject_memory 와 같은 방식. 충전 사실은 여기서 만들지 않는다.
    pref = memory.load_preferences(request.runtime.context.user_id)
    note = preference_note(pref)
    if not note:
        return handler(request)
    base = request.system_message.content if request.system_message else ""
    return handler(request.override(system_message=SystemMessage(base + note)))


RETRYABLE_ERRORS = ("APITimeoutError", "APIConnectionError", "RateLimitError")


@wrap_model_call
def model_budget(request, handler):
    s = request.runtime.context.session
    s.counters["model"] += 1
    n = s.counters["model"]
    print(f"### model_budget : 모델 호출 {n}/{MAX_MODEL_CALLS}")
    if n > MAX_MODEL_CALLS:
        raise RuntimeError("MODEL_BUDGET_EXCEEDED")

    try:
        return handler(request)
    except Exception as e:
        if type(e).__name__ in RETRYABLE_ERRORS:
            # 재시도도 실제 모델 호출이다. 남은 여력이 없으면 한도를 넘기지 않는다.
            if s.counters["model"] + 1 > MAX_MODEL_CALLS:
                print(f"### model_budget : 일시 오류({type(e).__name__}) - 재시도 여력 없음")
                raise RuntimeError("MODEL_BUDGET_EXCEEDED")
            print(f"### model_budget : 일시 오류({type(e).__name__}) - 1회 재시도")
            s.counters["model"] += 1
            return handler(request)
        raise


# ---------------------------------------------------------------
# wrap_tool_call : 도구 정책 + 복원력
# ---------------------------------------------------------------
def _blocked(request, code, msg):
    return ToolMessage(content=json.dumps(tool_error(code, msg), ensure_ascii=False),
                       tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error")


def _payload(message):
    try:
        return json.loads(message.content) if isinstance(message.content, str) else {}
    except ValueError:
        return {}


@wrap_tool_call
def tool_policy(request, handler):
    name = request.tool_call["name"]
    args = request.tool_call["args"]
    s = request.runtime.context.session
    s.counters["tool"] += 1
    print(f"### tool_policy : [{s.counters['tool']}] {name}({args})")

    # 1. 허용 목록 + 한도
    if name not in ALLOWED_TOOLS:
        return _blocked(request, "TOOL_NOT_ALLOWED", f"{name} 은 허용되지 않은 도구")
    if s.counters["tool"] > MAX_TOOL_CALLS:
        return _blocked(request, "LIMIT_EXCEEDED", "Tool 호출 한도(12회) 초과. 확인된 사실만 안내하세요.")
    if name in READ_TOOLS and s.counters["api"] >= MAX_API_CALLS:
        return _blocked(request, "LIMIT_EXCEEDED", "외부 API 호출 한도 초과")

    # 평소 검색에는 영향이 없고, 대체 활동 판정 ToolMessage가 있는 동안만 실행 시점을 제한한다.
    if name == "search_nearby_places":
        blocked = guard_alternative_search(request.state.get("messages", []), args.get("category"))
        if blocked:
            return _blocked(request, *blocked)

    # 2. 실행. 선행 조건 검사는 각 도구 안에서 PRECONDITION_FAILED 로 처리한다.
    result = handler(request)

    # 3. 읽기 도구가 일시 오류(timeout/5xx/429)면 딱 1회만 더 시도
    if name in READ_TOOLS and isinstance(result, ToolMessage):
        p = _payload(result)
        if p.get("status") == "error" and p.get("retryable"):
            print(f"### tool_policy : {name} 일시 오류 - 1회 재시도")
            result = handler(request)
    return result


# ---------------------------------------------------------------
# after_agent : 출력 근거 검사
# ---------------------------------------------------------------
@after_agent
def output_integrity(state, runtime):
    decision = state.get("structured_response")
    if decision is None:
        return None
    s = runtime.context.session
    valid = []
    for cid in decision.candidate_ids:
        if cid in s.candidates and cid not in valid:
            valid.append(cid)
    if valid != list(decision.candidate_ids):
        print(f"### output_integrity : 근거 없는 후보 제거 {decision.candidate_ids} -> {valid}")
        decision.candidate_ids = valid
        return {"structured_response": decision}
    return None
