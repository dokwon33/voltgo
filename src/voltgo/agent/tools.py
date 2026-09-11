"""
VoltGo Tool 8개 + find_station (설계서 2.5)

노트북 [4] 의 @tool 패턴. 실행 컨텍스트가 필요한 도구는 runtime: ToolRuntime 을 받는다 (노트북 [5] 4-2).
모델 인자에는 runtime 이 안 보이므로 실차 ID·좌표·API 키를 모델이 마음대로 정할 수 없다.

반환은 전부 ToolResult.dump() (dict). 원문 응답 전문은 넣지 않는다.
"""
from datetime import datetime, timedelta
from typing import Optional

from langchain.tools import ToolRuntime, tool

from voltgo.agent import memory
from voltgo.agent.schemas import (
    Category, ConfirmedPlan, DeleteResult, PreferenceRecord, ToolResult, tool_error,
)
from voltgo.clients import ClientError
from voltgo.core import feasibility
from voltgo.core.place_policy import (
    DEFAULT_DIST_M, DWELL_DEFAULT_MIN, MAX_DIST_M, MAX_ROUTE_CANDIDATES, filter_places,
)
from voltgo.core import time_budget
from voltgo.core.time_budget import STALE_AFTER_SEC

APPROVAL_TTL_SEC = 120   # 승인 대기 2분 넘으면 무효 (설계서 3.3)


def _fetch_charging(ctx):
    """공급자 호출 + 세션 기록. 실패하면 ClientError 그대로 올린다"""
    snap = ctx.charging_provider.get_charging_status()
    ctx.session.charging = snap
    ctx.session.counters["api"] += 1
    return snap


# ---------------------------------------------------------------
# 1. 충전 상태
# ---------------------------------------------------------------
@tool
def get_charging_status(runtime: ToolRuntime, force_refresh: bool = False) -> dict:
    """현재 충전 상태(충전 중 여부, SoC, 잔여시간)와 측정 시각을 가져옵니다.
    새 계획을 세우거나 오래된 상태를 다시 확인할 때 가장 먼저 호출합니다.

    Args:
        force_refresh: True 면 60초 캐시를 무시하고 다시 조회
    """
    ctx = runtime.context
    s = ctx.session
    now = ctx.clock()

    # 60초 안이면 캐시 재사용
    if s.charging and not force_refresh:
        age = (now - s.charging.observed_at).total_seconds()
        if age <= STALE_AFTER_SEC:
            return ToolResult(status="ok", data=s.charging, source=s.charging.source,
                              observed_at=s.charging.observed_at, message="캐시(60초 이내)").dump()

    if ctx.charging_provider is None:
        return tool_error("NEED_INPUT", "충전 정보 공급자가 없습니다. 잔여시간을 직접 알려주세요.")

    try:
        snap = _fetch_charging(ctx)
    except ClientError as e:
        # 인증 실패는 재시도 없음, live 실패를 자동으로 Mock 으로 바꾸지 않는다
        return tool_error(e.code, str(e), retryable=e.retryable)

    if snap.source == "mock":
        s.warnings.append("충전 정보는 Mock 데이터입니다")
    if snap.plug_type == "slow":
        s.warnings.append("완속 충전이라 '30~40분' 전제가 맞지 않을 수 있습니다")

    return ToolResult(status="ok", data=snap, source=snap.source, observed_at=snap.observed_at,
                      message=f"charging={snap.charging}, soc={snap.soc_pct}%, plug={snap.plug_type}").dump()


