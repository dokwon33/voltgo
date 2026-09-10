"""
TMAP 장소 API (필요_API_목록 T1 주변 카테고리 검색, T3 통합검색)

  GET https://apis.openapi.sk.com/tmap/pois/search/around
  GET https://apis.openapi.sk.com/tmap/pois
  헤더 appKey, 쿼리 version=1 필수 (공통 처리는 clients/tmap_base.py)

주의 (API규격_검토 §2, 설계서 2.5.3)
  - radius 는 km 정수 1~33. 500m 는 코드에서 거른다 (core/place_policy.filter_places)
  - 주변검색 sort 기본값이 price 라서 sort=distance 를 꼭 준다. 통합검색 거리순은 searchtypCd=R
  - 응답 좌표는 문자열 -> float(). 도보 목적지는 입구 좌표(frontLat/frontLon)
  - 입구 좌표가 잘못된 POI 는 중심 좌표로 고치지 않고 후보에서 뺀다
"""
import json
from pathlib import Path
from typing import Optional

from voltgo.agent.schemas import Origin, Place, StationCandidate
from voltgo.clients import ClientError
from voltgo.clients.tmap_base import COORD_TYPE, TMAP_URL, TmapHttp, _raise_for_status  # noqa: F401 (tmap_routes 호환)
from voltgo.core.place_policy import CATEGORY_TO_TMAP, haversine_m

MOCK_DIR = Path(__file__).resolve().parents[3] / "data" / "mock"

# 좌표 sanity check 범위 (대한민국 대략)
_KR_LAT = (33.0, 39.5)
_KR_LON = (124.0, 132.0)


def _pois_from(body: dict) -> list[dict]:
    # 정상 빈 결과(totalCount=0, 204) 는 오류가 아니다
    return ((body.get("searchPoiInfo") or {}).get("pois") or {}).get("poi") or []


