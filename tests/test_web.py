"""실제 HTTP 파서·쿠키·HITL을 통한 브라우저별 격리. 외부 모델/API는 호출하지 않는다."""
import copy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_approval import ScriptedModel, _ai, _tc, _plan_ready
from tests.test_decide_entrypoint import _done
from voltgo.agent import memory
from voltgo.agent.agent import build_agent
from voltgo.web_sessions import BrowserSessions, COOKIE_NAME


@pytest.fixture
def web(context, monkeypatch):
    spec = importlib.util.spec_from_file_location("voltgo_web_test", Path(__file__).resolve().parents[1] / "scripts/web.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def make_context(user_id, **kwargs):
        fresh = copy.deepcopy(context)
        fresh.user_id = user_id
        return fresh

    monkeypatch.setattr(module, "make_context", make_context)
    monkeypatch.setattr(module.Handler, "log_message", lambda *a: None)
    return module


class Socket:
    def __init__(self, raw):
        self.input = io.BytesIO(raw)
        self.output = io.BytesIO()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class Browser:
    """서로 같은 IP에서도 쿠키 저장소만 다른 두 브라우저를 재현한다."""
    def __init__(self, web, bootstrap=True):
        self.web = web
        self.cookie = ""
        self.user_id = None
        self.headers = {}
        if bootstrap:
            status, payload = self.request("GET", "/api/health")
            assert status == 200
            self.user_id = payload["user_id"]

    def request(self, method, path, body=None, *, raw_body=None, length=None, headers=None):
        raw = raw_body if raw_body is not None else json.dumps(body).encode() if body is not None else b""
        fields = {"Host": "localhost:8000", "Content-Type": "application/json",
                  "Content-Length": str(len(raw) if length is None else length)}
        if self.cookie:
            fields["Cookie"] = self.cookie
        fields.update(headers or {})
        head = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
        socket = Socket(head.encode() + raw)
        self.web.Handler(socket, ("127.0.0.1", 12345), SimpleNamespace())
        head, data = socket.output.getvalue().split(b"\r\n\r\n", 1)
        lines = head.decode().splitlines()
        self.headers = dict((k.lower(), v.strip()) for k, v in (line.split(":", 1) for line in lines[1:]))
        if "set-cookie" in self.headers:
            self.cookie = self.headers["set-cookie"].split(";", 1)[0]
        return int(lines[0].split()[1]), json.loads(data) if "application/json" in self.headers.get("content-type", "") else data

    def conversation(self):
        status, data = self.request("POST", "/api/session", {})
        assert status == 201
        assert data["session"]["user_id"] == self.user_id
        return data["thread_id"]

    def summary(self, tid):
        return self.request("GET", "/api/session?thread_id=" + tid)


def test_home_assets_and_session_work_without_model_key(web, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    browser = Browser(web)
    for path in ["/", "/map.js", "/map.css", "/map-config.js", "/session.js", "/img/volty_hello.png"]:
        status, content = browser.request("GET", path)
        assert status == 200 and content
    tid = browser.conversation()
    status, data = browser.summary(tid)
    assert status == 200
    assert data["session"]["charging"]["source"] == "mock"
    assert data["session"]["home_budget"] is not None
    status, data = browser.request("POST", "/api/ask", {"thread_id": tid, "text": "밥 먹고 싶어"})
    assert status == 503 and "OPENAI_API_KEY" in data["error"]


@pytest.mark.parametrize("body,raw,length,status", [
    (None, b"{", None, 400), ([], None, None, 400),
    ({"text": "질문", "thread_id": []}, None, None, 400),
    ({"text": 1, "thread_id": "x"}, None, None, 400),
    ({"text": " ", "thread_id": "x"}, None, None, 400),
    (None, b"", "invalid", 400), (None, b"", 16385, 413),
])
def test_malformed_requests_return_json_errors(web, body, raw, length, status):
    code, data = Browser(web).request("POST", "/api/ask", body, raw_body=raw, length=length)
    assert code == status and data["error"]
    assert web.contexts == {}


def test_unknown_route_and_invalid_decision(web):
    browser = Browser(web)
    tid = browser.conversation()
    assert browser.request("POST", "/api/unknown", {})[0] == 404
    assert browser.request("POST", "/api/decide", {"thread_id": tid, "decision": "yes"})[0] == 400


def test_web_confirm_has_no_extra_approval(web, context):
    version = _plan_ready(context)
    web.agent = build_agent(model=ScriptedModel(script=[
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": version}, "p")]), _done(["A"]),
    ]))
    browser = Browser(web)
    tid = browser.conversation()
    status, data = browser.request("POST", "/api/ask", {"thread_id": tid, "text": "A로 갈게"})
    assert status == 200 and data["response"]["status"] == "confirmed"
    assert data["session"]["confirmed"]["plan_id"] == "A"


@pytest.mark.parametrize("decision,saved", [("approve", True), ("reject", False)])
def test_web_preference_approval_and_blocked_new_question(web, decision, saved):
    web.agent = build_agent(model=ScriptedModel(script=[
        _ai([_tc("save_preferences", {"category": "cafe"}, "s")]), _done(),
    ]))
    browser = Browser(web)
    tid = browser.conversation()
    status, data = browser.request("POST", "/api/ask", {"thread_id": tid, "text": "카페 기억해줘"})
    assert status == 200 and data["response"]["status"] == "awaiting_approval"
    approval_id = data["approval_id"]
    assert browser.summary(tid)[1]["approval_id"] == approval_id  # 새로고침해도 본인 승인만 복원
    assert memory.load_preferences(browser.user_id) is None
    assert browser.request("POST", "/api/ask", {"thread_id": tid, "text": "다른 질문"})[0] == 409
    body = {"thread_id": tid, "decision": decision, "approval_id": approval_id}
    status, data = browser.request("POST", "/api/decide", body)
    assert status == 200
    assert (data["session"]["preferences"] is not None) == saved
    assert (memory.load_preferences(browser.user_id) is not None) == saved
    assert browser.request("POST", "/api/decide", body)[0] == 404  # 이미 소비한 ID는 재실행하지 않음
    assert browser.summary(tid)[1]["approval_id"] is None


def test_upstream_exception_details_are_not_exposed(web, monkeypatch):
    def fail():
        raise RuntimeError("request_headers=demo-secret-token")
    monkeypatch.setattr(web, "get_agent", fail)
    browser = Browser(web)
    tid = browser.conversation()
    status, data = browser.request("POST", "/api/ask", {"thread_id": tid, "text": "안녕"})
    assert status == 500 and "demo-secret-token" not in data["error"]


def test_identity_is_server_issued_and_health_is_not_cached(web, monkeypatch):
    monkeypatch.setenv("VOLTGO_USER_ID", "shared-env-user")
    a, b = Browser(web), Browser(web)
    assert a.user_id != b.user_id and a.cookie != b.cookie
    assert a.user_id not in a.cookie
    assert "HttpOnly" in a.headers["set-cookie"] and "SameSite=Strict" in a.headers["set-cookie"]
    status, data = a.request("GET", "/api/health?user_id=" + b.user_id, headers={"X-User-Id": b.user_id})
    assert data["user_id"] == a.user_id != "shared-env-user"
    assert a.headers["cache-control"] == "no-store" and a.headers["vary"] == "Cookie"
    forged = Browser(web, bootstrap=False)
    forged.cookie = f"{COOKIE_NAME}={a.user_id}"
    assert forged.request("GET", "/api/health")[1]["user_id"] not in (a.user_id, b.user_id)


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/session?thread_id=x", None), ("POST", "/api/session", {}),
    ("POST", "/api/ask", {"thread_id": "x", "text": "hi"}),
    ("POST", "/api/charging/refresh", {"thread_id": "x"}),
    ("POST", "/api/decide", {"thread_id": "x", "decision": "approve", "approval_id": "x"}),
])
def test_every_conversation_api_requires_valid_cookie(web, method, path, body):
    status, data = Browser(web, bootstrap=False).request(method, path, body)
    assert status == 401 and data["code"] == "SESSION_EXPIRED"
    assert not web.contexts and web.agent is None


