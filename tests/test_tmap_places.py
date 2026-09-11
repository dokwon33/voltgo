# 담당 C - TMAP 장소 클라이언트 (요청 파라미터, 응답 파싱, 오류 변환) + find_station / search_nearby_places 도구
# 실제 API 는 부르지 않는다. requests.Session 자리에 가짜 객체를 넣어 요청/응답을 확인한다.
import json
from types import SimpleNamespace

import pytest
import requests

from voltgo.agent import tools
from voltgo.clients import ClientError
from voltgo.clients.tmap_base import TmapHttp
from voltgo.clients.tmap_places import MockPlacesClient, TmapPlacesClient, front_coords, to_place


class FakeResp:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self.content = (text if text is not None else (json.dumps(body) if body is not None else "")).encode()

    def json(self):
        return json.loads(self.content)


class FakeSession:
    """requests.Session.request 대신. 마지막 요청을 기록하고 정해 둔 응답(또는 예외)을 돌려준다"""

    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(SimpleNamespace(method=method, url=url, params=params, headers=headers))
        if self.exc:
            raise self.exc
        return self.resp


def poi(pid, name, lat, lon, **kw):
    return {"id": pid, "name": name, "navSeq": "1", "frontLat": lat, "frontLon": lon,
            "noorLat": "37.5", "noorLon": "127.0", "middleBizName": "음식", "lowerBizName": "한식", **kw}


def body_of(*pois):
    return {"searchPoiInfo": {"totalCount": str(len(pois)), "count": str(len(pois)), "page": "1",
                              "pois": {"poi": list(pois)}}}


def client_with(resp=None, exc=None):
    fake = FakeSession(resp, exc)
    return TmapPlacesClient(http=TmapHttp(app_key="test-key", session=fake)), fake


def rt(context):
    return SimpleNamespace(context=context)


# ---------------------------------------------------------------
# 응답 파싱
# ---------------------------------------------------------------
@pytest.mark.parametrize("lat,lon,expected", [
    ("37.50200", "127.03800", (37.502, 127.038)),   # 문자열 -> float
    ("0", "0", None),                                # 0 좌표
    ("", "127.0", None),                             # 빈 값
    ("abc", "127.0", None),                          # 숫자 아님
    ("127.038", "37.502", None),                     # 위경도 뒤바뀜 (국내 범위 밖)
])
def test_front_coords(lat, lon, expected):
    assert front_coords({"frontLat": lat, "frontLon": lon}) == expected


def test_bad_entrance_is_dropped_not_replaced_by_center(origin):
    # 입구 좌표가 잘못되면 중심 좌표(noor)로 고치지 않고 뺀다 (설계서 2.5.3)
    assert to_place(poi("X", "입구오류", "0", "0"), "meal", origin) is None
    p = to_place(poi("A", "김밥", "37.50200", "127.03800", navSeq="2"), "meal", origin)
    assert (p.latitude, p.longitude, p.nav_seq, p.raw_category) == (37.502, 127.038, "2", "한식")


def test_parking_duplicate_prefers_main_place(origin):
    # 실호출: 같은 id 로 '진오봉참치 주차장' 이 본 장소보다 먼저 올 수 있다
    body = body_of(
        poi("11278427", "진오봉참치 주차장", "37.50076832", "127.03037927", parkFlag="0"),
        poi("11278427", "진오봉참치", "37.50076832", "127.03037927", parkFlag="1"),
        poi("10798642", "동경전통육개장 주차장", "37.50060", "127.03030"),      # 주차장만 있음 -> 제외
    )
    client, _ = client_with(FakeResp(200, body))
    places = client.search_around(origin, "meal")
    assert [(p.poi_id, p.name) for p in places] == [("11278427", "진오봉참치")]


def test_mock_places_follow_real_response(origin):
    meal = MockPlacesClient().search_around(origin, "meal")
    assert [p.name for p in meal if p.poi_id == "A"] == ["김밥천국 역삼점"]   # 주차장 복제본 제거
    assert "P" not in {p.poi_id for p in meal}                               # 주차장만 있는 항목 제외
    assert MockPlacesClient().search_around(origin, "mart") == []            # totalCount=0


# ---------------------------------------------------------------
# 요청 파라미터
# ---------------------------------------------------------------
def test_search_around_request(origin):
    client, fake = client_with(FakeResp(200, body_of(poi("A", "김밥", "37.50200", "127.03800"))))
    places = client.search_around(origin, "cafe", radius_km=1)

    call = fake.calls[0]
    assert call.method == "GET" and call.url.endswith("/tmap/pois/search/around")
    assert call.params["version"] == 1
    assert call.params["sort"] == "distance"                # 기본값 price 라서 꼭 준다
    assert call.params["radius"] == 1 and isinstance(call.params["radius"], int)   # km 정수
    assert call.params["categories"] == "카페;커피"
    assert call.params["reqCoordType"] == call.params["resCoordType"] == "WGS84GEO"
    assert call.headers["appKey"] == "test-key"
    assert "appKey" not in call.params                     # 키는 헤더로만
    assert [p.poi_id for p in places] == ["A"] and places[0].poi_source == "tmap"


