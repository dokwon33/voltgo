"""
장기 기억 = 사용자 선호 (설계서 3.1 Store)

노트북 [5] 4장은 InMemoryStore 를 썼는데, 프로세스가 죽으면 날아간다.
설계서대로 user_id 별 로컬 JSON 파일에 저장한다. data/prefs/ 는 git 에서 제외.
"""
import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from voltgo.agent.schemas import PreferenceRecord

PREF_DIR = Path(__file__).resolve().parents[3] / "data" / "prefs"


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


def load_preferences(user_id: str) -> Optional[PreferenceRecord]:
    p = _path(user_id)
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
    # 임시 파일에 쓰고 os.replace 로 바꿔치기 -> 쓰다 죽어도 반쪽짜리 파일이 안 남는다
    PREF_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(record.user_id)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def delete_preferences(user_id: str) -> bool:
    p = _path(user_id)
    if p.exists():
        p.unlink()
        return True
    return False
