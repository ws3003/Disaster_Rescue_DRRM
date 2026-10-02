"""V4.7 LocationTool + GPS 优先/回退/富化测试(离线)。"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.location_tool import LocationTool  # noqa: E402

_MOCK_GPS = {"latitude": 31.2304, "longitude": 121.4737}


class LocationGpsTest(unittest.TestCase):
    def _gps(self, ret):
        return mock.patch("src.tools.gps_provider.GPSProvider.get_location", return_value=ret)

    def _amap(self, addr):
        if addr is None:
            return mock.patch("src.tools.amap_provider.AmapProvider.regeo",
                              side_effect=RuntimeError("boom"))
        return mock.patch("src.tools.amap_provider.AmapProvider.regeo", return_value=addr)

    def test_gps_success_source_gps(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self._gps(dict(_MOCK_GPS)):
                loc = LocationTool().get_current_location()
        self.assertEqual(loc["source"], "gps")
        self.assertEqual(loc["latitude"], 31.2304)

    def test_gps_fail_fallback_mock(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self._gps({}):
                loc = LocationTool().get_current_location()
        self.assertEqual(loc["source"], "mock")
        self.assertIn("三江学院附近", loc["address"])

    def test_gps_success_plus_amap_address(self):
        with mock.patch.dict(os.environ, {"AMAP_API_KEY": "sk-test"}):
            with self._gps(dict(_MOCK_GPS)):
                with self._amap("江苏省南京市三江学院"):
                    loc = LocationTool().get_current_location()
        self.assertEqual(loc["source"], "gps")
        self.assertEqual(loc["address"], "江苏省南京市三江学院")
        self.assertEqual(loc["latitude"], 31.2304)

    def test_gps_fail_amap_success_source_amap(self):
        with mock.patch.dict(os.environ, {"AMAP_API_KEY": "sk-test"}):
            with self._gps({}):                       # GPS 失败 → mock 坐标
                with self._amap("江苏省南京市三江学院"):
                    loc = LocationTool().get_current_location()
        self.assertEqual(loc["source"], "amap")       # mock 坐标 + 高德地址
        self.assertEqual(loc["address"], "江苏省南京市三江学院")


if __name__ == "__main__":
    unittest.main(verbosity=2)