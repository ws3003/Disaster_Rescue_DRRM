"""src/tools/weather_tool.py — 天气信息工具 (V4.1 Assistant 信息辅助)。

定位: 信息类工具, 仅查询并返回现场天气; 不作为风险评分因素, 不触发任何动作。
Phase 2 阶段: 使用 mock provider(零网络), 不接入真实天气 API。
真实接入预留: WEATHER_API_KEY 环境变量(不写死; 本阶段 mock 不使用)。

工具契约: 继承 src.tools.base.Tool, run(**kwargs) 分发 action="get_current"。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

from src.tools.base import Tool

# 真实天气 API 接入时读取的 Key(本阶段 mock 不调用; 仅占位标注, 不 fetch 网络)
_WEATHER_API_KEY_ENV = "WEATHER_API_KEY"


class WeatherTool(Tool):
    """现场天气信息查询(mock provider)。"""

    name = "weather_tool"

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

    def get_current(self, lat: Optional[Any] = None, lng: Optional[Any] = None) -> Dict[str, Any]:
        """返回当前天气(mock)。真实接入时代理会解析 WEATHER_API_KEY 并发起请求。"""
        try:
            weather = {
                "condition": "晴",
                "temperature": 25,
                "wind": "3级",
                "humidity": 50,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "source": "mock",
                "lat": lat,
                "lng": lng,
            }
            return {"ok": True, "weather": weather, "provider": "mock"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "get_current", "error": str(exc)}


if __name__ == "__main__":
    t = WeatherTool()
    print(t.info())
    print("Key env 预留:", _WEATHER_API_KEY_ENV)
    print(json := t.run(action="get_current", lat=31.2, lng=121.5))