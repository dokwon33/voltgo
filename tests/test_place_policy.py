# 담당 C - 장소 정책 (반경 필터, 중복 제거, 체류 기본값)
from voltgo.agent.schemas import Place
from voltgo.core.place_policy import DWELL_DEFAULT_MIN, dwell_sec_for, filter_places, haversine_m


def _place(pid, dist):
    return Place(poi_id=pid, name=pid, category="meal", latitude=0, longitude=0, distance_m=dist, poi_source="mock")


def test_haversine_roughly_right():
    # 위도 0.001도 ≈ 111m
    assert 105 <= haversine_m(37.5, 127.0, 37.501, 127.0) <= 115


def test_filter_drops_600m_and_dedupes(origin):
    places = [_place("A", 250), _place("B", 300), _place("A", 250), _place("D", 600)]
    picked = filter_places(places, origin, max_dist_m=500)
    assert [p.poi_id for p in picked] == ["A", "B"]


def test_filter_limit_5(origin):
    places = [_place(str(i), i * 10) for i in range(10)]
    assert len(filter_places(places, origin, max_dist_m=500)) == 5


def test_dwell_defaults_and_override():
    assert dwell_sec_for("meal") == DWELL_DEFAULT_MIN["meal"] * 60
    assert dwell_sec_for("cafe", 20) == 1200
