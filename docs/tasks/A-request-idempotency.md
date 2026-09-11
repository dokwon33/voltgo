# A: request_id 기반 승인 재전송 처리

기준: PR #4 `173fba9`의 후속 브랜치 `feat/a-request-idempotency`.
설계서 C017의 동일 요청 재전송 / 내용 변경 거부를 실행 래퍼에 연결한다.

## 동작

- `ask()`가 승인 대기를 반환하면 `VoltGoResponse.request_id`에 코드가 만든 UUID가 담긴다.
  여러 쓰기 도구를 함께 승인하는 경우 배치 전체에 ID 하나를 발급한다.
- `decide(..., request_id=...)`는 발급 당시 도구 이름·인자와 checkpoint의 interrupt ID를 확인한다.
- 같은 ID와 같은 결정/거절 사유를 재전송하면 보관한 응답의 사본을 반환한다.
  모델, Tool, 선호 파일 쓰기를 다시 실행하지 않는다.
- 같은 ID에 다른 결정/사유를 보내거나 승인 대상이 바뀌면 `REQUEST_ID_CONFLICT`를 반환한다.
  문자열 `"approve"`와 같은 개수의 `["approve", ...]`는 같은 결정으로 취급한다.
- 잘못된 결정이나 결정 개수는 실행 전에 거부하므로, 같은 ID로 올바르게 다시 응답할 수 있다.
- 완료 응답은 조건 버전 변경 후에도 보관한다. 과거 응답 조회는 현재 Session을 되돌리지 않는다.
- 실행 중 오류가 나면 그 오류 응답도 보관한다. 쓰기 이후 모델 단계가 실패했을 수 있으므로
  같은 ID를 자동 재실행하지 않는다. 이 경우 부수 효과가 전혀 없었다는 뜻은 아니다.

## 호출 예시

```python
response = ask(agent, "A를 확정하고 카페 선호를 기억해줘", context, thread_id="t1")
if response.status == "awaiting_approval":
    request_id = response.request_id
    response = decide(agent, "approve", context, thread_id="t1", request_id=request_id)
    # 응답을 받지 못해 재전송해도 같은 ID/결정을 사용한다.
    replay = decide(agent, "approve", context, thread_id="t1", request_id=request_id)
```

`request_id` 생략 호출은 기존 호출부 호환용이며 **현재 대기 요청**을 대상으로 한다.
네트워크 재전송을 수행하는 호출자는 응답의 ID를 반드시 보관해야 한다.
승인을 처리한 뒤 새 승인이 이어지면 반환 응답의 ID는 새 요청의 ID다.
CLI도 승인 화면에서 받은 ID를 전달한다.

## Session과 승인 시각

Session은 현재 CLI MVP 구조를 유지한다. 같은 대화에서는 동일한 Context/Session을 재사용하고,
다른 사용자나 대화에는 별도 Session을 만든다. checkpoint 키는 사용자 ID와 thread ID를 함께 사용한다.

Session을 다른 사용자/대화에 재사용하면 `SESSION_SCOPE_MISMATCH`,
checkpoint만 있고 새 Session을 넘기면 `SESSION_NOT_RESTORED`를 반환한다.
프로세스 재시작, 여러 프로세스/동시 호출, 저장소와 checkpoint 사이의 원자적 트랜잭션은
이번 변경의 보장 범위가 아니다. 해당 실행 환경이 필요해지면 Graph State와 영속 요청 저장소를
함께 설계해야 한다.

승인 시각은 각 요청에 보관한다. 새 interrupt에는 새 시각을 부여하고 같은 interrupt를 다시
표시할 때는 시각을 유지한다. 승인/거절 결과가 완료되면 활성 요청과 시각을 정리한다.
이에 따라 선호 저장 승인 이후 새 계획 승인이 이전 시각으로 만료되는 문제도 실행 래퍼에서 해결된다.

기존 `plan_id:version` 캐시는 `confirmed_by_plan`으로 이름을 바꿔 계획 중복 확정 방지 용도를 명시한다.
요청 ID 중복 처리는 별도의 `approval_requests` 기록이 담당한다.
공개 응답에는 선택 필드 `request_id`만 추가하며, LLM이 호출하는 Tool 인자는 바꾸지 않는다.

## 검증

```bash
python -m pytest -q
```

`tests/test_request_idempotency.py`에서 실제 build_agent/ask/decide와 Mock 모델·도구로 검증한다.
확정·선호 저장 재전송, 복수 승인, 내용 충돌, 사용자/대화 격리, 조건 변경 후 재조회,
후속 승인 ID, 승인 시각, 쓰기 이후 실패를 포함한다.

PR #4에 있던 테스트 import 오류는 `from tests.test_approval import ...`로 별도 수정한다.
PR #3의 충전 계산 변경은 이 브랜치에 포함하지 않으며, 임시 결합본으로 호환성을 검증한다.
