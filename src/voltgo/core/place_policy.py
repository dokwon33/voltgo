"""
장소 정책 (설계서 2.2 체류 기본값, API규격_검토 §2.3 카테고리 매핑)
"""
import math

from voltgo.agent.schemas import Origin, Place

# 설계서 category -> TMAP categories 파라미터 (';' 로 여러 업종 한 번에)
CATEGORY_TO_TMAP = {
    "meal": "음식",
    "cafe": "카페;커피",
    "convenience": "편의점",
    "mart": "마트;대형마트",
}

# 체류 기본값(분). 실측이 아니라 팀 정책값이다. 사용자 입력이 우선.
DWELL_DEFAULT_MIN = {
    "meal": 20,
    "cafe": 15,
    "convenience": 10,
    "mart": 25,
}

MAX_ROUTE_CANDIDATES = 5   # 경로 조회는 최대 5개까지만 (호출 상한)
DEFAULT_DIST_M = 500       # 기본 도보 반경 (편도 약 6~7분)
MAX_DIST_M = 1000          # 결과가 없을 때 1회 재검색으로 넓힐 수 있는 상한 (설계서 2.5.1)


def haversine_m(lat1, lon1, lat2, lon2) -> int:
    """두 좌표 사이 직선 거리(m)"""
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(round(2 * r * math.asin(math.sqrt(a))))


def filter_places(places: list[Place], origin: Origin, max_dist_m: int = 500,
                  limit: int = MAX_ROUTE_CANDIDATES) -> list[Place]:
    """
    TMAP radius 는 km 정수라 500m 지정이 안 된다.
    -> radius=1 로 받아온 뒤 여기서 직선거리 <= max_dist_m 만 남긴다.
    poi_id 중복 제거, 가까운 순 정렬, 최대 limit 개.
    """
    seen = set()
    picked = []
    for p in places:
        if p.poi_id in seen:
            continue
        seen.add(p.poi_id)
        if p.distance_m <= max_dist_m:
            picked.append(p)

    picked.sort(key=lambda p: (p.distance_m, p.poi_id))
    return picked[:limit]


def dwell_sec_for(category: str, override_min=None) -> int:
    minutes = override_min if override_min is not None else DWELL_DEFAULT_MIN[category]
    return int(minutes) * 60
