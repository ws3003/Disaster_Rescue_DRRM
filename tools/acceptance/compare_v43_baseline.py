"""Read-only V4.3/B five-image comparison. Run outside the repository.

Uses the current local model in both runs. Does not reconstruct historical weights.
Copies source/input snapshots to a NEW result directory. No Git writes, downloads,
camera access, dependency installation, notification, or source/config changes.
"""
import argparse
from datetime import datetime
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile

FROZEN = "5a10605c280d8c4d7af85aa186425aa0b8eab514"
BRANCH = "feature/v4.4-b-backend"
SOURCES = ("src/detector.py", "src/pose.py", "src/state.py", "tests/evaluate_states.py")
CONFIG = "config/config.yaml"
LABELS = "tests/test_labels.json"
BOOTSTRAP = (
    "import random,runpy,sys; import numpy as np; import torch; "
    "random.seed(0); np.random.seed(0); torch.manual_seed(0); "
    "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
)


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(data, text=False):
    if text:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
    )


def checked_labels(data):
    labels = json.loads(data)["images"]
    if not labels:
        raise ValueError("No labelled cases")
    names = set()
    for item in labels:
        name = item["file"]
        if (not isinstance(name, str) or Path(name).name != name or name in (".", "..")
                or "/" in name or "\\" in name or name in names):
            raise ValueError(f"Unsafe or duplicate image filename: {name!r}")
        if item["gt_state"] not in ("Standing", "Sitting", "Lying", "Fall", "Help-Waving"):
            raise ValueError(f"Unknown ground-truth label: {item['gt_state']}")
        names.add(name)
    return labels


def prepare_snapshots(repo, out, read_blob):
    frozen_labels = read_blob(LABELS)
    current_labels = (repo / LABELS).read_bytes()
    labels = checked_labels(frozen_labels)
    if sha(frozen_labels, text=True) != sha(current_labels, text=True):
        raise ValueError("Current and frozen labels differ; review before comparing")
    paths = list(SOURCES) + [CONFIG, LABELS]
    paths += ["data/test_images/" + item["file"] for item in labels]
    fingerprints = {}
    for relative in paths:
        before = read_blob(relative)
        current = (repo / relative).read_bytes()
        is_text = relative.endswith((".py", ".yaml", ".json"))
        fingerprints[relative] = {
            "frozen_sha256": sha(before), "current_sha256": sha(current),
            "equal_ignoring_crlf" if is_text else "equal": sha(before, is_text) == sha(current, is_text),
        }
        if relative.startswith("data/") and before != current:
            raise ValueError(f"Image differs from frozen input: {relative}; review before comparing")
        for side, data in (("frozen", before), ("current", current)):
            dest = out / side / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    shared = out / "shared"
    shared.mkdir()
    (shared / "labels.json").write_bytes(frozen_labels)
    # Both processes read exactly the same immutable copy of the images.
    for item in labels:
        dest = shared / "images" / item["file"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((out / "frozen" / "data/test_images" / item["file"]).read_bytes())
    return labels, fingerprints


def parse_report(text, labels):
    expected = {item["file"]: item["gt_state"] for item in labels}
    rows = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(\S+\.jpg)\s+(\S+)\s+(\S+)\s+[✓✗](?:\s|$)", line)
        if not match:
            continue
        name, ground_truth, predicted = match.groups()
        if name not in expected or expected[name] != ground_truth or name in rows:
            raise ValueError(f"Unexpected, changed or duplicate report row: {name}")
        rows[name] = {"file": name, "expected": ground_truth, "predicted": predicted}
    if set(rows) != set(expected):
        raise ValueError("Incomplete/unrecognised evaluation report; inspect raw log")
    return [rows[item["file"]] for item in labels]


def run_evaluation(source, shared, config, log_path, labels, timeout):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONHASHSEED="0",
               PYTHONDONTWRITEBYTECODE="1", GIT_OPTIONAL_LOCKS="0",
               YOLO_AUTOINSTALL="false")
    # No PYTHONPATH inherited from another checkout may select the other source tree.
    env.pop("PYTHONPATH", None)
    command = [sys.executable, "-B", "-c", BOOTSTRAP,
               str(source / "tests/evaluate_states.py"),
               "--config", str(config), "--images", str(shared / "images"),
               "--labels", str(shared / "labels.json")]
    with log_path.open("wb") as log:
        completed = subprocess.run(command, cwd=source, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, timeout=timeout)
    if completed.returncode not in (0, 1):
        raise RuntimeError(f"Evaluation crashed, exit={completed.returncode}; see {log_path}")
    rows = parse_report(log_path.read_text(encoding="utf-8", errors="replace"), labels)
    correct = sum(row["expected"] == row["predicted"] for row in rows)
    if completed.returncode != (0 if correct == len(rows) else 1):
        raise RuntimeError(f"Exit status disagrees with report; see {log_path}")
    return {"returncode": completed.returncode, "correct": correct,
            "total": len(rows), "rows": rows, "log": str(log_path)}


