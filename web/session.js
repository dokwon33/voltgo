// 사용자 ID는 /api/health에서 확인한 값만 사용한다. 인증 토큰은 HttpOnly 쿠키에만 둔다.
window.VoltGoHistory = class {
  constructor(userId, storage) {
    if (!/^visitor_[a-f0-9]{32}$/.test(userId)) throw new Error('접속 사용자를 확인하지 못했어요');
    this.key = 'voltgo.history.v1:' + userId;
    try { this.storage = storage || window.localStorage; } catch (_) { this.storage = null; }
  }
  load() {
    try {
      const data = JSON.parse(this.storage?.getItem(this.key) || 'null');
      return data && typeof data.thread_id === 'string' && Array.isArray(data.entries) ? data : null;
    } catch (_) { return null; }
  }
  save(threadId, entries) {
    try { this.storage?.setItem(this.key, JSON.stringify({ thread_id: threadId, entries: entries.slice(-100) })); }
    catch (_) { /* 저장이 차단된 브라우저에서도 현재 대화는 계속한다. */ }
  }
};
