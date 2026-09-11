"""
장기 기억 격리 (C014 · C015 · C018) 와 모델 호출 한도 (C021)

선호 파일은 임시 폴더에 쓰고, 실제 사용자 파일은 건드리지 않는다.
"""
from datetime import datetime, timezone

import pytest

from voltgo.agent import memory
from voltgo.agent.middleware import MAX_MODEL_CALLS, model_budget
from voltgo.agent.schemas import PreferenceRecord


@pytest.fixture(autouse=True)
def tmp_pref_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "PREF_DIR", tmp_path / "prefs")
    yield


def _rec(user_id, category="cafe"):
    return PreferenceRecord(user_id=user_id, preferred_category=category,
                            consent_at=datetime.now(timezone.utc))


# ---------------------------------------------------------------- C015
def test_구두점만_다른_사용자_ID_가_같은_파일을_쓰지_않는다():
    memory.save_preferences(_rec("a.b", "cafe"))
    memory.save_preferences(_rec("ab", "mart"))

    assert memory.load_preferences("a.b").preferred_category == "cafe"
    assert memory.load_preferences("ab").preferred_category == "mart"
    assert memory._path("a.b") != memory._path("ab")


def test_다른_사용자의_선호는_읽히지_않는다():
    memory.save_preferences(_rec("u1", "cafe"))
    assert memory.load_preferences("u2") is None


def test_파일_소유자가_다르면_읽지_않는다():
    # 같은 경로에 남의 기록이 들어간 경우를 흉내
    memory.save_preferences(_rec("u1", "cafe"))
    p = memory._path("u1")
    p.write_text(p.read_text(encoding="utf-8").replace('"u1"', '"u9"'), encoding="utf-8")
    assert memory.load_preferences("u1") is None


# ---------------------------------------------------------------- C014 · C018
def test_저장_후_다시_읽고_삭제하면_사라진다():
    memory.save_preferences(_rec("u1", "cafe"))
    assert memory.load_preferences("u1").preferred_category == "cafe"

    assert memory.delete_preferences("u1") is True
    assert memory.load_preferences("u1") is None
    assert memory.delete_preferences("u1") is False      # 두 번째 삭제는 False


def test_본인_삭제가_다른_사용자_기록을_지우지_않는다():
    memory.save_preferences(_rec("u1"))
    memory.save_preferences(_rec("u2"))
    memory.delete_preferences("u1")
    assert memory.load_preferences("u2") is not None


def test_빈_사용자_ID_는_거부한다():
    with pytest.raises(ValueError):
        memory._path("")


# ---------------------------------------------------------------- C021
class _Session:
    def __init__(self, used):
        self.counters = {"model": used}


class _Runtime:
    def __init__(self, session):
        self.context = type("C", (), {"session": session})()


class _Request:
    def __init__(self, session):
        self.runtime = _Runtime(session)


class _Timeout(Exception):
    pass


_Timeout.__name__ = "APITimeoutError"


def test_마지막_호출에서_일시_오류가_나도_한도를_넘기지_않는다():
    session = _Session(MAX_MODEL_CALLS - 1)      # 이번 호출이 마지막 허용분
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        raise _Timeout("timeout")

    with pytest.raises(RuntimeError, match="MODEL_BUDGET_EXCEEDED"):
        model_budget.wrap_model_call(_Request(session), handler)

    assert calls["n"] == 1, "재시도로 한도를 넘는 추가 호출이 있으면 안 된다"
    assert session.counters["model"] == MAX_MODEL_CALLS


def test_여력이_있으면_일시_오류는_한_번_재시도한다():
    session = _Session(0)
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Timeout("timeout")
        return "ok"

    assert model_budget.wrap_model_call(_Request(session), handler) == "ok"
    assert calls["n"] == 2
    assert session.counters["model"] == 2


# ================================================================
# C014 : 프로세스를 새로 띄워도 선호가 복원된다
# ================================================================
import subprocess                                                  # noqa: E402
import sys                                                         # noqa: E402
from pathlib import Path                                           # noqa: E402

_CHILD = """
import sys
from pathlib import Path
from voltgo.agent import memory

memory.PREF_DIR = Path(sys.argv[1])
record = memory.load_preferences(sys.argv[2])
print("NONE" if record is None else record.preferred_category)
"""


def _run_child(pref_dir: Path, user_id: str) -> str:
    """같은 선호 폴더를 보는 새 파이썬 프로세스에서 읽어 온다."""
    src = Path(__file__).resolve().parents[1] / "src"
    out = subprocess.run([sys.executable, "-c", _CHILD, str(pref_dir), user_id],
                         capture_output=True, text=True, env={"PYTHONPATH": str(src), "PATH": ""})
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_새_프로세스에서도_저장한_선호가_복원된다(tmp_path):
    memory.PREF_DIR = tmp_path / "prefs"          # autouse fixture 가 이미 바꿔 두지만 명시
    memory.save_preferences(_rec("u1", "cafe"))

    assert _run_child(memory.PREF_DIR, "u1") == "cafe", "재시작 후 복원되지 않았다"


def test_새_프로세스에서도_다른_사용자_선호는_안_보인다(tmp_path):
    memory.PREF_DIR = tmp_path / "prefs"
    memory.save_preferences(_rec("u1", "cafe"))

    assert _run_child(memory.PREF_DIR, "u2") == "NONE"
