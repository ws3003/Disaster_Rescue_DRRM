"""V4.3 Voice/Assistant 现场视觉状态接入测试(离线)。"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.assistant.responder import AssistantResponder  # noqa: E402
from src.assistant.context import AssistantContext  # noqa: E402


def _mk(db, snap, vision, persons):
    for p in (db, snap, vision):
        try:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, "
                  "trigger_event TEXT, plan_trace TEXT, status TEXT, created TEXT, finished TEXT)")
    import json
    Path(vision).write_text(json.dumps({"ts": "2026", "persons": persons}), encoding="utf-8")


class SceneVisionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "c.db")
        self.snap = os.path.join(self._tmp.name, "s"); os.makedirs(self.snap, exist_ok=True)
        self.vision = os.path.join(self._tmp.name, "v.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _res(self, persons):
        _mk(self.db, self.snap, self.vision, persons)
        return AssistantResponder(context=AssistantContext(
            db_path=self.db, snapshot_dir=self.snap, vision_path=self.vision))

    def test_scene_uses_vision(self):
        out = self._res([{"id": 1, "state": "Fall", "priority": "HIGH", "risk_score": 0.92}]
                        ).answer("现场情况怎么样")
        self.assertEqual(out["intent"], "scene_status")
        self.assertIn("1 名人员", out["answer"])
        self.assertIn("Fall", out["answer"])
        self.assertIn("HIGH", out["answer"])
        self.assertIn("风险分数 0.92", out["answer"])

    def test_scene_fallback_when_no_vision(self):
        # persons 为空 → 不回退? _answer_scene 仅当 persons 非空才用视觉; 空则回退历史
        out = self._res([]).answer("现场情况怎么样")
        self.assertIn("intent", out)  # 正常返回(历史/兜底), 不崩溃


if __name__ == "__main__":
    unittest.main(verbosity=2)