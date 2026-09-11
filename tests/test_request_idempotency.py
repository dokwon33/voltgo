"""C017: 실제 승인 진입점에서 부수 효과와 재전송 결과를 검증한다."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import pytest
from langchain_core.messages import ToolMessage

from voltgo.agent import memory
from voltgo.agent.agent import ask, build_agent, decide
from voltgo.agent.requests import config_for, present_result
from voltgo.agent.state import Session
from tests.test_approval import ScriptedModel, _ai, _tc
from tests.test_decide_entrypoint import _plan_ready


def _finish():
    return _ai([_tc("ModelDecision", {
        "candidate_ids": [], "next_action": "done", "explanation": "처리했습니다."
    }, "final")])


def _save(tid="save", category="cafe"):
    return _ai([_tc("save_preferences", {"category": category}, tid)])


def _track_writes(monkeypatch):
    writes = []
    original = memory.save_preferences

    def save(record):
        writes.append(record.model_copy(deep=True))
        return original(record)

    monkeypatch.setattr(memory, "save_preferences", save)
    return writes


def test_same_request_reuses_saved_result_without_model_or_tool_calls(context, monkeypatch):
    writes = _track_writes(monkeypatch)
    agent = build_agent(model=ScriptedModel(script=[_save(), _finish()]))
    pending = ask(agent, "카페 기억해줘", context, "t1")
    assert pending.request_id
    calls = []
    invoke = agent.invoke

    def counted(*args, **kwargs):
        calls.append(1)
        return invoke(*args, **kwargs)

    monkeypatch.setattr(agent, "invoke", counted)
    first = decide(agent, "approve", context, "t1", request_id=pending.request_id)
    stored = memory.load_preferences(context.user_id)
    context.clock = lambda: stored.consent_at + timedelta(minutes=3)
    again = decide(agent, ["approve"], context, "t1", request_id=pending.request_id)
    assert first.model_dump() == again.model_dump()
    assert memory.load_preferences(context.user_id) == stored
    assert len(calls) == len(writes) == 1
    again.message = "client mutation"
    assert decide(agent, "approve", context, "t1", request_id=pending.request_id).message == first.message
    # 조건 변경 뒤 완료된 승인을 조회해도 현재 Session을 바꾸지 않는다.
    context.session.bump_version()
    assert decide(agent, "approve", context, "t1", request_id=pending.request_id) == first
    assert context.session.confirmed is None
    assert context.session.condition_version == 2
    assert len(calls) == len(writes) == 1

def test_same_request_different_decision_is_rejected_without_another_write(context, monkeypatch):
    writes = _track_writes(monkeypatch)
    agent = build_agent(model=ScriptedModel(script=[_save(), _finish()]))
    request = ask(agent, "카페 기억해줘", context, "t1")
    first = decide(agent, "approve", context, "t1", request_id=request.request_id)
    again = decide(agent, "approve", context, "t1", request_id=request.request_id)
    conflict = decide(agent, "reject", context, "t1", request_id=request.request_id)
    assert first == again
    assert len(writes) == 1
    assert "REQUEST_ID_CONFLICT" in conflict.message
    assert memory.load_preferences(context.user_id) == writes[0]


def test_invalid_decision_can_be_corrected_without_consuming_request(context):
    agent = build_agent(model=ScriptedModel(script=[_save(), _finish()]))
    request = ask(agent, "카페 기억해줘", context, "t1")
    invalid = decide(agent, "yes", context, "t1", request_id=request.request_id)
    assert "INVALID_DECISION" in invalid.message
    assert context.session.approval_requests[request.request_id].decision_payload is None
    response = decide(agent, "approve", context, "t1", request_id=request.request_id)
    assert response.status != "error"


def test_reject_reason_is_part_of_retry_payload(context, monkeypatch):
    writes = _track_writes(monkeypatch)
    agent = build_agent(model=ScriptedModel(script=[_save(), _finish()]))
    request = ask(agent, "카페 기억해줘", context, "t1")
    first = decide(agent, "reject", context, "t1", "저장 안 할게", request_id=request.request_id)
    assert decide(agent, "reject", context, "t1", "저장 안 할게", request_id=request.request_id) == first
    conflict = decide(agent, "reject", context, "t1", "다른 이유", request_id=request.request_id)
    assert "REQUEST_ID_CONFLICT" in conflict.message
    assert not writes


def test_different_users_with_same_thread_name_have_independent_requests(context, monkeypatch):
    writes = _track_writes(monkeypatch)
    other = replace(context, user_id="u2", session=Session(origin=context.session.origin))
    agent = build_agent(model=ScriptedModel(script=[_save("s1"), _save("s2", "meal"), _finish(), _finish()]))
    one = ask(agent, "카페 기억해줘", context, "same-name")
    two = ask(agent, "식사 기억해줘", other, "same-name")
    assert one.request_id != two.request_id
    wrong = decide(agent, "approve", other, "same-name", request_id=one.request_id)
    assert "UNKNOWN_REQUEST_ID" in wrong.message
    assert not writes
    decide(agent, "approve", context, "same-name", request_id=one.request_id)
    decide(agent, "approve", other, "same-name", request_id=two.request_id)
    assert [(r.user_id, r.preferred_category) for r in writes] == [("u1", "cafe"), ("u2", "meal")]


def test_session_cannot_be_reused_for_another_user_or_thread(context, monkeypatch):
    writes = _track_writes(monkeypatch)
    agent = build_agent(model=ScriptedModel(script=[_save(), _finish()]))
    request = ask(agent, "카페 기억해줘", context, "t1")
    wrong_thread = decide(agent, "approve", context, "t2", request_id=request.request_id)
    wrong_user = decide(agent, "approve", replace(context, user_id="u2"), "t1", request_id=request.request_id)
    assert "SESSION_SCOPE_MISMATCH" in wrong_thread.message
    assert "SESSION_SCOPE_MISMATCH" in wrong_user.message
    assert not wrong_user.candidates and not wrong_user.warnings
    assert not writes


def test_new_session_cannot_resume_old_checkpoint(context, monkeypatch):
    writes = _track_writes(monkeypatch)
    agent = build_agent(model=ScriptedModel(script=[_save(), _finish()]))
    request = ask(agent, "카페 기억해줘", context, "t1")
    empty_context = replace(context, session=Session())
    response = decide(agent, "approve", empty_context, "t1", request_id=request.request_id)
    assert "SESSION_NOT_RESTORED" in response.message
    assert not writes


def test_changed_pending_tool_payload_is_rejected(context, monkeypatch):
    writes = _track_writes(monkeypatch)
    agent = build_agent(model=ScriptedModel(script=[_save(), _finish()]))
    request = ask(agent, "카페 기억해줘", context, "t1")
    state = agent.get_state(config_for(context.user_id, "t1"))
    item = state.interrupts[0]
    value = deepcopy(item.value)
    value["action_requests"][0]["args"]["category"] = "meal"
    altered = state._replace(interrupts=(replace(item, value=value),))
    monkeypatch.setattr(agent, "get_state", lambda _: altered)
    response = decide(agent, "approve", context, "t1", request_id=request.request_id)
    assert "REQUEST_ID_CONFLICT" in response.message
    assert not writes


def test_mixed_plan_and_save_replay_does_not_repeat_either_write(context, monkeypatch):
    version = _plan_ready(context)
    writes = _track_writes(monkeypatch)
    agent = build_agent(model=ScriptedModel(script=[
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": version}, "confirm"),
             _tc("save_preferences", {"category": "cafe"}, "save")]), _finish()]))
    request = ask(agent, "확정하고 카페 기억해줘", context, "t1")
    first = decide(agent, "approve", context, "t1", request_id=request.request_id)
    confirmed = context.session.confirmed.model_copy(deep=True)
    assert context.session.approval_requests[request.request_id].action_count == 1
    again = decide(agent, ["approve"], context, "t1", request_id=request.request_id)
    assert first == again
    assert context.session.confirmed == confirmed
    assert len(writes) == 1


def test_new_interrupt_gets_new_id_and_old_replay_does_not_consume_it(context, monkeypatch):
    writes = _track_writes(monkeypatch)
    agent = build_agent(model=ScriptedModel(script=[_save("s1"), _save("s2", "meal"), _finish()]))
    first = ask(agent, "카페와 식사 선호 저장", context, "t1")
    second = decide(agent, "approve", context, "t1", request_id=first.request_id)
    assert second.status == "awaiting_approval"
    assert first.request_id != second.request_id
    assert decide(agent, "approve", context, "t1", request_id=first.request_id) == second
    assert len(writes) == 1
    decide(agent, "approve", context, "t1", request_id=second.request_id)
    assert len(writes) == 2


def test_new_approval_after_saved_preference_uses_its_own_start_time(context):
    base = context.clock()
    agent = build_agent(model=ScriptedModel(script=[
        _save("first"), _finish(), _save("second", "meal"), _finish()]))
    saved = ask(agent, "카페 기억해줘", context, "t1")
    decide(agent, "approve", context, "t1", request_id=saved.request_id)
    assert context.session.approval_requested_at is None
    later = base + timedelta(minutes=3)
    context.clock = lambda: later
    pending = ask(agent, "식사 선호로 바꿔서 기억해줘", context, "t1")
    assert pending.request_id != saved.request_id
    assert context.session.approval_requested_at == later
    response = decide(agent, "approve", context, "t1", request_id=pending.request_id)
    assert response.status != "error"
    assert memory.load_preferences(context.user_id).preferred_category == "meal"
    assert context.session.approval_requested_at is None

def test_redisplaying_same_interrupt_does_not_extend_approval_deadline(context):
    agent = build_agent(model=ScriptedModel(script=[_save(), _finish()]))
    first = ask(agent, "카페 기억해줘", context, "t1")
    requested_at = context.session.approval_requested_at
    context.clock = lambda: requested_at + timedelta(seconds=121)
    state = agent.get_state(config_for(context.user_id, "t1"))
    again = present_result({"__interrupt__": state.interrupts}, context)
    assert first.request_id == again.request_id
    assert context.session.approval_requested_at == requested_at


def test_failure_after_write_is_cached_instead_of_retrying_write(context, monkeypatch):
    writes = _track_writes(monkeypatch)

    class FailingModel(ScriptedModel):
        def _generate(self, *args, **kwargs):
            if self.idx > 0:
                raise RuntimeError("failure after preference was saved")
            return super()._generate(*args, **kwargs)

    agent = build_agent(model=FailingModel(script=[_save()]))
    request = ask(agent, "카페 기억해줘", context, "t1")
    first = decide(agent, "approve", context, "t1", request_id=request.request_id)
    assert first.status == "error"
    assert "APPROVAL_EXECUTION_FAILED" in first.message
    assert len(writes) == 1
    assert decide(agent, "approve", context, "t1", request_id=request.request_id) == first
    assert len(writes) == 1



def test_multiple_preference_fields_survive_batch_approval_and_replay(context, monkeypatch):
    writes = _track_writes(monkeypatch)
    agent = build_agent(model=ScriptedModel(script=[
        _ai([_tc("save_preferences", {"category": "cafe"}, "category"),
             _tc("save_preferences", {"dwell_min": 15}, "dwell")]), _finish()]))
    pending = ask(agent, "카페와 체류 15분을 기억해줘", context, "batch")
    assert context.session.approval_requests[pending.request_id].action_count == 2
    assert not writes
    first = decide(agent, "approve", context, "batch", request_id=pending.request_id)
    record = memory.load_preferences(context.user_id)
    assert (record.preferred_category, record.dwell_min) == ("cafe", 15)
    counters = dict(context.session.counters)
    replay = decide(agent, ["approve", "approve"], context, "batch", request_id=pending.request_id)
    assert replay == first
    assert memory.load_preferences(context.user_id) == record
    assert len(writes) == 2
    assert context.session.counters == counters


def test_delayed_result_delivery_does_not_extend_approval_deadline(context, monkeypatch):
    base = context.clock()
    agent = build_agent(model=ScriptedModel(script=[_save(), _finish()]))
    invoke = agent.invoke

    def delayed_delivery(*args, **kwargs):
        result = invoke(*args, **kwargs)
        context.clock = lambda: base + timedelta(seconds=10)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(agent, "invoke", delayed_delivery)
        pending = ask(agent, "카페 기억해줘", context, "delivery")
    assert context.session.approval_requests[pending.request_id].requested_at == base
    context.clock = lambda: base + timedelta(seconds=121)
    response = decide(agent, "approve", context, "delivery", request_id=pending.request_id)
    assert memory.load_preferences(context.user_id) is None
    messages = agent.get_state(config_for(context.user_id, "delivery")).values["messages"]
    result = next(json.loads(m.content) for m in messages
                  if isinstance(m, ToolMessage) and m.name == "save_preferences")
    assert result["error_code"] == "APPROVAL_EXPIRED"
    assert decide(agent, "approve", context, "delivery", request_id=pending.request_id) == response
    assert memory.load_preferences(context.user_id) is None


@pytest.mark.parametrize("seconds,expired", [(120, False), (121, True)])
def test_followup_approval_during_resume_has_its_own_ttl(context, monkeypatch, seconds, expired):
    base = context.clock()
    second_start = base + timedelta(seconds=100)
    writes = _track_writes(monkeypatch)

    class AdvancingModel(ScriptedModel):
        def _generate(self, *args, **kwargs):
            if self.idx == 1:
                context.clock = lambda: second_start
            return super()._generate(*args, **kwargs)

    agent = build_agent(model=AdvancingModel(script=[_save("s1"), _save("s2", "meal"), _finish()]))
    first = ask(agent, "선호를 차례로 저장해줘", context, "followup")
    context.clock = lambda: base + timedelta(seconds=90)
    second = decide(agent, "approve", context, "followup", request_id=first.request_id)
    assert second.status == "awaiting_approval"
    assert second.request_id != first.request_id
    assert context.session.approval_requested_at == second_start
    assert context.session.approval_requests[second.request_id].requested_at == second_start
    assert len(writes) == 1
    context.clock = lambda: second_start + timedelta(seconds=seconds)
    # 이전 승인의 재전송은 새 승인을 실행하거나 만료 시각을 늘리지 않는다.
    assert decide(agent, "approve", context, "followup", request_id=first.request_id) == second
    assert context.session.approval_requested_at == second_start
    final = decide(agent, "approve", context, "followup", request_id=second.request_id)
    messages = agent.get_state(config_for(context.user_id, "followup")).values["messages"]
    result = [json.loads(m.content) for m in messages
              if isinstance(m, ToolMessage) and m.name == "save_preferences"][-1]
    if expired:
        assert result["error_code"] == "APPROVAL_EXPIRED"
        assert memory.load_preferences(context.user_id).preferred_category == "cafe"
        assert len(writes) == 1
    else:
        assert result["status"] == "ok"
        assert memory.load_preferences(context.user_id).preferred_category == "meal"
        assert len(writes) == 2
    assert context.session.approval_requested_at is None
    assert decide(agent, "approve", context, "followup", request_id=second.request_id) == final



def test_condition_change_in_same_batch_cannot_bypass_save_expiry(context, monkeypatch):
    from threading import Event
    from voltgo.agent import tools

    _plan_ready(context)
    changed = Event()
    calculate = tools.calculate_time_budget.func
    save = tools.save_preferences.func

    def change_first(*args, **kwargs):
        try:
            return calculate(*args, **kwargs)
        finally:
            changed.set()

    def save_after_change(*args, **kwargs):
        assert changed.wait(timeout=5), "조건 변경 도구가 실행되지 않았다"
        return save(*args, **kwargs)

    monkeypatch.setattr(tools.calculate_time_budget, "func", change_first)
    monkeypatch.setattr(tools.save_preferences, "func", save_after_change)
    agent = build_agent(model=ScriptedModel(script=[
        _ai([_tc("calculate_time_budget", {"target_soc_pct": 80, "user_limit_min": 20}, "change"),
             _tc("save_preferences", {"category": "cafe"}, "save")]), _finish()]))
    base = context.clock()
    pending = ask(agent, "제한을 20분으로 바꾸고 카페를 기억해줘", context, "change-save")
    version = context.session.condition_version
    context.clock = lambda: base + timedelta(seconds=121)
    decide(agent, "approve", context, "change-save", request_id=pending.request_id)
    assert context.session.condition_version == version + 1
    messages = agent.get_state(config_for(context.user_id, "change-save")).values["messages"]
    result = next(json.loads(m.content) for m in messages
                  if isinstance(m, ToolMessage) and m.name == "save_preferences")
    assert result["error_code"] == "APPROVAL_EXPIRED"
    assert memory.load_preferences(context.user_id) is None
