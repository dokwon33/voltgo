"""
VoltGo 데이터 계약 (설계서 2.4)

- 모델(LLM)이 만드는 건 ModelDecision 하나뿐이다.
- 나머지 숫자/시각/장소는 전부 코드가 채운다. (Tool 결과 -> State -> VoltGoResponse)
"""
from datetime import datetime
from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, Field

# 장소 카테고리 (설계서 2.4 CandidatePlan.category)
Category = Literal["meal", "cafe", "convenience", "mart"]


# ---------------------------------------------------------------
# 내부 데이터 계약 (Tool 사이에서 주고받는 값)
# ---------------------------------------------------------------
class Origin(BaseModel):
    """출발지(충전소) 좌표. 현대차 API에 위치가 없어서 manual / mock 만 있다."""
    latitude: float
    longitude: float
    name: str = "충전소"
    source: Literal["manual", "mock", "unknown"] = "manual"


class StationCandidate(Origin):
    """find_station 검색 후보 (설계서 2.5 ToolResult[list[StationCandidate]]).
    Origin 을 상속하므로 사용자가 고른 후보를 그대로 session.origin 에 넣는다."""
    poi_id: str
    address: str = ""                         # 후보가 여러 개일 때 이름과 같이 보여준다
    nav_seq: str = ""                         # 같은 POI 의 입구 구분


class ChargingSnapshot(BaseModel):
    """충전 상태 1건. 현대차 원문 필드는 clients/hyundai.py 어댑터에서 여기로 변환한다."""
    charging: Optional[bool] = None
    soc_pct: Optional[float] = None
    target_soc_pct: Optional[float] = None    # 계산에 실제로 쓸 목표 (API가 안 주면 None, 2.5.2)
    reported_target_soc_pct: Optional[float] = None  # API 원문 목표값 그대로 보관, 계산 경로에서 덮어쓰지 않음
    capacity_kwh: Optional[float] = None      # API 미제공 -> 정책값/사용자 입력
    capacity_source: Literal["manual", "mock", "unknown"] = "unknown"  # 값의 출처, source(전체 스냅샷)와 별개
    avg_power_kw: Optional[float] = None      # API 미제공 -> 정책값/사용자 입력
    avg_power_source: Literal["manual", "mock", "unknown"] = "unknown"  # 값의 출처, source(전체 스냅샷)와 별개
    reported_remaining_sec: Optional[int] = None  # remainTime 을 초로 변환한 값
    plug_type: Literal["fast", "slow", "none"] = "none"
    observed_at: datetime                     # 차량이 보낸 시각 (timestamp)
    fetched_at: Optional[datetime] = None     # 우리가 실제로 조회한 시각 (관측시각과 구분, 2.5.2)
    source: Literal["hyundai", "manual", "mock"]


class TimeBudget(BaseModel):
    """복귀 마감과 지금 남은 가용 시간 (설계서 2.2 계산 규칙)"""
    computed_at: datetime
    finish_at: datetime                       # 충전 완료 추정 시각
    user_limit_at: Optional[datetime] = None  # 사용자가 말한 제한 시각
    return_deadline: datetime                 # min(finish_at, user_limit_at) - buffer
    buffer_min: int = 5
    available_sec: int                        # max(return_deadline - now, 0)
    estimate_basis: Literal["reported_remaining", "energy_power", "unknown"]


class Place(BaseModel):
    poi_id: str
    name: str = Field(max_length=100)
    category: Category
    latitude: float                           # 도보 목적지 = 입구 좌표(frontLat/frontLon)
    longitude: float
    distance_m: int                           # 출발지에서 직선 거리
    raw_category: str = ""                    # TMAP middleBizName/lowerBizName 원문
    nav_seq: str = ""                         # 입구 구분 (한 장소에 입구가 여러 개일 때)
    opening_status: Literal["open", "closed", "unknown"] = "unknown"
    poi_source: Literal["tmap", "mock"]


