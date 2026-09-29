"""src/tools/weather_tool.py — 天气信息工具 (V4.1 Assistant 信息辅助)。

定位: 信息类工具, 仅查询并返回现场天气; 不作为风险评分因素, 不触发任何动作。

Provider 架构 (Phase5.1):
  - mock  默认, 零网络, 返回固定天气(离线/测试/CI)
  - live  真实天气 API, 惰性 requests, 由环境变量驱动

环境变量:
  WEATHER_PROVIDER   选择器: mock(默认) / live
  WEATHER_API_KEY    真实天气 key(live 必需)
  WEATHER_BASE_URL   天气 API 端点(live 必需)

接口不变: get_current(lat, lng) -> {ok, weather:{condition,temperature,wind,humidity,timestamp,provider}}
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

from src.tools.base import Tool

_WEATHER_PROVIDER_ENV = "WEATHER_PROVIDER"
_WEATHER_API_KEY_ENV = "WEATHER_API_KEY"
_WEATHER_BASE_URL_ENV = "WEATHER_BASE_URL"


class WeatherTool(Tool):
    """现场天气信息查询 (mock / live 双 provider)。"""

    name = "weather_tool"

    SUPPORTED_PROVIDERS = ("mock", "live")

    def __init__(self, provider: Optional[str] = None):
        """provider 缺省读 WEATHER_PROVIDER 环境变量, 再缺省 "mock"。"""
        if provider is None:
            provider = os.environ.get(_WEATHER_PROVIDER_ENV, "mock")
        provider = str(provider or "mock").lower()
        if provider not in self.SUPPORTED_PROVIDERS:
            raise ValueError(
                f"unsupported weather provider: {provider!r}, "
                f"support: {self.SUPPORTED_PROVIDERS}"
            )
        self.provider = provider

    @property
    def description(self) -> str:
        return ("查询现场天气信息: current(获取当前天气: 状况/温度/风力/湿度)。"
                "信息类, 不作为风险因素, 不触发动作。")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get_current"]},
                "lat": {"type": ["number", "string"], "description": "纬度(可选,mock 忽略)"},
                "lng": {"type": ["number", "string"], "description": "经度(可选,mock 忽略)"},
            },
            "additionalProperties": True,
        }

    def run(self, **kwargs: Any) -> Dict[str, Any]:
        action = kwargs.get("action")
        if action == "get_current":
            return self.get_current(
                lat=kwargs.get("lat"),
                lng=kwargs.get("lng"),
            )
        return {"ok": False, "action": action or "?",
                "error": f"Unsupported weather action: {action}"}

    # ------------------------------------------------------------ 对外入口(接口不变)
    def get_current(self, lat: Optional[Any] = None, lng: Optional[Any] = None) -> Dict[str, Any]:
        if self.provider == "live":
            return self._get_live(lat, lng)
        return self._get_mock(lat, lng)

    # ------------------------------------------------------------ mock
    def _get_mock(self, lat: Optional[Any] = None, lng: Optional[Any] = None) -> Dict[str, Any]:
        try:
            weather = {
                "condition": "晴",
                "temperature": 25,
                "wind": "3级",
                "humidity": 50,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "provider": "mock",
                "lat": lat,
                "lng": lng,
            }
            return {"ok": True, "weather": weather, "provider": self.provider}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "get_current", "error": str(exc)}

    # ------------------------------------------------------------ live
    def _get_live(self, lat: Optional[Any] = None, lng: Optional[Any] = None) -> Dict[str, Any]:
        try:
            key = os.environ.get(_WEATHER_API_KEY_ENV, "").strip()
            base = os.environ.get(_WEATHER_BASE_URL_ENV, "").strip()
            if not key or not base:
                return {"ok": False, "action": "get_current",
                        "error": f"{_WEATHER_API_KEY_ENV} / {_WEATHER_BASE_URL_ENV} 未配置"}
            import requests  # 惰性加载, 不影响 mock / 启动

            params: Dict[str, Any] = {"key": key}
            if lat is not None:
                params["lat"] = lat
            if lng is not None:
                params["lng"] = lng
            resp = requests.get(base, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            weather = self._normalize_live(data)
            weather["provider"] = "live"
            weather["timestamp"] = datetime.now().isoformat(timespec="seconds")
            return {"ok": True, "weather": weather, "provider": self.provider}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "get_current", "error": str(exc)}

    @staticmethod
    def _normalize_live(data: Dict[str, Any]) -> Dict[str, Any]:
        """尽力归一化常见天气 API 结构为统一字段(某些字段可能缺失)。"""
        def _extract(obj, *keys):
            for k in keys:
                v = obj.get(k) if isinstance(obj, dict) else None
                if v is not None:
                    return v
            return None

        now = data.get("now") if isinstance(data.get("now"), dict) else {}
        current = data.get("current") if isinstance(data.get("current"), dict) else {}
        main = data.get("main") if isinstance(data.get("main"), dict) else {}
        cond0 = data.get("condition") if isinstance(data.get("condition"), dict) else {}
        weather_list = data.get("weather")
        w0 = weather_list[0] if isinstance(weather_list, list) and weather_list else {}

        temp = (_extract(now, "temp")
                or _extract(current, "temp_c")
                or main.get("temp"))
        condition = (_extract(now, "text")
                     or (cond0.get("text")
                         or _extract(current, "condition"))
                     or w0.get("main")
                     or w0.get("description"))
        wind = (_extract(now, "windScale")
                or _extract(current, "wind_kph")
                or _extract(current, "wind"))
        humidity = (_extract(now, "humidity")
                    or main.get("humidity"))

        return {
            "condition": condition,
            "temperature": temp,
            "wind": wind,
            "humidity": humidity,
        }


if __name__ == "__main__":
    t = WeatherTool()
    print(t.info())
    print("Provider(=mock 若未 set):", t.provider)
    print("live 需 env:", _WEATHER_PROVIDER_ENV, _WEATHER_API_KEY_ENV, _WEATHER_BASE_URL_ENV)
    print(t.run(action="get_current", lat=31.2, lng=121.5))