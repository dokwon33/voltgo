"""차량 목표는 대화로 변경할 수 없고, 실제 공급자의 새 조회값만 반영한다."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from voltgo.agent import tools
from voltgo.agent.agent import ask
from voltgo.core.time_budget import calculate_time_budget
from tests.test_approval import _ai, _tc, _plan_ready
from tests.test_decide_entrypoint import _agent, _done, _tool_results


def test_model_has_no_target_soc_argument():
    assert set(tools.calculate_time_budget.tool_call_schema.model_fields) == {"user_limit_min"}


@pytest.mark.parametrize("requested", [60, 100])
def test_chat_cannot_change_vehicle_target_across_turns(context, requested):
    agent = _agent([
        _ai([_tc("get_charging_status", {}, "charge")]),
        _ai([_tc("calculate_time_budget", {}, "budget")]), _done(),
        # 프롬프트를 무시하고 옛 목표 인자를 넣는 모델까지 방어한다.
        _ai([_tc("calculate_time_budget", {"target_soc_pct": requested}, "attempt")]), _done(),
        _ai([_tc("calculate_time_budget", {"user_limit_min": 10}, "limit")]), _done(),
    ])
    ask(agent, "충전 상태 확인해줘", context, "vehicle-target")
    s = context.session
    original_snapshot = s.charging.model_dump()
    original_finish = s.time_budget.finish_at
    original_deadline = s.time_budget.return_deadline

    ask(agent, f"목표 충전량을 {requested}%로 바꿔줘", context, "vehicle-target")
    assert _tool_results(agent, "vehicle-target", "calculate_time_budget")[-1]["status"] == "ok"
    assert s.charging.model_dump() == original_snapshot
    assert s.target_soc_pct == 80
    assert s.time_budget.finish_at == original_finish
    assert s.time_budget.return_deadline == original_deadline
    assert s.user_limit_min is None

    ask(agent, "외출은 10분 이내로 할게", context, "vehicle-target")
    assert s.charging.model_dump() == original_snapshot
    assert s.time_budget.finish_at == original_finish
    assert s.time_budget.return_deadline == context.clock() + timedelta(minutes=5)


@pytest.mark.parametrize("remaining", [None, 1800])
def test_core_uses_reported_target_even_with_legacy_override(snapshot, now, remaining):
    snap = snapshot.model_copy(update={"soc_pct": 70, "target_soc_pct": 60,
        "reported_target_soc_pct": 80, "reported_remaining_sec": remaining})
    budget, error = calculate_time_budget(snap, now)
    assert error is None  # 옛 60% 기준으로 TARGET_REACHED 처리하지 않는다.
    assert budget.finish_at == now + timedelta(seconds=remaining if remaining is not None else 450)
    assert snap.target_soc_pct == 60 and snap.reported_target_soc_pct == 80


def test_unknown_vehicle_target_requires_refresh(context, snapshot):
    context.session.charging = snapshot.model_copy(update={"target_soc_pct": None, "reported_target_soc_pct": None})
    result = tools.calculate_time_budget.func(SimpleNamespace(context=context))
    assert result["error_code"] == "NEED_INPUT"
    assert "force_refresh=True" in result["message"]
    assert context.session.target_soc_pct is None
    assert context.session.time_budget is None


def test_confirm_fetches_new_vehicle_target(context):
    version = _plan_ready(context)
    s = context.session
    updated = s.charging.model_copy(update={"target_soc_pct": 90,
        "reported_target_soc_pct": 90, "reported_remaining_sec": 3600})
    context.charging_provider = SimpleNamespace(get_charging_status=lambda: updated)
    result = tools.confirm_plan.func(SimpleNamespace(context=context), plan_id="A", version=version)
    assert result["status"] == "ok"
    assert s.charging.vehicle_target_soc_pct == 90
    assert s.target_soc_pct == 90
    assert s.time_budget.finish_at == context.clock() + timedelta(minutes=60)


@pytest.mark.parametrize("target", [0, 101, float("nan"), float("inf")])
def test_invalid_vehicle_target_is_not_used(snapshot, now, target):
    snap = snapshot.model_copy(update={"reported_target_soc_pct": target})
    assert calculate_time_budget(snap, now) == (None, "NEED_INPUT")