@pytest.mark.parametrize("operation", ["read", "ask", "refresh", "approve"])
def test_other_browser_cannot_read_or_mutate_known_conversation(web, monkeypatch, operation):
    a, b = Browser(web), Browser(web)
    tid = a.conversation()
    before = copy.deepcopy(web.contexts[a.user_id, tid].session)

    def unexpected(*args, **kwargs):
        pytest.fail("소유자 검사 전에 모델 또는 충전 공급자를 호출했습니다")
    monkeypatch.setattr(web, "get_agent", unexpected)
    monkeypatch.setattr(web, "session_summary", unexpected)
    monkeypatch.setattr(web, "get_charging_status", SimpleNamespace(func=unexpected))
    if operation == "read":
        status, data = b.summary(tid)
    else:
        path = {"ask": "/api/ask", "refresh": "/api/charging/refresh", "approve": "/api/decide"}[operation]
        status, data = b.request("POST", path, {"thread_id": tid, "text": "질문", "decision": "approve", "approval_id": "known"})
    assert status == 404 and data == {"error": "대화를 찾을 수 없습니다"}
    assert web.contexts[a.user_id, tid].session == before
    assert len(web.contexts) == 1


def test_client_cannot_assign_identity_or_conversation_id(web):
    browser = Browser(web)
    for body in [{"user_id": "victim"}, {"thread_id": "chosen-by-client"}]:
        assert browser.request("POST", "/api/session", body)[0] == 400
    assert browser.summary("unknown")[0] == 404
    assert not web.contexts


