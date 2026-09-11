"""실제 ask/decide + 도구 + HITL 경로 검증. 외부 API·모델은 호출하지 않는다."""
from datetime import timedelta
import json

import pytest
from langchain_core.messages import ToolMessage

from voltgo.agent import memory
from voltgo.agent.agent import ask, build_agent, decide
from voltgo.agent.approval import pending_approvals
from voltgo.agent.middleware import MAX_MODEL_CALLS
from tests.test_approval import ScriptedModel, _ai, _tc, _plan_ready


def _done(ids=None):
    return _ai([_tc("ModelDecision", {
        "candidate_ids": ids or [], "next_action": "done", "explanation": "처리했습니다."
    }, "final")])


def _agent(script):
    return build_agent(model=ScriptedModel(script=script))


def _save(category="cafe", tid="save"):
    return _tc("save_preferences", {"category": category}, tid)


def _config(thread_id):
    return {"configurable": {"thread_id": thread_id}}


def _tool_results(agent, thread_id, name):
    messages = agent.get_state(_config(thread_id)).values["messages"]
    return [json.loads(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == name]


def test_승인_없이_decide_하면_오류로_안내한다(context):
    r = decide(_agent([_done()]), "approve", context, "d-0")
    assert r.status == "error" and "NO_PENDING_APPROVAL" in r.message


def test_계획_선택은_추가_승인_없이_재검증_후_확정된다(context):
    v = _plan_ready(context)
    agent = _agent([_ai([_tc("confirm_plan", {"plan_id": "A", "version": v}, "t1")]), _done(["A"])])
    r = ask(agent, "A 계획으로 확정해줘", context, "plan")
    assert r.status == "confirmed"
    assert context.session.confirmed.plan_id == "A"
    assert not pending_approvals(agent, _config("plan"))
    assert context.session.approval_requested_at is None


def test_선호_승인시각은_interrupt_전에_기록되고_승인_후에만_저장된다(context):
    agent = _agent([_ai([_save()]), _done()])
    r = ask(agent, "카페 선호 기억해줘", context, "save")
    assert r.status == "awaiting_approval" and "cafe" in r.message
    assert context.session.approval_requested_at == context.clock()
    assert memory.load_preferences(context.user_id) is None
    decide(agent, "approve", context, "save")
    assert memory.load_preferences(context.user_id).preferred_category == "cafe"
    assert context.session.approval_requested_at is None
    # 이미 재개한 요청을 다시 승인해도 실행하지 않는다.
    r = decide(agent, "approve", context, "save")
    assert "NO_PENDING_APPROVAL" in r.message
    assert len(_tool_results(agent, "save", "save_preferences")) == 1


@pytest.mark.parametrize("seconds,expired", [(120, False), (121, True)])
def test_실제_승인_재개에서_120초_경계를_검증한다(context, seconds, expired):
    agent = _agent([_ai([_save()]), _done()])
    base = context.clock()
    ask(agent, "카페 기억해줘", context, "ttl")
    context.clock = lambda: base + timedelta(seconds=seconds)
    decide(agent, "approve", context, "ttl")
    result = _tool_results(agent, "ttl", "save_preferences")[0]
    if expired:
        assert result["error_code"] == "APPROVAL_EXPIRED"
        assert memory.load_preferences(context.user_id) is None
    else:
        assert result["status"] == "ok"
        assert memory.load_preferences(context.user_id).preferred_category == "cafe"
    assert context.session.approval_requested_at is None


def test_여러_저장_승인이_함께_만료돼도_모두_저장되지_않는다(context):
    agent = _agent([_ai([_save(tid="s1"), _save("meal", "s2")]), _done()])
    base = context.clock()
    ask(agent, "선호 기억해줘", context, "batch")
    context.clock = lambda: base + timedelta(seconds=121)
    decide(agent, "approve", context, "batch")
    results = _tool_results(agent, "batch", "save_preferences")
    assert len(results) == 2
    assert all(r["error_code"] == "APPROVAL_EXPIRED" for r in results)
    assert memory.load_preferences(context.user_id) is None


def test_계획과_선호가_함께_와도_저장_거절은_계획_확정을_막지_않는다(context):
    v = _plan_ready(context)
    agent = _agent([
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": v}, "t1"), _save()]), _done(["A"]),
    ])
    r = ask(agent, "A 확정하고 카페 기억해줘", context, "mixed")
    assert r.status == "awaiting_approval"
    requests = pending_approvals(agent, _config("mixed"))
    assert [r["name"] for r in requests] == ["save_preferences"]
    r = decide(agent, "reject", context, "mixed")
    assert r.status == "confirmed"
    assert context.session.confirmed.plan_id == "A"
    assert memory.load_preferences(context.user_id) is None
    assert context.session.approval_requested_at is None


def test_결정을_리스트로_주면_저장_요청_순서대로_적용된다(context):
    agent = _agent([_ai([_save(tid="s1"), _save("meal", "s2")]), _done()])
    ask(agent, "선호 기억해줘", context, "list")
    decide(agent, ["approve", "reject"], context, "list")
    assert memory.load_preferences(context.user_id).preferred_category == "cafe"


@pytest.mark.parametrize("decision,code", [(["approve", "reject"], "DECISION_COUNT_MISMATCH"), ("yes", "INVALID_DECISION")])
def test_잘못된_결정은_승인_대기를_유지한다(context, decision, code):
    agent = _agent([_ai([_save()]), _done()])
    ask(agent, "카페 기억해줘", context, "invalid")
    r = decide(agent, decision, context, "invalid")
    assert r.status == "error" and code in r.message
    assert memory.load_preferences(context.user_id) is None
    assert pending_approvals(agent, _config("invalid"))
    decide(agent, "approve", context, "invalid")
    assert memory.load_preferences(context.user_id) is not None


def test_거절_후_다시_요청하면_새_승인_시각으로_측정한다(context):
    agent = _agent([_ai([_save(tid="s1")]), _done(), _ai([_save(tid="s2")]), _done()])
    base = context.clock()
    ask(agent, "카페 기억해줘", context, "again")
    decide(agent, "reject", context, "again")
    assert memory.load_preferences(context.user_id) is None
    context.clock = lambda: base + timedelta(seconds=200)
    ask(agent, "다시 기억해줘", context, "again")
    assert context.session.approval_requested_at == context.clock()
    decide(agent, "approve", context, "again")
    assert memory.load_preferences(context.user_id).preferred_category == "cafe"


def test_시간이_지나_불가능해진_계획은_추가_승인_없이도_확정되지_않는다(context):
    v = _plan_ready(context)
    base = context.clock()
    # 후보 신선도 5분 이내여도, 사용자 마감이 가까우면 재검증에서 탈락한다.
    context.clock = lambda: base + timedelta(minutes=3)
    context.session.limit_said_at = base - timedelta(minutes=10)
    context.charging_provider.clock = context.clock
    agent = _agent([_ai([_tc("confirm_plan", {"plan_id": "A", "version": v}, "p")]), _done()])
    r = ask(agent, "A 확정해줘", context, "late")
    assert r.status != "awaiting_approval"
    assert context.session.confirmed is None
    assert _tool_results(agent, "late", "confirm_plan")[0]["error_code"] == "RECHECK_FAILED"


def test_재개_중_모델_한도에_걸리면_오류_응답으로_돌려준다(context):
    agent = _agent([_ai([_save()]), _done()])
    ask(agent, "카페 기억해줘", context, "budget")
    context.session.counters["model"] = MAX_MODEL_CALLS
    r = decide(agent, "approve", context, "budget")
    assert r.status == "error" and "LIMIT_EXCEEDED" in r.message


@pytest.mark.parametrize("start_together", [False, True], ids=["normal", "parallel-start"])
def test_복수_저장을_모두_승인하면_두_필드가_모두_남는다(context, monkeypatch, start_together):
    from threading import Barrier

    if start_together:
        # 갱신 진입을 동시에 시작한다. 읽기/쓰기 내부는 저장소가 직렬화해야 한다.
        barrier = Barrier(2, timeout=5)
        update = memory.update_preferences

        def concurrent_update(*args, **kwargs):
            barrier.wait()
            return update(*args, **kwargs)

        monkeypatch.setattr(memory, "update_preferences", concurrent_update)

    agent = _agent([
        _ai([_save(tid="category"),
             _tc("save_preferences", {"dwell_min": 15}, "dwell")]),
        _done(),
    ])
    response = ask(agent, "카페 선호와 체류 15분을 기억해줘", context, "both-fields")
    assert response.status == "awaiting_approval"
    assert memory.load_preferences(context.user_id) is None

    decide(agent, "approve", context, "both-fields")
    results = _tool_results(agent, "both-fields", "save_preferences")
    assert len(results) == 2
    assert all(result["status"] == "ok" for result in results), results
    record = memory.load_preferences(context.user_id)
    assert record.preferred_category == "cafe"
    assert record.dwell_min == 15
    assert record.consent_at == context.clock()
    assert context.session.approval_requested_at is None
