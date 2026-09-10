"""
현대차 Mock 공급자 (USE_MOCK_CHARGING=true)

data/mock/charging_*.json 을 원문 필드명 그대로 읽고, hyundai.py 의 어댑터로 변환한다.
timestamp 는 fixture 값 대신 '지금' 으로 바꿔 넣는다. (안 그러면 60초 신선도 검사에 걸림)
"""
import json
from pathlib import Path
from typing import Callable, Optional

from voltgo.agent.schemas import ChargingSnapshot
from voltgo.agent.state import now_kst
from voltgo.clients.hyundai import parse_charging_response

MOCK_DIR = Path(__file__).resolve().parents[3] / "data" / "mock"


def load_raw(fixture: str = "charging_ok") -> dict:
    with open(MOCK_DIR / f"{fixture}.json", encoding="utf-8") as f:
        return json.load(f)


class MockChargingProvider:
    def __init__(self, fixture: str = "charging_ok", clock: Callable = now_kst,
                 capacity_kwh: Optional[float] = None, avg_power_kw: Optional[float] = None):
        self.fixture = fixture
        self.clock = clock
        self.capacity_kwh = capacity_kwh
        self.avg_power_kw = avg_power_kw
        self.calls = 0

    def get_charging_status(self) -> ChargingSnapshot:
        raw = load_raw(self.fixture)
        raw["timestamp"] = self.clock().strftime("%Y%m%d%H%M%S")
        self.calls += 1
        return parse_charging_response(raw, source="mock",
                                       capacity_kwh=self.capacity_kwh, avg_power_kw=self.avg_power_kw)
