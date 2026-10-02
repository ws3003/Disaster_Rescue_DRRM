"""V4.4 SceneContextTool 数据抽象层测试。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.scene_context_tool import SceneContextTool  # noqa: E402


class SceneContextToolTest(unittest.TestCase):
    def test_import(self):
        # import 成功即通过(顶部已 import)
        self.assertTrue(SceneContextTool)

    def test_get_current_context_returns_dict(self):
        ctx = SceneContextTool().get_current_context()
        self.assertIsInstance(ctx, dict)

    def test_contains_required_fields(self):
        ctx = SceneContextTool().get_current_context()
        for k in ("persons", "risk_summary", "events", "tasks", "image", "timestamp"):
            self.assertIn(k, ctx)
        # 类型
        self.assertIsInstance(ctx["persons"], list)
        self.assertIsInstance(ctx["risk_summary"], dict)
        self.assertIsInstance(ctx["events"], list)
        self.assertIsInstance(ctx["tasks"], list)
        self.assertIn("count", ctx["risk_summary"])
        self.assertIn("highest", ctx["risk_summary"])
        # timestamp 为 ISO 字符串
        self.assertIsInstance(ctx["timestamp"], str)
        self.assertTrue(ctx["timestamp"])


if __name__ == "__main__":
    unittest.main(verbosity=2)