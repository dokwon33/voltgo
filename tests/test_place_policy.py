# 담당 C - 장소 정책 (반경 필터, 중복 제거, 체류 기본값)
import pytest

from voltgo.agent.schemas import Place
from voltgo.core.place_policy import (
    DWELL_DEFAULT_MIN, auto_max_dist_m, dwell_sec_for, filter_places, haversine_m,
)


def _place(pid, dist):
    return Place(poi_id=pid, name=pid, category="meal", latitude=0, longitude=0, distance_m=dist, poi_source="mock")


def test_haversine_roughly_right():
    # 위도 0.001도 ≈ 111m
    assert 105 <= haversine_m(37.5, 127.0, 37.501, 127.0) <= 115


def test_filter_drops_600m_and_dedupes(origin):
    places = [_place("A", 250), _place("B", 300), _place("A", 250), _place("D", 600)]
    picked = filter_places(places, origin, max_dist_m=500)
    assert [p.poi_id for p in picked] == ["A", "B"]


def test_filter_500m_boundary(origin):
    # 500m 는 포함, 501m 는 제외
    places = [_place("in", 500), _place("out", 501), _place("near", 499)]
    assert [p.poi_id for p in filter_places(places, origin, max_dist_m=500)] == ["near", "in"]


def test_filter_limit_5(origin):
    places = [_place(str(i), i * 10) for i in range(10)]
    assert len(filter_places(places, origin, max_dist_m=500)) == 5


def test_dwell_defaults_and_override():
    assert dwell_sec_for("meal") == DWELL_DEFAULT_MIN["meal"] * 60
    assert dwell_sec_for("cafe", 20) == 1200


# 남은 시간으로 반경 자동 조정 (최소 500m, 최대 1000m)
@pytest.mark.parametrize("available_min,dwell_min,expected", [
    (25, 20, 500),     # 빠듯함 -> 줄이지 않고 기본 500m
    (10, 20, 500),     # 체류가 더 길어도 500m (체류 기본값으로 후보를 빼지 않는다)
    (40, 20, 540),     # 편도 10분 x 70m / 1.3 ≈ 538m
    (60, 0, 1000),     # 넉넉함 -> 상한 1000m
])
def test_auto_max_dist(available_min, dwell_min, expected):
    assert auto_max_dist_m(available_min * 60, dwell_min * 60) == expected
