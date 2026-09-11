# 지도 프론트 연결 계약

프론트 구현: `web/map.js`, `web/map.css`, `web/index.html`.
백엔드 변경 및 경로 API 호출은 포함하지 않는다.

## 백엔드 담당자에게 요청할 내용

`POST /api/ask`, `POST /api/decide`의 기존 `response`, `session` 옆에
웹 브리지가 Session의 출발지·장소 좌표와 경로 메타데이터를 아래 `map_data`에 전달합니다.
현재 RoundTrip에 없는 가는 길·오는 길 선 좌표를 추가해 주세요. 해당 응답의 `response.candidates`와 같은
출발지·조건 버전으로 생성한 데이터여야 합니다. 프론트는 각 응답의 지도를
따로 기억하므로 이전 카드 선택 시 그 카드 당시의 지도를 보여줍니다.

```json
{
  "response": { "candidates": [] },
  "session": {},
  "map_data": {
    "origin": {
      "name": "출발 충전소",
      "latitude": 37.5665,
      "longitude": 126.978,
      "source": "mock"
    },
    "places": [
      {
        "poi_id": "A",
        "name": "추천 카페",
        "latitude": 37.5675,
        "longitude": 126.979,
        "poi_source": "mock"
      }
    ],
    "routes": [
      {
        "poi_id": "A",
        "route_source": "mock",
        "outbound": {
          "type": "MultiLineString",
          "coordinates": [
            [[126.978, 37.5665], [126.9785, 37.567], [126.979, 37.5675]]
          ]
        },
        "inbound": {
          "type": "MultiLineString",
          "coordinates": [
            [[126.979, 37.5675], [126.979, 37.5665], [126.978, 37.5665]]
          ]
        }
      }
    ]
  }
}
```

위 좌표는 형식을 보여주기 위한 예시로 실제 도보 경로가 아니다.
기존 `response`와 `session`은 그대로 유지하며, 예제의 빈 `candidates` 대신
기존 추천 목록을 보낸다. 추천의 `poi_id`와 `places`/`routes`의 `poi_id`가
일치해야 마커와 경로가 표시된다. `plan_id`는 카드 선택에 사용한다.

- 좌표계: WGS84, `latitude`/`longitude`는 JSON 숫자.
- 경로 좌표: **`[경도, 위도]`**, 문자열이 아닌 숫자.
- `outbound`는 충전소 → 장소, `inbound`는 장소 → 충전소로 각각 조회한 결과.
- TMAP `features` 중 `geometry.type === "LineString"`인 구간을 순서대로
  모아 `MultiLineString.coordinates`로 전달하면 된다. 구간 경계를 유지한다.
- 단일 구간인 경우 `{ "type": "LineString", "coordinates": [[경도, 위도], ...] }`도 지원한다.
- `route_source`: `tmap` 또는 `mock`. 예시 경로는 반드시 `mock`으로 표시한다.
- 경로가 없으면 해당 방향을 `null`로 보내도 된다. 마커는 표시하고 해당 방향의
  경로 미제공을 안내한다. 출발지·목적지만 직선으로 연결하지 않는다.
- `map_data` 자체가 없어도 기존 대화·추천·확정 기능은 동작한다.
- 지도 데이터는 모델 응답으로 생성하지 말고, 서버가 보관한 실제 좌표에서 조립한다.
  화면에서 지도 선택/왕복 전환 시 추가 경로 API를 호출하지 않는다.

## 지도 SDK 설정

SK open API에서 앱을 생성하고 TMAP 지도(Raster Map) 사용 권한을 확인한다.
로컬 `.env`의 `TMAP_MAP_APP_KEY`에 브라우저 지도용 앱 키를 넣고 웹 서버를 재시작한다.
`scripts/web.py`가 `/map-config.js` 요청에 해당 값만 주입해 응답한다.
장소·경로 조회용 `TMAP_APP_KEY`와 `OPENAI_API_KEY`는 이 응답에 포함하지 않는다.

`TMAP_MAP_APP_KEY`는 우리 서비스에서 브라우저 공개용 키를 지정하는 환경변수 이름이다.
TMAP의 별도 키 종류를 뜻하지 않는다. 같은 앱 키를 사용하려면 지도 사용 권한과
브라우저 공개 가능 여부를 확인하고 명시적으로 이 항목에도 설정한다.
실제 키는 저장소에 커밋하지 않는다.

```dotenv
TMAP_MAP_APP_KEY=발급받은_지도용_앱키
```

정적 호스팅만 사용하는 경우에는 배포 단계에서 `web/map-config.js`를 생성한다.

```js
window.VOLTGO_MAP_CONFIG = { appKey: '브라우저 지도용 앱 키' };
```

좌표가 있는 추천을 처음 표시할 때 SDK를 한 번 로드한다. 키 미설정·네트워크
오류는 지도 영역 안에 안내하고 다시 시도할 수 있다. 기본 지도 중심점을
임의의 장소로 표시하지 않는다.

## 화면 동작

- 카드의 지도 버튼 또는 상단 지도 아이콘으로 연다. 충전소 `C`, 추천 순서 `1`, `2`, … 마커를 표시한다.
  지도를 보고 있는 중에 새 추천을 받으면 새 목록의 첫 추천으로 갱신한다.
- 카드의 `지도에서 보기` 또는 장소 마커로 경로를 선택한다. 계획 확정은 기존
  `여기로 갈래요` 버튼을 사용한다.
- 가는 길은 초록 실선, 오는 길은 보라 점선이다. 겹치면 `가는 길`/`오는 길`
  버튼으로 한 방향씩 확인한다.
- `전체 보기`는 선택한 장소·충전소·현재 표시한 경로를 화면에 맞춘다.
- 새 응답은 이전 좌표를 재사용하지 않는다. 이전 카드는 해당 응답의 좌표를 유지한다.
- 지도 안의 `추천 목록으로`와 상단 뒤로가기는 지도를 닫고 선택한 카드로 돌아간다.
  브라우저 뒤로가기·앞으로가기도 지도와 대화 화면에 연결되어 있다.
- 대화 화면에서 뒤로가면 대화를 유지한 채 홈으로 돌아간다. 홈의
  `이전 대화 이어보기`로 같은 대화와 추천을 다시 열 수 있다.
- 상단의 `처음으로` 버튼은 제거했다. 뒤로가기는 대화와 지도 데이터를 유지한다.

공식 참고: [TMAP JavaScript SDK 가이드](https://tmapapi.tmapmobility.com/main.html),
[지도 생성 예제](https://tmapapi.tmapmobility.com/main.html#webv2/sample/webSample01),
[경로 응답 예제](https://tmap-skopenapi.readme.io/reference/경로안내-샘플예제).
