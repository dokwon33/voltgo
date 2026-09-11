"""
VoltGo 웹 화면 (scripts/demo.py 의 브라우저 판)

  python scripts/web.py             # http://localhost:8000
  python scripts/web.py --fixed     # 2026-09-10 14:00 고정 시계 (설계서 C001 조건)
  python scripts/web.py --port 8080

외부 패키지 없이 표준 http.server 로 띄운다. 에이전트 호출은 demo.py 와 같은 ask() / decide().
  GET  /                 web/index.html
  GET  /api/health       실행 모드 (충전 Mock/실연동, TMAP Mock/실연동, 시계)
  GET  /api/session      ?thread_id=  -> 차량·조건·후보·승인·지도 요약
  POST /api/ask          {thread_id, text}      -> {response: VoltGoResponse, session}
  POST /api/decide       {thread_id, decision, request_id} (approve / reject)
  POST /api/refresh      {thread_id} -> 강제 충전 조회 + 지난 추천 무효화
"""
import json
import inspect
import os
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from demo import make_context                       # demo.py 와 같은 Context 조립 (.env 도 여기서 읽는다)
from voltgo.agent import memory
from voltgo.agent.agent import ask, build_agent, decide
from voltgo.agent.approval import pending_approvals
from voltgo.agent.tools import get_charging_status
from voltgo.clients import ClientError
from voltgo.core.time_budget import calculate_time_budget

WEB_DIR = ROOT / "web"
FIXED = "--fixed" in sys.argv
USER_ID = os.getenv("VOLTGO_USER_ID", "user_001")

agent = None                   # 첫 질문 때 만든다. 키가 없어도 홈 화면(차량 상태)은 뜨게
contexts = {}                  # thread_id -> Context. thread 마다 Session 이 따로 있다
lock = threading.Lock()        # 에이전트는 한 번에 한 요청만 처리
INSTANCE_ID = uuid4().hex
approval_tokens = {}
decision_results = {}


def get_agent():
    global agent
    if agent is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY 가 없어요. .env 에 넣고 서버를 다시 켜 주세요.")
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
        "instance_id": INSTANCE_ID,
    }


def peek_charging(context):
    """첫 화면에서도 공통 조회 도구를 써서 같은 스냅샷/조회 횟수를 기록한다."""
    if context.session.charging:
        return context.session.charging
    try:
        result = get_charging_status.func(SimpleNamespace(context=context))
        return context.session.charging if result["status"] == "ok" else None
    except Exception:
        return None


def peek_budget(context, snap):
    """홈 카드의 완료·복귀 시각. 대화 전이면 core 의 같은 계산식으로 미리 보여준다 (사용자 제한 없음)."""
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
    # 기존 화면 계약은 유지하되 목표/잔여시간은 차량 조회값으로만 채운다.
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


def envelope(context, thread_id, response=None):
    """웹 전용 DTO. 핵심 에이전트의 응답 계약/승인 정책은 유지한다."""
    s = context.session
    actions = pending_approvals(agent, {"configurable": {"thread_id": thread_id}}) if agent else []
    pending = None
    if actions and s.approval_requested_at:
        fingerprint = (s.approval_requested_at.isoformat(), json.dumps(actions, sort_keys=True, ensure_ascii=False))
        previous = approval_tokens.get(thread_id)
        if previous is None or previous[0] != fingerprint:
            native_id = getattr(response, "request_id", None)
            approval_tokens[thread_id] = (fingerprint, native_id or uuid4().hex)
        pending = {
            "request_id": approval_tokens[thread_id][1], "actions": actions,
            "expires_at": (s.approval_requested_at + timedelta(seconds=120)).isoformat(),
        }
    else:
        approval_tokens.pop(thread_id, None)
    return {
        "instance_id": INSTANCE_ID,
        "session": session_summary(context),
        "response": response.model_dump(mode="json") if response else None,
        "pending_approval": pending,
        "map_data": {
            "origin": s.origin.model_dump(mode="json") if s.origin else None,
            "places": [p.model_dump(mode="json") for p in s.places.values()],
            # 현재 RoundTrip 은 시간/거리만 제공한다. 실제 선 좌표는 D의 계약 확장 대기.
            "routes": [r.model_dump(mode="json") for r in s.routes.values()],
        },
    }