def test_unknown_category_rejected_before_call(origin):
    client, fake = client_with(FakeResp(200, body_of()))
    with pytest.raises(ClientError) as e:
        client.search_around(origin, "sauna")
    assert e.value.code == "NEED_INPUT" and fake.calls == []


def test_find_station_request_and_parse():
    raw = poi("S1", "역삼역 EV충전소", "37.50060", "127.03660", upperAddrName="서울", middleAddrName="강남구",
              lowerAddrName="역삼동", firstNo="825", secondNo="0")
    client, fake = client_with(FakeResp(200, body_of(raw, raw)))      # 같은 id 두 번 -> 하나로
    found = client.find_station(" 역삼역 EV충전소 ")

    assert fake.calls[0].url.endswith("/tmap/pois")
    assert fake.calls[0].params["searchKeyword"] == "역삼역 EV충전소"
    assert len(found) == 1
    s = found[0]
    assert (s.poi_id, s.latitude, s.longitude, s.source) == ("S1", 37.5006, 127.0366, "manual")
    assert s.address == "서울 강남구 역삼동 825"


def test_find_station_blank_keyword_no_call():
    client, fake = client_with(FakeResp(200, body_of()))
    assert client.find_station("  ") == [] and fake.calls == []


# ---------------------------------------------------------------
# 빈 결과 / 오류 변환 (설계서 2.5.4)
# ---------------------------------------------------------------
@pytest.mark.parametrize("resp", [
    FakeResp(204),                                                  # 본문 없는 204
    FakeResp(200, {"searchPoiInfo": {"totalCount": "0", "pois": {"poi": []}}}),
    FakeResp(200, {"searchPoiInfo": {"totalCount": "0"}}),
])
def test_empty_result_is_not_error(origin, resp):
    client, _ = client_with(resp)
    assert client.search_around(origin, "meal") == []


@pytest.mark.parametrize("resp,exc,code,retryable", [
    (FakeResp(401), None, "AUTH_ERROR", False),
    (FakeResp(403), None, "AUTH_ERROR", False),
    (FakeResp(429), None, "RATE_LIMIT", True),
    (FakeResp(503), None, "UPSTREAM", True),
    (FakeResp(400), None, "UPSTREAM", False),
    (FakeResp(200, text="<html>"), None, "UPSTREAM", False),     # JSON 아님
    (None, requests.Timeout(), "UPSTREAM", True),
    (None, requests.ConnectionError(), "UPSTREAM", True),
])
def test_error_mapping(origin, resp, exc, code, retryable):
    client, fake = client_with(resp, exc)
    with pytest.raises(ClientError) as e:
        client.search_around(origin, "meal")
    assert (e.value.code, e.value.retryable) == (code, retryable)
    assert len(fake.calls) == 1            # 클라이언트는 재시도하지 않는다 (middleware 가 1회)


def test_missing_key(monkeypatch):
    monkeypatch.delenv("TMAP_APP_KEY", raising=False)
    with pytest.raises(ClientError) as e:
        TmapPlacesClient()
    assert e.value.code == "AUTH_ERROR"


# ---------------------------------------------------------------
# Mock 클라이언트
# ---------------------------------------------------------------
def test_mock_find_station_filters_keyword_and_bad_coords():
    client = MockPlacesClient()
    assert [s.poi_id for s in client.find_station("역삼역")] == ["S1", "S2"]
    assert [s.poi_id for s in client.find_station("강남역 EV충전소")] == ["S3"]
    assert client.find_station("선릉역") == []          # 입구 좌표 오류 샘플은 빠진다
    assert client.find_station("")[0].name == "역삼역 EV충전소"   # 시연 기본 충전소


# ---------------------------------------------------------------
# 도구: find_station
# ---------------------------------------------------------------
def test_find_station_multiple_asks_then_select(context):
    s = context.session
    s.origin = None

    r = tools.find_station.func(rt(context), keyword="역삼역")
    assert r["status"] == "partial" and r["error_code"] == "NEED_INPUT"
    assert [c["poi_id"] for c in r["data"]] == ["S1", "S2"]
    assert "역삼동" in r["message"]                         # 이름과 주소를 같이 보여준다
    assert s.origin is None                                 # 고르기 전에는 확정하지 않는다

    r = tools.find_station.func(rt(context), keyword="역삼역", station_id="S2")
    assert r["status"] == "ok"
    assert s.origin.name == "역삼역 EV충전소 2주차장"
    assert s.counters["api"] == 1                           # 선택할 때는 API 를 다시 안 부른다
    assert s.station_candidates == {}


class FailingPlaces:
    """find_station 이 항상 API 오류"""

    def find_station(self, keyword):
        raise ClientError("UPSTREAM", "timeout", retryable=True)


