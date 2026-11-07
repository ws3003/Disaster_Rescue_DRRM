"""WP-2.1 共享查询业务函数直测（server.s4.query_*）。

隔离：tempfile 临时 SQLite，不写 data/drrm.db；不依赖 HTTP/认证。
验证：抽取出的共享函数与此前 S4 handler 内联逻辑行为一致，
返回 (data, meta) 结构与原有响应契约一致。
"""
from __future__ import annotations

import os
import tempfile

import pytest

from server import s4
from src.database.db import Database

PERSON_EVENT_TYPE = "high_risk_alert"


@pytest.fixture()
def db():
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "s4_shared_test.db"))
    d._test_event_id = _seed(d)
    yield d
    try:
        tmp.cleanup()
    except Exception:  # noqa: BLE001
        pass


def _seed(d: Database) -> str:
    d.save_reading(sensor_id="s1", temperature=51.0, humidity=31.0,
                   device_captured_at="2026-10-24T10:00:00+08:00")
    ev_id = d.save_event(type=PERSON_EVENT_TYPE, severity="high", person_id=1,
                         payload={"state": "Lying", "priority": "HIGH", "risk_score": 0.9})
    # 预期 alert_key: sensor:<alert_id>
    d.save_sensor_alert(type="sensor_over_temp", temperature=52.0, threshold=45.0)
    return ev_id


class TestSharedFunctions:
    def test_query_system_status_placeholder(self, db):
        data, meta = s4.query_system_status(db)
        assert data["device"]["status"] == "unknown"
        assert data["modules"] == {}
        assert meta == {}

    def test_query_alerts_merged_person_first(self, db):
        data, meta = s4.query_alerts(db, None, None, None)
        assert meta["page"] == 1 and meta["page_size"] == 20
        assert meta["total"] >= 1
        keys = {item.get("category") for item in data}
        assert "person" in keys  # 合并：先人员组
        assert set(meta) == {"page", "page_size", "total", "has_more"}

    def test_query_alerts_category_person(self, db):
        data, meta = s4.query_alerts(db, "person", None, None)
        assert all(item.get("category") == "person" for item in data)
        assert meta["total"] == 1

    def test_query_alerts_includes_c4_person_alert(self, db):
        d_id = db.save_event(
            type="person_alert", severity="HIGH", person_id=2,
            payload={"state": "Help-Waving", "priority": "HIGH", "risk_score": 0.8},
        )
        data, meta = s4.query_alerts(db, "person", None, None)
        assert meta["total"] == 2
        assert {item["alert_key"] for item in data} >= {f"person:{d_id}"}

    def test_person_risk_score_text_is_normalized_to_number(self, db):
        event_id = db.save_event(
            type="person_alert", severity="HIGH", person_id=2,
            payload={"state": "Help-Waving", "priority": "HIGH", "risk_score": "0.8"},
        )
        data, _ = s4.query_alerts(db, "person", None, None)
        item = next(row for row in data if row["alert_key"] == f"person:{event_id}")
        assert item["risk_score"] == 0.8
        assert isinstance(item["risk_score"], float)

    def test_query_alerts_category_sensor(self, db):
        data, meta = s4.query_alerts(db, "sensor_environment", None, None)
        assert all(item.get("category") == "sensor_environment" for item in data)
        assert meta["total"] >= 1

    def test_query_alert_detail_person(self, db):
        data, meta = s4.query_alert_detail(db, f"person:{db._test_event_id}")
        assert meta == {}
        assert data["source"] == "person" and data["category"] == "person"
        assert data["state"] == "Lying"

    def test_query_alert_detail_404(self, db):
        with pytest.raises(s4.S4Error) as ex:
            s4.query_alert_detail(db, "person:999999")
        assert ex.value.http_status == 404

    def test_query_alert_related_sensor_media_placeholder(self, db):
        alert_id = db.save_sensor_alert(type="sensor_over_temp", temperature=60.0,
                                        threshold=45.0)
        data, meta = s4.query_alert_related(db, f"sensor:{alert_id}")
        # media 保持占位，不做真实媒体（WP-4）
        assert data["media"] == [] and data["media_supported"] is False
        assert "readings" in data
        assert meta == {}

    def test_query_alert_related_person_no_reading(self, db):
        data, meta = s4.query_alert_related(db, f"person:{db._test_event_id}")
        assert data["readings"] == []
        assert data["media"] == [] and data["media_supported"] is False
        assert meta == {}

    def test_query_readings_pagination(self, db):
        data, meta = s4.query_readings(db, None, None, None, None, None)
        assert meta["total"] >= 1
        assert data and "reading_id" in data[0]
        assert set(meta) == {"page", "page_size", "total", "has_more"}

    def test_query_readings_start_gt_end_422(self, db):
        with pytest.raises(s4.S4Error) as ex:
            s4.query_readings(db, None, "2026-10-25T00:00:00Z",
                              "2026-10-24T00:00:00Z", None, None)
        assert ex.value.http_status == 422