# ---------------------------------------------------------------
# 2. 시간 예산
# ---------------------------------------------------------------
@tool
def calculate_time_budget(runtime: ToolRuntime, target_soc_pct: float = 80,
                          user_limit_min: Optional[int] = None) -> dict:
    """검증된 충전 정보와 사용자의 시간 제한으로 복귀 마감 시각과 지금 남은 가용 시간(초)을 계산합니다.
    get_charging_status 다음에 호출합니다.

    Args:
        target_soc_pct: 목표 충전량(%). 기본 80
        user_limit_min: 사용자가 말한 시간 제한(분). "30분 있어" -> 30. 없으면 None
    """
    ctx = runtime.context
    s = ctx.session
    now = ctx.clock()

    if s.charging is None:
        return tool_error("PRECONDITION_FAILED", "get_charging_status 를 먼저 호출하세요")
    if not (0 < target_soc_pct <= 100):
        return tool_error("NEED_INPUT", "목표 SoC 는 0~100 사이여야 합니다")
    if user_limit_min is not None and user_limit_min <= 0:
        return tool_error("NEED_INPUT", "시간 제한은 1분 이상이어야 합니다")

    # 조건이 바뀌면 이전 후보/승인은 버린다
    if (target_soc_pct != s.target_soc_pct) or (user_limit_min != s.user_limit_min):
        if s.candidates or s.confirmed:
            s.bump_version()
    s.target_soc_pct = target_soc_pct
    if user_limit_min is not None:
        s.user_limit_min = user_limit_min
        s.limit_said_at = now       # 제한을 말한 시각 기준으로 마감을 잡는다

    snap = s.charging.model_copy(update={"target_soc_pct": target_soc_pct})
    # 같은 이름의 도구 함수와 겹치지 않게 모듈 경로로 부른다
    budget, err = time_budget.calculate_time_budget(snap, now, buffer_min=ctx.buffer_min,
                                                    user_limit_min=s.user_limit_min, limit_said_at=s.limit_said_at)
    if err is not None:
        msgs = {
            "NOT_CHARGING": "충전 중이 아닙니다. 외출 계획을 세우지 않습니다.",
            "TARGET_REACHED": "이미 목표 충전량에 도달했습니다.",
            "STALE_DATA": "충전 정보가 오래됐습니다. get_charging_status(force_refresh=True) 로 갱신하세요.",
            "NEED_INPUT": "잔여시간을 계산할 값이 없습니다. 남은 충전 시간(분)을 사용자에게 물어보세요.",
        }
        return tool_error(err, msgs.get(err, err))

    s.time_budget = budget
    if budget.estimate_basis == "energy_power":
        s.warnings.append("잔여시간은 배터리 용량·평균 전력 정책값으로 추정한 값입니다")

    return ToolResult(status="ok", data=budget, observed_at=now,
                      message=f"복귀 마감 {budget.return_deadline:%H:%M}, 가용 {budget.available_sec // 60}분").dump()


# ---------------------------------------------------------------
# 3. 장소 검색
# ---------------------------------------------------------------
def _set_origin(s, station):
    """출발지를 정한다. 다른 곳으로 바뀌면 이전 장소/경로/후보/승인은 버린다"""
    changed = s.origin is not None and (s.origin.latitude, s.origin.longitude) != (station.latitude, station.longitude)
    if changed:
        if s.candidates or s.confirmed:
            s.bump_version()
        s.places, s.routes, s.selected_ran = {}, {}, False
    s.origin = station
    s.station_candidates = {}


