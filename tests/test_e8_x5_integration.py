"""V4.4 E8 + X5 视频证据与 OSS 专项集成验收（Mock OSS / 临时 SQLite / 临时视频，不连真实硬件/OSS）。

聚焦端到端 + 既有隔离测试未覆盖的跨层断言：
- 触发录像 → 生成文件 → 登记 media_evidence → 上传(mock) → 状态/对象键/关联。
- media_evidence **元数据**进入 C4 Outbox；**视频二进制 / 对象键作来源键**不入 Outbox、不入载荷。
- S4 媒体访问：未登录 401；媒体不存在 404。
- X5：OSS_EVIDENCE_BUCKET 缺失 fail-closed、不回退原桶；上传失败无假成功。
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import server.api as api_mod
import server.s4 as s4_mod
from server.auth import get_current_user
from src.database.db import Database
from src.evidence.recorder import EvidenceRecorder, FakeRecorder
from src.storage import OSSStorageProvider, StorageConfig


class OkUploader:
    def upload(self, path, prefix="evidence"):
        return {"ok": True, "url": f"http://x/{prefix}/{os.path.basename(path)}",
                "provider": "aliyun_oss"}


class FailUploader:
    def upload(self, path, prefix="evidence"):
        return {"ok": False, "error": "oss unreachable"}


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "e8x5_int.db"))
    d._tmpdir = tmp
    return d


def _level2_event(alert_id, captured_at="2026-10-26T09:00:00+08:00"):
    return {"type": "alert_created", "level": 2, "alert_id": alert_id,
            "device_captured_at": captured_at}


def _new_sensor_alert(db):
    """真实传感器告警（media_evidence 校验 ref_id 必须存在于 sensor_alerts）。"""
    return db.save_sensor_alert(type="sensor_over_temp", temperature=52.0, threshold=45.0)


def _outbox_records(db):
    with db._connect() as c:
        return c.execute(
            "SELECT record_type, source_record_id, status FROM sync_outbox ORDER BY id").fetchall()


def _media_outbox_row(db, media_id):
    """取指定媒体在 Outbox 的元数据行（record_type='media_evidence', source_record_id=media_id）。"""
    with db._connect() as c:
        return c.execute(
            "SELECT record_type, source_record_id, status FROM sync_outbox "
            "WHERE record_type='media_evidence' AND source_record_id=?", (media_id,)).fetchone()


class TestE8VideoToMedia:
    def test_event_records_uploads_and_links_alert(self, db):
        alert_id = _new_sensor_alert(db)
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=OkUploader(),
                               tmp_dir=tempfile.gettempdir())
        r = rec.handle_event(_level2_event(alert_id))
        assert r.get("handled") is True and r.get("ok") is True
        media = rec.db.list_media_evidence_by_ref("sensor", alert_id)
        assert len(media) == 1
        row = media[0]
        assert row["status"] == "success" and row["upload_status"] == "success"
        assert row["object_key"] == f"evidence/{row['media_id']}.mp4"
        assert row["ref_id"] == alert_id
        # 上传后 media 元数据进入 C4 Outbox（source_record_id = media_id，非对象键，无视频二进制/对象键作来源键）
        mob = _media_outbox_row(db, row["media_id"])
        assert mob is not None and mob["record_type"] == "media_evidence"
        assert mob["source_record_id"] == row["media_id"]
        assert not mob["source_record_id"].startswith("evidence/")
        assert all(not x[1].startswith("evidence/") for x in _outbox_records(db))

    def test_upload_failure_no_fake_success(self, db):
        alert_id = _new_sensor_alert(db)
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=FailUploader(),
                               tmp_dir=tempfile.gettempdir())
        r = rec.handle_event(_level2_event(alert_id))
        assert r.get("ok") is False and r.get("error") == "upload_failed"
        media = rec.db.list_media_evidence_by_ref("sensor", alert_id)
        assert len(media) == 1
        row = media[0]
        # 失败 → failed-awaiting-retry；不得假成功、不得写入 object_key/成功时间
        assert row["status"] == "failed-awaiting-retry" and row["upload_status"] == "failed"
        assert row.get("object_key") is None and row.get("uploaded_at") is None
        # 本地证据保留（未清理）
        assert os.path.exists(row["local_path"])
        # 失败媒体元数据仍进入 C4 Outbox：同步的是 failed 状态, 不伪造成功、不上传二进制/对象键
        mob = _media_outbox_row(db, row["media_id"])
        assert mob is not None and mob["record_type"] == "media_evidence"
        assert mob["source_record_id"] == row["media_id"]
        assert not mob["source_record_id"].startswith("evidence/")

    def test_repeat_event_no_duplicate(self, db):
        alert_id = _new_sensor_alert(db)
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=OkUploader(),
                               tmp_dir=tempfile.gettempdir())
        e = _level2_event(alert_id)
        rec.handle_event(e)
        r2 = rec.handle_event(e)
        assert r2.get("handled") is False and r2.get("reason") == "already_exists"
        assert len(rec.db.list_media_evidence_by_ref("sensor", alert_id)) == 1


class TestX5FailClosed:
    def test_evidence_bucket_missing_fails_closed(self, monkeypatch, tmp_path):
        # 清空全部 OSS env，仅保留原桶桶名（不允许 evidence 回退到原桶）
        for k in ("OSS_ACCESS_KEY", "OSS_ACCESS_SECRET", "OSS_BUCKET",
                  "OSS_ENDPOINT", "OSS_EVIDENCE_BUCKET"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OSS_ACCESS_KEY", "ak")
        monkeypatch.setenv("OSS_ACCESS_SECRET", "sk")
        monkeypatch.setenv("OSS_ENDPOINT", "ep")
        monkeypatch.setenv("OSS_BUCKET", "orig-bucket")
        prov = OSSStorageProvider(StorageConfig(provider="aliyun_oss", bucket="orig-bucket"))
        f = tmp_path / "v.mp4"
        f.write_bytes(b"0" * 100)
        res = prov.upload(str(f), prefix="evidence")
        assert res.get("ok") is False
        assert "OSS_EVIDENCE_BUCKET" in (res.get("error") or "")

    def test_snapshots_keeps_original_bucket(self, monkeypatch, tmp_path):
        # 非 evidence 前缀仍走原桶逻辑，不被 evidence 改动影响
        import src.storage as S
        monkeypatch.setitem(__import__("sys").modules, "oss2", _FakeOss2S())
        for k in ("OSS_ACCESS_KEY", "OSS_ACCESS_SECRET", "OSS_BUCKET",
                  "OSS_ENDPOINT", "OSS_EVIDENCE_BUCKET"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OSS_ACCESS_KEY", "ak")
        monkeypatch.setenv("OSS_ACCESS_SECRET", "sk")
        monkeypatch.setenv("OSS_ENDPOINT", "ep")
        monkeypatch.setenv("OSS_BUCKET", "orig")
        monkeypatch.setenv("OSS_EVIDENCE_BUCKET", "evb")
        f = tmp_path / "p.jpg"
        f.write_bytes(b"j")
        # 注入假 oss2 以捕获 bucket 名（不联网）
        import src.sensor  # noqa: F401
        r = _upload_with_fake_oss2(S, prov=None, file=str(f), prefix="snapshots")
        assert r is not None and "orig" in r  # 原桶用于图片


class _FakeOss2S:
    last_bucket = None

    def Auth(self, ak, sk):
        return object()

    def Bucket(self, _a, _e, name):
        _FakeOss2S.last_bucket = name
        return _FakeBucketS(name)


class _FakeBucketS:
    def __init__(self, name):
        self.name = name

    def put_object_from_file(self, _k, _p, headers=None):
        return object()

    def sign_url(self, *a, **k):
        return f"https://{self.name}/x"


def _upload_with_fake_oss2(storage_mod, prov, file, prefix):
    import sys
    sys.modules["oss2"] = _FakeOss2S()
    p = _FakeOss2S
    from src.storage import create_storage
    _cfg = StorageConfig(provider="aliyun_oss", bucket="orig")
    inst = OSSStorageProvider(_cfg)
    res = inst.upload(file, prefix=prefix)
    return res.get("url") if res.get("ok") else res.get("error")


class TestS4MediaAuth:
    def test_media_list_unauthenticated_401(self, db):
        api_mod.app.dependency_overrides[s4_mod.get_db] = lambda: db
        api_mod.app.dependency_overrides.pop(get_current_user, None)
        try:
            r = TestClient(api_mod.app).get("/api/v1/alerts/sensor:E-19990101-DEAD0001/media")
            assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"
        finally:
            api_mod.app.dependency_overrides.clear()

    def test_media_access_missing_404(self, db):
        api_mod.app.dependency_overrides[s4_mod.get_db] = lambda: db
        api_mod.app.dependency_overrides[get_current_user] = lambda: {"user_id": "U-X"}
        try:
            r = TestClient(api_mod.app).post("/api/v1/media/M-19990101-DEAD0001/access")
            assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
        finally:
            api_mod.app.dependency_overrides.clear()


class TestC4Boundary:
    def test_media_metadata_in_outbox_video_binary_not(self, db):
        # 写一条 media_evidence 后：C4 Outbox 出现媒体元数据(record_type='media_evidence')；
        # 但视频二进制 / 对象键作来源键 / record_type='media' 等非法表示均不出现。
        alert_id = _new_sensor_alert(db)
        rec = EvidenceRecorder(db, recorder=FakeRecorder(), uploader=OkUploader(),
                               tmp_dir=tempfile.gettempdir())
        rec.handle_event(_level2_event(alert_id))
        media = rec.db.list_media_evidence_by_ref("sensor", alert_id)
        assert len(media) == 1
        media_id = media[0]["media_id"]
        ob = _outbox_records(db)
        # 允许: record_type='media_evidence' 且 source_record_id=media_id（元数据, 非对象键）
        assert any(x[0] == "media_evidence" and x[1] == media_id for x in ob)
        for row in ob:
            # 保留原语意: 仅允许 canonical C4 类型, 无视频对象键作来源键
            assert row[0] in ("person_event", "reading", "sensor_alert", "media_evidence")
            assert not row[1].startswith("evidence/")
        # 保留原语意: 无 record_type 精确 'media'（非 canonical 表示）
        assert all(x[0] != "media" for x in ob)
