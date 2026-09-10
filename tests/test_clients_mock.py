# 담당 B/C/D - 어댑터 단위 변환, Mock 클라이언트
import pytest

from voltgo.clients import ClientError
from voltgo.clients.hyundai import parse_charging_response
from voltgo.clients.mock_charging import MockChargingProvider
from voltgo.clients.tmap_places import MockPlacesClient
from voltgo.clients.tmap_routes import MockRoutesClient

RAW = {"batteryPlugin": 1, "batteryCharge": True, "soc": 40,
       "targetSOC": {"plugType": 0, "targetSOClevel": 80},
       "remainTime": {"value": 40, "unit": 1}, "timestamp": "20260910140000"}


@pytest.mark.parametrize("unit,value,expected", [(0, 1, 3600), (1, 40, 2400), (2, 90000, 90), (3, 75, 75)])
def test_remain_time_units(unit, value, expected):
    raw = dict(RAW, remainTime={"value": value, "unit": unit})
    snap = parse_charging_response(raw)
    assert snap.reported_remaining_sec == expected


def test_invalid_unit():
    with pytest.raises(ClientError) as e:
        parse_charging_response(dict(RAW, remainTime={"value": 1, "unit": 9}))
    assert e.value.code == "INVALID_UNIT"


def test_unplugged_means_not_charging_and_no_remaining():
    # 미연결이면 remainTime 은 가상값이라 버린다
    snap = parse_charging_response(dict(RAW, batteryPlugin=0, batteryCharge=True))
    assert snap.charging is False
    assert snap.reported_remaining_sec is None
    assert snap.plug_type == "none"


def test_timestamp_is_kst():
    snap = parse_charging_response(RAW)
    assert snap.observed_at.utcoffset().total_seconds() == 9 * 3600
    assert snap.observed_at.hour == 14


def test_mock_provider_stamps_now(now):
    snap = MockChargingProvider("charging_ok", clock=lambda: now).get_charging_status()
    assert snap.source == "mock"
    assert snap.observed_at == now
    assert snap.reported_remaining_sec == 2400


def test_mock_places_category_filter(origin):
    client = MockPlacesClient()
    meal = client.search_around(origin, "meal")
    assert sorted(p.poi_id for p in meal) == ["A", "B", "C"]
    assert all(p.poi_source == "mock" for p in meal)
    cafe = client.search_around(origin, "cafe")
    assert [p.poi_id for p in cafe] == ["D"]
    assert cafe[0].distance_m > 500        # 필터 테스트용으로 일부러 멀리 둔 장소


def test_mock_routes_round_trip(origin):
    client = MockRoutesClient()
    place = MockPlacesClient().search_around(origin, "meal")[0]
    trip = client.round_trip(origin, place)
    assert (trip.outbound_sec, trip.inbound_sec) == (240, 300)
    with pytest.raises(ClientError):
        client.round_trip(origin, place.model_copy(update={"poi_id": "Z"}))
