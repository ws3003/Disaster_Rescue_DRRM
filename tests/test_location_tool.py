"""V4.5 LocationTool 测试(离线, 不依赖网络/摄像头/Pi 硬件/API key)。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.location_tool import LocationTool  # noqa: E402


class LocationToolTest(unittest.TestCase):
    def test_import(self):
        self.assertTrue(LocationTool)

    def test_returns_dict(self):
        out = LocationTool().get_current_location()
        self.assertIsInstance(out, dict)

    def test_fields_present(self):
        out = LocationTool().get_current_location()
        for k in ("latitude", "longitude", "address", "source"):
            self.assertIn(k, out)
        self.assertIsInstance(out["latitude"], (int, float))
        self.assertIsInstance(out["longitude"], (int, float))

    def test_source_is_mock(self):
        out = LocationTool().get_current_location()
        self.assertEqual(out["source"], "mock")
        self.assertEqual(LocationTool().name, "location_tool")


if __name__ == "__main__":
    unittest.main(verbosity=2)