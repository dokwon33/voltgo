# A: 선호 저장 승인과 request_id 재전송 처리

통합 기준: PR3·PR5·PR4가 병합된 main `a2d9ac2`.
브랜치: `feat/a-request-idempotency`.

## 실행 정책과 구현 계획

- [x] 최신 main의 충전 목표 공통 처리와 경로 갱신·후보 무효화 유지.
- [x] 계획 선택은 `confirm_plan`이 현재 시각·충전 상태를 재검증한 뒤 확정. 추가 승인 없음.
- [x] `save_preferences`만 HITL 승인. 한 배치에 여러 저장이 있으면 하나의 request_id로 관리.
- [x] 승인 120초까지 저장 허용, 121초부터 `APPROVAL_EXPIRED`로 거부.
- [x] 응답 전달이 지연돼도 HITL 중단 직전에 기록한 시작 시각을 유지.
- [x] 계획 조건 변경은 후보·확정만 무효화. 별도 선호 승인의 만료 시각을 지우지 않음.
- [x] 사용자별 잠금으로 선호 읽기→필드 병합→저장 전체를 직렬화하고 고유 임시 파일 사용.
- [x] 동일 요청 재전송, 오류 응답 보관, 사용자·대화 격리와 후속 승인 시각 회귀 검사.
- [ ] 실제 모델·TMAP·현대차 API 시연 및 교수님 제출용 실제 실행 기록.

## 요청 ID 계약

`ask()`가 `awaiting_approval`을 반환하면 응답의 `request_id`에 코드가 만든 UUID가 담긴다.
발급 당시 저장 도구 이름·인자와 checkpoint interrupt ID를 묶어 검증한다.

- 같은 ID와 같은 결정/거절 사유를 다시 보내면 보관한 응답의 사본을 반환한다. 모델·도구·파일 쓰기를 재실행하지 않는다.
- 같은 ID에 다른 결정/거절 사유를 보내거나 승인 대상이 바뀌면 `REQUEST_ID_CONFLICT`를 반환한다.
- 문자열 `"approve"`와 같은 개수의 `["approve", ...]`는 같은 결정이다.
- 잘못된 결정/개수는 실행 전에 거부하므로 같은 ID로 올바르게 다시 응답할 수 있다.
- 완료 응답은 계획 조건 변경 후에도 보관한다. 과거 응답 조회가 현재 Session을 되돌리지 않는다.
- 실행 중 오류가 나면 오류 응답도 보관한다. 쓰기 후 모델이 실패할 수 있으므로 같은 ID로 자동 재실행하지 않는다. 오류 응답이 부수 효과가 없었다는 뜻은 아니다.

```python
response = ask(agent, "카페 선호와 체류 15분을 기억해줘", context, thread_id="t1")
if response.status == "awaiting_approval":
    request_id = response.request_id
    response = decide(agent, "approve", context, thread_id="t1", request_id=request_id)
    replay = decide(agent, "approve", context, thread_id="t1", request_id=request_id)
```

`request_id` 생략은 현재 대기 요청을 대상으로 하는 기존 호출부 호환용이다.
재전송하는 호출자는 승인 화면에서 받은 ID를 반드시 보관한다. CLI도 응답의 ID를 전달한다.
재개 중 새 승인이 이어지면 반환 응답의 ID는 새 승인 ID이며, 이전 ID 재전송은 새 승인을 실행하지 않는다.

계획 확정과 선호 저장이 같은 모델 메시지에 있어도 승인 목록에는 저장만 들어간다.
LangGraph는 그 배치 전체를 일시 중단하므로 계획 확정 도구도 저장 결정 후 실행될 수 있지만,
저장을 거절해도 계획 확정은 재검증을 거쳐 진행된다. 계획만 선택하면 추가 승인 대기가 없다.

## 승인 시각과 선호 저장

`after_model`은 등록 역순으로 실행된다. 시각 기록 미들웨어를 HITL 뒤에 등록해
승인 중단 전에 시각을 남기고, 요청 ID에도 그 값을 보관한다.
같은 interrupt를 다시 표시해도 만료 시각은 연장하지 않는다.
새 interrupt에는 새 ID와 새 시각을 부여하며, 기존 요청의 완료 정리가 새 요청의 시각을 지우지 않는다.

