"""Real DS18B20 -> test-threshold alert -> shared camera recording -> TEST DB.

No notifications, upload, sync worker or production data/config changes.
This verifies local hardware wiring, not an actual disaster/high-temperature event.
"""
import argparse
from contextlib import closing
import copy
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time


def make_profile(original, baseline, scenario, out):
    if scenario not in ("level1", "level2"):
        raise ValueError("Unknown scenario")
    if isinstance(baseline, bool) or not isinstance(baseline, (int, float)) or not math.isfinite(baseline):
        raise ValueError("Invalid real baseline temperature")
    if not 5 < baseline < 80:
        raise ValueError("Baseline outside this test profile's supported range; review thresholds manually")
    sensor = copy.deepcopy(original["sensor"])
    if sensor.get("reader") != "ds18b20":
        raise ValueError("This tool requires the configured real DS18B20; no Fake fallback")
    sensor.update({
        "sensor_id": "acceptance-" + str(sensor["sensor_id"]), "enabled": True,
        "temperature_threshold": round(baseline - 2, 3),
        "level2_threshold": round(baseline + 2 if scenario == "level1" else baseline - 1, 3),
        "recovery_threshold": round(baseline - 3, 3), "sample_period_seconds": 1.0,
    })
    e8 = copy.deepcopy(original.get("e8", {}))
    e8.update({"enabled": True, "record_seconds": 10, "tmp_dir": str(out / "media")})
    return {"sensor": sensor, "e8": e8,
            "acceptance": {"test_only": True, "scenario": scenario,
                           "real_baseline_c": baseline, "thresholds_are_test_values": True,
                           "upload_tested": False, "sync_tested": False}}


class ObservedReader:
    """Log actual readings/exceptions without replacing values or timestamps."""
    def __init__(self, inner, log_path):
        self.inner = inner
        self.log_path = log_path
        self.count = 0
        self.errors = []
        self.lock = threading.Lock()

    def read(self, now=None):
        try:
            reading = self.inner.read(now=now)
        except Exception as exc:
            with self.lock:
                self.errors.append(str(exc))
            raise
        with self.lock:
            self.count += 1
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(asdict(reading), ensure_ascii=False) + "\n")
        return reading

    def close(self):
        self.inner.close()


class ObservedRecorder:
    """Measure real CameraRecorder calls and concurrent sensor sampling."""
    def __init__(self, inner, reader):
        self.inner = inner
        self.reader = reader
        self.calls = []

    def record(self, seconds, dest_path):
        entry = {"requested_seconds": seconds, "path": str(dest_path),
                 "samples_before": self.reader.count, "completed": False}
        self.calls.append(entry)
        started = time.monotonic()
        try:
            path = self.inner.record(seconds, dest_path)
            entry["completed"] = True
            return path
        finally:
            entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
            entry["samples_during_recording"] = self.reader.count - entry["samples_before"]


def inspect_test_db(path):
    # Read-only inspection; explicit close avoids adding another leaked connection.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return {
            "readings": [dict(row) for row in conn.execute("SELECT * FROM readings")],
            "alerts": [dict(row) for row in conn.execute("SELECT * FROM sensor_alerts")],
            "media": [dict(row) for row in conn.execute("SELECT * FROM media_evidence")],
            "outbox_count": conn.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0],
            "verify_count": conn.execute("SELECT COUNT(*) FROM c4_verify").fetchone()[0],
        }


def assess(result):
    failures = []
    if result.get("test_only") is not True:
        failures.append("missing_test_only_label")
    if result.get("error"):
        failures.append("runtime_error")
    if result.get("camera_fault"):
        failures.append("camera_fault")
    db = result.get("database", {})
    readings, alerts, media = (db.get(key, []) for key in ("readings", "alerts", "media"))
    if len(readings) < result.get("confirm_high_count", 3):
        failures.append("not_enough_persisted_real_readings")
    if len(alerts) != 1 or alerts[0].get("status") != "active":
        failures.append("expected_one_active_test_alert")
    else:
        sensor_profile = result.get("test_profile", {}).get("sensor", {})
        key = "temperature_threshold" if result["scenario"] == "level1" else "level2_threshold"
        try:
            if float(alerts[0]["threshold"]) != float(sensor_profile[key]):
                failures.append("unexpected_alert_threshold")
        except (KeyError, TypeError, ValueError):
            failures.append("missing_alert_threshold_evidence")
    if not result.get("e7e8_closed") or result.get("camera_closed_state") != "CLOSED":
        failures.append("lifecycle_not_closed")
    if result.get("reader_errors") or result.get("runner_dropped"):
        failures.append("reader_or_runner_error")
    progress = result.get("sample_progress", [])
    if len(progress) < 4 or progress[-1]["samples"] <= progress[-4]["samples"]:
        failures.append("sampling_not_observed_near_test_end")
    if db.get("verify_count", 0) != 0 or db.get("outbox_count", 0) < len(readings) + len(alerts):
        failures.append("local_outbox_not_complete")
    calls = result.get("record_calls", [])
    if result["scenario"] == "level1":
        if media or calls:
            failures.append("level1_unexpectedly_recorded")
    else:
        if len(calls) != 1 or not calls[0].get("completed"):
            failures.append("expected_one_completed_real_recording")
        elif calls[0].get("samples_during_recording", 0) < 2:
            failures.append("sampling_not_observed_during_recording")
        if (len(media) != 1 or media[0].get("capture_result") != "ok"
                or len(alerts) != 1 or media[0].get("ref_id") != alerts[0].get("alert_id")
                or media[0].get("source") != "sensor" or media[0].get("media_type") != "video"):
            failures.append("media_not_linked_to_actual_alert")
        duration = result.get("video_duration_seconds")
        if duration is None or not 9 <= duration <= 11:
            failures.append("video_duration_not_approximately_10_seconds")
    if any(row.get("upload_status") == "success" or row.get("uploaded_at") for row in media):
        failures.append("unexpected_upload_success_without_uploader")
    if result.get("status_after") != result.get("status_before"):
        failures.append("worktree_changed_during_test")
    return failures