def compare_results(frozen, current):
    if not frozen or len(frozen) != len(current):
        raise ValueError("Need equal nonempty run counts")
    stable = (all(run["rows"] == frozen[0]["rows"] for run in frozen)
              and all(run["rows"] == current[0]["rows"] for run in current))
    equal = all(a["rows"] == b["rows"] for a, b in zip(frozen, current))
    all_correct = all(run["correct"] == run["total"] for run in frozen + current)
    if not stable:
        verdict, exit_code = "UNSTABLE_RESULTS_REVIEW_REQUIRED", 2
    elif not equal:
        verdict, exit_code = "DIFFERENCE_FOUND_NEEDS_CAUSE_ANALYSIS", 2
    elif not all_correct:
        verdict, exit_code = "SAME_FAILURES_WITH_FROZEN_SOURCE_IN_CURRENT_ENV", 1
    else:
        verdict, exit_code = "FIVE_IMAGE_COMPARISON_PASSED", 0
    return {"stable_across_repeats": stable, "paired_predictions_equal": equal,
            "all_five_cases_correct": all_correct, "verdict": verdict,
            "exit_code": exit_code}


def perform(args, out):
    repo = args.repo.expanduser().resolve()
    actual_root = Path(git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if actual_root != repo:
        raise ValueError("--repo must name the repository root")
    branch = git(repo, "branch", "--show-current").decode().strip()
    if branch != BRANCH:
        raise ValueError(f"Unexpected branch: {branch}; stopped without switching branches")
    git(repo, "merge-base", "--is-ancestor", FROZEN, "HEAD")
    labels, fingerprints = prepare_snapshots(
        repo, out, lambda relative: git(repo, "show", FROZEN + ":" + relative))
    manifest = {
        "device_time": datetime.now().astimezone().isoformat(), "repo": str(repo),
        "branch": branch, "head": git(repo, "rev-parse", "HEAD").decode().strip(),
        "status_before": git(repo, "status", "--short", "--untracked-files=normal").decode(),
        "frozen_commit": FROZEN, "python": sys.version, "executable": sys.executable,
        "platform": platform.platform(), "fingerprints": fingerprints,
        "model": None, "historical_model_identity_verified": False,
        "scope": "five labelled still images; not full V4.3 or nine-link acceptance",
    }
    manifest["package_versions"] = {}
    for name in ("torch", "ultralytics", "mediapipe", "numpy", "opencv-python", "PyYAML"):
        try:
            manifest["package_versions"][name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            manifest["package_versions"][name] = None
    save_json(out / "manifest.json", manifest)
    identical = all(item.get("equal_ignoring_crlf", item.get("equal"))
                    for item in fingerprints.values())
    print("Source/config/label/image fingerprints identical:", identical, flush=True)
    if args.audit_only:
        print("AUDIT ONLY: no model loaded, no inference performed.", flush=True)
        return 0

    import yaml
    config = yaml.safe_load((out / "current" / CONFIG).read_text(encoding="utf-8"))
    model = (repo / config["model"]["path"]).resolve()
    if not model.is_file():
        raise FileNotFoundError(f"Existing model not found: {model}; no download attempted")
    model_hash = file_sha(model)
    manifest["model"] = {"path": str(model), "sha256": model_hash, "bytes": model.stat().st_size}
    config["model"]["path"] = str(model)
    common_config = out / "shared" / "comparison.yaml"
    common_config.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    manifest["shared_config_sha256"] = file_sha(common_config)
    save_json(out / "manifest.json", manifest)
    results = {"frozen": [], "current": []}
    # Separate fresh processes for each side/repeat, same executable/config/seed/weights.
    for index in range(args.repeats):
        for side in ("frozen", "current"):
            log = out / f"{side}_{index + 1}.log"
            print(f"Running {side}, repeat {index + 1}/{args.repeats} ...", flush=True)
            result = run_evaluation(out / side, out / "shared", common_config,
                                    log, labels, args.timeout)
            results[side].append(result)
            save_json(out / "runs.json", results)
            print(f"  {result['correct']}/{result['total']} correct; log={log}", flush=True)
    if file_sha(model) != model_hash:
        raise RuntimeError("Model changed during comparison; results cannot be accepted")
    for relative, item in fingerprints.items():
        if file_sha(repo / relative) != item["current_sha256"]:
            raise RuntimeError(f"Source/input changed during comparison: {relative}")
    summary = compare_results(results["frozen"], results["current"])
    summary["source_config_inputs_identical"] = identical
    summary["historical_model_identity_verified"] = False
    summary["note"] = "Both use today's existing model/environment; historical model weights are unavailable in this comparison."
    summary["status_after"] = git(repo, "status", "--short", "--untracked-files=normal").decode()
    save_json(out / "summary.json", summary)
    print("\nIMAGE | LABEL | FROZEN | CURRENT", flush=True)
    for a, b in zip(results["frozen"][0]["rows"], results["current"][0]["rows"]):
        print(f"{a['file']} | {a['expected']} | {a['predicted']} | {b['predicted']}", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print("Any wrong labelled case remains FAILED, even when both sides agree.", flush=True)
    return summary["exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output-parent", type=Path, default=Path.home())
    args = parser.parse_args()
    if args.timeout < 10:
        parser.error("--timeout must be at least 10 seconds per run")
    parent = args.output_parent.expanduser().resolve()
    if not parent.is_dir():
        parser.error("--output-parent must be an existing directory")
    if parent.is_relative_to(args.repo.expanduser().resolve()):
        parser.error("Result directory must be outside the repository")
    out = Path(tempfile.mkdtemp(prefix="b_v43_compare_", dir=str(parent)))
    print("RESULT DIRECTORY:", out, flush=True)
    try:
        result = perform(args, out)
    except Exception as exc:
        save_json(out / "error.json", {"error": str(exc), "type": type(exc).__name__})
        print("COMPARISON STOPPED:", exc, file=sys.stderr)
        result = 2
    print("RESULT DIRECTORY:", out, flush=True)
    return result


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(main())
