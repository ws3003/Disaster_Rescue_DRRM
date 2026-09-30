"""V4.1 AMap 路线坐标化测试 (可选 live + 离线 mock 恒绿)。

- mock: rescue_route("现场","医院") 应通过(坐标化不破坏 mock)。
- live: 仅 MAP_PROVIDER=live 且 MAP_API_KEY 存在才跑; 无凭据 skip。
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.map_tool import MapTool  # noqa: E402

_LIVE = os.environ.get("MAP_PROVIDER", "").strip().lower() == "live" \
    and bool(os.environ.get("MAP_API_KEY", "").strip())


class MapRouteMockTest(unittest.TestCase):
    def test_mock_route_still_works(self):
        m = MapTool()  # provider 默认 mock(无 MAP_PROVIDER)
        r = m.run(action="rescue_route", origin="现场", destination="医院")
        self.assertTrue(r["ok"])
        self.assertIn("distance_km", r["route"])
        self.assertIn("duration_min", r["route"])

    def test_is_coordinate(self):
        self.assertTrue(MapTool._is_coordinate("118.7969,32.0603"))
        self.assertFalse(MapTool._is_coordinate("南京市第一医院"))


@unittest.skipUnless(_LIVE, "需 MAP_PROVIDER=live + MAP_API_KEY")
class MapRouteLiveTest(unittest.TestCase):
    def test_live_route_coordinates(self):
        m = MapTool(provider="live")
        r = m.run(action="rescue_route", origin="南京市第一医院", destination="南京南站")
        self.assertTrue(r["ok"], msg=f"live route 失败: {r.get('error')}")
        self.assertIsNotNone(r["route"].get("distance_km"))
        self.assertIsNotNone(r["route"].get("duration_min"))


if __name__ == "__main__":
    unittest.main(verbosity=2)