"""
Runtime Context 와 대화 State (설계서 3.1)

실습 노트북 [5] 의 @dataclass Context 패턴을 그대로 쓴다.
- Context : 호출할 때 넘기는 값 (user_id, 공급자, clock ...). 모델이 바꿀 수 없다.
- Session : 한 thread 안에서 Tool 들이 공유하는 상태. Context 안에 넣어서 같이 넘긴다.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from voltgo.agent.schemas import (
    CandidatePlan, ChargingSnapshot, ConfirmedPlan, Origin, Place, RoundTrip, StationCandidate, TimeBudget, VoltGoResponse,
)

# KST는 UTC+9  (노트북 [4] get_current_time 과 동일)
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(KST)


@dataclass
class ApprovalRequest:
    """발급 당시 승인 대상과 재전송 결과. LLM 입력이나 장기 선호에 저장하지 않는다."""
    payload: str
    interrupt_ids: tuple[str, ...]
    action_count: int
    requested_at: datetime
    prompt_response: Optional[VoltGoResponse] = None  # 새로고침 시 현재 승인 화면 복원
    decision_payload: Optional[str] = None
    response: Optional[VoltGoResponse] = None


@dataclass
class Session:
    """Tool 이 기록하고 다음 Tool 과 출력 조립기가 읽는 값들"""
    charging: Optional[ChargingSnapshot] = None
    origin: Optional[Origin] = None
    station_candidates: dict[str, StationCandidate] = field(default_factory=dict)  # poi_id -> 선택 대기 중인 충전소 후보
    time_budget: Optional[TimeBudget] = None
    places: dict[str, Place] = field(default_factory=dict)        # poi_id -> Place
    # (위도, 경도, 카테고리, radius_km) -> TMAP 이 준 필터 전 목록. 반경만 바꿔 재검색하면 API 를 다시 부르지 않는다
    place_cache: dict[tuple, list[Place]] = field(default_factory=dict)
    routes: dict[str, RoundTrip] = field(default_factory=dict)    # poi_id -> RoundTrip
    candidates: dict[str, CandidatePlan] = field(default_factory=dict)  # plan_id -> CandidatePlan
    selected_ran: bool = False          # select_feasible_plans 를 한 번이라도 돌렸는지

    # 마지막 계산에 사용한 차량 목표. 공급자 갱신에 따른 조건 변경 감지용이며 사용자 입력이 아니다.
    target_soc_pct: Optional[float] = None
    # 사용자 조건
    user_limit_min: Optional[int] = None
    limit_said_at: Optional[datetime] = None
    dwell_overrides: dict[str, int] = field(default_factory=dict)
    condition_version: int = 1          # 계획 조건이 바뀌면 +1, 이전 후보/확정은 무효

    # 확정 / 멱등
    confirmed: Optional[ConfirmedPlan] = None
    confirmed_by_plan: dict[str, ConfirmedPlan] = field(default_factory=dict)
    # 선호 저장 승인 요청을 사용자에게 보여준 시각. 후보를 만든 시각(evaluated_at)과 다르다.
    approval_requested_at: Optional[datetime] = None

    # 실행 래퍼가 관리하는 승인 요청. 조건 변경 후에도 완료 응답은 재전송에 사용한다.
    request_owner: Optional[tuple[str, str]] = None
    pending_request_id: Optional[str] = None
    approval_requests: dict[str, ApprovalRequest] = field(default_factory=dict)

    # 한도/기록
    counters: dict[str, int] = field(default_factory=lambda: {"model": 0, "tool": 0, "api": 0})
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def reset_counters(self):
        self.counters = {"model": 0, "tool": 0, "api": 0}

    def bump_version(self):
        # 계획 조건 변경 -> 새 버전. 이전 후보와 확정은 버린다 (설계서 C013)
        self.condition_version += 1
        self.candidates = {}
        self.confirmed = None
        self.confirmed_by_plan = {}   # 옛 버전의 확정 기록도 같이 버린다
        # 선호 저장 승인은 계획 조건과 독립적이다. 시각은 요청 완료 때 실행 래퍼가 정리한다.


@dataclass
class Context:
    user_id: str
    vehicle_ref: Optional[str] = None
    demo_mode: bool = True
    buffer_min: int = 5
    timezone: str = "Asia/Seoul"
    clock: Callable[[], datetime] = now_kst      # 테스트에서는 고정 시계를 넣는다
    charging_provider: Any = None                # get_charging_status() 를 가진 객체
    places_client: Any = None                    # search_around() / find_station()
    routes_client: Any = None                    # pedestrian()
    session: Session = field(default_factory=Session)
