"""현재 저장소 테스트를 케이스별로 실행하고 재현 증거를 reports에 보관한다."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def unique_selectors(selectors):
    """전체 파일 선택이 있으면 그 파일의 개별 함수 선택은 중복 실행하지 않는다."""
    unique = list(dict.fromkeys(selectors))
    files = {s for s in unique if "::" not in s}
    return [s for s in unique if "::" not in s or s.split("::", 1)[0] not in files]


def git_value(repo, *args):
    try:
        return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", help="TC01..TC23, list, all (문서 대표 검사), suite (Python 전체 + Node)")
    parser.add_argument("--output", type=Path, help="결과 디렉터리. 기본 reports/UTC실행시각")
    args = parser.parse_args()
    cases = json.loads((ROOT / "docs/testing/test_cases.json").read_text(encoding="utf-8"))
    if args.case == "list":
        for case in cases:
            print(f"{case['id']}  {case['title']}  담당 {case['owner']}  {case['engine']}")
        return 0
    catalog = {case["id"]: case for case in cases}
    if args.case not in catalog and args.case not in ("all", "suite"):
        parser.error("지원하지 않는 케이스입니다. list로 확인하세요.")
    selected = cases if args.case in ("all", "suite") else [catalog[args.case]]
    groups = {}
    for engine in ("pytest", "node"):
        selectors = unique_selectors([s for c in selected if c["engine"] == engine for s in c["selectors"]])
        if selectors:
            if args.case == "suite":
                selectors = ["tests"] if engine == "pytest" else [
                    str(p.relative_to(ROOT)) for p in sorted((ROOT / "web/tests").glob("*.cjs"))]
            groups[engine] = selectors
    now = datetime.now(timezone.utc)
    out = (args.output or ROOT / "reports" / now.strftime("%Y%m%dT%H%M%S%fZ")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    # 자동 검증은 외부 모델/API 대역만 쓴다. .env의 실제 키가 테스트 환경으로 복원되지 않게 한다.
    env.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT),
                "OPENAI_API_KEY": "", "TMAP_APP_KEY": "", "HYUNDAI_ACCESS_TOKEN": "", "HYUNDAI_CAR_ID": "",
                "USE_MOCK_CHARGING": "true", "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"})
    records = []
    for engine, selectors in groups.items():
        stem = args.case + "-" + engine
        if engine == "pytest":
            cmd = [sys.executable, "-m", "pytest", "-vv", "-s", "-p", "no:cacheprovider",
                   "--junitxml=" + str(out / (stem + ".xml")), *selectors]
        else:
            cmd = [shutil.which("node") or "node", "--test", *selectors]
        try:
            result = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace")
            output, code = result.stdout, result.returncode
        except OSError as exc:
            output, code = f"{engine} 실행 파일을 찾거나 실행할 수 없습니다: {type(exc).__name__}\n", 127
        (out / (stem + ".log")).write_text(output, encoding="utf-8")
        record = {"engine": engine, "command": cmd, "exit_code": code, "log": stem + ".log"}
        if engine == "pytest" and (out / (stem + ".xml")).exists():
            tree = ET.parse(out / (stem + ".xml"))
            suites = list(tree.iter("testsuite"))
            record["counts"] = {k: sum(int(s.attrib.get(k, 0)) for s in suites)
                                for k in ("tests", "failures", "errors", "skipped")}
            record["junit"] = stem + ".xml"
        records.append(record)
        print(f"{engine}: exit={code}; log={out / (stem + '.log')}")
        print("\n".join(output.splitlines()[-10:]))
    exit_code = next((r["exit_code"] for r in records if r["exit_code"] != 0), 0)
    status = git_value(ROOT, "status", "--porcelain")
    manifest = {"case": args.case, "run_at_utc": now.isoformat(), "actual_sha": git_value(ROOT, "rev-parse", "HEAD"),
                "working_tree_dirty": bool(status) if status is not None else None,
                "python": platform.python_version(), "platform": platform.platform(),
                "scope": "offline_deterministic", "runs": records, "exit_code": exit_code}
    (out / (args.case + ".json")).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"RESULTS: {out}\nEXIT_CODE: {exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