@pytest.mark.parametrize("fail", ["empty", "api_error"])
def test_failed_new_search_drops_old_candidates(context, fail):
    # PR #2 리뷰: 역삼역 후보(S1·S2) -> 다른 충전소 검색 실패 -> S1 선택 은 거절돼야 한다
    s = context.session
    s.origin = None
    r = tools.find_station.func(rt(context), keyword="역삼역")
    assert set(s.station_candidates) == {"S1", "S2"}

    if fail == "api_error":
        context.places_client = FailingPlaces()
        r = tools.find_station.func(rt(context), keyword="판교 충전소")
        assert r["error_code"] == "UPSTREAM"
    else:
        r = tools.find_station.func(rt(context), keyword="없는충전소")
        assert r["error_code"] == "NEED_INPUT"
    assert s.station_candidates == {}

    r = tools.find_station.func(rt(context), keyword="역삼역", station_id="S1")
    assert r["error_code"] == "PRECONDITION_FAILED"
    assert s.origin is None


def test_new_multi_search_replaces_candidates(context):
    s = context.session
    s.origin = None
    tools.find_station.func(rt(context), keyword="역삼역")            # S1, S2
    tools.find_station.func(rt(context), keyword="EV충전소")          # S1, S2, S3 (새 목록)
    assert set(s.station_candidates) == {"S1", "S2", "S3"}
    r = tools.find_station.func(rt(context), keyword="EV충전소", station_id="S3")
    assert r["status"] == "ok" and s.origin.poi_id == "S3"


def test_find_station_single_sets_origin(context):
    context.session.origin = None
    r = tools.find_station.func(rt(context), keyword="강남역 EV충전소")
    assert r["status"] == "ok" and context.session.origin.poi_id == "S3"


def test_find_station_errors(context):
    r = tools.find_station.func(rt(context), keyword="없는충전소")
    assert r["error_code"] == "NEED_INPUT"
    r = tools.find_station.func(rt(context), keyword="역삼역", station_id="S9")
    assert r["error_code"] == "PRECONDITION_FAILED"
    r = tools.find_station.func(rt(context), keyword=" ")
    assert r["error_code"] == "NEED_INPUT"


def test_origin_change_drops_places(context):
    tools.get_charging_status.func(rt(context))
    tools.calculate_time_budget.func(rt(context), user_limit_min=30)
    tools.search_nearby_places.func(rt(context), category="meal")
    assert context.session.places

    tools.find_station.func(rt(context), keyword="강남역 EV충전소")
    assert context.session.places == {} and context.session.routes == {}


# ---------------------------------------------------------------
# 도구: search_nearby_places
# ---------------------------------------------------------------
def test_search_max_dist_limit(context):
    tools.get_charging_status.func(rt(context))
    tools.calculate_time_budget.func(rt(context), user_limit_min=30)
    r = tools.search_nearby_places.func(rt(context), category="meal", max_dist_m=1500)
    assert r["error_code"] == "NEED_INPUT"
    assert context.places_client.calls == 0


def test_search_widen_to_1000_finds_far_cafe(context):
    tools.get_charging_status.func(rt(context))
    tools.calculate_time_budget.func(rt(context), user_limit_min=30)
    r = tools.search_nearby_places.func(rt(context), category="cafe")
    assert r["status"] == "ok" and r["data"] == []          # 500m 안에는 카페 없음 -> 정상 빈 결과
    r = tools.search_nearby_places.func(rt(context), category="cafe", max_dist_m=1000)
    assert [p["poi_id"] for p in r["data"]] == ["D"]


# ---------------------------------------------------------------
# 도구: 반경 자동 조정
# ---------------------------------------------------------------
def _budget_no_limit(context):
    # mock 잔여 40분, 사용자 제한 없음 -> 마감 14:35, 가용 35분
    tools.get_charging_status.func(rt(context))
    tools.calculate_time_budget.func(rt(context))


def test_auto_radius_widens_when_time_is_ample(context):
    _budget_no_limit(context)
    # 카페 기본 체류 15분 -> 편도 10분 -> 540m : 714m 떨어진 카페 D 는 아직 밖
    r = tools.search_nearby_places.func(rt(context), category="cafe")
    assert r["data"] == [] and "540m" in r["message"] and "자동" in r["message"]
    # 사용자가 "5분이면 돼" -> 편도 15분 -> 810m : D 가 들어온다
    r = tools.search_nearby_places.func(rt(context), category="cafe", dwell_min=5)
    assert [p["poi_id"] for p in r["data"]] == ["D"]
    assert context.places_client.calls == 2


def test_explicit_max_dist_wins_over_auto(context):
    _budget_no_limit(context)
    r = tools.search_nearby_places.func(rt(context), category="cafe", dwell_min=5, max_dist_m=500)
    assert r["data"] == [] and "지정값" in r["message"]


def test_bad_dwell_rejected_before_call(context):
    _budget_no_limit(context)
    r = tools.search_nearby_places.func(rt(context), category="meal", dwell_min=0)
    assert r["error_code"] == "NEED_INPUT" and context.places_client.calls == 0
