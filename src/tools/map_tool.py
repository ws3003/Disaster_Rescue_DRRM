"""src/tools/map_tool.py — 地图/GIS 信息工具 (V4.1 Assistant 信息辅助)。

定位: 信息类工具, 仅提供位置、路线、地理信息。
禁止: 自动调度 / 派遣 / 救援动作。

Phase 3 阶段: 使用 mock provider(零网络), 不接入真实地图 API。
真实接入预留: MAP_API_KEY 环境变量(不写死; 本阶段 mock 不使用)。

工具契约: 继承 src.tools.base.Tool, run(**kwargs) 分发 action:
  - "geocode"       地址 → {lat, lng, place}
  - "rescue_route" 起终点 → {distance_km, duration_min, summary}
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.tools.base import Tool

# 真实地图 API 接入时读取的 Key(本阶段 mock 不调用; 仅占位标注)
_MAP_API_KEY_ENV = "MAP_API_KEY"


class MapTool(Tool):
    """地图/地理位置查询(mock provider)。全程信息呈现, 不做任何调度。"""

    name = "map_tool"

    @property
    def description(self) -> str:
        return ("地图地理信息查询: geocode(地址定位) / rescue_route(路线建议)。"
                "仅提供位置与路线信息, 不自动调度、不派遣。")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["geocode", "rescue_route"]},
                "address": {"type": ["string"], "description": "geocode: 地址/地点"},
                "origin": {"type": ["string"], "description": "rescue_route: 起点"},
                "destination": {"type": ["string"], "description": "rescue_route: 终点"},
            },
            "additionalProperties": True,
        }

    def run(self, **kwargs: Any) -> Dict[str, Any]:
        action = kwargs.get("action")
        if action == "geocode":
            return self.geocode(address=kwargs.get("address"))
        if action == "rescue_route":
            return self.rescue_route(
                origin=kwargs.get("origin"),
                destination=kwargs.get("destination"),
            )
        return {"ok": False, "action": action or "?", "error": f"Unsupported map action: {action}"}

    def geocode(self, address: Optional[str] = None) -> Dict[str, Any]:
        """地址定位(mock)。真实接入时代理会使用 MAP_API_KEY 解析地址。"""
        try:
            place = (str(address).strip() if address else None) or "未知现场点"
            # mock 坐标(上海): 真实接入时从这里替换为地图服务结果
            location = {"lat": 31.2304, "lng": 121.4737, "place": place}
            return {"ok": True, "location": location, "provider": "mock"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "geocode", "error": str(exc)}

    def rescue_route(self, origin: Optional[str] = None,
                     destination: Optional[str] = None) -> Dict[str, Any]:
        """路线建议(mock)。仅给出距离/时长/概要文本, 不派单。"""
        try:
            o = str(origin or "现场")
            d = str(destination or "最近救援点")
            route = {
                "distance_km": 5.2,
                "duration_min": 15,
                "summary": f"{o} → {d}：沿主干道行驶，注意避开可能壅塞路段。",
                "provider": "mock",
            }
            return {"ok": True, "route": route}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "rescue_route", "error": str(exc)}


if __name__ == "__main__":
    t = MapTool()
    print(t.info())
    print("Key env 预留:", _MAP_API_KEY_ENV)
    print(t.run(action="geocode", address="上海中心"))
    print(t.run(action="rescue_route", origin="现场", destination="急救站"))