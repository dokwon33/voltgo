// scripts/web.py는 이 URL에 .env의 TMAP_MAP_APP_KEY를 주입해 응답합니다.
// 정적 호스팅 시에는 배포 단계에서 브라우저 공개용 지도 키를 주입하세요.
// 실제 키는 이 파일에 커밋하지 않습니다.
window.VOLTGO_MAP_CONFIG = { appKey: '' };
