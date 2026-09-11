"""
TMAP 장소 API 실호출 확인용 (담당 C). 설계서 '실호출 확인 후 확정' 항목을 채우기 위한 탐색 스크립트.

  python scripts/probe_tmap_places.py "역삼역 전기차충전소"

- .env 의 TMAP_APP_KEY 필요
- 원문 응답은 data/raw/tmap/ 에 저장 (git 제외). 테스트 fixture 로 쓸 때는 필요한 필드만 골라 data/mock/ 에 옮긴다
- 호출 수: 통합검색 1 + 카테고리 4 = 5회
"""
import json
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voltgo.clients import ClientError  # noqa: E402
from voltgo.clients.tmap_base import COORD_TYPE, TmapHttp
from voltgo.clients.tmap_places import _parse_places, _parse_stations, _pois_from
from voltgo.core.place_policy import CATEGORY_TO_TMAP, DEFAULT_DIST_M, filter_places

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw" / "tmap"


def save(name: str, body: dict):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def main(keyword: str):
    load_dotenv()
    try:
        http = TmapHttp()
    except ClientError:
        sys.exit(".env 에 TMAP_APP_KEY 를 넣어 주세요 (cp .env.example .env)")

    # 1. 통합검색 -> 충전소 후보
    body = http.get("/pois", {"searchKeyword": keyword, "searchType": "all", "searchtypCd": "A", "count": 5,
                              "page": 1, "multiPoint": "N", "reqCoordType": COORD_TYPE, "resCoordType": COORD_TYPE})
    save("station_search", body)
    raw_pois = _pois_from(body)
    stations = _parse_stations(raw_pois, "manual")
    print(f"[통합검색] '{keyword}' 원문 {len(raw_pois)}건 -> 유효 {len(stations)}건")
    for s in stations:
        print(f"  {s.poi_id} {s.name} ({s.address}) {s.latitude:.5f},{s.longitude:.5f}")
    if not stations:
        print("충전소를 못 찾았습니다. 다른 이름으로 다시 시도하세요.")
        return
    origin = stations[0]
    print(f"-> 출발지: {origin.name}\n")

    # 2. 카테고리별 주변 검색 (radius=1km) -> 500m 필터
    for category, tmap_cat in CATEGORY_TO_TMAP.items():
        body = http.get("/pois/search/around", {
            "centerLat": origin.latitude, "centerLon": origin.longitude, "categories": tmap_cat,
            "radius": 1, "count": 20, "page": 1, "sort": "distance", "multiPoint": "N",
            "reqCoordType": COORD_TYPE, "resCoordType": COORD_TYPE,
        })
        save(f"around_{category}", body)
        raw_pois = _pois_from(body)
        places = _parse_places(raw_pois, category, origin, "tmap")
        picked = filter_places(places, origin, max_dist_m=DEFAULT_DIST_M)
        biz = Counter(f"{p.get('middleBizName')}/{p.get('lowerBizName')}" for p in raw_pois)
        print(f"[주변검색] {category} ({tmap_cat}) 원문 {len(raw_pois)}건 / 주차장·좌표오류 제외 {len(places)}건 "
              f"/ {DEFAULT_DIST_M}m 이내 상위 {len(picked)}건")
        print(f"  업종 분포: {dict(biz.most_common(5))}")
        for p in picked:
            print(f"  {p.poi_id} {p.name} {p.distance_m}m [{p.raw_category}]")
        if raw_pois and "radius" in raw_pois[0]:
            print(f"  (응답 radius 필드 예: {raw_pois[0]['radius']} / 코드 거리 {places[0].distance_m if places else '-'}m)")
        print()

    print(f"원문 저장: {RAW_DIR}")


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "역삼역 전기차충전소")
    except ClientError as e:
        hint = " -> 키 값과 TMAP 상품 사용 신청 여부를 확인하세요" if e.code == "AUTH_ERROR" else ""
        sys.exit(f"[{e.code}] {e}{hint}")
