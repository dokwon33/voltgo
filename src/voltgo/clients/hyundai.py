"""
현대자동차 Developers API 클라이언트 (필요_API_목록 H5)

  GET https://prd.kr-ccapi.hyundai.com/api/v1/car/status/{carId}/ev/charging
  Authorization: Bearer {access_token}

OAuth 로그인(H1~H3)은 브라우저가 필요해서 코드로 안 넣었다.
README 절차대로 토큰을 받아 .env 의 HYUNDAI_ACCESS_TOKEN / HYUNDAI_CAR_ID 에 넣는다.
"""
import os
from datetime import datetime
from typing import Optional

import requests

from voltgo.agent.schemas import ChargingSnapshot
from voltgo.agent.state import KST
from voltgo.clients import ClientError

BASE_URL = "https://prd.kr-ccapi.hyundai.com"

# remainTime.unit : 0 hour / 1 min / 2 msec / 3 sec  -> 초로 환산하는 계수
UNIT_TO_SEC = {0: 3600, 1: 60, 2: 0.001, 3: 1}

# batteryPlugin : 0 미연결 / 1 급속 / 2 완속
PLUG_TYPE = {0: "none", 1: "fast", 2: "slow"}

# 배터리 용량/전력은 API 가 안 준다 -> 팀 정책값 (급속 48kW, 완속 7kW)
DEFAULT_CAPACITY_KWH = 60.0
DEFAULT_POWER_KW = {"fast": 48.0, "slow": 7.0, "none": None}


def parse_timestamp(value: str) -> datetime:
    # 'YYYYMMDDHHmmSS', 시간대 정보 없음 -> Asia/Seoul 로 본다
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=KST)


def parse_charging_response(raw: dict, source: str = "hyundai",
                            capacity_kwh: Optional[float] = None,
                            avg_power_kw: Optional[float] = None) -> ChargingSnapshot:
    """
    현대차 원문 필드 -> ChargingSnapshot 어댑터 (API규격_검토 §1.1)
    Mock 도 원문 필드명을 그대로 쓰기 때문에 같은 함수를 쓴다.
    """
    plugin = int(raw.get("batteryPlugin", 0))
    charging = bool(raw.get("batteryCharge", False))
    if plugin == 0:
        charging = False           # 미연결이면 충전 중일 수 없다

    soc = raw.get("soc")
    target = (raw.get("targetSOC") or {}).get("targetSOClevel")

    # remainTime 은 미연결 상태에서는 가상값이라 plugin != 0 && charging 일 때만 믿는다
    remaining_sec = None
    remain = raw.get("remainTime")
    if remain and plugin != 0 and charging:
        unit = int(remain.get("unit", -1))
        if unit not in UNIT_TO_SEC:
            raise ClientError("INVALID_UNIT", f"remainTime.unit={unit}")
        remaining_sec = int(round(float(remain["value"]) * UNIT_TO_SEC[unit]))

    plug_type = PLUG_TYPE.get(plugin, "none")

    return ChargingSnapshot(
        charging=charging,
        soc_pct=float(soc) if soc is not None else None,
        target_soc_pct=float(target) if target is not None else 80,
        capacity_kwh=capacity_kwh if capacity_kwh is not None else DEFAULT_CAPACITY_KWH,
        avg_power_kw=avg_power_kw if avg_power_kw is not None else DEFAULT_POWER_KW[plug_type],
        reported_remaining_sec=remaining_sec,
        plug_type=plug_type,
        observed_at=parse_timestamp(raw["timestamp"]),
        source=source,
    )


class HyundaiClient:
    def __init__(self, access_token: Optional[str] = None, car_id: Optional[str] = None, timeout: float = 10.0):
        self.access_token = access_token or os.getenv("HYUNDAI_ACCESS_TOKEN", "")
        self.car_id = car_id or os.getenv("HYUNDAI_CAR_ID", "")
        self.timeout = timeout
        if not self.access_token or not self.car_id:
            raise ClientError("AUTH_ERROR", "HYUNDAI_ACCESS_TOKEN / HYUNDAI_CAR_ID 가 없습니다")

    def fetch_raw(self) -> dict:
        url = f"{BASE_URL}/api/v1/car/status/{self.car_id}/ev/charging"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
        except requests.Timeout:
            raise ClientError("UPSTREAM", "timeout", retryable=True)

        if resp.status_code == 401:
            raise ClientError("AUTH_EXPIRED", "token 만료", retryable=False)
        if resp.status_code >= 500:
            raise ClientError("UPSTREAM", f"HTTP {resp.status_code}", retryable=True)

        body = resp.json()
        err = str(body.get("errCode", ""))
        if err in ("4045", "4046"):
            raise ClientError("NO_VEHICLE", "등록된 차량 없음")
        if err == "4002":
            raise ClientError("BAD_REQUEST", "잘못된 요청")
        if resp.status_code != 200:
            raise ClientError("UPSTREAM", f"HTTP {resp.status_code}")
        return body

    def get_charging_status(self) -> ChargingSnapshot:
        return parse_charging_response(self.fetch_raw(), source="hyundai")
