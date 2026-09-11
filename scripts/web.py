"""
VoltGo 웹 화면 (scripts/demo.py 의 브라우저 판)

  python scripts/web.py             # http://localhost:8000
  python scripts/web.py --fixed     # 2026-09-10 14:00 고정 시계 (설계서 C001 조건)
  python scripts/web.py --port 8080

외부 패키지 없이 표준 http.server 로 띄운다. 에이전트 호출은 demo.py 와 같은 ask() / decide().
  GET  /                 web/index.html
  GET  /map-config.js    .env 의 TMAP_MAP_APP_KEY(브라우저 공개용 지도 키)만 주입
  GET  /api/health       익명 접속 세션 발급·검증 + 현재 접속자의 실행 정보
  POST /api/session      {} -> 서버 발급 thread_id + Session 요약
  GET  /api/session      ?thread_id= -> 본인 대화의 상태·승인·지도 복원
  POST /api/ask          {thread_id, text, selection?} -> {response, session, map_data, request_id}
  POST /api/decide       {thread_id, request_id, decision} -> {response, session}
  POST /api/charging/refresh {thread_id} -> 본인 대화의 충전 상태 갱신 + 지난 추천 무효화
"""
import json
import os
import secrets
import sys
import threading
import webbrowser
from datetime import timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from demo import make_context                       # demo.py 와 같은 Context 조립 (.env 도 여기서 읽는다)
from voltgo.agent import memory
from voltgo.agent.agent import ask, build_agent, decide
from voltgo.agent.middleware import input_rejection
from voltgo.agent.tools import APPROVAL_TTL_SEC, CANDIDATE_TTL_SEC, get_charging_status
from voltgo.clients import ClientError
from voltgo.core.time_budget import calculate_time_budget
from voltgo.web_sessions import BrowserSessions

WEB_DIR = ROOT / "web"
FIXED = "--fixed" in sys.argv
COOKIE_SECURE = os.getenv("VOLTGO_COOKIE_SECURE", "false").lower() == "true"
INSTANCE_ID = uuid4().hex      # 서버를 다시 켜면 바뀐다. 화면은 이 값으로 '이어서 대화 가능' 여부를 표시한다

agent = None                   # 첫 질문 때 만든다. 키가 없어도 홈 화면(차량 상태)은 뜨게
visitors = BrowserSessions()
contexts = {}                  # (서버가 발급한 user_id, thread_id) -> Context
owners = {}                    # 서버가 발급한 thread_id -> user_id
lock = threading.Lock()        # 에이전트는 한 번에 한 요청만 처리


class MissingModelKey(RuntimeError):
    pass


class ConversationNotFound(Exception):
    pass


class StaleSelection(ValueError):
    """화면에서 누른 후보·충전소가 지금 조건과 맞지 않는다."""


def get_agent():
    global agent
    if agent is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise MissingModelKey("OPENAI_API_KEY 가 없어요. .env 에 넣고 서버를 다시 켜 주세요.")
        agent = build_agent()
    return agent


def new_conversation(user_id: str):
    thread_id = "web-" + secrets.token_hex(16)
    context = make_context(user_id, fixed_clock=FIXED)
    contexts[user_id, thread_id] = context
    owners[thread_id] = user_id
    return thread_id, context


def context_for(user_id: str, thread_id: str):
    # 존재하지 않는 대화와 다른 사용자의 대화는 같은 404로 처리한다.
    if owners.get(thread_id) != user_id:
        raise ConversationNotFound("대화를 찾을 수 없습니다")
    return contexts[user_id, thread_id]


def pending_approval(context):
    """CLI와 같은 승인 원장을 읽는다. 완료 응답은 decide 재전송용으로 보존한다."""
    session = context.session
    pending = session.approval_requests.get(session.pending_request_id)
    if pending is None or pending.decision_payload is not None:
        return {"request_id": None, "pending_response": None, "pending_approval": None}
    return {"request_id": session.pending_request_id,
            "pending_response": pending.prompt_response.model_dump(mode="json") if pending.prompt_response else None,
            "pending_approval": {"request_id": session.pending_request_id, "actions": json.loads(pending.payload),
                                 "expires_at": (pending.requested_at + timedelta(seconds=APPROVAL_TTL_SEC)).isoformat()}}


