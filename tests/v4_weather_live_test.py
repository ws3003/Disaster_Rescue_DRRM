"""V4.1 Phase5.1 天气 live provider 测试 (可选, 默认 skip)。

仅在显式设置 WEATHER_PROVIDER=live 且提供 WEATHER_API_KEY / WEATHER_BASE_URL 时才运行,
避免在无凭据/离线环境失败。离线回归永远走 mock。
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.weather_tool import WeatherTool  # noqa: E402

_LIVE = os.environ.get("WEATHER_PROVIDER", "").strip().lower() == "live"
_HAS_KEY = bool(os.environ.get("WEATHER_API_KEY", "").strip())
_HAS_URL = bool(os.environ.get("WEATHER_BASE_URL", "").strip())


@unittest.skipUnless(_LIVE and _HAS_KEY and _HAS_URL,
                     "需 WEATHER_PROVIDER=live + WEATHER_API_KEY + WEATHER_BASE_URL")
class WeatherLiveTest(unittest.TestCase):
    def test_live_returns_weather(self):
        t = WeatherTool(provider="live")
        r = t.run(action="get_current", lat=31.2304, lng=121.4737)
        self.assertTrue(r["ok"], msg=f"live 请求失败: {r.get('error')}")
        self.assertIn("provider", r["weather"])
        self.assertEqual(r["weather"]["provider"], "live")


if __name__ == "__main__":
    unittest.main(verbosity=2)