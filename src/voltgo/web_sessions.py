"""단일 프로세스 데모용 익명 브라우저 세션. 계정 로그인과는 별개다."""
import secrets
import time
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from threading import Lock

COOKIE_NAME = "voltgo_session"
SESSION_TTL_SEC = 24 * 60 * 60


@dataclass(frozen=True)
class Visitor:
    user_id: str
    expires_at: float


class BrowserSessions:
    def __init__(self, *, clock=time.time, ttl=SESSION_TTL_SEC):
        self.clock = clock
        self.ttl = ttl
        self._sessions = {}
        self._lock = Lock()

    def resolve(self, cookie_header: str):
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header or "")
        except CookieError:
            return None
        cookie = cookies.get(COOKIE_NAME)
        if cookie is None:
            return None
        with self._lock:
            visitor = self._sessions.get(cookie.value)
            if visitor is not None and visitor.expires_at <= self.clock():
                self._sessions.pop(cookie.value, None)
                return None
            return visitor

    def issue(self, *, secure=False):
        with self._lock:
            now = self.clock()
            self._sessions = {k: v for k, v in self._sessions.items() if v.expires_at > now}
            token = secrets.token_urlsafe(32)
            visitor = Visitor("visitor_" + secrets.token_hex(16), now + self.ttl)
            self._sessions[token] = visitor
        cookies = SimpleCookie()
        cookies[COOKIE_NAME] = token
        cookie = cookies[COOKIE_NAME]
        cookie["path"] = "/"
        cookie["httponly"] = True
        cookie["samesite"] = "Strict"
        cookie["max-age"] = self.ttl
        if secure:
            cookie["secure"] = True
        return visitor, cookie.OutputString()