def front_coords(raw: dict) -> Optional[tuple[float, float]]:
    """입구 좌표 (lat, lon). 없거나 숫자가 아니거나 국내 범위 밖이면 None"""
    try:
        lat = float(raw["frontLat"])
        lon = float(raw["frontLon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (_KR_LAT[0] <= lat <= _KR_LAT[1] and _KR_LON[0] <= lon <= _KR_LON[1]):
        return None
    return lat, lon


def _address(raw: dict) -> str:
    parts = [raw.get(k) for k in ("upperAddrName", "middleAddrName", "lowerAddrName", "detailAddrName")]
    addr = " ".join(str(p) for p in parts if p)
    first, second = raw.get("firstNo"), raw.get("secondNo")
    if first and str(first) != "0":
        addr += f" {first}" + (f"-{second}" if second and str(second) != "0" else "")
    return addr.strip()


def to_place(raw: dict, category: str, origin: Origin, source: str = "tmap") -> Optional[Place]:
    """TMAP poi 원문 -> Place. 입구 좌표가 잘못됐으면 None (후보 제외)"""
    coords = front_coords(raw)
    if coords is None or not raw.get("id"):
        return None
    lat, lon = coords
    return Place(
        poi_id=str(raw["id"]),
        name=str(raw.get("name") or "")[:100],
        category=category,
        latitude=lat,
        longitude=lon,
        distance_m=haversine_m(origin.latitude, origin.longitude, lat, lon),
        # 주변검색 응답에는 업종 필드가 없다 (통합검색에만 있음) -> 보통 빈 문자열
        raw_category=str(raw.get("lowerBizName") or raw.get("middleBizName") or ""),
        nav_seq=str(raw.get("navSeq") or ""),
        opening_status="unknown",      # 상세 API(useTime) 를 안 쓰므로 MVP 는 항상 unknown
        poi_source=source,
    )


def to_station(raw: dict, source: str = "manual") -> Optional[StationCandidate]:
    """통합검색 poi 원문 -> 충전소 후보. location_source 는 사용자가 정한 출발점이라 manual"""
    coords = front_coords(raw)
    if coords is None or not raw.get("id"):
        return None
    lat, lon = coords
    return StationCandidate(
        poi_id=str(raw["id"]),
        latitude=lat,
        longitude=lon,
        name=str(raw.get("name") or "충전소")[:100],
        address=_address(raw),
        nav_seq=str(raw.get("navSeq") or ""),
        source=source,
    )


def _is_parking(raw: dict) -> bool:
    return "주차장" in str(raw.get("name") or "")


def _parse_places(pois: list[dict], category: str, origin: Origin, source: str) -> list[Place]:
    """
    실호출 확인 (2026-09-10): 주변검색은 같은 id 로 '대동천[중식]' 과 '대동천 주차장[중식]' 을 같이 준다.
    주차장 항목은 입구 좌표가 주차장이라 도보 목적지로 쓰면 안 된다 -> 본 장소를 남기고, 주차장만 있으면 뺀다.
    """
    by_id: dict[str, Place] = {}
    for raw in pois:
        if _is_parking(raw):
            continue
        p = to_place(raw, category, origin, source)
        if p is not None and p.poi_id not in by_id:
            by_id[p.poi_id] = p
    return list(by_id.values())


def _parse_stations(pois: list[dict], source: str) -> list[StationCandidate]:
    seen, found = set(), []
    for p in pois:
        s = to_station(p, source)
        if s is None or s.poi_id in seen:
            continue
        seen.add(s.poi_id)
        found.append(s)
    return found


class TmapPlacesClient:
    def __init__(self, app_key: Optional[str] = None, timeout: float = 10.0, http: Optional[TmapHttp] = None):
        self.http = http or TmapHttp(app_key=app_key, timeout=timeout)

    def search_around(self, origin: Origin, category: str, radius_km: int = 1, count: int = 20) -> list[Place]:
        if category not in CATEGORY_TO_TMAP:
            raise ClientError("NEED_INPUT", f"지원하지 않는 카테고리: {category}")
        body = self.http.get("/pois/search/around", {
            "centerLat": origin.latitude,
            "centerLon": origin.longitude,
            "categories": CATEGORY_TO_TMAP[category],
            "radius": int(radius_km),
            "count": count,
            "page": 1,
            "sort": "distance",
            "multiPoint": "N",
            "reqCoordType": COORD_TYPE,
            "resCoordType": COORD_TYPE,
        })
        return _parse_places(_pois_from(body), category, origin, "tmap")

    def find_station(self, keyword: str, count: int = 5) -> list[StationCandidate]:
        """충전소 이름 -> 좌표 후보. 2개 이상이면 도구가 사용자에게 되묻는다."""
        keyword = keyword.strip()
        if not keyword:
            return []
        body = self.http.get("/pois", {
            "searchKeyword": keyword,
            "searchType": "all",
            "searchtypCd": "A",          # 이름 검색은 정확도순. (거리순 R 은 기준 좌표가 있어야 의미가 있다)
            "count": count,
            "page": 1,
            "multiPoint": "N",
            "reqCoordType": COORD_TYPE,
            "resCoordType": COORD_TYPE,
        })
        return _parse_stations(_pois_from(body), "manual")


class MockPlacesClient:
    """data/mock/places_sample.json, stations_sample.json (TMAP 응답 구조 그대로) 를 읽는다.
    places_sample 은 카테고리별 주변검색 응답 {category: 응답 본문} 이다. (업종 필터는 서버가 하므로)"""

    def __init__(self, fixture: str = "places_sample", stations_fixture: str = "stations_sample"):
        with open(MOCK_DIR / f"{fixture}.json", encoding="utf-8") as f:
            self.body = json.load(f)
        with open(MOCK_DIR / f"{stations_fixture}.json", encoding="utf-8") as f:
            self.stations_body = json.load(f)
        self.calls = 0              # 주변 검색 호출 수
        self.station_calls = 0      # 충전소 검색 호출 수

    def search_around(self, origin: Origin, category: str, radius_km: int = 1, count: int = 20) -> list[Place]:
        self.calls += 1
        return _parse_places(_pois_from(self.body.get(category) or {}), category, origin, "mock")

    def find_station(self, keyword: str, count: int = 5) -> list[StationCandidate]:
        """이름에 keyword 가 들어간 충전소. 빈 keyword 는 전체 (첫 번째가 시연 기본 충전소)"""
        self.station_calls += 1
        key = keyword.replace(" ", "")
        pois = [p for p in _pois_from(self.stations_body) if key in str(p.get("name", "")).replace(" ", "")]
        return _parse_stations(pois, "mock")[:count]
