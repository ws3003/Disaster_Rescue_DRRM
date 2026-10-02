"""V4.6 LocationTool + Amap 富化/回退测试(离线)。"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.location_tool import LocationTool  # noqa: E402


class LocationAmapTest(unittest.TestCase):
    def test_no_key_returns_mock(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            loc = LocationTool().get_current_location()
        for k in ("latitude", "longitude", "address", "source"):
            self.assertIn(k, loc)
        self.assertEqual(loc["source"], "mock")
        self.assertIn("三江学院附近", loc["address"])

    def test_amap_success_enrich_address(self):
        with mock.patch.dict(os.environ, {"AMAP_API_KEY": "sk-test"}):
            with mock.patch("src.tools.amap_provider.AmapProvider.regeo",
                            return_value="江苏省南京市三江学院"):
                loc = LocationTool().get_current_location()
        self.assertEqual(loc["source"], "amap")
        self.assertEqual(loc["address"], "江苏省南京市三江学院")
        self.assertEqual(loc["latitude"], 32.0617)   # 坐标不变
        self.assertEqual(loc["longitude"], 118.7778)

    def test_amap_failure_fallback_mock(self):
        with mock.patch.dict(os.environ, {"AMAP_API_KEY": "sk-test"}):
            with mock.patch("src.tools.amap_provider.AmapProvider.regeo",
                            side_effect=RuntimeError("net down")):
                loc = LocationTool().get_current_location()
        self.assertEqual(loc["source"], "mock")       # 异常吞掉, 回退 mock
        self.assertIn("三江学院附近", loc["address"])


if __name__ == "__main__":
    unittest.main(verbosity=2)