선호 저장 승인 120초/121초 경계는 모든 저장에 동일하게 적용한다.
같은 묶음의 다른 도구가 계획 조건을 바꾸더라도 선호 승인의 시각은 유지한다.
프로세스 내 사용자별 잠금은 여러 저장 필드를 보존하고, 요청별 임시 파일은 파일 충돌을 막는다.
다른 사용자의 선호 갱신은 병렬로 진행할 수 있다. 이 보장은 여러 프로세스 간 트랜잭션을 뜻하지 않는다.

## Session과 스키마

현재 CLI는 동일한 Context/Session을 같은 대화에서 재사용한다.
다른 사용자·대화에는 별도 Session을 만든다. checkpoint 키는 사용자 ID와 thread ID의 조합이다.
잘못된 재사용은 `SESSION_SCOPE_MISMATCH`, checkpoint만 있고 새 Session을 넘기면
`SESSION_NOT_RESTORED`로 거부한다.

Session을 Graph State로 옮기지는 않았다. 프로세스 재시작 후 승인 복원,
여러 프로세스·동시 사용자 요청에 대한 영속 멱등 보장, checkpoint와 파일 저장의 원자성은 별도 설계 범위다.
단일 사용자 요청 안에서 ToolNode가 여러 저장을 병렬 실행하는 경우는 이번 테스트에 포함한다.

계획 중복 확정 캐시는 `confirmed_by_plan`, 승인 재전송 기록은 `approval_requests`로 구분한다.
PR5의 경로 갱신 코드와 테스트도 이 이름으로 일치시켰다.
공개 응답에는 선택 필드 `VoltGoResponse.request_id`만 추가하며 LLM Tool 인자는 변경하지 않는다.
PR3의 ChargingSnapshot 필드와 공통 충전 목표 계산은 유지한다.

## 재현 가능한 검증

저장소 루트에서 requirements.txt를 설치한 가상환경으로 실행한다.
외부 모델/API를 호출하지 않으며 선호 파일은 pytest 임시 폴더에 저장한다.

```bash
python -m pytest -q -p no:cacheprovider
python -m pytest tests/test_request_idempotency.py tests/test_decide_entrypoint.py tests/test_approval.py -q -p no:cacheprovider
```

주요 입력과 기대 결과:

| 시나리오 | 입력 | 기대 결과 / 확인 |
|---|---|---|
| 계획 선택 | 후보 A를 선택 | confirmed, 추가 승인과 request_id 없음 |
| 선호 만료 | 14:00 요청 뒤 120초/121초에 승인 | 120초 저장, 121초 APPROVAL_EXPIRED·파일 없음 |
| 복수 저장 | cafe와 dwell_min=15를 함께 승인 | 두 도구 성공, 최종 파일에 두 필드 보존 |
| 재전송 | 같은 request_id로 approve 반복 | 같은 응답, 도구/모델/파일 쓰기 횟수 증가 없음 |
| 결정 충돌 | 처리한 ID에 reject | REQUEST_ID_CONFLICT, 추가 쓰기 없음 |
| 후속 승인 | 첫 저장 승인 후 두 번째 승인 발생 | 새 ID·시각, 두 번째 요청 자체의 120/121초 기준 적용 |
| 전달 지연 | 요청 생성 10초 뒤 응답 전달 | 요청 생성 시각 유지, 121초 승인 거절 |
| 조건 변경과 저장 | 시간 제한 변경 도구와 만료된 저장을 같은 묶음에 실행 | 버전은 증가하나 저장은 APPROVAL_EXPIRED |
| 실패 후 재전송 | 저장 후 모델 오류 | 오류 응답 보관, 저장 반복 없음 |
| 세션 격리 | 동일 thread 이름의 u1/u2, 타 Session으로 승인 | 요청 분리, 잘못된 재개 거부 |

pytest의 PASS와 실제 도구 결과/파일 상태를 함께 판정한다.
Pydantic Context 직렬화 경고는 남아 있으므로 경고 없는 실행으로 보고하지 않는다.
실제 LLM 대화 품질과 실계정 API는 별도 시연 결과가 필요하다.
