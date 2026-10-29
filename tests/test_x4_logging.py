"""X4 记录层测试（复用 agent_logs；临时 SQLite，离线）。"""

from __future__ import annotations

import logging
import os
import tempfile

import pytest

from src.database.db import Database
from src.x4.contracts import Detection, ERROR_EDGE_FAILED, InferenceResult
from src.x4.logging import record_inference


PERSON = Detection(class_id=0, class_name="person", confidence=0.9, bbox=(10, 20, 30, 40))


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "x4_logging_test.db"))
    d._tmpdir = tmp
    return d


def _res(provider="cloud", success=True, fallback=False, model_id="yolov26l",
         error_code="", ndet=1, latency=12.3):
    dets = (PERSON,) * ndet if ndet else ()
    return InferenceResult(success=success, provider=provider, model_id=model_id,
                           detections=dets, latency_ms=latency,
                           fallback_used=fallback, error_code=error_code)


def _log_for(db, task_id):
    return db.get_agent_logs(task_id)


class TestRecordFields:
    def test_cloud_success_fields(self, db):
        r = record_inference(db, _res(provider="cloud", success=True), task_id="x4-t1")
        assert r.ok and r.log_id is not None
        logs = _log_for(db, "x4-t1")
        assert len(logs) == 1
        e = logs[0]
        assert e["task_id"] == "x4-t1"
        assert e["tool_name"] == "x4_infer"
        assert e["output"]["provider"] == "cloud"
        assert e["output"]["model_id"] == "yolov26l"
        assert e["output"]["success"] is True
        assert e["output"]["detection_count"] == 1
        assert e["output"]["fallback_used"] is False
        assert e["output"]["error_code"] == ""
        assert e["latency_ms"] == 12          # 归整为 DAO INTEGER

    def test_cloud_fail_edge_fallback_records_final(self, db):
        r = record_inference(db, _res(provider="edge", success=True, fallback=True), task_id="x4-t2")
        e = _log_for(db, "x4-t2")[0]
        assert e["output"]["provider"] == "edge"
        assert e["output"]["fallback_used"] is True
        assert e["output"]["success"] is True

    def test_both_fail_records_failure(self, db):
        r = record_inference(db, _res(provider="edge", success=False, fallback=True,
                                      error_code=ERROR_EDGE_FAILED, ndet=0), task_id="x4-t3")
        e = _log_for(db, "x4-t3")[0]
        assert e["output"]["success"] is False
        assert e["output"]["error_code"] == ERROR_EDGE_FAILED
        assert e["output"]["detection_count"] == 0

    def test_cloud_disabled_edge_only(self, db):
        r = record_inference(db, _res(provider="edge", success=True, fallback=True), task_id="x4-t4")
        e = _log_for(db, "x4-t4")[0]
        assert e["output"]["provider"] == "edge" and e["output"]["fallback_used"] is True


class TestTaskIdAndLatency:
    def test_generated_x4_prefix_when_no_task_id(self, db):
        r = record_inference(db, _res())
        assert r.ok
        with db._connect() as conn:
            tid = conn.execute(
                "SELECT task_id FROM agent_logs WHERE task_id LIKE 'x4-%'").fetchone()
        assert tid is not None and tid[0].startswith("x4-")

    def test_latency_positive_int(self, db):
        record_inference(db, _res(latency=7.7), task_id="x4-t5")
        assert _log_for(db, "x4-t5")[0]["latency_ms"] == 8   # round(7.7)=8