def health(user_id):
    return {
        "ok": True,
        "model": os.getenv("MODEL_NAME", "gpt-4.1-mini"),
        "charging": "mock" if os.getenv("USE_MOCK_CHARGING", "true").lower() == "true" else "hyundai",
        "tmap": "tmap" if os.getenv("TMAP_APP_KEY") else "mock",
        "clock": "fixed" if FIXED else "real",
        "user_id": user_id,
        "identity": "anonymous_browser_session",
        "instance_id": INSTANCE_ID,
    }


def peek_charging(context):
    """홈 화면에 보여줄 차량 상태. 도구가 아직 조회하기 전이면 공급자에서 바로 읽는다 (Session 에는 넣지 않는다)."""
    if context.session.charging:
        return context.session.charging
    try:
        return context.charging_provider.get_charging_status()
    except Exception:
        return None


def peek_budget(context, snap):
    """홈 카드의 완료·복귀 시각. 대화 전이면 core 의 같은 계산식으로 미리 보여준다 (사용자 제한 없음)."""
    if context.session.time_budget:
        return context.session.time_budget
    if snap is None:
        return None
    budget, _ = calculate_time_budget(snap, context.clock(), buffer_min=context.buffer_min)
    return budget


def session_summary(context):
    """VoltGoResponse 에 없는 값(SoC, 출발지, 후보, 저장 선호, 호출 횟수)을 Session 에서 꺼내 화면에 준다."""
    s = context.session
    pref = memory.load_preferences(context.user_id)
    dump = lambda m: m.model_dump(mode="json") if m else None
    snap = peek_charging(context)
    # 목표 충전량은 차량 조회값으로만 채운다 (대화로 바꾸지 않는다).
    display = snap.model_copy(update={"target_soc_pct": snap.vehicle_target_soc_pct}) if snap else None
    current_budget = None
    if s.time_budget and display:
        current_budget, _ = calculate_time_budget(
            display, context.clock(), buffer_min=context.buffer_min,
            user_limit_min=s.user_limit_min, limit_said_at=s.limit_said_at)
    return {
        "user_id": context.user_id,
        "now": context.clock().isoformat(),
        "charging": dump(snap),
        "display_charging": dump(display),
        "home_budget": dump(peek_budget(context, display)),
        "effective_target_soc_pct": display.target_soc_pct if display else None,
        "origin": s.origin.name if s.origin else None,
        "station_candidates": [dump(c) for c in s.station_candidates.values()],
        "user_limit_min": s.user_limit_min,
        "dwell_overrides": s.dwell_overrides,
        "candidates": [dump(c) for c in s.candidates.values()],
        "time_budget": dump(current_budget),
        "condition_version": s.condition_version,
        "confirmed": dump(s.confirmed),
        "preferences": dump(pref),
        "counters": s.counters,
    }


def map_data(context):
    """지도에 그릴 출발지·장소·경로. 현재 RoundTrip 은 시간/거리만 있어 선 좌표는 D 의 계약 확장 대기."""
    s = context.session
    return {
        "origin": s.origin.model_dump(mode="json") if s.origin else None,
        "places": [p.model_dump(mode="json") for p in s.places.values()],
        "routes": [r.model_dump(mode="json") for r in s.routes.values()],
    }


def envelope(context, thread_id, response=None):
    """웹 전용 응답. 핵심 에이전트의 응답 계약·승인 정책은 그대로 두고 화면에 필요한 값만 붙인다."""
    return {
        "thread_id": thread_id,
        "instance_id": INSTANCE_ID,
        "session": session_summary(context),
        "response": response.model_dump(mode="json") if response else None,
        **pending_approval(context),
        "map_data": map_data(context),
    }


