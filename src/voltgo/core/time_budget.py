"""
시간 예산 계산 (설계서 2.2) - 순수 함수만. API 나 LLM 은 모르는 모듈.

  남은 에너지(kWh)   = max(목표 SoC - 현재 SoC, 0) / 100 * 배터리 용량
  남은 충전시간(분)  = 남은 에너지 / 평균 전력 * 60
  충전 완료 시각     = 데이터 기준 시각 + 잔여시간
  사용자 제한 시각   = 제한을 말한 시각 + 허용 시간
  복귀 마감          = min(충전 완료, 사용자 제한) - 버퍼
  가용시간           = max(복귀 마감 - 지금, 0)     (초 단위)
"""
import math
from datetime import datetime, timedelta
from typing import Optional

from voltgo.agent.schemas import ChargingSnapshot, TimeBudget

STALE_AFTER_SEC = 60   # 충전값은 60초 안에서만 재사용 (설계서 1.5 성능)


def estimate_remaining_sec(soc_pct, target_soc_pct, capacity_kwh, avg_power_kw) -> Optional[int]:
    """단순 추정식. 값이 하나라도 없거나 전력이 0이면 None (0 으로 나누지 않는다)"""
    values = [soc_pct, target_soc_pct, capacity_kwh, avg_power_kw]
    if any(v is None for v in values):
        return None
    if any(not math.isfinite(v) for v in values):
        return None
    if capacity_kwh <= 0 or avg_power_kw <= 0:
        return None

    remaining_kwh = max(target_soc_pct - soc_pct, 0) / 100 * capacity_kwh
    remaining_min = remaining_kwh / avg_power_kw * 60
    return int(round(remaining_min * 60))


def calculate_time_budget(
    snapshot: ChargingSnapshot,
    now: datetime,
    buffer_min: int = 5,
    user_limit_min: Optional[int] = None,
    limit_said_at: Optional[datetime] = None,
) -> tuple[Optional[TimeBudget], Optional[str]]:
    """(TimeBudget, None) 또는 (None, 오류코드) 를 돌려준다."""

    # 1. 충전 중이 아니거나 이미 목표에 도달 -> 외출 계획 없음 (C003)
    if snapshot.charging is False:
        return None, "NOT_CHARGING"
    if snapshot.soc_pct is not None and snapshot.soc_pct >= snapshot.target_soc_pct:
        return None, "TARGET_REACHED"

    # 2. 너무 오래된 값이면 다시 조회하라고 알려준다
    # fetched_at(우리가 조회한 시각) 있으면 그걸로, 없으면 observed_at(차량 전송 시각)로 대체
    freshness_ref = snapshot.fetched_at or snapshot.observed_at
    if (now - freshness_ref).total_seconds() > STALE_AFTER_SEC:
        return None, "STALE_DATA"

    # 3. 잔여시간: API 가 준 값 우선, 없으면 단순 추정
    if snapshot.reported_remaining_sec is not None and snapshot.charging:
        remaining_sec = snapshot.reported_remaining_sec
        basis = "reported_remaining"
    else:
        remaining_sec = estimate_remaining_sec(
            snapshot.soc_pct, snapshot.target_soc_pct, snapshot.capacity_kwh, snapshot.avg_power_kw
        )
        basis = "energy_power"
        if remaining_sec is None:
            return None, "NEED_INPUT"     # C004: 값을 만들어내지 않고 질문

    finish_at = snapshot.observed_at + timedelta(seconds=remaining_sec)

    # 4. 사용자가 "30분" 이라고 말했으면 그것도 마감 후보
    user_limit_at = None
    if user_limit_min is not None:
        user_limit_at = (limit_said_at or now) + timedelta(minutes=user_limit_min)

    base_deadline = finish_at if user_limit_at is None else min(finish_at, user_limit_at)
    return_deadline = base_deadline - timedelta(minutes=buffer_min)
    available_sec = max(int((return_deadline - now).total_seconds()), 0)

    budget = TimeBudget(
        computed_at=now,
        finish_at=finish_at,
        user_limit_at=user_limit_at,
        return_deadline=return_deadline,
        buffer_min=buffer_min,
        available_sec=available_sec,
        estimate_basis=basis,
    )
    return budget, None