def test_tampered_cookie_and_user_id_payload_do_not_impersonate_owner(web):
    browser = Browser(web)
    tid = browser.conversation()
    original = browser.cookie
    browser.cookie = original[:-1] + ("A" if original[-1] != "A" else "B")
    assert browser.summary(tid)[0] == 401
    browser.cookie = original
    assert browser.request("POST", "/api/ask", {"thread_id": tid, "text": "질문", "user_id": "victim"})[0] == 400
    assert browser.request("POST", "/api/decide", {"thread_id": tid, "decision": "approve", "approval_id": "가짜"})[0] == 400


def test_two_browsers_save_and_delete_preferences_independently(web):
    class RecordingModel(ScriptedModel):
        seen: list = []

        def _generate(self, messages, **kwargs):
            self.seen.append([str(m.content) for m in messages])
            return super()._generate(messages, **kwargs)

    model = RecordingModel(script=[
        _ai([_tc("save_preferences", {"category": "cafe"}, "save-a")]),
        _ai([_tc("save_preferences", {"category": "meal"}, "save-b")]),
        _done(), _done(),
        _ai([_tc("delete_preferences", {}, "delete-a")]), _done(),
    ])
    web.agent = build_agent(model=model)
    a, b = Browser(web), Browser(web)
    at, bt = a.conversation(), b.conversation()
    ap = a.request("POST", "/api/ask", {"thread_id": at, "text": "A만의 카페 취향"})[1]["approval_id"]
    bp = b.request("POST", "/api/ask", {"thread_id": bt, "text": "B만의 식사 취향"})[1]["approval_id"]
    assert ap != bp
    assert b.request("POST", "/api/decide", {"thread_id": bt, "decision": "approve", "approval_id": ap})[0] == 404
    assert b.summary(bt)[1]["approval_id"] == bp
    assert memory.load_preferences(a.user_id) is None and memory.load_preferences(b.user_id) is None
    assert a.request("POST", "/api/decide", {"thread_id": at, "decision": "approve", "approval_id": ap})[0] == 200
    assert memory.load_preferences(b.user_id) is None
    assert b.request("POST", "/api/decide", {"thread_id": bt, "decision": "approve", "approval_id": bp})[0] == 200
    assert memory.load_preferences(a.user_id).preferred_category == "cafe"
    assert memory.load_preferences(b.user_id).preferred_category == "meal"
    assert any("B만의" in message for message in model.seen[1])
    assert all("A만의" not in message for message in model.seen[1])
    assert a.request("POST", "/api/ask", {"thread_id": at, "text": "저장한 취향 지워줘"})[0] == 200
    assert memory.load_preferences(a.user_id) is None
    assert b.summary(bt)[1]["session"]["preferences"]["preferred_category"] == "meal"
    assert a.summary(a.conversation())[1]["session"]["preferences"] is None
    assert b.summary(b.conversation())[1]["session"]["preferences"]["preferred_category"] == "meal"


