# 담당 B - 시간 예산 (C001, C003, C004)
from datetime import timedelta

from voltgo.core.time_budget import calculate_time_budget, estimate_remaining_sec


def test_c001_simple_estimate(snapshot, now):
    # 24kWh / 48kW = 30분 -> 완료 14:30, 사용자 제한 30분도 14:30, 버퍼 5분 -> 마감 14:25
    budget, err = calculate_time_budget(snapshot, now, buffer_min=5, user_limit_min=30, limit_said_at=now)
    assert err is None
    assert budget.finish_at == now + timedelta(minutes=30)
    assert budget.return_deadline == now + timedelta(minutes=25)
    assert budget.available_sec == 1500
    assert budget.estimate_basis == "energy_power"


def test_reported_remaining_wins(snapshot, now):
    # API 가 잔여시간을 주면 추정식보다 우선
    snap = snapshot.model_copy(update={"reported_remaining_sec": 40 * 60})
    budget, err = calculate_time_budget(snap, now)
    assert err is None
    assert budget.estimate_basis == "reported_remaining"
    assert budget.finish_at == now + timedelta(minutes=40)


def test_user_limit_earlier_than_finish(snapshot, now):
    # 사용자가 "10분" 이라고 하면 그게 더 이른 마감
    budget, _ = calculate_time_budget(snapshot, now, user_limit_min=10, limit_said_at=now)
    assert budget.return_deadline == now + timedelta(minutes=5)
    assert budget.available_sec == 300


def test_c003_not_charging(snapshot, now):
    snap = snapshot.model_copy(update={"charging": False})
    assert calculate_time_budget(snap, now) == (None, "NOT_CHARGING")

    snap = snapshot.model_copy(update={"soc_pct": 80})
    assert calculate_time_budget(snap, now) == (None, "TARGET_REACHED")


def test_c004_missing_values_need_input(snapshot, now):
    # 전력 0 / 용량 없음 -> 0 으로 나누지 않고 NEED_INPUT
    snap = snapshot.model_copy(update={"avg_power_kw": 0})
    assert calculate_time_budget(snap, now) == (None, "NEED_INPUT")
    snap = snapshot.model_copy(update={"capacity_kwh": None})
    assert calculate_time_budget(snap, now) == (None, "NEED_INPUT")
    assert estimate_remaining_sec(40, 80, 60, 0) is None


def test_missing_target_needs_input(snapshot, now):
    # 차량 목표가 없으면 비교/추정 없이 재조회. 사용자 대화로 보충하지 않는다.
    snap = snapshot.model_copy(update={"target_soc_pct": None})
    assert calculate_time_budget(snap, now) == (None, "NEED_INPUT")


def test_stale_snapshot(snapshot, now):
    # 61초 지난 값은 다시 조회하라고 한다
    assert calculate_time_budget(snapshot, now + timedelta(seconds=61)) == (None, "STALE_DATA")


def test_available_never_negative(snapshot, now):
    # 이미 마감이 지났으면 가용 0초 (음수 금지)
    later = now + timedelta(seconds=59)
    budget, _ = calculate_time_budget(snapshot, later, user_limit_min=1, limit_said_at=now - timedelta(minutes=10))
    assert budget.available_sec == 0