def perform(args, out, result):
    import yaml
    import cv2
    repo = args.repo.expanduser().resolve()
    sys.path.insert(0, str(repo))
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    def git(*items):
        return subprocess.check_output(["git", "-C", str(repo), *items], env=env).decode().strip()
    if git("branch", "--show-current") != "feature/v4.4-b-backend":
        raise ValueError("Not on the B branch; stopped without switching branches")
    result.update({"head": git("rev-parse", "HEAD"), "status_before": git("status", "--short"),
                   "alarm_source_sha256": hashlib.sha256((repo / "src/sensor/alarm.py").read_bytes()).hexdigest()})
    from src.sensor.ds18b20_reader import DS18B20Reader
    from src.sensor.config import load_sensor_config
    from src.camera_service import CameraService
    from src.database.db import Database
    from src.e7e8_integration import EdgeE7E8, build_shared_recorder
    from scripts.e7e8_runner import load_e8_config

    original = yaml.safe_load((repo / "config/edge.yaml").read_text(encoding="utf-8"))
    sensor = original["sensor"]
    if sensor.get("reader") != "ds18b20":
        raise ValueError("Configured sensor is not real DS18B20")
    reader_args = {"device_id": sensor.get("device_id")}
    if sensor.get("device_dir"):
        reader_args["device_dir"] = sensor["device_dir"]
    initial = DS18B20Reader(**reader_args)
    try:
        baseline = [initial.read() for _ in range(3)]
    finally:
        initial.close()
    result["baseline_readings"] = [asdict(value) for value in baseline]
    profile = make_profile(original, statistics.median(value.temperature_c for value in baseline),
                           args.scenario, out)
    config_path = out / "TEST_ONLY.yaml"
    config_path.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
    cfg, e8_cfg = load_sensor_config(config_path), load_e8_config(config_path)
    result.update({"confirm_high_count": cfg.confirm_high_count,
                   "test_profile": profile, "scope": "real readings with TEST thresholds; local chain only"})
    db_path = out / "TEST_ONLY_DO_NOT_SYNC.db"
    db = Database(db_path=str(db_path))
    reader = ObservedReader(DS18B20Reader(**reader_args), out / "real_readings.jsonl")
    service = CameraService(width=e8_cfg.width, height=e8_cfg.height, fps=e8_cfg.fps)
    recording = ObservedRecorder(build_shared_recorder(service, e8_cfg), reader)
    edge = None
    try:
        service.start()
        if service.state != "RUNNING":
            raise RuntimeError(service.fault())
        edge = EdgeE7E8(cfg, e8_cfg, db, camera_service=service, uploader=None,
                       reader=reader, recorder=recording)
        edge.start()
        started = time.monotonic()
        result["sample_progress"] = []
        while time.monotonic() - started < args.duration:
            time.sleep(2)
            result["sample_progress"].append({"seconds": round(time.monotonic() - started, 3),
                                              "samples": reader.count})
            print(f"Real samples={reader.count}; real recording calls={len(recording.calls)}", flush=True)
        result["camera_fault"] = service.fault()
    finally:
        closed = edge.close(timeout=15) if edge is not None else True
        result["e7e8_closed"] = closed
        if edge is None:
            reader.close()
        if closed:
            service.close(timeout=5)
        else:
            result["error"] = "E7/E8 still active; camera was not closed underneath its consumers"
        result.update({"camera_closed_state": service.state,
                       "reader_errors": list(reader.errors), "record_calls": recording.calls,
                       "runner_dropped": edge.runner.dropped if edge and edge.runner else []})
        if closed:
            result["database"] = inspect_test_db(db_path)

    if not result["e7e8_closed"]:
        return
    media = result["database"]["media"]
    if len(media) == 1 and media[0].get("local_path"):
        path = Path(media[0]["local_path"])
        if not path.resolve().is_relative_to(out.resolve()):
            raise RuntimeError("Media path escaped the isolated output directory")
        cap = cv2.VideoCapture(str(path))
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
            count = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                count += 1
            result.update({"video_decoded_frames": count, "video_fps": fps,
                           "video_duration_seconds": round(count / fps, 3) if fps > 0 else None})
        finally:
            cap.release()
    result["status_after"] = git("status", "--short")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--scenario", choices=("level1", "level2"), default="level2")
    parser.add_argument("--duration", type=int, default=25)
    args = parser.parse_args()
    if not 20 <= args.duration <= 60:
        parser.error("--duration must be between 20 and 60 seconds")
    if Path.home().resolve().is_relative_to(args.repo.expanduser().resolve()):
        parser.error("Test results must be outside the repository")
    out = Path(tempfile.mkdtemp(prefix="b_sensor_video_TEST_", dir=str(Path.home())))
    result = {"test_only": True, "scenario": args.scenario,
              "upload_tested": False, "sync_tested": False}
    print("TEST RESULT DIRECTORY:", out, flush=True)
    try:
        perform(args, out, result)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["failures"] = assess(result)
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Failures:", result["failures"], flush=True)
    print("TEST RESULT DIRECTORY:", out, flush=True)
    print("No OSS upload or cross-device synchronization was tested.", flush=True)
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(main())