class RoundTrip(BaseModel):
    """가는 길 / 오는 길 각각 조회한 보행 경로. 한쪽만 있으면 후보에서 뺀다."""
    poi_id: str
    outbound_sec: int
    inbound_sec: int
    outbound_m: int = 0
    inbound_m: int = 0
    route_source: Literal["tmap", "mock"]


class CandidatePlan(BaseModel):
    plan_id: str
    version: int = Field(ge=1)
    poi_id: str
    name: str = Field(max_length=100)
    category: Category
    outbound_sec: int = Field(ge=0)
    inbound_sec: int = Field(ge=0)
    dwell_sec: int = Field(gt=0)
    total_sec: int
    return_at: datetime
    leave_by: datetime
    slack_sec: int = Field(ge=0)
    route_summary: str = Field(max_length=300)
    opening_status: Literal["open", "closed", "unknown"] = "unknown"
    poi_source: Literal["tmap", "mock"]
    route_source: Literal["tmap", "mock"]
    evaluated_at: datetime


class ConfirmedPlan(BaseModel):
    plan_id: str
    version: int
    confirmed_at: datetime
    return_at: datetime
    leave_by: datetime


class PreferenceRecord(BaseModel):
    user_id: str
    preferred_category: Optional[Category] = None
    dwell_min: Optional[int] = None
    consent_at: datetime
    schema_version: int = 1


class DeleteResult(BaseModel):
    deleted: bool
    user_id: str


# ---------------------------------------------------------------
# Tool 반환 공통 포장 (설계서 2.4 ToolResult[T])
# ---------------------------------------------------------------
T = TypeVar("T")


class ToolResult(BaseModel, Generic[T]):
    status: Literal["ok", "partial", "error"]
    data: Optional[T] = None
    error_code: Optional[str] = None
    retryable: bool = False
    source: str = "code"
    observed_at: Optional[datetime] = None
    message: str = ""                          # 모델에게 보여줄 짧은 설명 (원본 응답 전문 금지)

    def dump(self) -> dict[str, Any]:
        # ToolMessage 로 넘길 때는 dict 로
        return self.model_dump(mode="json")


def tool_error(code: str, message: str = "", retryable: bool = False) -> dict:
    return ToolResult(status="error", error_code=code, message=message, retryable=retryable).dump()


# ---------------------------------------------------------------
# 구조화 출력 (설계서 2.4)
# ---------------------------------------------------------------
class ModelDecision(BaseModel):
    """모델이 최종적으로 내놓는 결정. 후보 ID 와 설명만 고르고 숫자는 만들지 않는다."""
    candidate_ids: list[str] = Field(
        default_factory=list, max_length=3,
        description="설명할 후보의 plan_id (0~3개). select_feasible_plans 가 통과시킨 것만"
    )
    next_action: Literal["clarify", "choose", "done", "stop"] = Field(
        description="clarify=정보 더 필요, choose=후보 제시, done=확정/저장 완료, stop=불가능/범위 밖"
    )
    explanation: str = Field(max_length=500, description="추천 이유 또는 사용자에게 할 질문 (한국어)")


class VoltGoResponse(BaseModel):
    status: Literal["ok", "need_input", "no_feasible", "awaiting_approval", "confirmed", "error"]
    message: str = Field(min_length=1, max_length=1000)
    generated_at: datetime
    request_id: Optional[str] = None          # 승인 배치 ID. decide 재전송 시 그대로 전달
    charging_source: Literal["hyundai", "manual", "mock", "unknown"] = "unknown"
    location_source: Literal["manual", "mock", "unknown"] = "unknown"
    estimate_basis: Literal["reported_remaining", "energy_power", "unknown"] = "unknown"
    finish_at: Optional[datetime] = None
    return_deadline: Optional[datetime] = None
    buffer_min: int = Field(default=5, ge=5, le=15)
    candidates: list[CandidatePlan] = Field(default_factory=list, max_length=3)
    selected_plan_id: Optional[str] = None
    warnings: list[str] = Field(default_factory=list, max_length=5)
    missing_fields: list[str] = Field(default_factory=list)
