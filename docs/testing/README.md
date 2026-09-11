# VoltGo 테스트 케이스와 실행 방법

6반 4조의 팀원과 평가자가 같은 입력으로 동작을 재현하고 결과를 확인하기 위한 안내서다. 현재 기능, 실행 명령, 테스트별 입력·기대 결과·로그 근거를 함께 제공한다. 자동 회귀는 통과했으며 실제 모델의 대화 품질과 실계정 API 동작은 별도 시연으로 확인한다.

2026년 9월 11일 교수님 공지의 요구는 설계서와 코드의 기능·흐름·명칭 일치, 재현 가능한 실행 방법, 테스트별 입력·기대 결과·로그 등 확인 근거다. 이 문서는 테스트 부록이며 최종 설계서 파일명은 `6반_4조_설계서(최종).pdf`다. 기존 설계서 본문은 F 담당자가 최신 변경과 함께 취합한다.

## 1 현재 구성과 검증 범위

기준 main: PR10까지 병합된 `684728f45c2c9cd3fed0ec5927c04e7961efa622`. 후속 변경: `feat/a-runtime-hardening`. 실제 실행 SHA와 작업 트리 변경 유무는 실행 결과 JSON에 기록한다.

정상 흐름은 충전소 확인 → 충전 조회 → 시간 예산 → 장소 검색 → 왕복 경로 → 후보 선별 → 사용자 선택 → 재검증·확정이다. 선호 저장은 별도의 사용자 승인 뒤 실행한다. 경로 조회가 정상인데 시간 때문에 후보가 없으면 PR10의 대체 활동 판정으로 넘어가며, 다음 사용자 응답 전에는 다른 업종을 자동 검색하지 않는다.

| 구성 | 실제 코드와 책임 |
|---|---|
| 실행기 A | `agent.py`, `requests.py`, `assembler.py`, `state.py`, `schemas.py`로 ask/decide, 요청 ID, 출력 조립 |
| 충전 B | `clients/hyundai.py`, `core/time_budget.py`로 충전 응답 정규화와 시간 계산 |
| 장소 C | `clients/tmap_places.py`, `core/place_policy.py`로 충전소 선택·반경·캐시·필터 |
| 경로와 판정 D | `clients/tmap_routes.py`, `core/feasibility.py`, `agent/alternative_agent.py`로 왕복·선별·대체 활동 |
| 승인과 정책 E | `agent/approval.py`, `middleware.py`, `memory.py`로 HITL·한도·선호 저장 |
| 접점과 제출 | CLI `scripts/demo.py`, 웹 `scripts/web.py`와 `web/`, 문서·발표 취합 F |

등록 도구는 10개다: `find_station`, `get_charging_status`, `calculate_time_budget`, `search_nearby_places`, `get_walking_routes`, `select_feasible_plans`, `assess_time_shortage_alternatives`, `confirm_plan`, `save_preferences`, `delete_preferences`.

공개 출력은 ToolResult, ModelDecision, VoltGoResponse를 유지한다. 계획 확정에는 추가 승인 화면이 없고 `save_preferences`만 HITL 대상이다. 웹도 `request_id`를 그대로 사용하며 기존 웹 전용 `approval_id`는 사용하지 않는다. 상세 계약은 [웹 세션 문서](../user-session-isolation.md)에 있다.

## 2 환경 준비

저장소 접근 권한, Git, Python 3.12, Node.js가 필요하다. 확인한 환경은 macOS, Python 3.12.14, Node.js 26.7.0이다. 아래 Windows 절차는 안내이며 이 환경에서 직접 실행하지 않았다.

새 폴더에서 저장소와 작업 브랜치를 준비한다. 기존 작업 디렉터리의 변경사항을 덮어쓰지 않도록 별도 폴더를 사용한다.

```sh
git clone --branch feat/a-runtime-hardening https://github.com/dokwon33/voltgo.git voltgo-verify
cd voltgo-verify
git rev-parse HEAD
```

