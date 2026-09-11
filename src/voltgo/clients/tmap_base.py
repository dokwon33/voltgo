"""
TMAP 공통 HTTP 호출부 (장소 C / 경로 D 가 같이 쓴다)

  - 기본 주소 https://apis.openapi.sk.com/tmap, 쿼리 version=1, appKey 는 헤더로만 (설계서 2.5.3)
  - HTTP 상태 -> ClientError 표준 코드 (설계서 2.5.4)
  - 재시도는 여기서 하지 않는다. 읽기 도구 1회 추가 시도는 middleware 가 retryable 을 보고 한다.
    (여기서도 재시도하면 '읽기 시도 총 2회 이하' 를 넘는다)
"""
import os
from typing import Optional

import requests

from voltgo.clients import ClientError

TMAP_URL = "https://apis.openapi.sk.com/tmap"
COORD_TYPE = "WGS84GEO"


def _error_code(resp: requests.Response) -> str:
    """TMAP 오류 본문 {"error": {"code": "INVALID_API_KEY", ...}} 의 code. 없으면 빈 문자열"""
    try:
        return str(resp.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return ""


def _raise_for_status(resp: requests.Response):
    if resp.status_code in (401, 403):
        detail = _error_code(resp)
        raise ClientError("AUTH_ERROR", f"appKey 오류 ({detail})" if detail else "appKey 오류")  # 재시도/Mock 전환 없음 (C009)
    if resp.status_code == 429:
        raise ClientError("RATE_LIMIT", "호출 한도", retryable=True)
    if resp.status_code >= 500:
        raise ClientError("UPSTREAM", f"HTTP {resp.status_code}", retryable=True)
    if resp.status_code not in (200, 204):
        raise ClientError("UPSTREAM", f"HTTP {resp.status_code}")


def _json_or_empty(resp: requests.Response) -> dict:
    # 검색 결과가 없을 때 본문 없이 204 가 올 수 있다 -> 정상 빈 결과
    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        raise ClientError("UPSTREAM", "JSON 아님")


class TmapHttp:
    def __init__(self, app_key: Optional[str] = None, timeout: float = 10.0, session=None):
        self.app_key = app_key or os.getenv("TMAP_APP_KEY", "")
        self.timeout = timeout
        self.http = session or requests.Session()
        if not self.app_key:
            raise ClientError("AUTH_ERROR", "TMAP_APP_KEY 가 없습니다")

    def _headers(self) -> dict:
        return {"appKey": self.app_key, "Accept": "application/json"}

    def _send(self, method: str, path: str, params: dict, body: Optional[dict] = None) -> dict:
        params = {"version": 1, **params}
        try:
            resp = self.http.request(method, f"{TMAP_URL}{path}", params=params, json=body,
                                     headers=self._headers(), timeout=self.timeout)
        except requests.Timeout:
            raise ClientError("UPSTREAM", "timeout", retryable=True)
        except requests.RequestException as e:
            raise ClientError("UPSTREAM", f"연결 실패: {type(e).__name__}", retryable=True)
        _raise_for_status(resp)
        return _json_or_empty(resp)

    def get(self, path: str, params: dict) -> dict:
        return self._send("GET", path, params)

    def post(self, path: str, body: dict, params: Optional[dict] = None) -> dict:
        return self._send("POST", path, params or {}, body)
