"""
VoltGo 웹 화면 (scripts/demo.py 의 브라우저 판)

  python scripts/web.py             # http://localhost:8000
  python scripts/web.py --fixed     # 2026-09-10 14:00 고정 시계 (설계서 C001 조건)
  python scripts/web.py --port 8080

외부 패키지 없이 표준 http.server 로 띄운다. 에이전트 호출은 demo.py 와 같은 ask() / decide().
  GET  /                 web/index.html
  GET  /api/health       실행 모드 (충전 Mock/실연동, TMAP Mock/실연동, 시계)
  GET  /api/session      ?thread_id=  -> 왼쪽 패널용 Session 요약 (SoC, 출발지, 저장 선호 ...)
  POST /api/ask          {thread_id, text}      -> {response: VoltGoResponse, session}
  POST /api/decide       {thread_id, decision}  -> {response, session}   (approve / reject)
"""
import json
import os
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from demo import make_context                       # demo.py 와 같은 Context 조립 (.env 도 여기서 읽는다)
from voltgo.agent import memory
from voltgo.agent.agent import ask, build_agent, decide
from voltgo.agent.approval import pending_approvals
from voltgo.clients import ClientError
from voltgo.core.time_budget import calculate_time_budget

WEB_DIR = ROOT / "web"
FIXED = "--fixed" in sys.argv
USER_ID = os.getenv("VOLTGO_USER_ID", "user_001")

agent = None                   # 첫 질문 때 만든다. 키가 없어도 홈 화면(차량 상태)은 뜨게
contexts = {}                  # thread_id -> Context. thread 마다 Session 이 따로 있다
lock = threading.Lock()        # 에이전트는 한 번에 한 요청만 처리


class MissingModelKey(RuntimeError):
    pass


def get_agent():
    global agent
    if agent is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise MissingModelKey("OPENAI_API_KEY 가 없어요. .env 에 넣고 서버를 다시 켜 주세요.")
        agent = build_agent()
    return agent


def context_for(thread_id: str):
    if thread_id not in contexts:
        contexts[thread_id] = make_context(USER_ID, fixed_clock=FIXED)
    return contexts[thread_id]


def health():
    return {
        "ok": True,
        "model": os.getenv("MODEL_NAME", "gpt-4.1-mini"),
        "charging": "mock" if os.getenv("USE_MOCK_CHARGING", "true").lower() == "true" else "hyundai",
        "tmap": "tmap" if os.getenv("TMAP_APP_KEY") else "mock",
        "clock": "fixed" if FIXED else "real",
        "user_id": USER_ID,
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
    """VoltGoResponse 에 없는 값(SoC, 출발지, 저장 선호, 호출 횟수)을 Session 에서 꺼내 화면에 준다."""
    s = context.session
    pref = memory.load_preferences(context.user_id)
    dump = lambda m: m.model_dump(mode="json") if m else None
    snap = peek_charging(context)
    return {
        "user_id": context.user_id,
        "now": context.clock().isoformat(),
        "charging": dump(snap),
        "home_budget": dump(peek_budget(context, snap)),
        "origin": s.origin.name if s.origin else None,
        "time_budget": dump(s.time_budget),
        "condition_version": s.condition_version,
        "confirmed": dump(s.confirmed),
        "preferences": dump(pref),
        "counters": s.counters,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/health":
            return self._json(health())
        if url.path == "/api/session":
            thread_id = (parse_qs(url.query).get("thread_id") or ["web"])[0]
            if not self._valid_thread(thread_id):
                return self._json({"error": "thread_id는 1~128자 문자열이어야 합니다"}, 400)
            try:
                with lock:
                    summary = session_summary(context_for(thread_id))
                return self._json({"session": summary})
            except Exception:
                return self._json({"error": "세션 상태를 불러오지 못했습니다"}, 500)
        if url.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    @staticmethod
    def _valid_thread(value):
        return isinstance(value, str) and 1 <= len(value.strip()) <= 128

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/ask", "/api/decide"):
            return self._json({"error": "없는 경로입니다"}, 404)
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
        thread_id = body.get("thread_id", "web")
        if not self._valid_thread(thread_id):
            return self._json({"error": "thread_id는 1~128자 문자열이어야 합니다"}, 400)
        text = body.get("text", "")
        if path == "/api/ask" and (not isinstance(text, str) or not text.strip()):
            return self._json({"error": "질문을 입력해 주세요"}, 400)
        decision = body.get("decision")
        if path == "/api/decide" and decision not in ("approve", "reject"):
            return self._json({"error": "approve 또는 reject를 선택해 주세요"}, 400)

        try:
            with lock:
                context = context_for(thread_id)
                current_agent = get_agent()
                if path == "/api/ask":
                    if pending_approvals(current_agent, {"configurable": {"thread_id": thread_id}}):
                        return self._json({"error": "대기 중인 선호 저장을 먼저 승인하거나 거절해 주세요"}, 409)
                    res = ask(current_agent, text, context, thread_id)
                else:
                    res = decide(current_agent, decision, context, thread_id)
                payload = {"response": res.model_dump(mode="json"), "session": session_summary(context)}
        except MissingModelKey as e:
            return self._json({"error": str(e)}, 503)
        except ClientError as e:
            return self._json({"error": f"외부 API 오류: {e.code}"}, 502)
        except Exception:
            # 외부 예외 원문에는 인증 헤더·URL 등이 포함될 수 있다.
            return self._json({"error": "요청을 처리하지 못했습니다. 서버 설정과 연결을 확인해 주세요."}, 500)
        self._json(payload)

    def _json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
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
    print(f"* VoltGo web : {url}   user={USER_ID} clock={'14:00 고정' if FIXED else '실제 시각'}")
    webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
