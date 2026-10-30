"""V4.4 C4 reading/sensor_alert 业务数据副本同步测试（B）。

- 服务端：POST /api/v1/c4/records 首次落 readings/sensor_alerts 业务副本（同事务 c4_idem）、
  重复已同步不重复插入、同 ID 异 hash 冲突不覆盖、非法 business 拒绝、旧客户端(无 business)不写副本。
- Worker：reading/sensor_alert 载荷携带 business；person_event 不携带。
临时库 + TestClient/Mock，不连真实网络。
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import server.c4 as c4_mod
from server.api import app as api_app
from src.database import canonical
from src.database.db import Database
from src.sync import bridge
from src.sync.worker import OutboxWorker

_TOKEN = "worker-token"


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "c4_bizcopy.db"))
    d._tmpdir = tmp
    return d


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setenv("C4_API_TOKEN", _TOKEN)
    api_app.dependency_overrides[c4_mod.get_db] = lambda: db
    try:
        yield TestClient(api_app)
    finally:
        api_app.dependency_overrides.clear()


def _hash(rt, business):
    view = bridge._canonical_view(rt, business)
    h, reasons = canonical.classify(rt, view, version=bridge.SYNC_NORM_VERSION)
    assert not reasons, reasons
    return h


def _reading(recv):
    return {"record_type": "reading", "source_record_id": f"R-{recv}-AAAA0001",
            "content_hash": _hash("reading", _reading_biz(recv)), "edge_id": "pi-1",
            "business": _reading_biz(recv)}


def _reading_biz(recv, temp="26.75"):
    return {"sensor_id": "s1", "temperature": temp, "humidity": "40",
            "device_captured_at": f"2026-10-{recv:02d}T10:00:00Z"}


def _alert(recv):
    biz = {"type": "sensor_hightemp", "temperature": "52.5", "threshold": "50",
           "occurred_at": f"2026-10-{recv:02d}T11:00:00Z", "reading_id": f"R-{recv}-AAAA0001"}
    return {"record_type": "sensor_alert", "source_record_id": f"E-{recv}-BBBB0002",
            "content_hash": _hash("sensor_alert", biz), "edge_id": "pi-1", "business": biz}


def _post(client, rec):
    return client.post("/api/v1/c4/records", json={"records": [rec]},
                       headers={"Authorization": f"Bearer {_TOKEN}"})


class TestReadingCopy:
    def test_first_sync_writes_reading_copy_and_idem(self, client, db):
        r = _post(client, _reading(1))
        body = r.json()
        assert r.status_code == 200 and body["data"][0]["status"] == "synced"
        with db._connect() as c:
            row = c.execute("SELECT temperature, humidity, received_at FROM readings "
                            "WHERE reading_id=?", ("R-1-AAAA0001",)).fetchone()
            c_idem = c.execute("SELECT 1 FROM c4_idem WHERE source_record_id=?",
                               ("R-1-AAAA0001",)).fetchone()
        assert row is not None and float(row[0]) == 26.75 and float(row[1]) == 40.0
        assert row[2]  # received_at 已填（服务端接收时间）
        assert c_idem is not None

    def test_duplicate_synced_no_dup(self, client, db):
        _post(client, _reading(1))
        r2 = _post(client, _reading(1))
        assert r2.json()["data"][0]["status"] == "already_synced"
        with db._connect() as c:
            assert c.execute("SELECT COUNT(*) FROM readings WHERE reading_id=?",
                             ("R-1-AAAA0001",)).fetchone()[0] == 1

    def test_same_id_diff_content_conflict_no_overwrite(self, client, db):
        _post(client, _reading(1))
        rec2 = _reading(1)
        rec2["business"] = _reading_biz(1, temp="30.0")       # 同 ID 不同内容
        rec2["content_hash"] = _hash("reading", rec2["business"])
        r3 = _post(client, rec2)
        assert r3.json()["data"][0]["status"] == "content_mismatch"
        with db._connect() as c:
            v = c.execute("SELECT temperature FROM readings WHERE reading_id=?",
                          ("R-1-AAAA0001",)).fetchone()[0]
            conflicts = c.execute("SELECT COUNT(*) FROM c4_content_conflict").fetchone()[0]
        assert float(v) == 26.75      # 首次副本保留，不被覆盖
        assert conflicts == 1

    def test_invalid_business_rejected_400(self, client):
        rec = _reading(1)
        rec["business"] = {"sensor_id": "s1"}   # 缺 temperature/device_captured_at
        r = _post(client, rec)
        assert r.status_code == 400

    def test_old_client_no_business_synced_no_copy(self, client, db):
        # 旧客户端(无 business) → 只登记 c4_idem、不写业务副本（向后兼容）
        rec = {"record_type": "reading", "source_record_id": "R-9-AAAA0009",
               "content_hash": "deadbeef00000000000000000000000000000001", "edge_id": "pi-1"}
        r = _post(client, rec)
        assert r.json()["data"][0]["status"] == "synced"
        with db._connect() as c:
            assert c.execute("SELECT 1 FROM c4_idem WHERE source_record_id=?",
                             ("R-9-AAAA0009",)).fetchone() is not None
            assert c.execute("SELECT COUNT(*) FROM readings WHERE reading_id=?",
                             ("R-9-AAAA0009",)).fetchone()[0] == 0


class TestSensorAlertCopy:
    def test_first_sync_writes_alert_copy(self, client, db):
        a = _alert(1)
        r = _post(client, a)
        assert r.json()["data"][0]["status"] == "synced"
        with db._connect() as c:
            row = c.execute("SELECT type, temperature, threshold, reading_id, received_at "
                            "FROM sensor_alerts WHERE alert_id=?", ("E-1-BBBB0002",)).fetchone()
        assert row is not None and row[0] == "sensor_hightemp"
        assert float(row[1]) == 52.5 and float(row[2]) == 50.0 and row[3] == "R-1-AAAA0001"
        assert row[4]


class TestPersonEventUnchanged:
    def test_person_event_no_business_still_registers(self, client, db):
        rec = {"record_type": "person_event", "source_record_id": "E-P-00000001",
               "content_hash": "abc123"}   # 无 business，沿用旧协议
        r = _post(client, rec)
        assert r.status_code == 200 and r.json()["data"][0]["status"] == "synced"
        with db._connect() as c:
            assert c.execute("SELECT 1 FROM c4_idem WHERE source_record_id=?",
                             ("E-P-00000001",)).fetchone() is not None


class TestWorkerPayload:
    def test_worker_sends_business_for_reading(self, db):
        import dataclasses
        from src.sensor import reader as _r
        from src.sensor.alarm import HighTempAlarm
        from src.sensor.config import SensorConfig
        from src.sensor.loop import SampledSensorLoop
        cfg = SensorConfig(sensor_id="s1", temperature_threshold=40.0, sample_period_seconds=1.0,
                           confirm_high_count=1, confirm_recovery_count=1, cooldown_seconds=0.0)
        SampledSensorLoop(cfg, _r.FakeReader(lambda: (26.75, 40.0)), HighTempAlarm(cfg, db),
                          db, sleep=lambda _f: None).step()
        sent = []

        class _Resp:
            def __init__(self, r):
                self.r = r
                self.status_code = 200

            def json(self):
                d = self.r[0]
                return {"code": "OK", "data": [{"record_type": d["record_type"],
                                                "source_record_id": d["source_record_id"],
                                                "status": "synced", "error_code": None}],
                        "meta": {}}

        def post(url, json=None, headers=None, timeout=10.0):
            sent.append(json["records"][0])
            return _Resp(sent[-1])
        w = OutboxWorker(db, "http://win/api/v1", _TOKEN, http_post=post, worker_id="w", now_ms=lambda: 1)
        w.process_batch(10)
        assert sent and sent[0]["record_type"] == "reading"
        assert sent[0]["business"]["temperature"] == "26.75"   # 载荷携带精确业务体
        assert float(sent[0]["business"]["humidity"]) == 40.0
        assert "content_hash" in sent[0]


class TestS4EndToEnd:
    """业务副本落库后，S4-06 能在 Windows 现有查询读到 Pi 同步的温湿度数据。"""

    def test_s4_reads_synced_reading(self, db, monkeypatch):
        import server.s4 as s4_mod
        monkeypatch.setenv("C4_API_TOKEN", _TOKEN)
        monkeypatch.setenv("S4_API_TOKEN", "s4-token")
        api_app.dependency_overrides[c4_mod.get_db] = lambda: db
        api_app.dependency_overrides[s4_mod.get_db] = lambda: db
        try:
            c = TestClient(api_app)
            # 1) Pi reading 经 C4 同步（业务副本落库）
            r = c.post("/api/v1/c4/records", json={"records": [_reading(1)]},
                       headers={"Authorization": f"Bearer {_TOKEN}"})
            assert r.json()["data"][0]["status"] == "synced"
            # 2) S4-06 查询到该 reading
            q = c.get("/api/v1/environment/readings",
                      headers={"Authorization": "Bearer s4-token"})
            assert q.status_code == 200
            data = q.json()["data"]
            assert any(float(x["temperature"]) == 26.75 for x in data)
        finally:
            api_app.dependency_overrides.clear()

    def test_s4_full_chain_duplicate_single_row(self, db, monkeypatch):
        import server.s4 as s4_mod
        monkeypatch.setenv("C4_API_TOKEN", _TOKEN)
        monkeypatch.setenv("S4_API_TOKEN", "s4-token")
        api_app.dependency_overrides[c4_mod.get_db] = lambda: db
        api_app.dependency_overrides[s4_mod.get_db] = lambda: db
        try:
            c = TestClient(api_app)
            for _ in range(2):  # 重复同步
                c.post("/api/v1/c4/records", json={"records": [_reading(2)]},
                       headers={"Authorization": f"Bearer {_TOKEN}"})
            q = c.get("/api/v1/environment/readings",
                      headers={"Authorization": "Bearer s4-token"})
            rows = [x for x in q.json()["data"] if x["reading_id"] == "R-2-AAAA0001"]
            assert len(rows) == 1  # 重复同步不产生重复行
        finally:
            api_app.dependency_overrides.clear()

    def test_s4_04_alert_related_reading(self, db, monkeypatch):
        import server.s4 as s4_mod
        monkeypatch.setenv("C4_API_TOKEN", _TOKEN)
        monkeypatch.setenv("S4_API_TOKEN", "s4-token")
        api_app.dependency_overrides[c4_mod.get_db] = lambda: db
        api_app.dependency_overrides[s4_mod.get_db] = lambda: db
        try:
            c = TestClient(api_app)
            # 1) reading 同步（副本落库，reading_id=R-1-AAAA0001）
            c.post("/api/v1/c4/records", json={"records": [_reading(1)]},
                   headers={"Authorization": f"Bearer {_TOKEN}"})
            # 2) sensor_alert 同步并关联该 reading_id
            c.post("/api/v1/c4/records", json={"records": [_alert(1)]},
                   headers={"Authorization": f"Bearer {_TOKEN}"})
            # 3) S4-04 告警详情关联信息 → 应返回关联读数
            r = c.get("/api/v1/alerts/sensor:E-1-BBBB0002/related",
                      headers={"Authorization": "Bearer s4-token"})
            assert r.status_code == 200
            data = r.json()["data"]
            assert data["readings"], "related 应包含关联读数"
            assert float(data["readings"][0]["temperature"]) == 26.75
        finally:
            api_app.dependency_overrides.clear()
