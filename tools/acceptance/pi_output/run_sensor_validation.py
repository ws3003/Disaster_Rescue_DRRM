"""Run the installed E7/E8 helper; write new artifacts under ~/testtest/results.

Original repository helper and existing results/backups are never modified.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

EXPECTED_HELPER_SHA256 = "9cac239680e22b196320f5060d70603d40503d4643f8c127d0daeaaed427a4af"


def load_original(repo):
    source = repo / "tools/acceptance/verify_sensor_recording.py"
    if not source.is_file():
        raise RuntimeError("Installed verification helper is missing")
    content = source.read_bytes()
    if hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest() != EXPECTED_HELPER_SHA256:
        raise RuntimeError("Installed helper differs from the reviewed version; review before running")
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location("installed_e7e8_verification", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--scenario", choices=("level1", "level2"), required=True)
    parser.add_argument("--duration", type=int, default=25)
    parser.add_argument("--output-root", type=Path, default=Path.home() / "testtest")
    args = parser.parse_args()
    args.repo = args.repo.expanduser().resolve()
    root = args.output_root.expanduser().resolve()
    parent = (root / "results").resolve()
    if not 20 <= args.duration <= 60:
        parser.error("duration must be 20..60 seconds")
    if root.is_relative_to(args.repo) or parent.is_relative_to(args.repo):
        parser.error("Output must remain outside the project")
    if not parent.is_relative_to(root):
        parser.error("Results path escaped requested output root")
    helper = load_original(args.repo)
    parent.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="b_sensor_video_TEST_", dir=str(parent)))
    result = {"test_only": True, "scenario": args.scenario,
              "upload_tested": False, "sync_tested": False,
              "output_root": str(root), "helper_sha256_lf": EXPECTED_HELPER_SHA256}
    print("TEST RESULT DIRECTORY:", out, flush=True)
    try:
        helper.perform(args, out, result)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    # Same acceptance rules: do not turn previous reader faults into a pass.
    result["failures"] = helper.assess(result)
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Failures:", result["failures"], flush=True)
    print("TEST RESULT DIRECTORY:", out, flush=True)
    print("No OSS upload or cross-device synchronization was tested.", flush=True)
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("STOPPED:", exc, file=sys.stderr)
        raise SystemExit(1)