macOS 또는 Linux:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
node --version
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
node --version
.venv\Scripts\python.exe scripts/run_cases.py suite
```

Windows에서는 이후 명령의 `python`을 `.venv\Scripts\python.exe`로 바꾸면 활성화 없이 실행할 수 있다.

의존성은 requirements.txt에 고정되어 있다: langchain 1.4.0, langchain-openai 1.6.2, langgraph 1.2.11, pydantic 2.13.5, python-dotenv 1.2.3, requests 2.34.2, pytest 9.1.1. Node 테스트는 추가 npm 패키지가 필요 없다.

자동 검증에는 API 키가 필요 없다. 실행기는 자식 프로세스에서 .env 로딩과 tracing을 끄고 실계정 키를 비운다. 실제 graph·도구·HTTP 핸들러를 실행하되 모델 응답과 외부 API만 고정 대역으로 바꾼다. 선호 파일은 pytest 임시 디렉터리를 사용한다.

## 3 실행과 판정

저장소 루트에서 실행한다. 별도 validation-guide 폴더를 설치할 필요가 없다.

```sh
python scripts/run_cases.py list
python scripts/run_cases.py suite
python scripts/run_cases.py all
python scripts/run_cases.py TC12
python scripts/run_cases.py TC22
python scripts/run_cases.py TC23
```

`suite`는 Python 전체와 Node 브라우저 검증을 모두 실행한다. `all`은 문서에 연결한 대표 검사를 모아 실행한다. 전체 파일과 해당 파일의 개별 함수가 겹치면 한 번만 실행한다. 개별 TC 명령은 그 항목만 실행한다. TC 23개는 테스트 묶음 수이며 pytest 검사 수와 다르다. suite와 all은 중복을 포함하므로 성공 수를 합산하지 않는다.

직접 확인할 때:

```sh
python -m pytest -vv -s tests/test_runtime.py
node --test tests/test_web_history.cjs
```

결과는 `reports/<UTC실행시각>/`에 저장한다. `--output reports/review`로 위치를 지정할 수 있다. 같은 출력 디렉터리와 같은 명령을 재사용하면 이전 결과 파일이 덮어써지므로 실행별 경로를 권장한다.

| 증거 | 확인 방법 |
|---|---|
| `suite-pytest.log` | PASSED, 도구/한도 로그, 실패 traceback, 최종 성공 수와 경고 수 |
| `suite-pytest.xml` | 개별 testcase의 failure/error/skipped 및 전체 검사 수 |
| `suite-node.log` | 실제 페이지 JavaScript 검사의 pass/fail 수 |
| `suite.json` | 실제 SHA, 작업 트리 변경 유무, 시간, 환경, 명령, 각 종료 코드 |

Pass는 종료 코드 0, 실패·오류·의도치 않은 skip 없음, 각 TC의 assert 충족으로 판정한다. 저장은 응답 문구뿐 아니라 파일·필드·쓰기 횟수를 확인한다. pytest의 검증 실패는 1, 수집/사용 오류는 2, 실행 파일 부재는 실행기에서 127로 기록한다. Python이 성공해도 Node가 없거나 실패하면 suite 전체는 실패다.

현재 변경의 전체 검사에서 Python 235개와 Node 6개가 통과했다. 문서 대표 검사 all은 Python 142개와 Node 6개가 통과했다. Pydantic Context 직렬화 경고 150건이 남아 있다. 경고를 숨기거나 경고 없는 실행으로 보고하지 않는다. 모델 로그의 9/8은 실행 전 차단 시도이며 실제 호출 수는 TC17·TC22의 모델/handler 횟수 assert로 확인한다.

공통 고정 시각은 2026-09-10 14:00 KST다. 순수 계산 예시의 SoC 40→80%, 60kWh, 48kW는 30분 추정으로 완료 14:30이다. charging_ok fixture의 원문 잔여시간은 40분으로 완료 14:40이다. 사용자 제한 30분·버퍼 5분을 주면 두 경우 모두 복귀 마감은 14:25다. 이 두 입력의 완료 시각을 혼동하지 않는다.

## 4 자동 테스트 케이스

각 항목은 입력, 기대 결과, 정상 동작 확인 방법을 제공한다. 설계서 C 번호는 기존 연결을 유지하며 추가 회귀는 TC 번호로 구분한다. 정확한 실행 선택자는 [test_cases.json](test_cases.json)이 관리한다.

### TC01 시간 예산 계산

담당 B · 설계 연결 C001

입력: SoC 40→80%, 60kWh, 48kW, 원문 잔여시간 없음. 제한 30분·버퍼 5분.

기대 결과: 완료 14:30, 복귀 마감 14:25, 가용 1,500초, energy_power.

확인: budget의 시각·초 값에 대한 assert 통과.

실행: `python scripts/run_cases.py TC01`

### TC02 도구 전체 흐름

담당 A · 설계 연결 C001 C002

입력: charging_ok(잔여 40분), 제한 30분, meal, 체류 12분, 후보 A/B/C.

기대 결과: A/B 추천, C 제외. A 확정 후 장소 출발 마감 14:20.

확인: confirmed.plan_id=A 및 데이터·시각 assert. 계획 선택은 추가 승인 없이 재검증 후 확정.

실행: `python scripts/run_cases.py TC02`

### TC03 시간 경계와 선택 재검증

담당 D · 설계 연결 C005 C006

입력: 가용 1,500초에 총 소요 1,500/1,501초. 후보 A를 14:05에 재검증.

기대 결과: 1,500초는 포함, 1,501초는 제외. A 복귀 14:26이 마감 14:25를 넘으면 거절.

확인: 후보 목록과 recheck_plan 반환값 assert.

실행: `python scripts/run_cases.py TC03`

### TC04 충전 중단과 목표 도달

담당 B · 설계 연결 C003

입력: charging=false 또는 현재 SoC=목표 80%. charging_done fixture.

기대 결과: NOT_CHARGING 또는 TARGET_REACHED. 장소 조회 없음.

확인: 오류 코드 및 places_client.calls=0 assert.

실행: `python scripts/run_cases.py TC04`

### TC05 계산값 부족과 오래된 데이터

담당 B · 설계 연결 C004

입력: 평균 전력 0, 용량 또는 목표 None, 신선도 기준 시각에서 61초 경과.

기대 결과: 값 부족은 NEED_INPUT, 61초 경과는 STALE_DATA.

확인: 계산 코어의 NEED_INPUT·STALE_DATA assert. 실계정 API 호출은 별도 검증.

실행: `python scripts/run_cases.py TC05`

### TC06 충전소 선택과 재검색

담당 C · 설계 연결 C027

입력: Origin 없음, 역삼역 검색으로 S1/S2 반환, S2 선택. 새 검색이 빈 결과/오류인 경우도 실행.

기대 결과: 선택 전 Origin=None. S2 선택 후 설정, 선택 시 API 재호출 없음. 실패한 새 검색은 옛 후보 제거.

확인: station_candidates·Origin·API 호출 수 assert.

실행: `python scripts/run_cases.py TC06`

### TC07 거리 필터와 충전 시간 단위

담당 B C · 설계 연결 C025 C026

입력: 장소 거리 500m 경계·600m, 중복 POI. 잔여시간 1 hour, 40 min, 90000 msec, 75 sec.

기대 결과: 거리 초과·중복 제외. 단위별 3600/2400/90/75초로 정규화.

확인: 필터 ID 목록과 정규화 초 값 assert. C025의 동일 1800초 네 입력과는 다른 표본.

실행: `python scripts/run_cases.py TC07`

### TC08 TMAP 오류 구분

담당 C D · 설계 연결 C008 C009

입력: FakeSession으로 401/403/429/503/400 및 timeout·connection error 주입.

기대 결과: 401/403은 AUTH_ERROR·재시도 불가, 429/503/timeout은 재시도 가능 정보 보존.

확인: 예외 code/retryable 보존. 경로 503·timeout 뒤 middleware 재시도 1회 및 실제 Fake HTTP 호출 수 assert.

실행: `python scripts/run_cases.py TC08`

### TC09 경로 실패와 옛 후보 무효화

담당 D · 설계 연결 C010

입력: A 복귀 경로 실패·B 성공. 기존 후보/확정 뒤 재조회 실패 또는 이동시간 증가.

기대 결과: A 제외·B 유지 및 partial warning. 옛 경로 기반 후보·확정 제거, 새 경로로 재선별 필요.

확인: Fake HTTP 호출, Session.routes/candidates/confirmed, 버전 및 선별 결과 assert.

실행: `python scripts/run_cases.py TC09`

### TC10 조건 변경과 출력 근거

담당 A D · 설계 연결 C013 C011 C012

입력: 체류 12→20분 변경. 모델 후보 [A,X]. Mock 충전·장소·경로.

기대 결과: 계획 버전 증가·옛 확정 해제. X 제거, 실제 후보명/마감 및 Mock 출처 표시.

확인: 버전·confirmed·response.candidates·warnings assert.

실행: `python scripts/run_cases.py TC10`

### TC11 선호 승인과 계획 확정 정책

담당 A E · 설계 연결 C002 C016

입력: 계획 A 선택. 별도 선호 저장 approve/reject. 저장 두 건에는 [approve,reject].

기대 결과: 계획만 선택하면 즉시 재검증·확정. 저장은 승인 전 0회, 승인 후 1회. 저장 거절이 계획 확정을 막지 않음.

확인: 승인 대상 목록·CALLS·실제 confirmed·선호 파일 assert.

실행: `python scripts/run_cases.py TC11`

### TC12 선호 승인 만료와 조건 변경

담당 A E · 설계 연결 C016

입력: 14:00 선호 요청 뒤 120/121초에 승인. 응답 전달 지연 또는 같은 배치의 계획 조건 변경도 주입.

기대 결과: 120초 저장, 121초 APPROVAL_EXPIRED·저장 없음. 전달 지연·계획 변경은 만료를 연장하지 않음.

확인: 실제 graph 승인 재개에서 120/121초 경계, ToolResult의 APPROVAL_EXPIRED 및 임시 선호 파일 유무 assert. 별도 복제 테스트는 사용하지 않음.

실행: `python scripts/run_cases.py TC12`

### TC13 승인 재전송과 내용 충돌

담당 A · 설계 연결 C017

입력: 같은 ID/approve 재전송, 같은 ID/reject로 변경. cafe와 체류 15분 동시 저장 뒤 재전송.

기대 결과: 동일 요청은 기존 응답. 다른 결정은 REQUEST_ID_CONFLICT. 쓰기 재실행 없음.

확인: 단건 쓰기 1회, 두 필드 저장은 총 2회 이후 증가 없음. 응답 사본·파일의 두 필드·카운터 assert.

실행: `python scripts/run_cases.py TC13`

### TC14 새 승인 시각과 후속 요청

담당 A · 설계 연결 C016 C017

입력: 첫 선호 승인 3분 후 새 저장 요청. 재개 중 새 저장 승인 생성 뒤 자체 120/121초 경계 검증.

기대 결과: 새 ID와 새 승인 시각 발급. 이전 요청 재전송이 다음 승인을 실행하지 않음.

확인: 새 ID·시각·저장 횟수 및 만료 결과 assert. 과거 ID는 후속 승인을 소비하지 않음.

실행: `python scripts/run_cases.py TC14`

### TC15 사용자와 Session 격리

담당 A E · 설계 연결 C015 C017

입력: 동일 thread 이름을 쓰는 u1/u2. 타 대화·타 사용자 또는 새 Session으로 승인 시도.

기대 결과: 요청과 선호 분리. 잘못된 사용은 UNKNOWN_REQUEST_ID/SESSION_SCOPE_MISMATCH/SESSION_NOT_RESTORED.

확인: 오류 코드와 사용자별 쓰기 기록 assert.

실행: `python scripts/run_cases.py TC15`

### TC16 선호 보관과 삭제

담당 E · 설계 연결 C014 C015 C018

입력: u1 선호 저장 후 새 프로세스의 u1/u2 조회·삭제. 저장/교체 실패도 주입.

기대 결과: u1만 복원·삭제. 실패하면 기존 기록 보존 및 임시 파일 제거, 다른 사용자 선호 유지.

확인: subprocess 출력과 임시 선호 파일/조회 결과 assert. 실제 사용자 파일은 사용하지 않음.

실행: `python scripts/run_cases.py TC16`

### TC17 모델 한도와 쓰기 이후 실패

담당 A E · 설계 연결 C021 C017

입력: 마지막 모델 호출 timeout. 승인 재개 시 한도 소진. 선호 저장 후 모델 오류.

기대 결과: 허용 한도를 넘는 모델 실행 없음. LIMIT_EXCEEDED 응답. 쓰기 후 실패 재전송은 저장 반복 없음.

확인: handler 호출 수·LIMIT_EXCEEDED·APPROVAL_EXECUTION_FAILED 및 저장 1회 assert.

실행: `python scripts/run_cases.py TC17`

### TC18 확정 단계의 충전 목표 불일치

담당 B E · 설계 연결 C028

입력: 확정 재검증에 차량 목표 100%/사용자 80%, 반대로 차량 80%/사용자 100% 주입.

기대 결과: 낮은 사용자 목표는 추정 마감 14:25. 높은 사용자 목표는 차량 80% 기준 잔여시간 사용.

확인: 추천·확정의 동일 마감 및 예산 시각 assert. PR3 공통 목표 처리 포함, 실계정 API는 별도.

실행: `python scripts/run_cases.py TC18`

### TC19 검색 반경과 캐시 재사용

담당 C · 설계 연결 추가 회귀

입력: 500m 검색 후 1000m로 확대, 업종·출발지 변경, 실패 뒤 재검색.

기대 결과: 같은 1km 원본의 재필터는 API 추가 호출 없음. 업종·출발지 변경 시 별도 조회, 오류는 캐시하지 않음.

확인: Mock 클라이언트 호출 수와 Session 캐시·후보 목록 assert.

실행: `python scripts/run_cases.py TC19`

### TC20 시간 부족 대체 활동과 사용자 선택

담당 D · 설계 연결 추가 회귀

입력: 식사 시간이 부족한 첫 턴 뒤 편의점 동의·거절·불명확 응답·명시 업종·새 시간 조건을 각각 입력.

기대 결과: 첫 턴 자동 재검색 없음. 사용자 다음 응답 후 선택 업종을 한 번 재계획. 경로 오류를 시간 부족으로 오판하지 않음.

확인: PR10의 실제 graph 회귀 16개: 도구 호출 기록, reason, 후보, 사용자 조건, Session 불변 검증.

실행: `python scripts/run_cases.py TC20`

### TC21 웹 세션과 승인 재전송

담당 A · 설계 연결 추가 회귀

입력: 같은 IP의 두 쿠키 저장소, 타 대화 ID, 동일 승인 재전송·결정 변경·거절 사유 변경·저장 후 모델 실패.

기대 결과: 타 사용자/대화 접근 404. 같은 request_id/결정은 200과 기존 response, 충돌은 409. 새 승인 대기는 유지.

확인: 실제 HTTP 파서와 HITL, 임시 선호 쓰기 수·응답 동일성·pending_response·쿠키 만료 assert.

실행: `python scripts/run_cases.py TC21`

### TC22 A 실행기 오류와 전체 연결

담당 A · 설계 연결 추가 회귀

입력: 빈 입력·501자·가짜 키, 모델 timeout 2회/일반 오류, 충전소 검색과 반경 확대를 포함한 대체 추천, 이후 저장·재전송.

기대 결과: 거부 입력 checkpoint 0건. 오류는 AGENT_EXECUTION_FAILED로 정리하고 다음 턴 복구. 실제 모델 8회 내 시간 부족 안내 완료.

확인: checkpoint 이력·모델 실제 호출 수·응답 상태·비밀값 비노출·저장 횟수·대체 추천 이후 승인 계약 assert.

실행: `python scripts/run_cases.py TC22`

### TC23 브라우저 기록과 승인 화면 복원

담당 A · 설계 연결 추가 회귀

입력: 사용자/대화 변조, 초기화 도중 사용자 변경, 거부 입력, 이전 응답 재전송 중 새 승인 대기.

기대 결과: 소유권 확인 후 자기 기록만 표시. 거부 입력을 localStorage에 저장하지 않음. 새 request_id와 승인 화면 유지.

확인: Node가 실제 페이지 script와 session.js를 실행. 저장소 접근·DOM·POST 본문 assert.

실행: `python scripts/run_cases.py TC23`

## 5 실제 모델과 API 시연

다음 수동 시연은 이번 자동 검증 결과에 포함하지 않는다. LLM 문구와 도구 선택은 실제 모델로 확인해야 한다. 처음 설정한다면 .env.example을 참고해 .env를 작성하고 기존 설정은 보존한다. 모델 키는 파일에 직접 넣고 제출 문서나 로그에 복사하지 않는다.

```dotenv
OPENAI_API_KEY=<발급받은 키>
MODEL_NAME=gpt-4.1-mini
TMAP_APP_KEY=
USE_MOCK_CHARGING=true
MOCK_CHARGING_FIXTURE=charging_ok
VOLTGO_USER_ID=voltgo_test_001
```

```sh
python scripts/demo.py --fixed
python scripts/web.py --fixed
```

둘 중 필요한 UI를 실행한다. 웹 주소는 터미널 출력에 표시하며 기본값은 http://localhost:8000이다. 모델 키가 없어도 웹 홈·세션은 표시되지만 질문은 503을 반환한다. 키 없는 자동 회귀는 앞 절의 run_cases를 사용한다.

| ID | 입력과 실행 | 기대 결과와 기록 |
|---|---|---|
| M01 | 30분 안에 식사, 체류 12분 요청 후 표시된 후보 선택 | 마감 14:25와 왕복·체류 근거. 선택 후 추가 승인 없이 현재 상태 재검증·확정 |
| M02 | 20분 안에 식사 요청 후 편의점 동의, 별도 대화에서는 거절 | 식사 시간 부족이면 대안 제안. 다음 동의 뒤만 편의점 검색, 거절 뒤 자동 검색 없음 |
| M03 | 후보를 받은 뒤 체류를 20분으로 변경 | 이전 후보/확정 무효화와 새 선별. 불가능한 옛 계획 확정 금지 |
| M04 | 카페 선호·체류 15분 저장 요청 후 승인, 새 대화에서 조회·삭제 | 승인 전 저장 없음, 두 필드 보존, 본인만 조회·삭제 |
| M05 | 일반 창과 다른 프로필/시크릿 창에서 서로 다른 선호 요청 | 각자의 기록·선호·대기 승인만 표시. 같은 프로필의 탭은 같은 사용자 |
| M06 | 선호 승인 화면에서 새로고침 후 승인. 필요시 동일 request_id로 재전송 | 대기 화면 복원, 최초 결과와 재전송 response 동일, 추가 저장 없음 |

`--fixed`는 시간이 흐르지 않는다. 승인 2분 만료를 기다리는 시험에는 사용하지 말고 TC12를 실행하거나 실제 시계 모드에서 확인한다. 승인 120초까지 유효하고 121초부터 거절한다.

웹 재전송 수동 확인은 같은 브라우저 쿠키를 유지한 상태에서 개발자 도구 Network의 `/api/ask` 응답 ID를 기록하고 `/api/decide`의 JSON `{thread_id, request_id, decision}`을 같은 내용으로 재전송한다. 동일 `response`와 저장 로그 1회가 근거다. 최상위 `request_id`는 현재 대기 승인 ID이므로 완료 후 null일 수 있다.

TMAP 실연동은 TMAP_APP_KEY를 설정한 뒤 출발 충전소를 입력·선택하고 장소/경로 출처가 tmap인지 확인한다. 현대차는 B 담당자의 유효한 토큰·carId로 USE_MOCK_CHARGING=false를 설정해 관측 시각·충전 상태·잔여시간 단위를 검증한다. 자동 회귀 통과를 실계정 연동 성공으로 기록하지 않는다. 지도 UI는 준비되어 있으나 서버 map_data 연결은 완료되지 않았다.

## 6 기록과 제출

실행 결과에는 일시·실행자·코드 SHA·TC/M 번호·실제 입력·기대값·실제값·판정·로그 경로·후속 조치를 남긴다. 양식은 [실행결과 기록양식](실행결과_기록양식.md)을 사용한다. 실제 수행하지 않은 수동 검사는 미실행으로 표시한다.

A의 네 가지 후속 수정과 레거시 정리 범위는 [A 작업 문서](../tasks/A-runtime-hardening.md)에 있다. 충전 종료 시 이전 상태 정리, API 배치 한도, 사용자 목표 미입력 구분, 수동 잔여시간 입력, 지도 데이터 연결은 담당자 협의 항목으로 남아 있다. 이를 이 테스트 통과만으로 완료 처리하지 않는다.

최종 main 병합 뒤에는 그 main에서 suite를 재실행하고 SHA와 증거를 갱신한다. 메모리 세션·승인 원장은 서버 재시작 뒤 복구되지 않으며 일반 질문의 재전송 멱등 계약은 제공하지 않는다. 최종 설계서에는 도구 10개, 선호만 HITL, 대체 활동 다음 턴 재검색, 웹 request_id, 익명 세션 범위, 실제/Mock 구분을 일치시킨다.

## 근거

- [교수님 공지 2026년 9월 11일 10시 33분](https://theskala.slack.com/archives/C0BDXD0HS83/p1789090388645039)
- [PR10 대체 활동 변경](https://github.com/dokwon33/voltgo/pull/10)
- [기준 main](https://github.com/dokwon33/voltgo/tree/684728f45c2c9cd3fed0ec5927c04e7961efa622)
