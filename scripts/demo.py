"""
VoltGo CLI 시연 (노트북 [4] 1-4 '대화형 질의' 패턴)

  python scripts/demo.py             # 실제 시각. 충전은 Mock, TMAP 은 키 있으면 실연동
  python scripts/demo.py --fixed     # 2026-09-10 14:00 고정 시계 (설계서 C001 조건)

승인이 필요한 단계(선호 저장)에서는 approve / reject 를 물어본다. 계획 확정은 승인 없이 재검증 후 바로 기록된다.
"""
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

from voltgo.agent.agent import ask, build_agent, decide
from voltgo.agent.state import KST, Context, Session, now_kst
from voltgo.clients import ClientError
from voltgo.clients.hyundai import HyundaiClient
from voltgo.clients.mock_charging import MockChargingProvider
from voltgo.clients.tmap_places import MockPlacesClient, TmapPlacesClient
from voltgo.clients.tmap_routes import MockRoutesClient, TmapRoutesClient

load_dotenv()


def make_context(user_id: str, fixed_clock: bool = False) -> Context:
    clock = (lambda: datetime(2026, 9, 10, 14, 0, tzinfo=KST)) if fixed_clock else now_kst

    # 1. 충전 공급자 : Mock 이 기본. USE_MOCK_CHARGING=false 면 현대차 실연동
    use_mock = os.getenv("USE_MOCK_CHARGING", "true").lower() == "true"
    if use_mock:
        charging = MockChargingProvider(fixture=os.getenv("MOCK_CHARGING_FIXTURE", "charging_ok"), clock=clock)
    else:
        charging = HyundaiClient()

    # 2. TMAP : 키가 있으면 실연동, 없으면 fixture
    session = Session()
    if os.getenv("TMAP_APP_KEY"):
        places, routes = TmapPlacesClient(), TmapRoutesClient()
        print("* TMAP 실연동 모드 (출발 충전소는 대화 중 find_station 으로 잡는다)")
    else:
        places, routes = MockPlacesClient(), MockRoutesClient()
        session.origin = places.find_station("")[0]      # fixture 의 충전소 좌표
        print(f"* TMAP Mock 모드 (출발지: {session.origin.name})")

    return Context(user_id=user_id, demo_mode=use_mock, clock=clock,
                   charging_provider=charging, places_client=places, routes_client=routes, session=session)


def main():
    fixed = "--fixed" in sys.argv
    user_id = os.getenv("VOLTGO_USER_ID", "user_001")
    thread_id = f"demo-{uuid.uuid4().hex[:6]}"

    agent = build_agent()
    context = make_context(user_id, fixed_clock=fixed)
    print(f"* user={user_id} thread={thread_id} clock={'14:00 고정' if fixed else '실제 시각'}")

    # 대화형 질의
    print("질문을 입력하세요 (종료: exit or quit)")
    while True:
        question = input("\n질문: ")
        if question.lower() in ["exit", "quit", "종료", "끝"]:
            print("종료합니다.")
            break

        try:
            response = ask(agent, question, context, thread_id)
        except ClientError as e:
            print(f"외부 API 오류: {e.code}")
            continue

        # 승인이 필요하면 여기서 사람이 결정한다
        while response.status == "awaiting_approval":
            print(f"\n[승인 요청] {response.message}")
            decision = input("approve / reject : ").strip().lower()
            if decision not in ("approve", "reject"):
                decision = "reject"
            response = decide(agent, decision, context, thread_id, request_id=response.request_id)

        print(f"\n답변 ({response.status}):\n{response.message}")


if __name__ == "__main__":
    main()
