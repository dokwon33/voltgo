# 담당 A - 출력 조립 (C011 출처 표시, C012 허위 ID 제거)
from datetime import timedelta
from types import SimpleNamespace

import pytest

from voltgo.agent import tools
from voltgo.agent.assembler import assemble
from voltgo.agent.schemas import ModelDecision


def _run_pipeline(context):
    r = SimpleNamespace(context=context)
    tools.get_charging_status.func(r)
    tools.calculate_time_budget.func(r, user_limit_min=30)
    tools.search_nearby_places.func(r, category="meal")
    tools.get_walking_routes.func(r, poi_ids=["A", "B", "C"])
    tools.select_feasible_plans.func(r, dwell_min=12)


def test_c012_unknown_candidate_dropped(context):
    _run_pipeline(context)
    decision = ModelDecision(candidate_ids=["A", "X"], next_action="choose", explanation="A 추천")
    resp = assemble({"structured_response": decision, "messages": []}, context)
    assert resp.status == "ok"
    assert [c.plan_id for c in resp.candidates] == ["A"]
    assert "14:25" in resp.message and "후보A" not in resp.message   # 이름은 fixture 값(김밥천국) 으로
    assert "김밥천국" in resp.message


def test_c011_mock_sources_are_visible(context):
    _run_pipeline(context)
    decision = ModelDecision(candidate_ids=["A"], next_action="choose", explanation="")
    resp = assemble({"structured_response": decision, "messages": []}, context)
    assert resp.charging_source == "mock"
    assert resp.location_source == "mock"
    assert any("Mock" in w for w in resp.warnings)


def test_clarify_gives_missing_fields(context):
    decision = ModelDecision(candidate_ids=[], next_action="clarify", explanation="어디서 충전 중인가요?")
    resp = assemble({"structured_response": decision, "messages": []}, context)
    assert resp.status == "need_input"
    assert resp.missing_fields == ["charging"]


def test_interrupt_becomes_awaiting_approval(context):
    _run_pipeline(context)
    interrupt = SimpleNamespace(value={"action_requests": [{"name": "save_preferences", "args": {"category": "cafe"}, "description": "선호를 저장할까요? (category=cafe)"}]})
    resp = assemble({"__interrupt__": [interrupt], "messages": []}, context)
    assert resp.status == "awaiting_approval"
    assert resp.candidates == []
    assert "cafe" in resp.message
    assert "approve" in resp.message


@pytest.mark.parametrize(("elapsed_min", "expected_min"), [(5, 20), (30, 0)])
def test_available_time_display_uses_current_time_and_never_goes_negative(context, elapsed_min, expected_min):
    _run_pipeline(context)
    computed_at = context.session.time_budget.computed_at
    context.clock = lambda: computed_at + timedelta(minutes=elapsed_min)
    decision = ModelDecision(candidate_ids=[], next_action="clarify", explanation="확인 중입니다.")

    resp = assemble({"structured_response": decision, "messages": []}, context)

    assert f"가용 {expected_min}분" in resp.message
