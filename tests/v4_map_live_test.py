"""V4.1 Phase5.2 地图 live provider 测试 (可选, 默认 skip)。

仅在显式设置 MAP_PROVIDER=live 且提供 MAP_API_KEY / MAP_GEOCODE_URL / MAP_ROUTE_URL 时运行,
避免在无凭据/离线环境失败。离线回归永远走 mock。
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.map_tool import MapTool  # noqa: E402

_LIVE = os.environ.get("MAP_PROVIDER", "").strip().lower() == "live"
_HAS_KEY = bool(os.environ.get("MAP_API_KEY", "").strip())
_HAS_GEO = bool(os.environ.get("MAP_GEOCODE_URL", "").strip())
_HAS_ROUTE = bool(os.environ.get("MAP_ROUTE_URL", "").strip())


@unittest.skipUnless(_LIVE and _HAS_KEY and _HAS_GEO and _HAS_ROUTE,
                     "需 MAP_PROVIDER=live + MAP_API_KEY + MAP_GEOCODE_URL + MAP_ROUTE_URL")
class MapLiveTest(unittest.TestCase):
    def test_live_geocode(self):
        t = MapTool(provider="live")
        r = t.run(action="geocode", address="上海市黄浦区")
        self.assertTrue(r["ok"], msg=f"live geocode 失败: {r.get('error')}")
        self.assertEqual(r["provider"], "live")
        for k in ("lat", "lng", "place"):
            self.assertIn(k, r["location"])

    def test_live_route(self):
        t = MapTool(provider="live")
        r = t.run(action="rescue_route", origin="上海市黄浦区", destination="上海市第一人民医院")
        self.assertTrue(r["ok"], msg=f"live route 失败: {r.get('error')}")
        self.assertEqual(r["provider"], "live")
        for k in ("distance_km", "duration_min", "summary"):
            self.assertIn(k, r["route"])


if __name__ == "__main__":
    unittest.main(verbosity=2)