def test_approval_id_is_also_bound_to_conversation_for_same_user(web):
    web.agent = build_agent(model=ScriptedModel(script=[_ai([_tc("save_preferences", {"category": "cafe"}, "s")])]))
    browser = Browser(web)
    first, second = browser.conversation(), browser.conversation()
    pending = browser.request("POST", "/api/ask", {"thread_id": first, "text": "기억해줘"})[1]["approval_id"]
    assert browser.request("POST", "/api/decide", {"thread_id": second, "approval_id": pending, "decision": "approve"})[0] == 404
    assert browser.summary(first)[1]["approval_id"] == pending
    assert web.contexts[browser.user_id, first] is not web.contexts[browser.user_id, second]


def test_failed_approval_is_not_executed_again(web, monkeypatch):
    web.agent = build_agent(model=ScriptedModel(script=[_ai([_tc("save_preferences", {"category": "cafe"}, "s")])]))
    browser = Browser(web)
    tid = browser.conversation()
    pending = browser.request("POST", "/api/ask", {"thread_id": tid, "text": "기억해줘"})[1]["approval_id"]
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("uncertain execution")
    monkeypatch.setattr(web, "decide", fail)
    body = {"thread_id": tid, "approval_id": pending, "decision": "approve"}
    assert browser.request("POST", "/api/decide", body)[0] == 500
    assert browser.request("POST", "/api/decide", body)[0] == 409
    assert len(calls) == 1
    assert browser.summary(tid)[1]["approval_id"] is None


def test_expired_cookie_cannot_recover_previous_visitor(web):
    clock = [0]
    web.visitors = BrowserSessions(clock=lambda: clock[0], ttl=10)
    browser = Browser(web)
    old_id, old_cookie, tid = browser.user_id, browser.cookie, browser.conversation()
    clock[0] = 10
    assert browser.summary(tid)[0] == 401
    browser.user_id = browser.request("GET", "/api/health")[1]["user_id"]
    assert browser.user_id != old_id and browser.cookie != old_cookie
    assert browser.summary(tid)[0] == 404


@pytest.mark.parametrize("headers,status", [
    ({"Origin": "https://elsewhere.example"}, 403),
    ({"Origin": "null"}, 403), ({"Sec-Fetch-Site": "cross-site"}, 403),
    ({"Origin": "http://[invalid"}, 403),
    ({"Content-Type": "text/plain"}, 415),
])
def test_cross_site_and_form_posts_are_rejected(web, headers, status):
    browser = Browser(web)
    assert browser.request("POST", "/api/session", {}, headers=headers)[0] == status
    assert not web.contexts


def test_https_cookie_setting_and_same_origin_refresh(web, monkeypatch):
    monkeypatch.setattr(web, "COOKIE_SECURE", True)
    browser = Browser(web)
    assert "Secure" in browser.headers["set-cookie"]
    tid = browser.conversation()
    context = web.contexts[browser.user_id, tid]
    assert context.session.charging is None
    status, data = browser.request("POST", "/api/charging/refresh", {"thread_id": tid}, headers={"Origin": "https://localhost:8000"})
    assert status == 200 and data["result"]["status"] == "ok"
    assert context.session.charging is not None
