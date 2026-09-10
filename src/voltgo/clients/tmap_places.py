"""
TMAP 장소 API (필요_API_목록 T1 주변 카테고리 검색, T3 통합검색)

  GET https://apis.openapi.sk.com/tmap/pois/search/around
  GET https://apis.openapi.sk.com/tmap/pois
  헤더 appKey, 쿼리 version=1 필수

주의 (API규격_검토 §2)
  - radius 는 km 정수 1~33. 500m 는 코드에서 거른다 (core/place_policy.filter_places)
  - 주변검색 sort 기본값이 price 라서 sort=distance 를 꼭 준다
  - 응답 좌표는 문자열 -> float()
"""
import json
import os
from pathlib import Path
from typing import Optional

import requests

from voltgo.agent.schemas import Origin, Place
from voltgo.clients import ClientError
from voltgo.core.place_policy import CATEGORY_TO_TMAP, haversine_m

TMAP_URL = "https://apis.openapi.sk.com/tmap"
MOCK_DIR = Path(__file__).resolve().parents[3] / "data" / "mock"


def _raise_for_status(resp: requests.Response):
    if resp.status_code in (401, 403):
        raise ClientError("AUTH_ERROR", "appKey 오류")            # 재시도/Mock 전환 없음 (C009)
    if resp.status_code == 429:
        raise ClientError("RATE_LIMIT", "호출 한도", retryable=True)
    if resp.status_code >= 500:
        raise ClientError("UPSTREAM", f"HTTP {resp.status_code}", retryable=True)
    if resp.status_code != 200:
        raise ClientError("UPSTREAM", f"HTTP {resp.status_code}")


def _pois_from(body: dict) -> list[dict]:
    # 정상 빈 결과(totalCount=0) 는 오류가 아니다
    return ((body.get("searchPoiInfo") or {}).get("pois") or {}).get("poi") or []


def to_place(raw: dict, category: str, origin: Origin, source: str = "tmap") -> Place:
    """TMAP poi 원문 -> Place. 도보 목적지는 입구 좌표(frontLat/frontLon)"""
    lat = float(raw.get("frontLat") or raw["noorLat"])
    lon = float(raw.get("frontLon") or raw["noorLon"])
    return Place(
        poi_id=str(raw["id"]),
        name=str(raw["name"])[:100],
        category=category,
        latitude=lat,
        longitude=lon,
        distance_m=haversine_m(origin.latitude, origin.longitude, lat, lon),
        raw_category=str(raw.get("lowerBizName") or raw.get("middleBizName") or ""),
        opening_status="unknown",      # 상세 API(useTime) 를 안 쓰므로 MVP 는 항상 unknown
        poi_source=source,
    )


class TmapPlacesClient:
    def __init__(self, app_key: Optional[str] = None, timeout: float = 10.0):
        self.app_key = app_key or os.getenv("TMAP_APP_KEY", "")
        self.timeout = timeout
        if not self.app_key:
            raise ClientError("AUTH_ERROR", "TMAP_APP_KEY 가 없습니다")

    def _get(self, path: str, params: dict) -> dict:
        params = {"version": 1, **params}
        headers = {"appKey": self.app_key, "Accept": "application/json"}   # appKey 는 헤더로만
        try:
            resp = requests.get(f"{TMAP_URL}{path}", params=params, headers=headers, timeout=self.timeout)
        except requests.Timeout:
            raise ClientError("UPSTREAM", "timeout", retryable=True)
        _raise_for_status(resp)
        return resp.json()

    def search_around(self, origin: Origin, category: str, radius_km: int = 1, count: int = 20) -> list[Place]:
        body = self._get("/pois/search/around", {
            "centerLat": origin.latitude,
            "centerLon": origin.longitude,
            "categories": CATEGORY_TO_TMAP[category],
            "radius": radius_km,
            "count": count,
            "sort": "distance",
            "multiPoint": "N",
        })
        return [to_place(p, category, origin) for p in _pois_from(body)]

    def find_station(self, keyword: str) -> list[Origin]:
        """충전소 이름 -> 좌표 후보. 2개 이상이면 되묻는다."""
        body = self._get("/pois", {
            "searchKeyword": keyword,
            "searchType": "all",
            "searchtypCd": "R",
            "count": 5,
        })
        found = []
        for p in _pois_from(body):
            found.append(Origin(latitude=float(p["frontLat"]), longitude=float(p["frontLon"]),
                                name=str(p["name"]), source="manual"))
        return found


class MockPlacesClient:
    """data/mock/places_sample.json (TMAP 응답 구조 그대로) 를 읽는다"""

    def __init__(self, fixture: str = "places_sample"):
        with open(MOCK_DIR / f"{fixture}.json", encoding="utf-8") as f:
            self.body = json.load(f)
        self.calls = 0

    def search_around(self, origin: Origin, category: str, radius_km: int = 1, count: int = 20) -> list[Place]:
        self.calls += 1
        wanted = CATEGORY_TO_TMAP[category].split(";")
        places = []
        for p in _pois_from(self.body):
            if p.get("middleBizName") in wanted or p.get("lowerBizName") in wanted:
                places.append(to_place(p, category, origin, source="mock"))
        return places

    def find_station(self, keyword: str) -> list[Origin]:
        station = self.body.get("station", {})
        return [Origin(latitude=station["lat"], longitude=station["lon"], name=station["name"], source="mock")]
