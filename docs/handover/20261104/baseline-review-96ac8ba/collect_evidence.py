"""Read repository evidence; write only to this separate audit directory."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

OUT = Path(__file__).resolve().parent
ROOT = OUT.parent.parent
REPOS = {"A": ROOT / "Disaster_Rescue_DRRM", "B": ROOT / "Disaster_Rescue_DRRM_B"}
ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONDONTWRITEBYTECODE": "1"}


def git(repo, *args):
    run = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo,
                         env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode:
        raise RuntimeError(f"git {args}: {run.stderr.decode('utf-8', 'replace')}")
    return run.stdout


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot():
    result = {}
    for label, repo in REPOS.items():
        tracked = git(repo, "ls-files", "-z").decode("utf-8").rstrip("\0").split("\0")
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").decode("utf-8").rstrip("\0")
        untracked = untracked.split("\0") if untracked else []
        all_paths = tracked + untracked
        result[label] = {
            "path": str(repo), "head": git(repo, "rev-parse", "HEAD").decode().strip(),
            "branch": git(repo, "branch", "--show-current").decode().strip(),
            "status": git(repo, "status", "--porcelain=v1", "--untracked-files=all").decode("utf-8"),
            "tracked_count": len(tracked), "untracked": untracked,
            "sha256": {p: digest((repo / p).read_bytes()) if (repo / p).is_file() else None
                       for p in all_paths},
        }
    return result


def initial():
    (OUT / "repositories-before.json").write_text(json.dumps(snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
    commands = {
        "status": ("status", "--short", "--branch"),
        "working-diff": ("diff", "--no-ext-diff", "--no-textconv"),
        "staged-diff": ("diff", "--cached", "--no-ext-diff", "--no-textconv"),
        "working-stat": ("diff", "--stat"),
        "staged-stat": ("diff", "--cached", "--stat"),
    }
    for label, repo in REPOS.items():
        for name, args in commands.items():
            (OUT / f"{label}-{name}.txt").write_bytes(git(repo, *args))
    a = REPOS["A"]
    paths = git(a, "ls-files", "miniprogram").decode("utf-8").splitlines()
    rows = []
    for p in paths:
        working = (a / p).read_bytes()
        committed = git(a, "show", "HEAD:" + p)
        rows.append({"path": p, "bytes": len(working), "sha256": digest(working),
                     "head_sha256": digest(committed),
                     "matches_HEAD_ignoring_CRLF": working.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n")})
    (OUT / "A-file-manifest.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "A-file-list.txt").write_text("\n".join(paths) + "\n", encoding="utf-8")
    for label, repo in REPOS.items():
        data = (repo / "schemas/s4_contract.json").read_bytes()
        json.loads(data)
        (OUT / f"{label}-s4_contract.json").write_bytes(data)
    (OUT / "A-to-B-contract.diff").write_bytes(git(a, "diff", "--no-ext-diff", "--no-textconv",
        "feature/v4.4-a-miniprogram:schemas/s4_contract.json", "feature/v4.4-b-backend:schemas/s4_contract.json"))
    b = REPOS["B"]
    (OUT / "B-existing-96ac8ba-files.txt").write_bytes(git(b, "diff-tree", "--no-commit-id", "--name-status", "-r", "96ac8ba"))
    (OUT / "B-existing-96ac8ba-stat.txt").write_bytes(git(b, "show", "--format=fuller", "--stat", "96ac8ba"))
    relevant = ["schemas/s4_contract.json", "server/s4.py", "server/auth.py", "server/wechat.py", "server/media.py", "server/api.py",
        "src/sync/worker.py", "src/sync/bridge.py", "src/database/canonical.py", "src/database/db.py", "src/database/schema.sql",
        "src/sensor/alarm.py", "src/tools/database_tool.py", "tests/test_s4_contract.py", "tests/test_auth.py", "tests/test_media_access.py",
        "tests/test_c4_s4_flow.py", "tests/test_c4_worker.py", "tests/test_c4_server.py", "tests/test_c4_worker_entry.py",
        "tests/test_c4_timestamp_normalize.py", "docs/B线接管进度_20261103.md", "docs/C4_S4联调缺口与修复方案_20261103.md",
        "docs/V4.4_B线第二阶段实施计划.md", "docs/V4.4_B线完整功能审计报告.md", "docs/V4.4_实现设计基线.md"]
    (OUT / "B-reference-files.json").write_text(json.dumps([
        {"path": p, "sha256": digest((b / p).read_bytes()), "tracked": bool(git(b, "ls-files", "--", p).strip())}
        for p in relevant], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"A tracked mini program files: {len(rows)}; matches HEAD: {all(r['matches_HEAD_ignoring_CRLF'] for r in rows)}")


def final():
    before = json.loads((OUT / "repositories-before.json").read_text(encoding="utf-8"))
    after = snapshot()
    (OUT / "repositories-after.json").write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
    unchanged = {label: before[label] == after[label] for label in REPOS}
    (OUT / "repository-preservation.json").write_text(json.dumps(unchanged, indent=2), encoding="utf-8")
    print(json.dumps(unchanged))
    if not all(unchanged.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    final() if "--final" in sys.argv else initial()
