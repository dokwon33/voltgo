"""
후보 선별 (설계서 2.2 후보 판정 / 2.5 정렬 기준)

  필요시간 = 가는 보행 + 체류 + 오는 보행
  통과     = 필요시간 <= 가용시간   (초 단위, 경계값 포함)
  정렬     = 저장 선호 일치 -> 여유 큰 순 -> 왕복 짧은 순 -> POI ID 순
"""
from datetime import datetime, timedelta
from typing import Optional

from voltgo.agent.schemas import CandidatePlan, Place, RoundTrip, TimeBudget
from voltgo.core.place_policy import dwell_sec_for

# 하나의 Place와 RoundTrip을 받아 복귀 가능한지 계산
def build_plan(place: Place, trip: RoundTrip, budget: TimeBudget, now: datetime,
               dwell_sec: int, version: int) -> Optional[CandidatePlan]:
    """시간 안에 들어오면 CandidatePlan, 아니면 None"""
    total_sec = trip.outbound_sec + dwell_sec + trip.inbound_sec
    available_sec = max(int((budget.return_deadline - now).total_seconds()), 0)
    if total_sec > available_sec:
        return None

    return_at = now + timedelta(seconds=total_sec)
    leave_by = budget.return_deadline - timedelta(seconds=trip.inbound_sec)
    slack_sec = int((budget.return_deadline - return_at).total_seconds())

    summary = (f"{place.name} 까지 도보 {trip.outbound_sec // 60}분 "
               f"({trip.outbound_m}m) → 체류 {dwell_sec // 60}분 → "
               f"차량까지 도보 {trip.inbound_sec // 60}분 ({trip.inbound_m}m)")

    return CandidatePlan(
        plan_id=place.poi_id,
        version=version,
        poi_id=place.poi_id,
        name=place.name,
        category=place.category,
        outbound_sec=trip.outbound_sec,
        inbound_sec=trip.inbound_sec,
        dwell_sec=dwell_sec,
        total_sec=total_sec,
        return_at=return_at,
        leave_by=leave_by,
        slack_sec=slack_sec,
        route_summary=summary[:300],
        opening_status=place.opening_status,
        poi_source=place.poi_source,
        route_source=trip.route_source,
        evaluated_at=now,
    )

# 여러 장소를 대상으로 build_plan()을 반복
def select_feasible_plans(places: dict[str, Place], routes: dict[str, RoundTrip],
                          budget: TimeBudget, now: datetime, version: int,
                          dwell_overrides: Optional[dict[str, int]] = None,
                          preferred_category: Optional[str] = None) -> list[CandidatePlan]:
    dwell_overrides = dwell_overrides or {}
    plans = []
    for poi_id, place in places.items():
        trip = routes.get(poi_id)
        if trip is None:
            continue                      # 양방향 경로 없는 후보는 채택 금지
        if place.opening_status == "closed":
            continue
        dwell = dwell_sec_for(place.category, dwell_overrides.get(poi_id))
        plan = build_plan(place, trip, budget, now, dwell, version)
        if plan is not None:
            plans.append(plan)

    plans.sort(key=lambda p: (
        0 if (preferred_category and p.category == preferred_category) else 1,
        -p.slack_sec,
        p.outbound_sec + p.inbound_sec,
        p.poi_id,
    ))
    return plans

# 승인 시점에 기존 계획을 현재 시각으로 다시 계산
def recheck_plan(plan: CandidatePlan, budget: TimeBudget, now: datetime) -> Optional[CandidatePlan]:
    """
    승인 직후 같은 계산을 다시 돌린다 (설계서 C006).
    시간이 지나 안 맞으면 None -> 확정하지 않고 재계획.
    """
    trip = RoundTrip(poi_id=plan.poi_id, outbound_sec=plan.outbound_sec, inbound_sec=plan.inbound_sec,
                     route_source=plan.route_source)
    place = Place(poi_id=plan.poi_id, name=plan.name, category=plan.category, latitude=0, longitude=0,
                  distance_m=0, opening_status=plan.opening_status, poi_source=plan.poi_source)
    return build_plan(place, trip, budget, now, plan.dwell_sec, plan.version)