@tool
def find_station(runtime: ToolRuntime, keyword: str, station_id: Optional[str] = None) -> dict:
    """충전소 이름으로 출발지 좌표를 찾습니다. 위치 정보가 없을 때 사용자가 말한 충전소명으로 호출합니다.
    후보가 여러 개면 이름·주소 목록을 돌려주므로 사용자에게 고르게 한 뒤,
    고른 후보의 poi_id 를 station_id 로 넣어 다시 호출합니다. (이때는 API 를 다시 부르지 않습니다)

    Args:
        keyword: 충전소 이름 (예: "역삼역 EV충전소")
        station_id: 직전 결과 후보 중 사용자가 고른 poi_id. 처음 검색할 때는 비워 둡니다
    """
    ctx = runtime.context
    s = ctx.session

    if station_id is not None:
        picked = s.station_candidates.get(station_id)
        if picked is None:
            return tool_error("PRECONDITION_FAILED", "직전 검색 후보에 없는 station_id 입니다. 먼저 keyword 로 검색하세요.")
        _set_origin(s, picked)
        return ToolResult(status="ok", data=picked, source=picked.source, message=f"출발지 설정: {picked.name}").dump()

    if not keyword.strip():
        return tool_error("NEED_INPUT", "충전소 이름이 비어 있습니다. 사용자에게 충전소 이름을 물어보세요.")
    if ctx.places_client is None:
        return tool_error("AUTH_ERROR", "장소 API 설정이 없습니다")
    try:
        found = ctx.places_client.find_station(keyword)
    except ClientError as e:
        return tool_error(e.code, str(e), retryable=e.retryable)
    s.counters["api"] += 1

    if not found:
        return tool_error("NEED_INPUT", "충전소를 찾지 못했습니다. 이름을 다시 알려달라고 하세요.")
    if len(found) >= 2:
        # 선택 전에는 Origin 을 확정하지 않는다 (설계서 2.5 find_station)
        s.station_candidates = {c.poi_id: c for c in found}
        lines = " / ".join(f"{c.poi_id}: {c.name} ({c.address})" for c in found)
        return ToolResult(status="partial", data=found, error_code="NEED_INPUT", source=found[0].source,
                          message=f"후보 {len(found)}곳: {lines}. 어느 곳인지 물어본 뒤 station_id 로 다시 호출하세요.").dump()

    _set_origin(s, found[0])
    return ToolResult(status="ok", data=found[0], source=found[0].source, message=f"출발지 설정: {found[0].name}").dump()


@tool
def search_nearby_places(runtime: ToolRuntime, category: Category,
                         radius_km: int = 1, max_dist_m: int = DEFAULT_DIST_M) -> dict:
    """충전소 주변에서 사용자가 원하는 종류의 장소를 찾습니다. calculate_time_budget 이 성공한 뒤 호출합니다.

    Args:
        category: meal(식사) / cafe / convenience(편의점) / mart
        radius_km: TMAP 검색 반경(km, 정수). 기본 1
        max_dist_m: 직선거리 필터(m). 기본 500. 결과가 없을 때만 1회 최대 1000 까지 늘려서 재검색
    """
    ctx = runtime.context
    s = ctx.session

    if s.time_budget is None:
        return tool_error("PRECONDITION_FAILED", "calculate_time_budget 를 먼저 호출하세요")
    if s.origin is None:
        return tool_error("NEED_INPUT", "출발지(충전소 위치)가 없습니다. 충전소 이름을 물어본 뒤 find_station 을 호출하세요.")
    if ctx.places_client is None:
        return tool_error("AUTH_ERROR", "장소 API 설정이 없습니다")
    if not (1 <= radius_km <= 3):
        return tool_error("NEED_INPUT", "radius_km 는 1~3 사이")
    if not (1 <= max_dist_m <= MAX_DIST_M):
        return tool_error("NEED_INPUT", f"max_dist_m 는 1~{MAX_DIST_M} 사이")

    try:
        places = ctx.places_client.search_around(s.origin, category, radius_km=radius_km)
    except ClientError as e:
        return tool_error(e.code, str(e), retryable=e.retryable)
    s.counters["api"] += 1

    picked = filter_places(places, s.origin, max_dist_m=max_dist_m, limit=MAX_ROUTE_CANDIDATES)

    # 새로 검색하면 이전 경로/후보는 의미가 없다
    s.places = {p.poi_id: p for p in picked}
    s.routes = {}
    s.candidates = {}
    s.selected_ran = False

    if picked and picked[0].poi_source == "mock":
        s.warnings.append("장소 정보는 Mock 데이터입니다")

    if not picked:
        return ToolResult(status="ok", data=[], message="정상 빈 결과. 반경을 한 번만 늘리거나 다른 카테고리를 제안하세요.").dump()

    return ToolResult(status="ok", data=picked, source=picked[0].poi_source,
                      message=f"{len(picked)}곳 (직선거리 {max_dist_m}m 이내, 가까운 순)").dump()


