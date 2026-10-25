"""V4.4 E9-01 环境读数表 (readings) —— 数据库基础能力测试。

本测试使用隔离的临时 SQLite 库, 不写入真实业务库 data/drrm.db。
不定义温湿度有效范围 (该校验属上层 REQ-E7-02, 非本层职责)。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys
from datetime import timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database.db import Database  # noqa: E402


class ReadingsTableTest(unittest.TestCase):

    def setUp(self) -> None:
        # 隔离临时库, 绝不触碰 data/drrm.db
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
                "WHERE type='table' AND name='readings'"
            ).fetchone()[0]
            index = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='index' AND name='idx_readings_sensor_time'"
            ).fetchone()[0]
        self.assertEqual(table, 1)
        self.assertEqual(index, 1)

    def test_repeat_initialization_is_idempotent(self) -> None:
        # 再次触发初始化 (executescript IF NOT EXISTS) 不应报错
        self.db._initialize_database()
        with self.db._connect() as conn:
            n = conn.execute(
                "SELECT count(*) FROM readings"
            ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_save_and_query_reading(self) -> None:
        reading_id = self.db.save_reading(
            sensor_id="dht22-1",
            temperature=36.5,
            humidity=58.0,
            device_captured_at="2026-10-24T10:00:00+08:00",
            edge_received_at="2026-10-24T10:00:01+08:00",
        )
        self.assertTrue(reading_id.startswith("R-"))
        got = self.db.get_reading(reading_id)
        self.assertIsNotNone(got)
        self.assertEqual(got["sensor_id"], "dht22-1")
        self.assertEqual(got["temperature"], 36.5)
        self.assertEqual(got["humidity"], 58.0)

    def test_time_range_query(self) -> None:
        self.db.save_reading(
            sensor_id="s1", temperature=30.0,
            device_captured_at="2026-10-24T10:00:00+08:00",
        )
        self.db.save_reading(
            sensor_id="s1", temperature=32.0,
            device_captured_at="2026-10-24T10:05:00+08:00",
        )
        self.db.save_reading(
            sensor_id="s1", temperature=34.0,
            device_captured_at="2026-10-24T10:10:00+08:00",
        )
        rows = self.db.query_readings(
            sensor_id="s1",
            start_device_captured_at="2026-10-24T10:04:00+08:00",
            end_device_captured_at="2026-10-24T10:11:00+08:00",
        )
        temps = [r["temperature"] for r in rows]
        self.assertEqual(temps, [32.0, 34.0])
        # 按 sensor 过滤
        other = self.db.query_readings(sensor_id="nope")
        self.assertEqual(other, [])

    def test_duplicate_primary_key_raises(self) -> None:
        # 相同 reading_id 二次插入应触发 IntegrityError (主键约束)
        rid = "R-20261024-00000000"
        record = (
            rid, "s1", 30.0, "2026-10-24T10:00:00+08:00"
        )
        with self.db._connect() as conn:
            conn.execute(
                """INSERT INTO readings
                   (reading_id, sensor_id, temperature,
                    device_captured_at)
                   VALUES (?, ?, ?, ?)""",
                record,
            )
        with self.assertRaises(Exception):
            with self.db._connect() as conn2:
                conn2.execute(
                    """INSERT INTO readings
                       (reading_id, sensor_id, temperature,
                        device_captured_at)
                       VALUES (?, ?, ?, ?)""",
                    record,
                )

    def test_new_timestamps_include_timezone(self) -> None:
        reading_id = self.db.save_reading(
            sensor_id="s1", temperature=30.0
        )
        got = self.db.get_reading(reading_id)
        self.assertIsNotNone(got)
        # 带时区 ISO 8601: 含 'T' 与毫秒偏移符('+'/'−'/'Z')
        for field in ("device_captured_at", "edge_received_at"):
            val = got[field]
            self.assertIn("T", val)
            self.assertTrue(
                val.endswith("Z") or ("+" in val[-6:]) or ("-" in val[-6:])
            )

    def test_received_at_null_on_edge_local_write(self) -> None:
        reading_id = self.db.save_reading(
            sensor_id="s1", temperature=30.0
        )
        got = self.db.get_reading(reading_id)
        self.assertIsNone(got["received_at"])

    def test_reading_id_stable_and_referenceable(self) -> None:
        rid = self.db.save_reading(
            sensor_id="s1", temperature=30.0
        )
        # 供 sensor_alerts 应用层关联校验的读取入口
        self.assertIsNotNone(self.db.get_reading(rid))
        self.assertIsNone(self.db.get_reading("R-NOT-EXIST"))


if __name__ == "__main__":
    unittest.main()