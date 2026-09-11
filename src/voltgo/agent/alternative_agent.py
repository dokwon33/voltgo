"""시간 부족 대체 활동용 read-only Tool과 최소 실행 guard."""
import json

from langchain.messages import AIMessage, ToolMessage
from langchain.tools import ToolRuntime, tool

from voltgo.agent.schemas import ToolResult
from voltgo.core.feasibility import build_plan
from voltgo.core.place_policy import DWELL_DEFAULT_MIN, dwell_sec_for

TOOL_NAME = "assess_time_shortage_alternatives"


def _payload(message) -> dict:
    if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
        return {}
    try:
        value = json.loads(message.content)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _latest_result(messages, name):
    for message in reversed(list(messages or [])):
        if isinstance(message, ToolMessage) and message.name == name:
            return _payload(message)
    return None


def _calls(messages, name):
    found = []
    for message in messages or []:
        if not isinstance(message, AIMessage):
            continue
        found.extend(call for call in message.tool_calls if call.get("name") == name)
    return found


def _last_marker(messages):
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, ToolMessage) and message.name == TOOL_NAME:
            data = _payload(message).get("data") or {}
            if data.get("reason") == "time_insufficient":
                return index, data
    return None


def _result(reason, **data):
    return ToolResult(status="ok", data={"reason": reason, **data}, message=f"reason={reason}").dump()


@tool
def assess_time_shortage_alternatives(runtime: ToolRuntime) -> dict:
    """장소·왕복 경로 조회는 정상이지만 모든 후보가 시간 때문에 탈락했는지 확인합니다.
    select_feasible_plans 결과가 빈 목록일 때만 호출하며 Session을 변경하지 않습니다.
    """
    s = runtime.context.session
    messages = runtime.state.get("messages", [])
    search = _latest_result(messages, "search_nearby_places")
    routes = _latest_result(messages, "get_walking_routes")
    selected = _latest_result(messages, "select_feasible_plans")
    if not (search and routes and selected):
        return _result("not_verified")
    if search.get("status") != "ok" or not search.get("data"):
        return _result("not_time_shortage")
    if routes.get("status") != "ok" or selected.get("status") != "ok" or selected.get("data") != []:
        return _result("not_time_shortage")
    if not s.selected_ran or s.candidates or not s.time_budget:
        return _result("not_time_shortage")

    eligible = {pid: place for pid, place in s.places.items() if place.opening_status != "closed"}
    if not eligible or set(s.routes) != set(eligible):
        return _result("not_time_shortage")
    now = runtime.context.clock()
    if any(build_plan(place, s.routes[pid], s.time_budget, now,
                      dwell_sec_for(place.category, s.dwell_overrides.get(pid)), s.condition_version)
           is not None for pid, place in eligible.items()):
        return _result("not_time_shortage")

    categories = {place.category for place in eligible.values()}
    if len(categories) != 1:
        return _result("not_time_shortage")
    original = next(iter(categories))
    alternatives = sorted(
        (category for category, minutes in DWELL_DEFAULT_MIN.items()
         if minutes < DWELL_DEFAULT_MIN[original]),
        key=lambda category: (DWELL_DEFAULT_MIN[category], category),
    )
    previous = _last_marker(messages)
    if previous:
        index, data = previous
        later = messages[index + 1:]
        recalculated = _latest_result(later, "calculate_time_budget") is not None
        searched = _calls(later, "search_nearby_places")
        latest_category = (searched[-1].get("args") or {}).get("category") if searched else None
        if not recalculated and latest_category in data.get("alternatives", []):
            return _result("alternative_exhausted", original_category=original, alternatives=[])
    if not alternatives:
        return _result("no_shorter_category", original_category=original, alternatives=[])
    defaults = {category: DWELL_DEFAULT_MIN[category] for category in alternatives}
    return _result("time_insufficient", original_category=original, alternatives=alternatives,
                   dwell_min_by_category=defaults)


def guard_alternative_search(messages, category):
    """대안 판정 뒤 새 사용자 turn 전 검색과 한 turn의 category 연쇄 변경만 차단한다."""
    messages = list(messages or [])
    marker = next((i for i in range(len(messages) - 1, -1, -1)
                   if isinstance(messages[i], ToolMessage) and messages[i].name == TOOL_NAME
                   and (_payload(messages[i]).get("data") or {}).get("reason")
                   in {"time_insufficient", "alternative_exhausted"}), None)
    if marker is None:
        return None
    later = messages[marker + 1:]
    human = next((i for i in range(len(later) - 1, -1, -1)
                  if getattr(later[i], "type", None) == "human"), None)
    if human is None:
        return "ALTERNATIVE_CONSENT_REQUIRED", "대체 활동 검색은 다음 사용자 응답 이후에만 가능합니다."
    current_turn = later[human + 1:]
    searches = _calls(current_turn, "search_nearby_places")
    if _latest_result(current_turn, "select_feasible_plans") is not None:
        return "ALTERNATIVE_CHAIN_BLOCKED", "대체 재계획 뒤에는 새 사용자 요청 없이 다시 검색할 수 없습니다."
    if searches and (searches[0].get("args") or {}).get("category") != category:
        return "ALTERNATIVE_CHAIN_BLOCKED", "한 번 선택한 category를 같은 turn에 자동 변경할 수 없습니다."
    return None
