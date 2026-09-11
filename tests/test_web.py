"""실제 HTTP 파서·핸들러와 Mock 에이전트 경로. 포트·외부 API 없이 실행한다."""
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


@pytest.fixture
def web(context, monkeypatch):
    spec = importlib.util.spec_from_file_location("voltgo_web_test", Path(__file__).resolve().parents[1] / "scripts/web.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "make_context", lambda *a, **kw: context)
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


def request(web, method, path, body=None, raw_body=None, length=None):
    raw = raw_body if raw_body is not None else json.dumps(body).encode() if body is not None else b""
    headers = f"{method} {path} HTTP/1.0\r\nContent-Length: {len(raw) if length is None else length}\r\n\r\n".encode()
    socket = Socket(headers + raw)
    web.Handler(socket, ("127.0.0.1", 12345), SimpleNamespace())
    head, data = socket.output.getvalue().split(b"\r\n\r\n", 1)
    code = int(head.split()[1])
    return code, json.loads(data) if b"application/json" in head else data


def test_home_assets_and_session_work_without_model_key(web, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for path in ["/", "/map.js", "/map.css", "/map-config.js", "/img/volty_hello.png"]:
        status, content = request(web, "GET", path)
        assert status == 200 and content
    assert request(web, "GET", "/api/health")[1]["ok"] is True
    status, data = request(web, "GET", "/api/session?thread_id=home")
    assert status == 200
    assert data["session"]["charging"]["source"] == "mock"
    assert data["session"]["home_budget"] is not None
    status, data = request(web, "POST", "/api/ask", {"thread_id": "home", "text": "밥 먹고 싶어"})
    assert status == 503 and "OPENAI_API_KEY" in data["error"]


@pytest.mark.parametrize("body,raw,length,status", [
    (None, b"{", None, 400),
    ([], None, None, 400),
    ({"text": "질문", "thread_id": []}, None, None, 400),
    ({"text": 1}, None, None, 400),
    ({"text": " "}, None, None, 400),
    (None, b"", "invalid", 400),
    (None, b"", 16385, 413),
])
def test_malformed_requests_return_json_errors(web, body, raw, length, status):
    code, data = request(web, "POST", "/api/ask", body, raw_body=raw, length=length)
    assert code == status and data["error"]
    assert web.contexts == {}, "잘못된 요청은 세션 생성·모델 호출 전에 거절한다"


def test_unknown_route_and_invalid_decision(web):
    assert request(web, "POST", "/api/unknown", {})[0] == 404
    assert request(web, "POST", "/api/decide", {"decision": "yes"})[0] == 400


def test_web_confirm_has_no_extra_approval(web, context):
    version = _plan_ready(context)
    web.agent = build_agent(model=ScriptedModel(script=[
        _ai([_tc("confirm_plan", {"plan_id": "A", "version": version}, "p")]), _done(["A"]),
    ]))
    status, data = request(web, "POST", "/api/ask", {"thread_id": "plan", "text": "A로 갈게"})
    assert status == 200
    assert data["response"]["status"] == "confirmed"
    assert data["session"]["confirmed"]["plan_id"] == "A"


@pytest.mark.parametrize("decision,saved", [("approve", True), ("reject", False)])
def test_web_preference_approval_and_blocked_new_question(web, context, decision, saved):
    web.agent = build_agent(model=ScriptedModel(script=[
        _ai([_tc("save_preferences", {"category": "cafe"}, "s")]), _done(),
    ]))
    status, data = request(web, "POST", "/api/ask", {"thread_id": "save", "text": "카페 기억해줘"})
    assert status == 200 and data["response"]["status"] == "awaiting_approval"
    assert memory.load_preferences(context.user_id) is None
    assert request(web, "POST", "/api/ask", {"thread_id": "save", "text": "다른 질문"})[0] == 409
    status, data = request(web, "POST", "/api/decide", {"thread_id": "save", "decision": decision})
    assert status == 200
    assert (data["session"]["preferences"] is not None) == saved
    assert (memory.load_preferences(context.user_id) is not None) == saved
    _, data = request(web, "POST", "/api/decide", {"thread_id": "save", "decision": decision})
    assert "NO_PENDING_APPROVAL" in data["response"]["message"]


def test_upstream_exception_details_are_not_exposed(web, monkeypatch):
    def fail():
        raise RuntimeError("request_headers=demo-secret-token")
    monkeypatch.setattr(web, "get_agent", fail)
    status, data = request(web, "POST", "/api/ask", {"text": "안녕"})
    assert status == 500
    assert "demo-secret-token" not in data["error"]