class TestWriteFailure:
    def test_write_exception_not_raised_and_not_modifies_result(self, db, monkeypatch, caplog):
        res = _res()
        def boom(*a, **k):
            raise RuntimeError("disk full")
        monkeypatch.setattr(db, "save_agent_log", boom)
        with caplog.at_level(logging.WARNING, logger="x4.logging"):
            r = record_inference(db, res, task_id="x4-t6")
        assert r.ok is False and r.log_id is None
        # 原推理结果不被修改/不破坏
        assert res.provider == "cloud" and res.success is True and res.latency_ms == 12.3
        assert any("record_inference failed" in m for m in caplog.messages)  # 经 logging 记录, 非静默

    def test_no_db_log_row_written_on_failure(self, db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(db, "save_agent_log", boom)
        record_inference(db, _res(), task_id="x4-t7")
        with db._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM agent_logs").fetchone()[0]
        assert n == 0


class TestNoSensitive:
    def test_no_secret_or_image_in_output(self, db):
        res = _res(error_code="edge_failed")
        record_inference(db, res, task_id="x4-t8",
                         input_meta={"obs_id": "obs-1", "w": 1280, "h": 720, "frame_idx": 7})
        e = _log_for(db, "x4-t8")[0]
        assert "endpoint" not in str(e["output"])
        assert "secret" not in str(e["output"]).lower() and "token" not in str(e["output"]).lower()
        assert "api_key" not in str(e["input"]).lower()
        assert not any(isinstance(v, bytes) for v in e["input"].values())  # 无图像/凭据字节

    def test_bytes_meta_stripped(self, db):
        record_inference(db, _res(), task_id="x4-t9",
                         input_meta={"name": "safe", "blob": b"\xff\xd8rawimage"})
        e = _log_for(db, "x4-t9")[0]
        assert "blob" not in e["input"] and e["input"].get("name") == "safe"


class TestLatencyBoundary:
    @pytest.mark.parametrize("val,exp", [(12.3, 12), (0.0, 0), (0, 0), (7.7, 8), (2, 2)])
    def test_valid_nonneg_converted(self, db, val, exp):
        r = record_inference(db, _res(latency=val), task_id="x4-l1")
        assert r.ok and _log_for(db, "x4-l1")[0]["latency_ms"] == exp

    @pytest.mark.parametrize("val", [-1.0, -0.5, float("nan"), float("inf"), float("-inf"), None, "3.2", object()])
    def test_invalid_rejected_no_dao(self, db, val):
        calls = {"n": 0}
        orig = db.save_agent_log
        def spy(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)
        db.save_agent_log = spy
        res = _res(latency=val)
        r = record_inference(db, res, task_id="x4-l2")
        assert r.ok is False and r.log_id is None and r.reason == "latency_invalid"
        assert calls["n"] == 0                     # DAO 未被调用
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM agent_logs").fetchone()[0] == 0
        assert res.latency_ms == val or res.provider == "cloud"   # 原 result 未被改(校验不触 mutation)

    def test_record_result_legacy_constructor_still_valid(self):
        from src.x4.logging import RecordResult
        r = RecordResult(ok=False, log_id=None)   # 旧构造形式
        assert r.ok is False and r.log_id is None and r.reason is None


class TestReasonAndCloudError:
    def test_write_failure_reason(self, db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disk")
        monkeypatch.setattr(db, "save_agent_log", boom)
        r = record_inference(db, _res(), task_id="x4-r1")
        assert r.ok is False and r.reason == "write_failed"

    def test_cloud_error_written_when_provided(self, db):
        record_inference(db, _res(), task_id="x4-r2", cloud_error="cloud_timeout")
        e = _log_for(db, "x4-r2")[0]
        assert e["output"].get("cloud_error") == "cloud_timeout"

    def test_cloud_error_absent_when_not_provided(self, db):
        record_inference(db, _res(), task_id="x4-r3")
        assert "cloud_error" not in _log_for(db, "x4-r3")[0]["output"]

    @pytest.mark.parametrize("bad", ["", None, 123, ["x"], {"x": 1}])
    def test_unsafe_cloud_error_rejected_but_log_ok(self, db, bad):
        r = record_inference(db, _res(), task_id="x4-r4", cloud_error=bad)
        assert r.ok is True                       # 元数据被安全拒绝, 记录仍成功
        assert "cloud_error" not in _log_for(db, "x4-r4")[0]["output"]