"""웹 브리지: 지도 키 노출 범위, 차량 목표 표시, 카드 선택 검증, 새로고침·승인 시트 정보.
실제 외부 API·모델은 호출하지 않는다. HTTP 는 tests/test_web.py 의 Browser 로 쿠키까지 거친다."""
import json
from datetime import timedelta

import pytest

from tests.test_approval import ScriptedModel, _ai, _plan_ready, _tc
from tests.test_decide_entrypoint import _done
from tests.test_web import Browser, web  # noqa: F401  (web 은 fixture)
from voltgo.agent import memory
from voltgo.agent.agent import build_agent


@pytest.mark.parametrize("browser_key", [None, "  browser-map-test-key  "])
def test_map_config_exposes_only_explicit_browser_key(web, monkeypatch, browser_key):
    monkeypatch.setenv("TMAP_APP_KEY", "server-tmap-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "server-openai-test-key")
    if browser_key is None:
        monkeypatch.delenv("TMAP_MAP_APP_KEY", raising=False)
    else:
        monkeypatch.setenv("TMAP_MAP_APP_KEY", browser_key)
    browser = Browser(web, bootstrap=False)
    status, raw = browser.request("GET", "/map-config.js")
    config = json.loads(raw.decode().removeprefix("window.VOLTGO_MAP_CONFIG = ").removesuffix(";\n"))
    assert status == 200
    assert config == {"appKey": (browser_key or "").strip()}
    assert browser.headers["cache-control"] == "no-store"
    assert browser.headers["content-type"].startswith("application/javascript")
    assert b"server-tmap-test-key" not in raw and b"server-openai-test-key" not in raw


def test_legacy_session_target_cannot_override_vehicle_display(web, context, snapshot, budget):
    context.session.charging = snapshot.model_copy(update={"target_soc_pct": 90, "reported_target_soc_pct": 90, "reported_remaining_sec": 3600})
    context.session.time_budget = budget
    context.session.target_soc_pct = 80
    result = web.session_summary(context)
    assert result["charging"]["reported_target_soc_pct"] == 90
    assert result["effective_target_soc_pct"] == 90
    assert result["display_charging"]["target_soc_pct"] == 90
    assert result["display_charging"]["reported_remaining_sec"] == 3600
    assert "requested_target_soc_pct" not in result


def test_unknown_goal_stays_unknown(web, context, snapshot):
    context.session.charging = snapshot.model_copy(update={"target_soc_pct": None, "reported_target_soc_pct": None})
    result = web.session_summary(context)
    assert result["effective_target_soc_pct"] is None
    assert result["home_budget"] is None


def _selection(context):
    _plan_ready(context)
    candidate = context.session.candidates["A"]
    return {"selection": {"kind": "plan", "id": candidate.plan_id, "version": candidate.version,
                          "evaluated_at": candidate.evaluated_at.isoformat()}}


def test_clicked_card_uses_matching_version_and_generation(web, context):
    body = _selection(context)
    assert "confirm_plan" in web.validate_selection(context, body)
    body["selection"]["version"] += 1
    with pytest.raises(ValueError, match="조건이 바뀌었거나"):
        web.validate_selection(context, body)


@pytest.mark.parametrize("change", ["generation", "expiry", "conditions"])
def test_old_card_cannot_select_reused_place_id(web, context, change):
    body = _selection(context)
    if change == "generation":
        body["selection"]["evaluated_at"] = (context.clock() - timedelta(seconds=1)).isoformat()
    elif change == "expiry":
        now = context.clock()
        context.clock = lambda: now + timedelta(seconds=301)
    else:
        context.session.bump_version()
    with pytest.raises(ValueError):
        web.validate_selection(context, body)


def test_stale_card_is_rejected_over_http_without_model_call(web):
    browser = Browser(web)
    tid = browser.conversation()
    body = _selection(web.contexts[browser.user_id, tid])
    body["selection"]["version"] += 1
    status, data = browser.request("POST", "/api/ask", {"thread_id": tid, "text": "A로 갈래요", **body})
    assert status == 409 and "조건이 바뀌었거나" in data["error"]
    assert web.agent is None


def test_refresh_invalidates_cards_and_returns_map_coordinates(web):
    browser = Browser(web)
    tid = browser.conversation()
    context = web.contexts[browser.user_id, tid]
    _plan_ready(context)
    status, data = browser.request("POST", "/api/charging/refresh", {"thread_id": tid})
    assert status == 200 and data["result"]["status"] == "ok"
    assert data["session"]["candidates"] == []
    assert data["map_data"]["origin"]["latitude"] == context.session.origin.latitude
    assert data["map_data"]["places"]


def test_approval_sheet_shows_what_will_be_saved_and_runs_once(web):
    web.agent = build_agent(model=ScriptedModel(script=[
        _ai([_tc("save_preferences", {"category": "cafe", "dwell_min": 15}, "save")]), _done(),
    ]))
    browser = Browser(web)
    tid = browser.conversation()
    status, data = browser.request("POST", "/api/ask", {"thread_id": tid, "text": "카페 취향 기억해줘"})
    pending = data["pending_approval"]
    assert status == 200 and pending["approval_id"] == data["approval_id"]
    assert pending["actions"][0]["args"]["dwell_min"] == 15
    assert pending["expires_at"]
    body = {"thread_id": tid, "decision": "approve", "approval_id": pending["approval_id"]}
    status, data = browser.request("POST", "/api/decide", body)
    assert status == 200 and data["session"]["preferences"]["preferred_category"] == "cafe"
    assert data["pending_approval"] is None
    assert browser.request("POST", "/api/decide", body)[0] == 404   # 같은 승인 ID 는 다시 실행하지 않는다
    assert memory.load_preferences(browser.user_id).dwell_min == 15
