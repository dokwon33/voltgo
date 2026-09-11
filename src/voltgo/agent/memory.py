"""
장기 기억 = 사용자 선호 (설계서 3.1 Store)

노트북 [5] 4장은 InMemoryStore 를 썼는데, 프로세스가 죽으면 날아간다.
설계서대로 user_id 별 로컬 JSON 파일에 저장한다. data/prefs/ 는 git 에서 제외.
"""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock, RLock
from typing import Optional
from weakref import WeakValueDictionary

from voltgo.agent.schemas import Category, PreferenceRecord

PREF_DIR = Path(__file__).resolve().parents[3] / "data" / "prefs"

# LangGraph가 같은 묶음의 도구를 병렬 실행해도 사용자별 갱신은 직렬화한다.
# 사용 중인 lock만 유지한다. 서로 다른 사용자는 동시에 저장할 수 있다.
_LOCKS = WeakValueDictionary()
_LOCKS_GUARD = Lock()


def _path(user_id: str) -> Path:
    """사용자별 선호 파일 경로.

    글자만 걸러내면 "a.b" 와 "ab" 가 같은 파일이 된다(다른 사용자의 선호를 읽게 됨).
    읽기 쉬운 접두사 + 전체 ID 해시로 서로 다른 ID 가 절대 겹치지 않게 한다.
    """
    if not user_id:
        raise ValueError("user_id 가 비어 있다")
    safe = "".join(ch for ch in user_id if ch.isalnum() or ch in "-_")[:40] or "user"
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
    return PREF_DIR / f"{safe}-{digest}.json"


def _lock_for(path: Path):
    with _LOCKS_GUARD:
        lock = _LOCKS.get(path)
        if lock is None:
            lock = RLock()
            _LOCKS[path] = lock
        return lock


def load_preferences(user_id: str) -> Optional[PreferenceRecord]:
    p = _path(user_id)
    with _lock_for(p):
        if not p.exists():
            return None
        try:
            with open(p, encoding="utf-8") as f:
                record = PreferenceRecord.model_validate(json.load(f))
        except Exception:
            return None          # 깨진 파일은 없는 걸로 (기본값 사용)
        if record.user_id != user_id:
            return None          # 파일 안의 소유자가 다르면 남의 기록이다
        return record


def save_preferences(record: PreferenceRecord) -> None:
    """완성된 기록을 원자적으로 교체한다. 부분 갱신에는 update_preferences를 쓴다."""
    p = _path(record.user_id)
    with _lock_for(p):
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            # 각 쓰기가 자기 임시 파일을 사용한다. 실패하면 기존 기록은 유지한다.
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent,
                                    prefix=f".{p.stem}.", suffix=".tmp", delete=False) as f:
                tmp = Path(f.name)
                json.dump(record.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def update_preferences(user_id: str, *, category: Optional[Category] = None,
                       dwell_min: Optional[int] = None, consent_at: datetime) -> PreferenceRecord:
    """부분 갱신의 읽기·병합·저장 전체를 같은 프로세스의 사용자별 lock으로 보호한다."""
    with _lock_for(_path(user_id)):
        old = load_preferences(user_id)
        record = PreferenceRecord(
            user_id=user_id,
            preferred_category=category if category is not None else (old.preferred_category if old else None),
            dwell_min=dwell_min if dwell_min is not None else (old.dwell_min if old else None),
            consent_at=consent_at,
        )
        save_preferences(record)
        return record


def delete_preferences(user_id: str) -> bool:
    p = _path(user_id)
    with _lock_for(p):
        if p.exists():
            p.unlink()
            return True
        return False
