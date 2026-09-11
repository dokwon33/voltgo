# 접속자별 웹 격리

요구사항 출처: [장인우님, New 4조, 2026-09-11 12:02](https://theskala.slack.com/archives/C0BUVHWFLDT/p1789095739085709).

기존 웹은 모든 접속자에게 하나의 `VOLTGO_USER_ID`를 사용하고, 클라이언트가 보낸 `thread_id`로 Context를 만들었다. 브라우저가 다르더라도 선호가 공유되고 다른 사람의 대화 ID를 알면 상태 조회·질문·승인 처리가 가능했다. 서버가 발급하고 검증한 익명 세션으로 이 경계를 분리한다.

## 데이터와 소유권

| 데이터 | 구분 기준 |
|---|---|
| 접속 사용자 | 서버 메모리에 등록된 무작위 세션 토큰 → 별도의 무작위 `visitor_…` ID |
| 선호 저장·삭제 | 검증한 세션의 `user_id`로 기존 memory 모듈 호출 |
| Context·Session | `(user_id, thread_id)` |
| 대화 소유권 | 서버가 대화 ID를 발급할 때 소유자 기록. 모든 조회·변경에서 검사 |
| 에이전트 checkpoint | 사용자·대화 조합을 인코딩한 내부 thread ID |
| 승인 캐시 | 해당 Session의 `approval_requests[request_id]`에 대기 화면과 완료 응답 저장 |
| 브라우저 기록 | `voltgo.history.v1:<서버에서 확인한 user_id>` |

사용자 ID를 요청 JSON에서 지정하면 400이다. 헤더나 쿼리의 사용자 ID도 인증에 사용하지 않는다. 공개된 `visitor_…` ID를 쿠키로 보내거나 세션 토큰을 변조해도 등록된 토큰이 아니므로 인증되지 않는다.

대화 조회, 질문, 충전 새로고침, 승인 처리 모두 소유자를 먼저 검사한다. 다른 사용자의 대화와 없는 대화는 같은 404를 반환하며 모델·공급자 호출 전에 종료한다. 같은 사용자의 다른 대화에 승인 ID를 보내도 404다.

## HTTP 계약

1. `GET /api/health`로 세션을 시작한다. 유효한 쿠키가 있으면 같은 사용자 ID가 반환된다.
2. `POST /api/session`에 `{}`를 보내 새 대화를 만든다. 클라이언트가 새 대화 ID를 정할 수 없다.
3. 이후 요청은 같은 쿠키와 반환된 `thread_id`를 사용한다.
4. `/api/ask` 응답이 `awaiting_approval`이면 응답 최상위의 `request_id`를 보관한다.
5. `/api/decide`에 `{thread_id, request_id, decision}`을 보낸다. 승인 ID는 자기 대화에만 적용된다. 같은 결정과 거절 사유의 재전송은 완료 응답을 반환한다.

`GET /api/session?thread_id=…`는 본인 상태와 `request_id`, `pending_response`를 반환한다. 프론트는 이 확인이 끝난 뒤에만 로컬 기록을 화면에 복원한다. 초기화 중 다른 탭에서 쿠키 사용자가 바뀌어 응답의 사용자 ID가 달라져도 이전 사용자 키로 기록을 저장하지 않고 화면을 비운다.

질문이 승인 화면을 덮어쓰지 않도록 승인 대기 중 `/api/ask`는 409다. 웹과 CLI가 동일한 `request_id`를 사용한다. 별도 승인 ID나 소비 캐시는 없다. 같은 ID와 같은 결정/거절 사유는 HTTP 200과 기존 `response`를 반환하며, 다른 결정/거절 사유는 409다. 승인 실행 중 오류도 200의 `response.status=error`로 보관하므로 같은 ID로 저장을 다시 실행하지 않는다.

최상위 `request_id`와 `pending_response`는 **현재 대기 요청**, `response`는 **이번 요청의 결과**다. 과거 ID 재전송 중 새 승인이 대기한다면 두 ID가 다를 수 있다. 새로고침과 승인 UI는 현재 대기 정보를 사용한다. `reason`은 선택 문자열이며 거절 사유에 적용한다.

이 브랜치에서 기존 웹 전용 `approval_id` 필드를 `request_id`로 통일했다. 웹 서버와 화면을 함께 갱신한다. 구형 필드만 보내는 외부 클라이언트는 400을 받으므로 변경이 필요하다. 서버 재시작 시 메모리 세션은 복구되지 않는다.

`POST /api/charging/refresh`는 모델 호출 없이 해당 대화의 충전 공급자를 갱신한다. 데이터 공급자 자체는 데모의 공통 환경설정을 따른다.

## 쿠키와 요청 보호

- 32바이트 난수로 세션 토큰을 발급한다. 토큰은 응답 JSON·브라우저 기록에 넣지 않는다.
- 쿠키는 `HttpOnly`, `SameSite=Strict`, `Path=/`, `Max-Age=86400`이다. Domain은 지정하지 않는다.
- HTTPS 배포에서는 `VOLTGO_COOKIE_SECURE=true`로 Secure 쿠키를 사용한다. 로컬 HTTP 실행의 기본값은 false다.
- POST는 JSON만 받는다. 다른 Origin 또는 `Sec-Fetch-Site: cross-site` 요청은 거절한다. CORS 허용 헤더는 제공하지 않는다.
- 사용자 데이터 응답은 `Cache-Control: no-store`, `Vary: Cookie`를 사용한다.

설정 근거: [MDN Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie), [Python secrets](https://docs.python.org/3/library/secrets.html).

## 실습 범위

같은 브라우저 프로필의 탭들은 같은 익명 사용자다. 다른 프로필·시크릿 창은 별도 사용자다. 새 대화는 상태가 분리되지만 같은 사용자의 선호는 공유한다.

세션은 발급 시점부터 24시간이며 서버 메모리에 저장한다. 쿠키 삭제·만료 또는 프로세스 재시작 시 새 익명 사용자로 시작한다. 기존 선호 파일이 남아 있어도 새 사용자에게 자동 연결하지 않는다. 로그인 계정, 기기 간 동일 사용자 식별, 사용자별 현대차 OAuth·차량 연결, 여러 서버 간 세션 공유는 구현 범위 밖이다. 현재 차량·TMAP 공급자 설정은 실습용 공통 설정이다.

기존 `feat/web-demo`의 웹 서버·화면·지도 UI를 가져와 main 위에 연결했다. 별도의 웹 프레임워크나 인증 패키지는 추가하지 않았다. 웹은 에이전트의 비공개 checkpoint를 해석하지 않고 공개 실행 래퍼가 관리하는 Session 승인 원장을 조회한다.

## 검증

```bash
python -m pytest -q
node --test tests/test_web_history.cjs
```

Python 검증은 실제 HTTP 파서·쿠키 헤더·웹 핸들러와 실제 에이전트/HITL을 사용하며 모델·외부 API 응답만 테스트 대역을 쓴다. 같은 IP의 서로 다른 쿠키 저장소 두 개로 다음을 확인한다.

- A와 B가 각자 선호를 저장하고 A만 삭제해도 B의 선호는 유지된다.
- B의 모델 입력에 A의 대화가 들어가지 않는다.
- 타인의 대화 조회·질문·충전 갱신·승인이 모두 404이며 외부 호출이 없다.
- 다른 사용자 또는 같은 사용자의 다른 대화에서 승인 ID를 재사용할 수 없다.
- 승인 중복·실패 후 재전송은 저장 반복 없이 원래 응답을 반환한다. 결정 충돌, 쿠키 변조·만료, 사용자 ID 위조, 다른 사이트의 POST는 거부한다.

JavaScript 검증은 실제 페이지 초기화 코드를 실행해 다른 사용자 기록 미열람, 조작된 대화 ID의 표시 차단, 초기화 도중 사용자 변경 감지, 본인 기록 복원을 확인한다.
