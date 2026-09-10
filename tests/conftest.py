"""
공통 fixture. 설계서 2.2 '계산 검증용 예시' 를 그대로 쓴다.
  기준 시각 14:00, SoC 40 -> 80%, 60kWh / 48kW, 사용자 제한 30분, 버퍼 5분
  후보 A 4/12/5분, B 4/16/5분, C 12/12/13분
"""
from datetime import datetime

import pytest

from voltgo.agent.schemas import ChargingSnapshot, Origin, Place, RoundTrip
from voltgo.agent.state import KST, Context, Session
from voltgo.clients.mock_charging import MockChargingProvider
from voltgo.clients.tmap_places import MockPlacesClient
from voltgo.clients.tmap_routes import MockRoutesClient
from voltgo.core.time_budget import calculate_time_budget

NOW = datetime(2026, 9, 10, 14, 0, tzinfo=KST)


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def snapshot(now):
    # 잔여시간 API 값 없음 -> 단순 추정 경로 (24kWh / 48kW = 30분)
    return ChargingSnapshot(charging=True, soc_pct=40, target_soc_pct=80, capacity_kwh=60, avg_power_kw=48,
                            reported_remaining_sec=None, plug_type="fast", observed_at=now, source="mock")


@pytest.fixture
def budget(snapshot, now):
    b, err = calculate_time_budget(snapshot, now, buffer_min=5, user_limit_min=30, limit_said_at=now)
    assert err is None
    return b


@pytest.fixture
def origin():
    return Origin(latitude=37.5006, longitude=127.0366, name="역삼역 EV충전소", source="mock")


@pytest.fixture
def places():
    def p(pid, name):
        return Place(poi_id=pid, name=name, category="meal", latitude=0, longitude=0, distance_m=100, poi_source="mock")
    return {"A": p("A", "후보A"), "B": p("B", "후보B"), "C": p("C", "후보C")}


@pytest.fixture
def routes():
    def r(pid, out_min, in_min):
        return RoundTrip(poi_id=pid, outbound_sec=out_min * 60, inbound_sec=in_min * 60, route_source="mock")
    return {"A": r("A", 4, 5), "B": r("B", 4, 5), "C": r("C", 12, 13)}


@pytest.fixture
def context(now, tmp_path, monkeypatch):
    """Mock 공급자 3종을 붙인 Context. 선호 파일은 임시 폴더에."""
    from voltgo.agent import memory
    monkeypatch.setattr(memory, "PREF_DIR", tmp_path)

    clock = lambda: NOW
    places = MockPlacesClient()
    session = Session()
    session.origin = places.find_station("")[0]
    return Context(user_id="u1", clock=clock,
                   charging_provider=MockChargingProvider(fixture="charging_ok", clock=clock),
                   places_client=places, routes_client=MockRoutesClient(), session=session)
