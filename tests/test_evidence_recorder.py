"""E8/X5 视频取证 Recorder 测试（FakeRecorder / FakeUploader / 临时库，Windows 离线）。

覆盖任务 §10 E8/X5 必测 1-14，及失败场景 C/D1/D2。不连真实硬件/OSS，不触 data/drrm.db。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.database.db import Database
from src.evidence.recorder import (EvidenceRecorder, FakeRecorder,
                                   MEDIA_SOURCE, MEDIA_TYPE)


class OkUploader:
    def __init__(self):
        self.calls = []

    def upload(self, path, prefix="evidence"):
        # 忠实反映真实 storage 返回: 成功 = {ok: True, url, provider}（无 object_key）
        self.calls.append((path, prefix))
        return {"ok": True, "url": f"http://x/{prefix}/{os.path.basename(path)}",
                "provider": "aliyun_oss"}


class FailUploader:
    def upload(self, path, prefix="evidence"):
        return {"ok": False, "error": "upload server boom"}


class NoOkUploader:
    def __init__(self, ok_value):
        self.ok_value = ok_value

    def upload(self, path, prefix="evidence"):
        d = {"url": "http://x/v", "provider": "aliyun_oss"}
        if self.ok_value is not _MISSING:
            d["ok"] = self.ok_value
        return d


class RaisesUploader:
    def upload(self, path, prefix="evidence"):
        raise RuntimeError("connection reset")


class _MISSING:
    pass


class BoomRecorder:
    def record(self, seconds, dest_path):
        raise RuntimeError("camera busy")


class CountingRecorder(FakeRecorder):
    def __init__(self):
        super().__init__()
        self.record_count = 0

    def record(self, seconds, dest_path):
        self.record_count += 1
        return super().record(seconds, dest_path)


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "evidence_test.db"))
    d._tmpdir = tmp
    return d


def _new_alert(db):
    return db.save_sensor_alert(type="sensor_hightemp", temperature=52.0, threshold=50.0,
                                device_captured_at="2026-10-28T10:00:00+08:00")


def _mk(db, tmp_path, **kw):
    kw.setdefault("tmp_dir", str(tmp_path))
    return EvidenceRecorder(db, **kw)


def _first_media(db):
    rows = db.list_media_evidence_by_ref("sensor", _alert_of(db))
    return rows[0] if rows else None


def _alert_of(db):
    return db.query_sensor_alerts(limit=1)[0]["alert_id"]


class TestTrigger:
    def test_level1_not_triggered(self, db, tmp_path):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=OkUploader())
        out = r.handle_event({"type": "alert_created", "level": 1, "alert_id": rid})
        assert out == {"handled": False, "reason": "not_level2"}
        assert len(db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)) == 0

    def test_level2_upgraded_triggers(self, db, tmp_path):
        rid = _new_alert(db)
        up = OkUploader()
        r = _mk(db, tmp_path, uploader=up)
        out = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2,
                              "device_captured_at": "2026-10-28T10:00:10+08:00"})
        assert out["ok"] is True and out["ref_id"] == rid
        rows = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)
        assert len(rows) == 1
        m = rows[0]
        assert m["source"] == MEDIA_SOURCE and m["media_type"] == MEDIA_TYPE and m["ref_id"] == rid
        assert m["status"] == "success" and m["upload_status"] == "success"
        assert m["capture_result"] == "ok" and m["retry_count"] == 0
        assert len(up.calls) == 1 and up.calls[0][1] == "evidence"

    def test_file_valid_and_associated(self, db, tmp_path):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=OkUploader())
        r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        m = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m["ref_id"] == rid
        assert m["local_path"] and os.path.exists(m["local_path"])
        assert os.path.getsize(m["local_path"]) > 0  # 非空视频文件
        # 确定性对象键: 本地文件名与 media_id 一致, object_key = evidence/{media_id}.mp4
        assert os.path.basename(m["local_path"]) == f"{m['media_id']}.mp4"
        assert m["object_key"] == f"evidence/{m['media_id']}.mp4"

    def test_deterministic_object_key(self, db, tmp_path):
        # 同一媒体在重试前后对象键不变（上传失败→恢复成功后核对）
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=FailUploader(), max_retries=3)
        r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        m1 = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m1["status"] == "failed-awaiting-retry"
        okr = _mk(db, tmp_path, uploader=OkUploader(), max_retries=3)
        okr.recover()
        m2 = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m2["media_id"] == m1["media_id"]
        assert m2["status"] == "success"
        assert m2["object_key"] == f"evidence/{m1['media_id']}.mp4"  # 稳定、与媒体绑定

    def test_recover_does_not_rerecord(self, db, tmp_path):
        # 上传失败后恢复：已成功录像的证据不重复录制（record_count 保持 1）
        rid = _new_alert(db)
        rec = CountingRecorder()
        r = _mk(db, tmp_path, recorder=rec, uploader=FailUploader(), max_retries=3)
        r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert rec.record_count == 1
        assert db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]["status"] == "failed-awaiting-retry"
        okr = _mk(db, tmp_path, recorder=rec, uploader=OkUploader(), max_retries=3)
        okr.recover()
        assert rec.record_count == 1  # 恢复阶段不再重新录像, 仅重传
        assert db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]["status"] == "success"

    def test_duplicate_event_no_rebuild(self, db, tmp_path):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=OkUploader())
        r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        out2 = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert out2["handled"] is False and out2["reason"] == "already_exists"
        assert len(db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)) == 1

    def test_two_distinct_alerts_two_rows(self, db, tmp_path):
        r1, r2 = _new_alert(db), _new_alert(db)
        r = _mk(db, tmp_path, uploader=OkUploader())
        r.handle_event({"type": "alert_upgraded", "alert_id": r1, "level": 2})
        r.handle_event({"type": "alert_upgraded", "alert_id": r2, "level": 2})
        assert len(db.list_media_evidence_by_ref(MEDIA_SOURCE, r1)) == 1
        assert len(db.list_media_evidence_by_ref(MEDIA_SOURCE, r2)) == 1


class TestFailures:
    def test_record_failure_keeps_alert_no_fake(self, db, tmp_path):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, recorder=BoomRecorder(), uploader=OkUploader())
        out = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert out["handled"] is False and out["error"] == "record_failed" and out["alert_kept"] is True
        assert len(db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)) == 0  # 不伪造成功记录
        assert db.query_sensor_alerts(limit=1)[0]["status"] == "active"    # 原告警仍在

    def test_register_failure_keeps_file(self, db, tmp_path, monkeypatch):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=OkUploader())

        def _boom(*a, **k):
            raise RuntimeError("register db down")

        monkeypatch.setattr(db, "save_media_evidence", _boom)
        out = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert out["error"] == "evidence_register_failed" and out["local_path_kept"]
        assert os.path.exists(out["local_path_kept"]) and os.path.getsize(out["local_path_kept"]) > 0

    def test_upload_failure_keeps_record(self, db, tmp_path):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=FailUploader(), max_retries=3)
        out = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert out["ok"] is False and out["error"] == "upload_failed"
        m = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m["status"] == "failed-awaiting-retry"
        assert m["upload_status"] == "failed"
        assert m["retry_count"] == 1
        assert "upload" in (m["last_error"] or "")
        assert os.path.exists(m["local_path"])  # 本地证据保留

    def test_status_update_failure_not_success(self, db, tmp_path, monkeypatch):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=OkUploader())
        orig = db.update_media_evidence_status

        def _fail_success(mid, *, status, **kw):
            if status == "success":
                return False
            return orig(mid, status=status, **kw)

        monkeypatch.setattr(db, "update_media_evidence_status", _fail_success)
        out = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert out["ok"] is False and out["error"] == "status_update_failed_after_upload"
        m = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m["status"] != "success"  # 不谎报成功


class TestRecover:
    def test_recover_retries_failed(self, db, tmp_path):
        rid = _new_alert(db)
        fail = _mk(db, tmp_path, uploader=FailUploader(), max_retries=3)
        fail.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]["status"] == "failed-awaiting-retry"
        # 上传端恢复后, 新 recorder 走 recover() 重试 -> 成功
        okr = _mk(db, tmp_path, uploader=OkUploader(), max_retries=3)
        res = okr.recover()
        assert res and res[0]["ok"] is True
        m = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m["status"] == "success" and m["upload_status"] == "success"

    def test_recover_respects_retry_cap(self, db, tmp_path):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=FailUploader(), max_retries=2)
        r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})  # retry_count=1
        r.recover()  # retry -> count=2
        assert db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]["retry_count"] == 2
        res = r.recover()
        assert res == []  # 已达上限, 不再重试
        assert db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]["status"] == "failed-awaiting-retry"

    def test_execute_media_in_progress_guard(self, db, tmp_path):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, recorder=BoomRecorder(), uploader=OkUploader())
        # 先建一条 process 中的媒体（模拟另一消费者已认领）
        mid = db.save_media_evidence(source=MEDIA_SOURCE, ref_id=rid, media_type=MEDIA_TYPE,
                                     local_path="/tmp/x.mp4", captured_at="2026-10-28T10:00:00Z",
                                     created_at="2026-10-28T10:00:00Z", status="processing")
        r._in_progress.add(mid)
        assert r.execute_media(mid)["reason"] == "in_progress"


class TestStrictUploadSuccess:
    @pytest.mark.parametrize("ok_value,label", [
        (_MISSING, "ok-missing"),
        (False, "ok-False"),
        ("true", "ok-str"),
        (1, "ok-int"),
    ])
    def test_non_bool_true_not_success(self, db, tmp_path, ok_value, label):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=NoOkUploader(ok_value))
        out = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert out["ok"] is False and out["error"] == "upload_failed", label
        m = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m["status"] == "failed-awaiting-retry" and m["upload_status"] == "failed"

    def test_uploader_raises_not_success(self, db, tmp_path):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=RaisesUploader())
        out = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert out["ok"] is False and out["error"] == "upload_failed"
        m = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m["status"] == "failed-awaiting-retry" and m["upload_status"] == "failed"

    def test_ok_true_only_success(self, db, tmp_path):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=OkUploader())
        out = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert out["ok"] is True
        m = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m["status"] == "success"


class TestSuccessWritebackRetry:
    def test_single_retry_no_reupload(self, db, tmp_path, monkeypatch):
        rid = _new_alert(db)
        up = OkUploader()
        r = _mk(db, tmp_path, uploader=up)
        orig = db.update_media_evidence_status
        n = {"calls": 0}

        def _flaky(mid, *, status, **kw):
            if status == "success":
                n["calls"] += 1
                if n["calls"] == 1:
                    return False  # 首次成功回写失败 -> recorder 应单次重试
            return orig(mid, status=status, **kw)

        monkeypatch.setattr(db, "update_media_evidence_status", _flaky)
        out = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert out["ok"] is True
        assert n["calls"] == 2                       # 恰一次重试，不无限
        assert len(up.calls) == 1                    # 未重复上传
        m = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m["status"] == "success"

    def test_still_fails_after_single_retry(self, db, tmp_path, monkeypatch):
        rid = _new_alert(db)
        r = _mk(db, tmp_path, uploader=OkUploader())
        orig = db.update_media_evidence_status

        def _always_fail(mid, *, status, **kw):
            if status == "success":
                return False
            return orig(mid, status=status, **kw)

        monkeypatch.setattr(db, "update_media_evidence_status", _always_fail)
        out = r.handle_event({"type": "alert_upgraded", "alert_id": rid, "level": 2})
        assert out["ok"] is False and out["error"] == "status_update_failed_after_upload"
        m = db.list_media_evidence_by_ref(MEDIA_SOURCE, rid)[0]
        assert m["status"] != "success"   # 不谎报成功