# ---------------------------------------------------------------
# 4. 왕복 경로
# ---------------------------------------------------------------
@tool
def get_walking_routes(runtime: ToolRuntime, poi_ids: list[str]) -> dict:
    """등록된 후보 장소까지 가는 길과 차량으로 돌아오는 길의 보행 경로를 각각 조회합니다.
    search_nearby_places 결과의 poi_id 만 넣을 수 있습니다 (최대 5개).

    Args:
        poi_ids: 경로를 조회할 장소 ID 목록
    """
    ctx = runtime.context
    s = ctx.session

    if not s.places:
        return tool_error("PRECONDITION_FAILED", "search_nearby_places 를 먼저 호출하세요")
    unknown = [p for p in poi_ids if p not in s.places]
    if unknown:
        return tool_error("PRECONDITION_FAILED", f"등록되지 않은 poi_id: {unknown}")
    poi_ids = list(dict.fromkeys(poi_ids))[:MAX_ROUTE_CANDIDATES]
    if not poi_ids:
        return tool_error("PRECONDITION_FAILED", "경로를 조회할 poi_id가 없습니다")

    # 경로가 갱신되면 이전 경로로 만든 후보와 확정은 더 이상 유효하지 않다.
    if s.candidates or s.confirmed or s.confirmed_by_request:
        s.bump_version()
    s.candidates = {}
    s.confirmed = None
    s.confirmed_by_request = {}
    s.selected_ran = False

    # 같은 ID를 중복 호출하지 않고, 이번 조회 결과만 다음 판정에 사용한다.
    s.routes = {}
    if ctx.routes_client is None:
        return tool_error("AUTH_ERROR", "경로 API 설정이 없습니다")

    ok, failed = [], []
    for pid in poi_ids:
        try:
            trip = ctx.routes_client.round_trip(s.origin, s.places[pid])
            s.routes[pid] = trip
            ok.append(trip)
        except ClientError as e:
            failed.append((pid, e))           # 한 방향만 실패해도 후보 제외 (편도 x2 금지)
            if e.code in ("AUTH_ERROR", "RATE_LIMIT"):
                return tool_error(e.code, str(e), retryable=e.retryable)
        s.counters["api"] += 2

    if ok and ok[0].route_source == "mock":
        s.warnings.append("보행 경로는 Mock 데이터입니다")
    if failed:
        failed_ids = [pid for pid, _ in failed]
        s.warnings.append(f"경로 조회 실패로 제외된 후보: {', '.join(failed_ids)}")

    if not ok:
        # UPSTREAM timeout/5xx를 ROUTE_PARSE로 덮어쓰면 middleware 재시도가 사라진다.
        first_error = failed[0][1]
        return tool_error(first_error.code, "모든 후보의 경로 조회에 실패했습니다",
                          retryable=first_error.retryable)
    status = "partial" if failed else "ok"
    return ToolResult(status=status, data=ok, source=ok[0].route_source,
                      message=f"성공 {len(ok)}개, 실패 {len(failed)}개").dump()