def validate_selection(context, body):
    """모델을 부르기 전에, 누른 후보의 ID 뿐 아니라 버전·생성 시각까지 지금 조건과 맞는지 확인한다."""
    s = context.session
    selection = body.get("selection")
    if not selection:
        return body.get("text", "")
    if not isinstance(selection, dict):
        raise StaleSelection("올바른 선택 정보가 필요합니다.")
    if selection.get("kind") == "station":
        station = s.station_candidates.get(selection.get("id"))
        if station is None:
            raise StaleSelection("지난 충전소 후보입니다. 충전소를 다시 검색해 주세요.")
        return f"출발 충전소는 {station.name}입니다. find_station의 station_id={station.poi_id}로 선택하고 다시 추천해줘."
    if selection.get("kind") != "plan":
        raise StaleSelection("선택 종류가 올바르지 않습니다.")
    candidate = s.candidates.get(selection.get("id"))
    if (candidate is None or candidate.version != selection.get("version")
            or candidate.version != s.condition_version
            or candidate.evaluated_at.isoformat() != selection.get("evaluated_at")
            or (context.clock() - candidate.evaluated_at).total_seconds() > CANDIDATE_TTL_SEC):
        raise StaleSelection("조건이 바뀌었거나 시간이 지난 추천입니다. 현재 조건으로 다시 추천받아 주세요.")
    return f"{candidate.name} 계획을 선택합니다. confirm_plan(plan_id={candidate.plan_id}, version={candidate.version})으로 지금 다시 검증하고 확정해줘."


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/map-config.js":
            return self._map_config()
        if url.path.startswith("/api/") and not self._same_origin():
            return self._json({"error": "같은 사이트에서 요청해 주세요"}, 403)
        if url.path == "/api/health":
            visitor = visitors.resolve(self.headers.get("Cookie", ""))
            cookie = None
            if visitor is None:
                visitor, cookie = visitors.issue(secure=COOKIE_SECURE)
            return self._json(health(visitor.user_id), cookie=cookie)
        if url.path == "/api/session":
            visitor = self._visitor()
            if visitor is None:
                return
            thread_id = (parse_qs(url.query).get("thread_id") or [None])[0]
            if not self._valid_thread(thread_id):
                return self._json({"error": "thread_id는 1~128자 문자열이어야 합니다"}, 400)
            try:
                with lock:
                    return self._json(envelope(context_for(visitor.user_id, thread_id), thread_id))
            except ConversationNotFound:
                return self._json({"error": "대화를 찾을 수 없습니다"}, 404)
            except Exception:
                return self._json({"error": "세션 상태를 불러오지 못했습니다"}, 500)
        if url.path.startswith("/api/"):
            return self._json({"error": "없는 경로입니다"}, 404)
        if url.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def _map_config(self):
        # 브라우저 공개용 지도 키만 준다. 서버 API 키(TMAP_APP_KEY)로 대체하지 않는다.
        config = {"appKey": os.getenv("TMAP_MAP_APP_KEY", "").strip()}
        raw = ("window.VOLTGO_MAP_CONFIG = " + json.dumps(config) + ";\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @staticmethod
    def _valid_thread(value):
        return isinstance(value, str) and 1 <= len(value.strip()) <= 128

    def _same_origin(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        return (parsed.scheme == ("https" if COOKIE_SECURE else "http")
                and parsed.netloc.lower() == self.headers.get("Host", "").lower()
                and not parsed.path and not parsed.query and not parsed.fragment)

    def _visitor(self):
        visitor = visitors.resolve(self.headers.get("Cookie", ""))
        if visitor is None:
            self._json({"error": "접속 세션이 만료되었습니다. 새로고침해 주세요.",
                        "code": "SESSION_EXPIRED"}, 401)
        return visitor

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/session", "/api/ask", "/api/decide", "/api/charging/refresh"):
            return self._json({"error": "없는 경로입니다"}, 404)
        if not self._same_origin():
            return self._json({"error": "같은 사이트에서 요청해 주세요"}, 403)
        if self.headers.get_content_type() != "application/json":
            return self._json({"error": "application/json 요청이 필요합니다"}, 415)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "요청 길이가 잘못되었습니다"}, 400)
        if length < 0 or length > 16384:
            return self._json({"error": "요청 크기는 16KB 이하여야 합니다"}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "올바른 JSON으로 요청해 주세요"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "JSON 객체가 필요합니다"}, 400)
        if "user_id" in body:
            return self._json({"error": "사용자는 서버 접속 세션으로 결정됩니다"}, 400)
        thread_id = body.get("thread_id")
        if path == "/api/session" and thread_id is not None:
            return self._json({"error": "새 대화 ID는 서버가 발급합니다"}, 400)
        if path != "/api/session" and not self._valid_thread(thread_id):
            return self._json({"error": "thread_id는 1~128자 문자열이어야 합니다"}, 400)
        text = body.get("text", "")
        if path == "/api/ask" and (not isinstance(text, str) or not text.strip()):
            return self._json({"error": "질문을 입력해 주세요"}, 400)
        if path == "/api/ask" and (rejection := input_rejection(text)):
            return self._json({"error": rejection}, 400)
        decision = body.get("decision")
        if path == "/api/decide" and decision not in ("approve", "reject"):
            return self._json({"error": "approve 또는 reject를 선택해 주세요"}, 400)
        if path == "/api/decide" and (not self._valid_thread(body.get("request_id"))
                                      or not body["request_id"].isascii()):
            return self._json({"error": "현재 승인 화면의 request_id가 필요합니다"}, 400)

        if path == "/api/decide" and not isinstance(body.get("reason", ""), str):
            return self._json({"error": "reason은 문자열이어야 합니다"}, 400)

        visitor = self._visitor()
        if visitor is None:
            return

        try:
            with lock:
                if path == "/api/session":
                    thread_id, context = new_conversation(visitor.user_id)
                    return self._json(envelope(context, thread_id), 201)
                context = context_for(visitor.user_id, thread_id)
                if path == "/api/charging/refresh":
                    if context.session.pending_request_id:
                        return self._json({"error": "대기 중인 선호 저장을 먼저 승인하거나 거절해 주세요"}, 409)
                    result = get_charging_status.func(SimpleNamespace(context=context), force_refresh=True)
                    if result["status"] != "ok":
                        return self._json({"error": result.get("message") or "충전 정보를 다시 읽지 못했습니다"}, 502)
                    # 충전 조건이 달라졌을 수 있으므로 화면에 남은 이전 추천은 모두 다시 받게 한다.
                    context.session.bump_version()
                    return self._json({**envelope(context, thread_id), "result": result})
                if path == "/api/ask":
                    if context.session.pending_request_id:
                        return self._json({"error": "대기 중인 선호 저장을 먼저 승인하거나 거절해 주세요"}, 409)
                    text = validate_selection(context, body)
                    if rejection := input_rejection(text):
                        return self._json({"error": rejection}, 400)
                    current_agent = get_agent()
                    res = ask(current_agent, text, context, thread_id)
                else:
                    if body["request_id"] not in context.session.approval_requests:
                        return self._json({"error": "승인 요청을 찾을 수 없습니다"}, 404)
                    current_agent = get_agent()
                    res = decide(current_agent, decision, context, thread_id,
                                 reason=body.get("reason", ""), request_id=body["request_id"])
                payload = envelope(context, thread_id, res)

        except ConversationNotFound:
            return self._json({"error": "대화를 찾을 수 없습니다"}, 404)
        except StaleSelection as e:
            return self._json({"error": str(e)}, 409)
        except MissingModelKey as e:
            return self._json({"error": str(e)}, 503)
        except ClientError as e:
            return self._json({"error": f"외부 API 오류: {e.code}"}, 502)
        except Exception:
            # 외부 예외 원문에는 인증 헤더·URL 등이 포함될 수 있다.
            return self._json({"error": "요청을 처리하지 못했습니다. 서버 설정과 연결을 확인해 주세요."}, 500)
        status = 409 if res.message.startswith(("[REQUEST_ID_CONFLICT]", "[REQUEST_IN_PROGRESS]")) else 200
        if status != 200:
            payload["error"] = res.message
        self._json(payload, status)

    def _json(self, data, status=200, *, cookie=None):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Cookie")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        # 정적 파일 로그는 생략. 에이전트 print 로그 사이에 API 호출만 남긴다
        if self.path.startswith("/api/"):
            super().log_message(fmt, *args)


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8000
    for p in range(port, port + 10):          # 8000 이 다른 프로그램에 잡혀 있으면 다음 번호로
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            print(f"* {p} 번 포트는 사용 중 -> 다음 번호로")
    else:
        sys.exit(f"{port}~{port + 9} 번 포트가 모두 사용 중입니다. --port 로 지정하세요.")
    url = f"http://localhost:{p}"
    print(f"* VoltGo web : {url}   브라우저별 접속 세션 / clock={'14:00 고정' if FIXED else '실제 시각'}")
    webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
