# 담당 D - TMAP 보행 경로 어댑터와 부분 실패 처리 (C010, C030)
import json
from types import SimpleNamespace

import pytest
import requests
from langchain.messages import ToolMessage

from voltgo.agent import tools
from voltgo.agent.middleware import tool_policy
from voltgo.agent.schemas import Place
from voltgo.clients import ClientError
from voltgo.clients.tmap_base import TmapHttp
from voltgo.clients.tmap_routes import TmapRoutesClient


class FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.content = (json.dumps(body) if body is not None else "").encode()

    def json(self):
        return json.loads(self.content)


class FakeSession:
    """requests.Session 대신 요청을 기록하고 준비한 응답을 순서대로 반환한다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(SimpleNamespace(
            method=method, url=url, params=params, body=json, headers=headers, timeout=timeout,
        ))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def leg(total_sec=240, total_m=320):
    return {"features": [{"properties": {"totalTime": total_sec, "totalDistance": total_m}}]}


def client_with(*responses):
    fake = FakeSession(*responses)
    http = TmapHttp(app_key="test-key", timeout=3.5, session=fake)
    return TmapRoutesClient(http=http), fake


def rt(context):
    return SimpleNamespace(context=context)


def place(pid, name, lat, lon):
    return Place(
        poi_id=pid,
        name=name,
        category="meal",
        latitude=lat,
        longitude=lon,
        nav_seq="1",
        distance_m=100,
        poi_source="tmap",
    )


def test_pedestrian_request_body_and_parse():
    client, fake = client_with(FakeResp(200, leg("321", "456")))

    result = client.pedestrian(37.5006, 127.0366, 37.502, 127.038, "충전소", "김밥집")

    assert result == (321, 456)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.method == "POST" and call.url.endswith("/tmap/routes/pedestrian")
    assert call.params == {"version": 1}
    assert call.body == {
        "startX": 127.0366,
        "startY": 37.5006,
        "endX": 127.038,
        "endY": 37.502,
        "startName": "충전소",
        "endName": "김밥집",
        "reqCoordType": "WGS84GEO",
        "resCoordType": "WGS84GEO",
        "searchOption": 0,
    }
    assert call.headers["appKey"] == "test-key"
    assert "appKey" not in call.params
    assert call.timeout == 3.5


def test_round_trip_uses_two_separate_reversed_requests(origin):
    client, fake = client_with(FakeResp(200, leg(240, 300)), FakeResp(200, leg(330, 410)))
    destination = place("A", "후보A", 37.502, 127.038)

    trip = client.round_trip(origin, destination)

    assert (trip.outbound_sec, trip.outbound_m) == (240, 300)
    assert (trip.inbound_sec, trip.inbound_m) == (330, 410)
    assert trip.route_source == "tmap"
    assert len(fake.calls) == 2
    outbound, inbound = (call.body for call in fake.calls)
    assert (outbound["startName"], outbound["endName"]) == (origin.name, destination.name)
    assert (inbound["startName"], inbound["endName"]) == (destination.name, origin.name)
    assert (inbound["startX"], inbound["startY"]) == (destination.longitude, destination.latitude)
    assert (inbound["endX"], inbound["endY"]) == (origin.longitude, origin.latitude)


@pytest.mark.parametrize("body", [
    {"features": [{"properties": {"totalDistance": 100}}]},
    {"features": [{"properties": {"totalTime": 100}}]},
    {"features": []},
])
def test_missing_route_totals_are_route_parse(body):
    client, _ = client_with(FakeResp(200, body))

    with pytest.raises(ClientError) as exc_info:
        client.pedestrian(37.5, 127.0, 37.6, 127.1, "출발", "도착")

    assert (exc_info.value.code, exc_info.value.retryable) == ("ROUTE_PARSE", False)


@pytest.mark.parametrize("status_code,code,retryable", [
    (401, "AUTH_ERROR", False),
    (403, "AUTH_ERROR", False),
    (429, "RATE_LIMIT", True),
    (503, "UPSTREAM", True),
    (400, "UPSTREAM", False),
])
def test_http_error_mapping(status_code, code, retryable):
    client, fake = client_with(FakeResp(status_code))

    with pytest.raises(ClientError) as exc_info:
        client.pedestrian(37.5, 127.0, 37.6, 127.1, "출발", "도착")

    assert (exc_info.value.code, exc_info.value.retryable) == (code, retryable)
    assert len(fake.calls) == 1


def test_c010_inbound_failure_drops_only_that_candidate(context, budget):
    # A: outbound 성공, inbound totalTime 누락. B: 양방향 성공.
    client, fake = client_with(
        FakeResp(200, leg(240, 300)),
        FakeResp(200, {"features": [{"properties": {"totalDistance": 350}}]}),
        FakeResp(200, leg(300, 380)),
        FakeResp(200, leg(360, 420)),
    )
    context.routes_client = client
    context.session.places = {
        "A": place("A", "후보A", 37.502, 127.038),
        "B": place("B", "후보B", 37.503, 127.039),
    }

    result = tools.get_walking_routes.func(rt(context), poi_ids=["A", "B"])

    assert result["status"] == "partial"
    assert [route["poi_id"] for route in result["data"]] == ["B"]
    assert set(context.session.routes) == {"B"}
    assert "A" in context.session.warnings[-1]
    assert len(fake.calls) == 4
    # A의 outbound 240초를 두 배한 경로가 남아 있지 않는다.
    assert all(route["poi_id"] != "A" for route in result["data"])

    context.session.time_budget = budget
    plans = tools.select_feasible_plans.func(rt(context), dwell_min=5)
    assert [plan["poi_id"] for plan in plans["data"]] == ["B"]


@pytest.mark.parametrize("first_response", [FakeResp(503), requests.Timeout()])
def test_retryable_route_error_is_preserved_and_middleware_retries(context, first_response):
    client, fake = client_with(
        first_response,
        FakeResp(200, leg(240, 300)),
        FakeResp(200, leg(300, 350)),
    )
    context.routes_client = client
    context.session.places = {"A": place("A", "후보A", 37.502, 127.038)}
    request = SimpleNamespace(
        tool_call={"id": "route-call", "name": "get_walking_routes", "args": {"poi_ids": ["A"]}},
        runtime=rt(context),
    )
    handler_calls = 0
    handler_payloads = []

    def handler(_request):
        nonlocal handler_calls
        handler_calls += 1
        result = tools.get_walking_routes.func(rt(context), poi_ids=["A"])
        handler_payloads.append(result)
        return ToolMessage(
            content=json.dumps(result),
            tool_call_id="route-call",
            name="get_walking_routes",
        )

    result = tool_policy.wrap_tool_call(request, handler)
    payload = json.loads(result.content)

    assert handler_calls == 2
    assert handler_payloads[0]["status"] == "error"
    assert handler_payloads[0]["error_code"] == "UPSTREAM"
    assert handler_payloads[0]["retryable"] is True
    assert len(fake.calls) == 3                  # 첫 실패 1회 + 재시도 왕복 2회
    assert payload["status"] == "ok"
    assert [route["poi_id"] for route in payload["data"]] == ["A"]


def _call_routes_with_policy(context, poi_ids):
    request = SimpleNamespace(
        tool_call={"id": "mixed-route-call", "name": "get_walking_routes", "args": {"poi_ids": poi_ids}},
        runtime=rt(context),
    )
    payloads = []

    def handler(request):
        result = tools.get_walking_routes.func(request.runtime, **request.tool_call["args"])
        payloads.append(result)
        return ToolMessage(content=json.dumps(result), tool_call_id="mixed-route-call", name="get_walking_routes")

    result = tool_policy.wrap_tool_call(request, handler)
    return json.loads(result.content), payloads


@pytest.mark.parametrize("failure", ["503", "timeout"])
@pytest.mark.parametrize("poi_ids", [["A", "B"], ["B", "A"]], ids=["parse_first", "transient_first"])
def test_mixed_route_failures_retry_regardless_of_candidate_order(context, places, failure, poi_ids):
    # A는 항상 파싱 실패. B는 일시 오류 뒤 재시도하면 왕복 경로를 복구할 수 있다.
    invalid = FakeResp(200, {"features": [{"properties": {"totalDistance": 320}}]})
    transient = FakeResp(503) if failure == "503" else requests.Timeout()
    outbound, inbound = FakeResp(200, leg(240, 300)), FakeResp(200, leg(300, 350))
    responses = ([invalid, transient, invalid, outbound, inbound] if poi_ids[0] == "A"
                 else [transient, invalid, outbound, inbound, invalid])
    client, fake = client_with(*responses)
    context.routes_client = client
    context.session.places = dict(places)

    payload, attempts = _call_routes_with_policy(context, poi_ids)

    assert len(attempts) == 2
    assert (attempts[0]["error_code"], attempts[0]["retryable"]) == ("UPSTREAM", True)
    assert payload["status"] == "partial"
    assert [route["poi_id"] for route in payload["data"]] == ["B"]
    assert set(context.session.routes) == {"B"}
    assert len(fake.calls) == 5                 # 첫 실패 2회 + 재시도에서 A 실패 1회/B 왕복 2회


def test_nonretryable_route_failures_do_not_retry(context, places):
    client, fake = client_with(FakeResp(200, {"features": []}), FakeResp(400))
    context.routes_client = client
    context.session.places = dict(places)

    payload, attempts = _call_routes_with_policy(context, ["A", "B"])

    assert payload["status"] == "error"
    assert (payload["error_code"], payload["retryable"]) == ("ROUTE_PARSE", False)
    assert len(attempts) == 1 and len(fake.calls) == 2
    assert context.session.routes == {}


def test_mixed_route_failures_retry_at_most_once(context, places):
    client, fake = client_with(
        FakeResp(200, {"features": []}), FakeResp(503),
        FakeResp(200, {"features": []}), FakeResp(503),
    )
    context.routes_client = client
    context.session.places = dict(places)

    payload, attempts = _call_routes_with_policy(context, ["A", "B"])

    assert payload["status"] == "error"
    assert (payload["error_code"], payload["retryable"]) == ("UPSTREAM", True)
    assert len(attempts) == 2 and len(fake.calls) == 4
    assert context.session.routes == {}
