"""Memory → SQLite 持久化 单元测试 (P0, 决策1/2/3)。

覆盖: Memory() 默认 / Memory(db) 注入 / record_alert 落库 events(/order memory_alert) /
同一实例 cooldown / 跨实例恢复 / 过期 / 不同 person·state 独立 / digest 行为保持。
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.agent.memory import Memory  # noqa: E402
from src.database.db import Database  # noqa: E402


def _mk_db(tmp_file):
    return Database(db_path=tmp_file)


class MemorySQLiteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "mem.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_t1_default_init(self):
        m = Memory()
        self.assertEqual(m.cooldown_minutes, 30)
        self.assertIsNone(m.db)
        self.assertFalse(m.should_cooldown(1, "Fall"))  # 无记录 → False

    def test_t2_db_injection(self):
        db = _mk_db(self.db_path)
        m = Memory(cooldown_minutes=30, db=db)
        self.assertIsNotNone(m.db)

    def test_t3_record_alert_persists_to_events(self):
        db = _mk_db(self.db_path)
        m = Memory(db=db)
        m.record_alert(person_id=1, state="Fall", risk_score=0.6, priority="HIGH")
        row = db.get_last_alert_for_state(person_id=1, state="Fall")
        self.assertIsNotNone(row)
        self.assertEqual(row["type"], "memory_alert")
        self.assertEqual(row["payload"]["state"], "Fall")

    def test_t4_same_instance_cooldown(self):
        db = _mk_db(self.db_path)
        m = Memory(cooldown_minutes=30, db=db)
        m.record_alert(person_id=1, state="Fall")
        self.assertTrue(m.should_cooldown(1, "Fall"))

    def test_t5_cross_instance_restore(self):
        db = _mk_db(self.db_path)
        m1 = Memory(cooldown_minutes=30, db=db)
        m1.record_alert(person_id=1, state="Fall")
        # 重建 Memory(同一 SQLite)，无内存缓存 → 应从 events 恢复
        m2 = Memory(cooldown_minutes=30, db=db)
        self.assertTrue(m2.should_cooldown(1, "Fall"))

    def test_t6_cooldown_expired(self):
        db = _mk_db(self.db_path)
        m = Memory(cooldown_minutes=30, db=db)
        old = datetime.now() - timedelta(minutes=31)
        m.record_alert(person_id=1, state="Fall", timestamp=old)
        self.assertFalse(m.should_cooldown(1, "Fall"))

    def test_t7_different_person_independent(self):
        db = _mk_db(self.db_path)
        m = Memory(cooldown_minutes=30, db=db)
        m.record_alert(person_id=1, state="Fall")
        self.assertFalse(m.should_cooldown(2, "Fall"))

    def test_t8_different_state_independent(self):
        db = _mk_db(self.db_path)
        m = Memory(cooldown_minutes=30, db=db)
        m.record_alert(person_id=1, state="Fall")
        self.assertFalse(m.should_cooldown(1, "Lying"))

    def test_t9_digest_still_from_frames(self):
        m = Memory()
        m.remember_frame({"timestamp": datetime.now().isoformat(),
                          "persons": [{"person_id": 1, "state": "Fall"}]})
        d = m.digest(window_minutes=10)
        self.assertIsInstance(d, str)
        self.assertNotIn("SQLite", d.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)