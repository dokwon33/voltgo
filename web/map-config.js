// TMAP JavaScript 지도용 appKey. 이 값은 브라우저에 공개된다.
// scripts/web.py 가 .env 의 TMAP_MAP_APP_KEY 를 /map-config.js 응답에 주입한다.
// 서버용 비밀 키(TMAP_APP_KEY)는 여기에 복사하지 않는다. 정적 호스팅이면 배포 단계에서 주입한다.
window.VOLTGO_MAP_CONFIG = { appKey: '' };
