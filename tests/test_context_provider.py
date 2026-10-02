"""V4.4 ContextProvider 桥接层测试。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.context_provider import ContextProvider  # noqa: E402


class ContextProviderTest(unittest.TestCase):
    def test_import(self):
        self.assertTrue(ContextProvider)

    def test_returns_dict(self):
        out = ContextProvider.get_scene_context()
        self.assertIsInstance(out, dict)

    def test_scene_context_present(self):
        out = ContextProvider.get_scene_context()
        self.assertIn("scene_context", out)
        sc = out["scene_context"]
        self.assertIsInstance(sc, dict)

    def test_persons_field_present(self):
        sc = ContextProvider.get_scene_context()["scene_context"]
        self.assertIn("persons", sc)
        self.assertIsInstance(sc["persons"], list)
        # 其余统一字段也在
        for k in ("risk_summary", "events", "tasks"):
            self.assertIn(k, sc)


if __name__ == "__main__":
    unittest.main(verbosity=2)