# ---------------------------------------------------------------
# 5. 후보 선별 (순수 계산)
# ---------------------------------------------------------------
@tool
def select_feasible_plans(runtime: ToolRuntime, dwell_min: Optional[int] = None,
                          dwell_overrides: Optional[dict[str, int]] = None) -> dict:
    """왕복 보행 시간과 체류 시간이 복귀 마감 안에 들어오는 후보만 계산하고 정렬합니다.
    get_walking_routes 다음에 호출합니다. 통과한 plan_id 만 사용자에게 추천할 수 있습니다.

    Args:
        dwell_min: 모든 후보에 적용할 체류 시간(분). 사용자가 "20분이면 돼" 라고 하면 20
        dwell_overrides: 특정 장소만 바꿀 때 {poi_id: 분}
    """
    ctx = runtime.context
    s = ctx.session
    now = ctx.clock()

    if s.time_budget is None or not s.places:
        return tool_error("PRECONDITION_FAILED", "시간 예산과 장소 검색이 먼저 필요합니다")
    if not s.routes:
        return tool_error("PRECONDITION_FAILED", "get_walking_routes 를 먼저 호출하세요")

    overrides = dict(dwell_overrides or {})
    if dwell_min is not None:
        for pid in s.places:
            overrides.setdefault(pid, dwell_min)
    bad = [k for k, v in overrides.items() if k not in s.places or v <= 0]
    if bad:
        return tool_error("PRECONDITION_FAILED", f"잘못된 dwell_overrides: {bad}")

    # 체류 조건이 바뀌면 새 버전 (이전 승인 무효)
    if overrides != s.dwell_overrides and (s.candidates or s.confirmed):
        s.bump_version()
    s.dwell_overrides = overrides

    # 가용시간은 '지금' 기준으로 다시 잰다. 처음 계산한 값을 그대로 쓰지 않는다.
    pref = memory.load_preferences(ctx.user_id)
    preferred = pref.preferred_category if pref else None
    plans = feasibility.select_feasible_plans(
        s.places, s.routes, s.time_budget, now, s.condition_version,
        dwell_overrides=overrides, preferred_category=preferred,
    )
    s.candidates = {p.plan_id: p for p in plans[:3]}
    s.selected_ran = True

    if not plans:
        return ToolResult(status="ok", data=[], observed_at=now,
                          message="시간 안에 다녀올 수 있는 후보가 없습니다. 반경 확대로 해결하지 말고 사용자에게 알리세요.").dump()

    lines = [f"{p.plan_id}: {p.name} 총 {p.total_sec // 60}분, 복귀 {p.return_at:%H:%M}, 여유 {p.slack_sec // 60}분"
             for p in plans[:3]]
    return ToolResult(status="ok", data=plans[:3], observed_at=now, message=" / ".join(lines)).dump()


# ---------------------------------------------------------------
# 6. 계획 확정 (HITL 승인 뒤에만 실행된다)
# ---------------------------------------------------------------
@tool
def confirm_plan(runtime: ToolRuntime, plan_id: str, version: int) -> dict:
    """사용자가 선택한 계획을 승인 뒤 다시 검증해서 현재 대화의 확정 계획으로 기록합니다.
    사용자가 후보를 골랐을 때만 호출합니다.

    Args:
        plan_id: 선택한 후보의 plan_id
        version: 그 후보의 version
    """
    ctx = runtime.context
    s = ctx.session
    now = ctx.clock()

    # 같은 요청은 한 번만 (멱등, C017)
    key = f"{plan_id}:{version}"
    if key in s.confirmed_by_request:
        return ToolResult(status="ok", data=s.confirmed_by_request[key], message="이미 확정된 계획").dump()

    plan = s.candidates.get(plan_id)
    if plan is None:
        return tool_error("PRECONDITION_FAILED", "통과한 후보에 없는 plan_id 입니다")
    if plan.version != s.condition_version or version != plan.version:
        return tool_error("VERSION_MISMATCH", "조건이 바뀌었습니다. 다시 선별하세요.")
    if (now - plan.evaluated_at).total_seconds() > APPROVAL_TTL_SEC:
        return tool_error("APPROVAL_EXPIRED", "승인 대기가 2분을 넘었습니다. 다시 선별하세요.")

    # 확정 직전 최신 충전 상태 + 현재 시각으로 같은 계산을 다시 돌린다 (C006)
    try:
        snap = _fetch_charging(ctx) if ctx.charging_provider else s.charging
    except ClientError as e:
        return tool_error(e.code, str(e), retryable=e.retryable)
    budget, err = time_budget.calculate_time_budget(
        snap.model_copy(update={"target_soc_pct": s.target_soc_pct}), now,
        buffer_min=ctx.buffer_min, user_limit_min=s.user_limit_min, limit_said_at=s.limit_said_at)
    if err is not None:
        return tool_error(err, "재검증 실패. 다시 계획하세요.")
    s.time_budget = budget

    rechecked = feasibility.recheck_plan(plan, budget, now)
    if rechecked is None:
        s.candidates.pop(plan_id, None)
        return tool_error("RECHECK_FAILED", "지금 출발하면 마감 안에 못 돌아옵니다. 재계획이 필요합니다.")

    confirmed = ConfirmedPlan(plan_id=plan_id, version=version, confirmed_at=now,
                              return_at=rechecked.return_at, leave_by=rechecked.leave_by)
    s.confirmed = confirmed
    s.confirmed_by_request[key] = confirmed
    s.candidates[plan_id] = rechecked
    return ToolResult(status="ok", data=confirmed, observed_at=now,
                      message=f"확정. 늦어도 {confirmed.leave_by:%H:%M} 에는 장소에서 출발").dump()


