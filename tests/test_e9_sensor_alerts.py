"""V4.4 E9-02 传感器环境告警表 (sensor_alerts) —— 数据库基础能力测试。

本测试使用隔离的临时 SQLite 库, 不写入真实业务库 data/drrm.db。
验证传感器告警独立于 events 存储 (REQ-E9-02/E7-05), 不与图像环境告警混用。
不定义阈值/持续/恢复规则 (属上层 REQ-E7-03/04, 非本层职责)。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database.db import Database  # noqa: E402


class SensorAlertsTableTest(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpfile = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False
        )
        self._tmpfile.close()
        self.db = Database(db_path=self._tmpfile.name)

    def tearDown(self) -> None:
        Path(self._tmpfile.name).unlink(missing_ok=True)

    def test_table_and_index_initialized(self) -> None:
        with self.db._connect() as conn:
            table = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='table' AND name='sensor_alerts'"
            ).fetchone()[0]
            index = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='index' AND name='idx_sensor_status_time'"
            ).fetchone()[0]
        self.assertEqual(table, 1)
        self.assertEqual(index, 1)

    def test_repeat_initialization_is_idempotent(self) -> None:
        self.db._initialize_database()
        with self.db._connect() as conn:
            n = conn.execute(
                "SELECT count(*) FROM sensor_alerts"
            ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_save_and_fetch_sensor_alert(self) -> None:
        alert_id = self.db.save_sensor_alert(
            type="sensor_over_temp",
            temperature=42.0,
            humidity=60.0,
            threshold=40.0,
            severity="HIGH",
        )
        self.assertTrue(alert_id.startswith("E-"))
        got = self.db.get_sensor_alert(alert_id)
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], "active")
        self.assertEqual(got["temperature"], 42.0)
        self.assertEqual(got["humidity"], 60.0)

    def test_active_to_resolved(self) -> None:
        alert_id = self.db.save_sensor_alert(type="sensor_over_temp")
        updated = self.db.update_sensor_alert_status(
            alert_id, "resolved"
        )
        self.assertTrue(updated)
        got = self.db.get_sensor_alert(alert_id)
        self.assertEqual(got["status"], "resolved")
        self.assertIsNotNone(got["resolved_at"])

    def test_update_missing_alert_returns_false(self) -> None:
        updated = self.db.update_sensor_alert_status(
            "E-20261024-00000000", "resolved"
        )
        self.assertFalse(updated)

    def test_invalid_status_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.db.update_sensor_alert_status(
                "E-20261024-00000000", "bogus"
            )

    def test_reading_id_nullable_and_validate_by_app(self) -> None:
        # 不关联读数
        aid1 = self.db.save_sensor_alert(type="t")
        self.assertIsNone(self.db.get_sensor_alert(aid1)["reading_id"])
        # 关联读数 -> 应用层可用 get_reading 校验存在
        rid = self.db.save_reading(sensor_id="s1", temperature=30.0)
        aid2 = self.db.save_sensor_alert(
            type="t", reading_id=rid
        )
        self.assertEqual(
            self.db.get_sensor_alert(aid2)["reading_id"], rid
        )
        # 应用层校验: reading 存在 / 缺失
        self.assertIsNotNone(self.db.get_reading(rid))
        self.assertIsNone(self.db.get_reading("R-NOT-EXIST"))

    def test_query_by_status_and_limit(self) -> None:
        a1 = self.db.save_sensor_alert(type="t1")
        a2 = self.db.save_sensor_alert(type="t2")
        self.db.update_sensor_alert_status(a1, "resolved")
        active = self.db.query_sensor_alerts(status="active")
        self.assertEqual([r["alert_id"] for r in active], [a2])
        resolved = self.db.query_sensor_alerts(status="resolved")
        self.assertEqual([r["alert_id"] for r in resolved], [a1])
        limited = self.db.query_sensor_alerts(limit=1)
        self.assertEqual(len(limited), 1)

    def test_sensor_alerts_isolated_from_events(self) -> None:
        # 写入 sensor_alert 后不应影响 events 表 (REQ-E9-02 独立存储)
        self.db.save_sensor_alert(type="sensor_over_temp")
        with self.db._connect() as conn:
            events = conn.execute(
                "SELECT count(*) FROM sensor_alerts"
            ).fetchone()[0]
        with self.db._connect() as conn:
            events_count = conn.execute(
                "SELECT count(*) FROM events"
            ).fetchone()[0]
        self.assertEqual(events, 1)          # sensor_alerts 有一条
        self.assertEqual(events_count, 0)    # events 表不受影响


if __name__ == "__main__":
    unittest.main()