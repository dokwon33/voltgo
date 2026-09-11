"""
TMAP 보행자 경로 API (필요_API_목록 T2)

  POST https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1
  body: startX(경도) startY(위도) endX endY startName endName

응답은 GeoJSON. features[0].properties.totalTime(초), totalDistance(m)
"""
import json
from pathlib import Path
from typing import Optional

from voltgo.agent.schemas import Origin, Place, RoundTrip
from voltgo.clients import ClientError
from voltgo.clients.tmap_base import COORD_TYPE, TmapHttp

MOCK_DIR = Path(__file__).resolve().parents[3] / "data" / "mock"

# TMAP 한 방향 응답을 내부 값으로 변환
def _parse_leg(body: dict) -> tuple[int, int]:
    """(totalTime 초, totalDistance m). 결측/형식 오류면 해당 후보를 제외한다."""
    try:
        props = body["features"][0]["properties"]
        total_sec = int(props["totalTime"])
        total_m = int(props["totalDistance"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise ClientError("ROUTE_PARSE", "totalTime 또는 totalDistance 형식 오류")
    if total_sec < 0 or total_m < 0:
        raise ClientError("ROUTE_PARSE", "경로 시간 또는 거리가 음수")
    return total_sec, total_m


class TmapRoutesClient:
    def __init__(self, app_key: Optional[str] = None, timeout: float = 10.0,
                 http: Optional[TmapHttp] = None):
        self.http = http or TmapHttp(app_key=app_key, timeout=timeout)

    # 한 방향의 보행 경로 조회
    def pedestrian(self, start_lat, start_lon, end_lat, end_lon, start_name, end_name) -> tuple[int, int]:
        payload = {
            "startX": start_lon, "startY": start_lat,     # X = 경도, Y = 위도 (헷갈리기 쉬움)
            "endX": end_lon, "endY": end_lat,
            "startName": start_name, "endName": end_name,
            "reqCoordType": COORD_TYPE, "resCoordType": COORD_TYPE,
            "searchOption": 0,
        }
        return _parse_leg(self.http.post("/routes/pedestrian", payload))

    # 하나의 장소에 대해 왕복 경로 구성
    def round_trip(self, origin: Origin, place: Place) -> RoundTrip:
        # 가는 길, 오는 길을 따로 부른다. 편도 x2 로 대신하지 않는다 (설계서 규칙)
        out_sec, out_m = self.pedestrian(origin.latitude, origin.longitude, place.latitude, place.longitude,
                                         origin.name, place.name)
        in_sec, in_m = self.pedestrian(place.latitude, place.longitude, origin.latitude, origin.longitude,
                                       place.name, origin.name)
        return RoundTrip(poi_id=place.poi_id, outbound_sec=out_sec, inbound_sec=in_sec,
                         outbound_m=out_m, inbound_m=in_m, route_source="tmap")


class MockRoutesClient:
    """data/mock/routes_sample.json : poi_id -> {outbound: {...}, inbound: {...}}"""

    def __init__(self, fixture: str = "routes_sample"):
        with open(MOCK_DIR / f"{fixture}.json", encoding="utf-8") as f:
            self.routes = json.load(f)
        self.calls = 0

    def round_trip(self, origin: Origin, place: Place) -> RoundTrip:
        legs = self.routes.get(place.poi_id)
        if legs is None:
            raise ClientError("ROUTE_PARSE", f"{place.poi_id} 경로 없음")
        self.calls += 2
        out_sec, out_m = _parse_leg(legs["outbound"])
        in_sec, in_m = _parse_leg(legs["inbound"])     # 복귀 경로가 없으면 여기서 ROUTE_PARSE
        return RoundTrip(poi_id=place.poi_id, outbound_sec=out_sec, inbound_sec=in_sec,
                         outbound_m=out_m, inbound_m=in_m, route_source="mock")