def validate_selection(context, body):
    """LLM 호출 전에 클릭한 후보의 ID뿐 아니라 버전/생성시각도 확인한다."""
    s = context.session
    selection = body.get("selection")
    if not selection:
        return body.get("text", "")
    if not isinstance(selection, dict):
        raise ValueError("올바른 선택 정보가 필요합니다.")
    if selection.get("kind") == "station":
        station = s.station_candidates.get(selection.get("id"))
        if station is None:
            raise ValueError("지난 충전소 후보입니다. 충전소를 다시 검색해 주세요.")
        return f"출발 충전소는 {station.name}입니다. find_station의 station_id={station.poi_id}로 선택하고 다시 추천해줘."
    if selection.get("kind") != "plan":
        raise ValueError("선택 종류가 올바르지 않습니다.")
    candidate = s.candidates.get(selection.get("id"))
    if (candidate is None or candidate.version != selection.get("version")
            or candidate.version != s.condition_version
            or candidate.evaluated_at.isoformat() != selection.get("evaluated_at")
            or (context.clock() - candidate.evaluated_at).total_seconds() > 300):
        raise ValueError("조건이 바뀌었거나 시간이 지난 추천입니다. 현재 조건으로 다시 추천받아 주세요.")
    return f"{candidate.name} 계획을 선택합니다. confirm_plan(plan_id={candidate.plan_id}, version={candidate.version})으로 지금 다시 검증하고 확정해줘."


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/map-config.js":
            # 이 키만 브라우저 공개용으로 지정한다. 서버 API 키로 대체하지 않는다.
            config = {"appKey": os.getenv("TMAP_MAP_APP_KEY", "").strip()}
            raw = ("window.VOLTGO_MAP_CONFIG = " + json.dumps(config) + ";\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if url.path == "/api/health":
            return self._json(health())
        if url.path == "/api/session":
            thread_id = (parse_qs(url.query).get("thread_id") or ["web"])[0]
            with lock:
                exists = thread_id in contexts
                if "existing" in parse_qs(url.query) and not exists:
                    return self._json({"exists": False, "instance_id": INSTANCE_ID})
                return self._json({**envelope(context_for(thread_id), thread_id), "exists": exists})
        if url.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= 65536:
                raise ValueError("요청이 너무 큽니다.")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("올바른 JSON 요청이 필요합니다.")
            thread_id = body.get("thread_id") or "web"
            with lock:
                context = context_for(thread_id)
                if body.get("instance_id") and body["instance_id"] != INSTANCE_ID:
                    return self._json({"error": "서버가 다시 시작됐어요. 대화 기록에서 새 대화를 시작해 주세요."}, 409)
                pending = envelope(context, thread_id)["pending_approval"]
                if self.path == "/api/ask":
                    if pending:
                        return self._json({"error": "대기 중인 선호 저장을 먼저 결정해 주세요."}, 409)
                    text = validate_selection(context, body)
                    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                        raise ValueError("질문을 1~2000자로 입력해 주세요.")
                    res = ask(get_agent(), text, context, thread_id)
                elif self.path == "/api/decide":
                    request_id, decision = body.get("request_id"), body.get("decision")
                    if decision not in ("approve", "reject"):
                        raise ValueError("approve 또는 reject가 필요합니다.")
                    cache_key = (thread_id, request_id)
                    if cache_key in decision_results:
                        previous_decision, result = decision_results[cache_key]
                        if previous_decision != decision:
                            return self._json({"error": "이미 다른 결정으로 처리한 요청입니다."}, 409)
                        return self._json(result)
                    if not pending or request_id != pending["request_id"]:
                        return self._json({"error": "지난 승인 요청입니다. 현재 대화를 다시 확인해 주세요."}, 409)
                    kwargs = {"request_id": request_id} if "request_id" in inspect.signature(decide).parameters else {}
                    res = decide(get_agent(), decision, context, thread_id, **kwargs)
                    result = envelope(context, thread_id, res)
                    decision_results[cache_key] = (decision, result)
                    return self._json(result)
                elif self.path == "/api/refresh":
                    if pending:
                        return self._json({"error": "대기 중인 선호 저장을 먼저 결정해 주세요."}, 409)
                    result = get_charging_status.func(SimpleNamespace(context=context), force_refresh=True)
                    if result["status"] != "ok":
                        return self._json({"error": result["message"]}, 502)
                    # 충전 조건이 달라졌을 수 있으므로 화면의 이전 추천은 모두 재계산한다.
                    context.session.bump_version()
                    return self._json(envelope(context, thread_id))
                else:
                    return self._json({"error": "없는 경로입니다"}, 404)
                result = envelope(context, thread_id, res)
        except (ValueError, TypeError) as e:
            return self._json({"error": str(e)}, 400)
        except ClientError as e:
            return self._json({"error": f"외부 API 오류: {e.code}"}, 502)
        except Exception as e:                   # 모델 키 오류 등. 원문을 그대로 화면에 보여준다
            return self._json({"error": f"{type(e).__name__}: {str(e)[:300]}"}, 500)

        self._json(result)

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
