"""V4.4 S4 契约/DAO/鉴权/错误语义 测试。

隔离策略: 全部使用 `tempfile` 临时 SQLite, 不写入 data/drrm.db。
直接调用 server.s4 路由函数(避免完整 server.api 初始化的副作用), 返回 Starlette JSONResponse。
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from server import s4
from src.database.db import Database

TOKEN = "test-token"
PERSON_ID_E1 = 1


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    monkeypatch.setenv("S4_API_TOKEN", TOKEN)


def _seed(db: Database) -> None:
    # 人员告警(high_risk_alert)
    db.save_event(
        type="high_risk_alert",
        severity="high",
        person_id=PERSON_ID_E1,
        payload={
            "title": "受困人员",
            "state": "Lying",
            "priority": "HIGH",
            "risk_score": 0.9,
        },
    )
    db.save_event(
        type="high_risk_alert",
        severity="medium",
        person_id=2,
        payload={},
    )
    # 非高危事件(不应进入 S4 人员告警)
    db.save_event(type="other_alert", severity="low", person_id=3, payload={})

    # 读数(先建, 供传感器告警关联)
    rid1 = db.save_reading(
        sensor_id="s1",
        temperature=51.0,
        humidity=31.0,
        device_captured_at="2026-10-24T10:00:00+08:00",
    )
    db.save_reading(
        sensor_id="s1",
        temperature=52.0,
        humidity=32.0,
        device_captured_at="2026-10-24T11:00:00+08:00",
    )
    db.save_reading(
        sensor_id="s2",
        temperature=40.0,
        humidity=50.0,
        device_captured_at="2026-10-24T12:00:00+08:00",
    )
    # 传感器告警(一个关联读数)
    db.save_sensor_alert(
        type="sensor_over_temp",
        temperature=52.0,
        humidity=30.0,
        threshold=45.0,
        severity="high",
        device_captured_at="2026-10-24T10:05:00+08:00",
        reading_id=rid1,
    )
    db.save_sensor_alert(
        type="sensor_over_temp",
        temperature=55.0,
        humidity=28.0,
        threshold=45.0,
        device_captured_at="2026-10-24T12:05:00+08:00",
    )


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "s4_test.db"))
    _seed(db)
    yield db
    db.close() if hasattr(db, "close") else None


def _call(fn, **kwargs):
    res = fn(**kwargs)
    return res.status_code, json.loads(res.body)


# ----------------------------------------------------------------------
# DAO: COUNT 与列表同一过滤、get_event
# ----------------------------------------------------------------------

class TestDao:
    def test_person_count_matches_list(self, db):
        lst = db.query_person_alerts(s4.PERSON_EVENT_TYPE, limit=1000)
        assert db.count_person_alerts(s4.PERSON_EVENT_TYPE) == len(lst) == 2
        assert all(x["type"] == "high_risk_alert" for x in lst)

    def test_sensor_count_matches_list(self, db):
        lst = db.query_sensor_alerts(limit=1000)
        assert db.count_sensor_alerts() == len(lst) == 2

    def test_reading_count_matches_list(self, db):
        lst = db.query_readings(sensor_id="s1", limit=1000)
        assert db.count_readings(sensor_id="s1") == len(lst) == 2

    def test_get_event(self, db):
        eid = db.query_person_alerts(s4.PERSON_EVENT_TYPE, limit=1000)[0]["event_id"]
        ev = db.get_event(eid)
        assert ev["event_id"] == eid
        assert isinstance(ev["payload"], dict)
        assert db.get_event("no-such-event") is None


# ----------------------------------------------------------------------
# S4-01 系统状态
# ----------------------------------------------------------------------

class TestSystemStatus:
    def test_ok_placeholder(self, db):
        code, body = _call(s4.system_status, authorization=f"Bearer {TOKEN}")
        assert code == 200
        assert body["code"] == "OK" and body["message"] == "ok"
        assert body["data"] == {"device": {"status": "unknown"}, "modules": {}}
        assert body["meta"] == {}

    def test_requires_auth(self, db):
        code, body = _call(s4.system_status, authorization=None)
        assert code == 401 and body["code"] == "UNAUTHORIZED"
        assert body["data"] is None and body["meta"] == {}

    @pytest.mark.parametrize("bad", [None, "", "Bearer wrong", "bearer " + TOKEN, TOKEN])
    def test_bad_auth_variants_401(self, db, bad):
        # 缺失/空值/错误值/大小写错误/无 Bearer 前缀 -> 一律 401
        code, body = _call(s4.system_status, authorization=bad)
        assert code == 401 and body["code"] == "UNAUTHORIZED"
        assert body["data"] is None and body["meta"] == {}


# ----------------------------------------------------------------------
# S4-02 告警列表: 合并/组序/筛选/分页/auth
# ----------------------------------------------------------------------

class TestAlerts:
    def test_merged_person_first(self, db):
        code, body = _call(
            s4.alerts, category=None, page=None, page_size=None,
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 200
        assert body["meta"]["total"] == 4  # 2 person + 2 sensor
        assert body["data"][0]["source"] == "person"
        assert body["data"][1]["source"] == "person"
        assert body["data"][2]["source"] == "sensor"
        assert body["data"][3]["source"] == "sensor"

    def test_category_person_only(self, db):
        code, body = _call(
            s4.alerts, category="person", page=None, page_size=None,
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 200
        assert body["meta"]["total"] == 2
        assert all(x["category"] == "person" for x in body["data"])

    def test_category_sensor_environment_only(self, db):
        code, body = _call(
            s4.alerts, category="sensor_environment", page=None, page_size=None,
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 200
        assert body["meta"]["total"] == 2
        assert all(x["category"] == "sensor_environment" for x in body["data"])

    def test_paging_has_more_and_last_page(self, db):
        code1, b1 = _call(
            s4.alerts, category="sensor_environment", page="1", page_size="2",
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code1 == 200 and len(b1["data"]) == 2 and b1["meta"]["has_more"] is False
        code3, b3 = _call(
            s4.alerts, category="sensor_environment", page="2", page_size="2",
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code3 == 200 and len(b3["data"]) == 0 and b3["meta"]["has_more"] is False

    def test_auth_401_and_missing_config_500(self, db, monkeypatch):
        code, body = _call(
            s4.alerts, category=None, page=None, page_size=None,
            authorization="Bearer wrong", db=db,
        )
        assert code == 401 and body["code"] == "UNAUTHORIZED"
        monkeypatch.delenv("S4_API_TOKEN")
        code, body = _call(
            s4.alerts, category=None, page=None, page_size=None,
            authorization="Bearer x", db=db,
        )
        assert code == 500 and body["code"] == "INTERNAL_ERROR"

    def test_invalid_category_400(self, db):
        code, body = _call(
            s4.alerts, category="bogus", page=None, page_size=None,
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 400 and body["code"] == "INVALID_PARAMETER"

    def test_paging_has_more_true_and_last_page(self, db):
        # 追加 5 条传感器告警 -> 共 7 条; page_size=2 时应出现 has_more=True 与末页
        for i in range(5):
            db.save_sensor_alert(
                type="sensor_over_temp",
                temperature=50.0 + i,
                humidity=30.0,
                threshold=45.0,
                device_captured_at=f"2026-10-24T13:0{i}:00+08:00",
            )
        code, b1 = _call(
            s4.alerts, category="sensor_environment", page="1", page_size="2",
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 200
        assert b1["meta"]["total"] == 7
        assert b1["meta"]["has_more"] is True
        assert len(b1["data"]) == 2
        code, b4 = _call(
            s4.alerts, category="sensor_environment", page="4", page_size="2",
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 200
        assert b4["meta"]["has_more"] is False  # 4*2=8 < 7 为假
        assert len(b4["data"]) == 1


# ----------------------------------------------------------------------
# S4-03 告警详情
# ----------------------------------------------------------------------

class TestAlertDetail:
    def test_person_detail(self, db):
        items = db.query_person_alerts(s4.PERSON_EVENT_TYPE, limit=1000)
        titled = next(x for x in items if (x.get("payload") or {}).get("title") == "受困人员")
        code, body = _call(s4.alert_detail, alert_key=f"person:{titled['event_id']}",
                           authorization=f"Bearer {TOKEN}", db=db)
        assert code == 200
        assert body["data"]["alert_key"] == f"person:{titled['event_id']}"
        assert body["data"]["source"] == "person"
        assert body["data"]["title"] == "受困人员"
        assert body["data"]["state"] == "Lying"
        assert body["data"]["priority"] == "HIGH"
        assert body["data"]["risk_score"] == 0.9

    def test_sensor_detail(self, db):
        aid = db.query_sensor_alerts(limit=1000)[0]["alert_id"]
        code, body = _call(s4.alert_detail, alert_key=f"sensor:{aid}",
                           authorization=f"Bearer {TOKEN}", db=db)
        assert code == 200
        assert body["data"]["source"] == "sensor"
        assert body["data"]["status"] == "active"

    def test_not_found_404(self, db):
        code, body = _call(s4.alert_detail, alert_key="person:missing",
                           authorization=f"Bearer {TOKEN}", db=db)
        assert code == 404 and body["code"] == "NOT_FOUND"

    def test_malformed_400(self, db):
        code, body = _call(s4.alert_detail, alert_key="person:",
                           authorization=f"Bearer {TOKEN}", db=db)
        assert code == 400 and body["code"] == "INVALID_PARAMETER"

    def test_person_detail_full_dto_fields(self, db):
        items = db.query_person_alerts(s4.PERSON_EVENT_TYPE, limit=1000)
        titled = next(x for x in items if (x.get("payload") or {}).get("title") == "受困人员")
        code, body = _call(s4.alert_detail, alert_key=f"person:{titled['event_id']}",
                           authorization=f"Bearer {TOKEN}", db=db)
        d = body["data"]
        assert code == 200
        # 契约 person_alert DTO 11 字段逐一断言
        assert set(d.keys()) == {
            "alert_key", "source", "category", "type", "severity",
            "ts", "person_id", "title", "state", "priority", "risk_score",
        }
        assert d["alert_key"] == f"person:{titled['event_id']}"
        assert d["source"] == "person" and d["category"] == "person"
        assert d["type"] == "high_risk_alert" and d["severity"] == "high"
        assert isinstance(d["ts"], str) and d["ts"]
        assert d["person_id"] == PERSON_ID_E1
        assert d["title"] == "受困人员" and d["state"] == "Lying"
        assert d["priority"] == "HIGH" and d["risk_score"] == 0.9

    def test_sensor_detail_full_dto_fields(self, db):
        rows = db.query_readings(sensor_id="s1", limit=1000)
        rid = next(r["reading_id"] for r in rows
                   if r["device_captured_at"] == "2026-10-24T10:00:00+08:00")
        alerts = db.query_sensor_alerts(limit=1000)
        linked = next(a for a in alerts if a.get("reading_id") == rid)
        code, body = _call(s4.alert_detail, alert_key=f"sensor:{linked['alert_id']}",
                           authorization=f"Bearer {TOKEN}", db=db)
        d = body["data"]
        assert code == 200
        # 契约 sensor_alert DTO 14 字段逐一断言
        assert set(d.keys()) == {
            "alert_key", "source", "category", "type", "severity", "status",
            "temperature", "humidity", "threshold", "occurred_at",
            "resolved_at", "device_captured_at", "reading_id", "received_at",
        }
        assert d["alert_key"] == f"sensor:{linked['alert_id']}"
        assert d["source"] == "sensor" and d["category"] == "sensor_environment"
        assert d["type"] == "sensor_over_temp" and d["severity"] == "high"
        assert d["status"] == "active"
        assert d["temperature"] == 52.0 and d["humidity"] == 30.0 and d["threshold"] == 45.0
        assert isinstance(d["occurred_at"], str) and d["occurred_at"]
        assert d["resolved_at"] is None and d["received_at"] is None
        assert d["device_captured_at"] == "2026-10-24T10:05:00+08:00"
        assert d["reading_id"] == rid


# ----------------------------------------------------------------------
# S4-04 关联读数与媒体占位
# ----------------------------------------------------------------------

class TestRelated:
    def test_sensor_with_reading(self, db):
        # 选择存在有效 reading_id 的传感器告警(不依赖排序后的第一条,
        # 因为多条告警 occurred_at 可能同秒、alert_id uuid 排序不保证命中)。
        linked = [a for a in db.query_sensor_alerts(limit=1000) if a.get("reading_id")]
        assert linked, "seed 应包含关联了读数的传感器告警"
        aid = linked[0]["alert_id"]
        code, body = _call(s4.alert_related, alert_key=f"sensor:{aid}",
                           authorization=f"Bearer {TOKEN}", db=db)
        assert code == 200
        assert len(body["data"]["readings"]) == 1
        assert body["data"]["readings"][0]["sensor_id"] == "s1"
        assert body["data"]["media"] == []
        assert body["data"]["media_supported"] is False

    def test_person_no_reading(self, db):
        eid = db.query_person_alerts(s4.PERSON_EVENT_TYPE, limit=1000)[0]["event_id"]
        code, body = _call(s4.alert_related, alert_key=f"person:{eid}",
                           authorization=f"Bearer {TOKEN}", db=db)
        assert code == 200
        assert body["data"]["readings"] == []
        assert body["data"]["media_supported"] is False

    def test_reading_full_dto_fields(self, db):
        rows = db.query_readings(sensor_id="s1", limit=1000)
        rid = next(r["reading_id"] for r in rows
                   if r["device_captured_at"] == "2026-10-24T10:00:00+08:00")
        alerts = db.query_sensor_alerts(limit=1000)
        linked = next(a for a in alerts if a.get("reading_id") == rid)
        code, body = _call(s4.alert_related, alert_key=f"sensor:{linked['alert_id']}",
                           authorization=f"Bearer {TOKEN}", db=db)
        r = body["data"]["readings"][0]
        assert code == 200
        # 契约 environment_reading DTO 7 字段逐一断言
        assert set(r.keys()) == {
            "reading_id", "sensor_id", "temperature", "humidity",
            "device_captured_at", "edge_received_at", "received_at",
        }
        assert r["reading_id"] == rid and r["sensor_id"] == "s1"
        assert r["temperature"] == 51.0 and r["humidity"] == 31.0
        assert r["device_captured_at"] == "2026-10-24T10:00:00+08:00"
        # edge_received_at 缺省=Pi 本地写库时间(带时区 ISO); received_at 在 C4 前为 None
        assert isinstance(r["edge_received_at"], str) and r["edge_received_at"]
        assert r["received_at"] is None


# ----------------------------------------------------------------------
# S4-06 环境读数: 闭区间/升序/422
# ----------------------------------------------------------------------

class TestEnvironmentReadings:
    def test_closed_interval_asc(self, db):
        code, body = _call(
            s4.environment_readings,
            sensor_id="s1",
            start="2026-10-24T10:30:00+08:00",
            end="2026-10-24T11:30:00+08:00",
            page=None, page_size=None,
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 200
        assert [r["device_captured_at"] for r in body["data"]] == [
            "2026-10-24T11:00:00+08:00"
        ]

    def test_start_gt_end_422(self, db):
        code, body = _call(
            s4.environment_readings,
            sensor_id=None, start="2026-10-25T00:00:00+08:00",
            end="2026-10-24T00:00:00+08:00",
            page=None, page_size=None,
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 422 and body["code"] == "VALIDATION_ERROR"

    def test_parameterized_no_injection(self, db):
        # 注入串不应泄露, 应返回 0 条
        code, body = _call(
            s4.environment_readings,
            sensor_id="' OR 1=1 --", start=None, end=None,
            page=None, page_size=None,
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 200
        assert body["meta"]["total"] == 0 and body["data"] == []

    def test_pagination_and_count_consistency(self, db):
        # 追加 5 条 s3 读数 -> 共 5 条; 分页 meta 与 COUNT 一致, 升序
        for i in range(5):
            db.save_reading(
                sensor_id="s3",
                temperature=20.0 + i,
                humidity=60.0,
                device_captured_at=f"2026-10-25T0{i}:00:00+08:00",
            )
        assert db.count_readings(sensor_id="s3") == 5
        code, b1 = _call(
            s4.environment_readings,
            sensor_id="s3", start=None, end=None, page="1", page_size="2",
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 200
        assert b1["meta"]["total"] == 5 and b1["meta"]["has_more"] is True
        assert len(b1["data"]) == 2
        ts = [r["device_captured_at"] for r in b1["data"]]
        assert ts == sorted(ts)  # ASC
        code, b3 = _call(
            s4.environment_readings,
            sensor_id="s3", start=None, end=None, page="3", page_size="2",
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 200
        assert b3["meta"]["total"] == 5 and b3["meta"]["has_more"] is False
        assert len(b3["data"]) == 1


# ----------------------------------------------------------------------
# 参数校验: 分页/错误信封
# ----------------------------------------------------------------------

class TestValidation:
    @pytest.mark.parametrize("page,page_size", [
        ("abc", None), ("0", None), (None, "101"), (None, "0"), ("-1", None),
    ])
    def test_invalid_paging_400(self, db, page, page_size):
        code, body = _call(
            s4.alerts, category=None, page=page, page_size=page_size,
            authorization=f"Bearer {TOKEN}", db=db,
        )
        assert code == 400 and body["code"] == "INVALID_PARAMETER"
        assert body["data"] is None and body["meta"] == {}

    def test_envelope_shape_on_error(self, db):
        code, body = _call(s4.alerts, category="bad", page=None, page_size=None,
                           authorization=f"Bearer {TOKEN}", db=db)
        assert sorted(body.keys()) == ["code", "data", "message", "meta"]
        assert body["data"] is None and body["meta"] == {}