# ---------------------------------------------------------------
# 7. 선호 저장 / 8. 삭제 (Store)
# ---------------------------------------------------------------
@tool
def save_preferences(runtime: ToolRuntime, category: Optional[Category] = None,
                     dwell_min: Optional[int] = None) -> dict:
    """사용자가 "기억해줘" 라고 명시적으로 요청한 선호만 승인 뒤 저장합니다.

    Args:
        category: 선호 카테고리 (meal/cafe/convenience/mart)
        dwell_min: 기본 체류 시간(분), 5~60
    """
    ctx = runtime.context
    if category is None and dwell_min is None:
        return tool_error("NEED_INPUT", "저장할 항목이 없습니다")
    if dwell_min is not None and not (5 <= dwell_min <= 60):
        return tool_error("NEED_INPUT", "체류 시간은 5~60분")

    old = memory.load_preferences(ctx.user_id)
    record = PreferenceRecord(
        user_id=ctx.user_id,
        preferred_category=category if category is not None else (old.preferred_category if old else None),
        dwell_min=dwell_min if dwell_min is not None else (old.dwell_min if old else None),
        consent_at=ctx.clock(),
    )
    try:
        memory.save_preferences(record)
    except OSError as e:
        # 실패했으면 기억했다고 말하면 안 된다
        return tool_error("STORE_ERROR", f"저장 실패: {type(e).__name__}")
    return ToolResult(status="ok", data=record, message="저장 완료").dump()


@tool
def delete_preferences(runtime: ToolRuntime) -> dict:
    """사용자가 삭제를 요청한 자신의 저장 선호를 지웁니다. 본인 것만 지울 수 있습니다."""
    ctx = runtime.context
    try:
        deleted = memory.delete_preferences(ctx.user_id)
    except OSError as e:
        return tool_error("STORE_ERROR", f"삭제 실패: {type(e).__name__}")
    return ToolResult(status="ok", data=DeleteResult(deleted=deleted, user_id=ctx.user_id),
                      message="삭제됨" if deleted else "저장된 선호 없음").dump()


# 에이전트에 등록할 도구 목록
TOOLS = [
    get_charging_status,
    calculate_time_budget,
    find_station,
    search_nearby_places,
    get_walking_routes,
    select_feasible_plans,
    confirm_plan,
    save_preferences,
    delete_preferences,
]

# 읽기 도구 / 쓰기 도구 구분 (미들웨어 재시도·승인 정책용)
READ_TOOLS = {"get_charging_status", "find_station", "search_nearby_places", "get_walking_routes"}
WRITE_TOOLS = {"confirm_plan", "save_preferences", "delete_preferences"}
