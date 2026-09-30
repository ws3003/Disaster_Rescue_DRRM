"""src/tools/map_tool.py — 地图/GIS 信息工具 (V4.1 Assistant 信息辅助)。

定位: 信息类工具, 仅提供位置、路线、地理信息。
禁止: 自动调度 / 派遣 / 救援动作; 只用于信息辅助回答。

Provider 架构 (Phase5.2):
  - mock  默认, 零网络, 返回固定位置/路线(离线/测试/CI)
  - live  真实地图 API, 惰性 requests, 由环境变量驱动(首选高德 AMap Web 服务)

环境变量:
  MAP_PROVIDER     选择器: mock(默认) / live
  MAP_API_KEY      地图服务 key(live 必需)
  MAP_GEOCODE_URL  地址解析端点(live 必需, 如 https://restapi.amap.com/v3/geocode/geo)
  MAP_ROUTE_URL    驾车路线端点(live 必需, 如 https://restapi.amap.com/v3/direction/driving)

接口不变: run / geocode / rescue_route.
  geocode(address)      -> {ok, location:{lat,lng,place}, provider}
  rescue_route(o,d)     -> {ok, route:{distance_km,duration_min,summary}, provider}
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from src.tools.base import Tool

_MAP_PROVIDER_ENV = "MAP_PROVIDER"
_MAP_API_KEY_ENV = "MAP_API_KEY"
_MAP_GEOCODE_URL_ENV = "MAP_GEOCODE_URL"
_MAP_ROUTE_URL_ENV = "MAP_ROUTE_URL"
_MAP_NEARBY_URL_ENV = "MAP_NEARBY_URL"
_MAP_NEARBY_DEFAULT_URL = "https://restapi.amap.com/v3/place/around"


class MapTool(Tool):
    """地图/地理位置查询 (mock / live 双 provider)。全程信息呈现, 不做调度。"""

    name = "map_tool"

    SUPPORTED_PROVIDERS = ("mock", "live")

    def __init__(self, provider: Optional[str] = None):
        """provider 缺省读 MAP_PROVIDER 环境变量, 再缺省 "mock"。"""
        if provider is None:
            provider = os.environ.get(_MAP_PROVIDER_ENV, "mock")
        provider = str(provider or "mock").lower()
        if provider not in self.SUPPORTED_PROVIDERS:
            raise ValueError(
                f"unsupported map provider: {provider!r}, "
                f"support: {self.SUPPORTED_PROVIDERS}"
            )
        self.provider = provider

    @property
    def description(self) -> str:
        return ("地图地理信息查询: geocode(地址定位) / rescue_route(路线建议)。"
                "仅提供位置与路线信息, 不自动调度、不派遣。")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["geocode", "rescue_route", "nearby"]},
                "address": {"type": ["string"], "description": "geocode: 地址/地点"},
                "origin": {"type": ["string"], "description": "rescue_route: 起点(坐标 或 地点)"},
                "destination": {"type": ["string"], "description": "rescue_route: 终点(坐标 或 地点)"},
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
        if action == "nearby":
            return self.nearby(
                keyword=kwargs.get("keywords", kwargs.get("keyword")),
                location=kwargs.get("location"),
            )
        return {"ok": False, "action": action or "?", "error": f"Unsupported map action: {action}"}

    # ------------------------------------------------------------ 对外入口(接口不变)
    def geocode(self, address: Optional[str] = None) -> Dict[str, Any]:
        if self.provider == "live":
            return self._geocode_live(address)
        return self._geocode_mock(address)

    def rescue_route(self, origin: Optional[str] = None,
                     destination: Optional[str] = None) -> Dict[str, Any]:
        if self.provider == "live":
            return self._route_live(origin, destination)
        return self._route_mock(origin, destination)

    def nearby(self, keyword: Optional[str] = None,
               location: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """周边设施 POI 查询（高德 /v3/place/around）。仅信息呈现, 不派单。"""
        if self.provider == "live":
            return self._nearby_live(keyword, location)
        return self._nearby_mock(keyword, location)

    # ------------------------------------------------------------ mock
    def _nearby_mock(self, keyword: Optional[str] = None,
                     location: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            kw = str(keyword or "医院")
            loc = location or {}
            poi = [
                {"name": f"模拟{kw}点A", "type": kw, "distance": "800米",
                 "address": "模拟地址1"},
                {"name": f"模拟{kw}点B", "type": kw, "distance": "1.2公里",
                 "address": "模拟地址2"},
            ]
            return {"ok": True, "poi": poi, "provider": self.provider}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "nearby", "error": str(exc)}

    # ------------------------------------------------------------ live (AMap /v3/place/around)
    def _nearby_live(self, keyword: Optional[str] = None,
                     location: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            key = os.environ.get(_MAP_API_KEY_ENV, "").strip()
            url = os.environ.get(_MAP_NEARBY_URL_ENV, "").strip() or _MAP_NEARBY_DEFAULT_URL
            if not key:
                return {"ok": False, "action": "nearby",
                        "error": f"{_MAP_API_KEY_ENV} 未配置"}
            loc = location or {}
            lat, lng = loc.get("lat"), loc.get("lng")
            if lat is None or lng is None:
                return {"ok": False, "action": "nearby", "error": "缺少 location{lat,lng}"}
            import requests  # 惰性加载

            params = {"key": key, "location": f"{lng},{lat}", "keywords": str(keyword or "")}
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            poi = self._norm_poi(resp.json())
            return {"ok": True, "poi": poi, "provider": self.provider}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "nearby", "error": str(exc)}

    # ------------------------------------------------------------ mock
    def _geocode_mock(self, address: Optional[str] = None) -> Dict[str, Any]:
        try:
            place = (str(address).strip() if address else None) or "未知现场点"
            location = {"lat": 31.2304, "lng": 121.4737, "place": place}
            return {"ok": True, "location": location, "provider": self.provider}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "geocode", "error": str(exc)}

    def _route_mock(self, origin: Optional[str] = None,
                    destination: Optional[str] = None) -> Dict[str, Any]:
        try:
            o = str(origin or "现场")
            d = str(destination or "最近救援点")
            route = {
                "distance_km": 5.2,
                "duration_min": 15,
                "summary": f"{o} → {d}：沿主干道行驶，注意避开可能壅塞路段。",
            }
            return {"ok": True, "route": route, "provider": self.provider}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "rescue_route", "error": str(exc)}

    # ------------------------------------------------------------ live (惰性 requests)
    def _geocode_live(self, address: Optional[str] = None) -> Dict[str, Any]:
        try:
            key = os.environ.get(_MAP_API_KEY_ENV, "").strip()
            url = os.environ.get(_MAP_GEOCODE_URL_ENV, "").strip()
            if not key or not url:
                return {"ok": False, "action": "geocode",
                        "error": f"{_MAP_API_KEY_ENV} / {_MAP_GEOCODE_URL_ENV} 未配置"}
            import requests  # 惰性加载, 不影响 mock / 启动

            resp = requests.get(url, params={"key": key, "address": address or ""}, timeout=10)
            resp.raise_for_status()
            location = self._norm_location(resp.json(), address)
            if location is None:
                return {"ok": False, "action": "geocode", "error": "地图服务未返回有效 geocode"}
            return {"ok": True, "location": location, "provider": self.provider}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "geocode", "error": str(exc)}

    def _route_live(self, origin: Optional[str] = None,
                    destination: Optional[str] = None) -> Dict[str, Any]:
        try:
            key = os.environ.get(_MAP_API_KEY_ENV, "").strip()
            url = os.environ.get(_MAP_ROUTE_URL_ENV, "").strip()
            if not key or not url:
                return {"ok": False, "action": "rescue_route",
                        "error": f"{_MAP_API_KEY_ENV} / {_MAP_ROUTE_URL_ENV} 未配置"}
            import requests  # 惰性加载

            o = str(origin or "现场")
            d = str(destination or "最近救援点")
            resp = requests.get(url, params={"key": key, "origin": o, "destination": d}, timeout=10)
            resp.raise_for_status()
            route = self._norm_route(resp.json(), o, d)
            if route is None:
                return {"ok": False, "action": "rescue_route", "error": "地图服务未返回有效路线"}
            return {"ok": True, "route": route, "provider": self.provider}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "rescue_route", "error": str(exc)}

    # ------------------------------------------------------------ 归一化(AMap 适配尽力而为)
    @staticmethod
    def _norm_location(data: Dict[str, Any], address: Optional[str]) -> Optional[Dict[str, Any]]:
        geocodes = data.get("geocodes") or []
        if not geocodes:
            return None
        g0 = geocodes[0] if isinstance(geocodes[0], dict) else {}
        loc = str(g0.get("location") or "")
        lat = lng = None
        if loc and "," in loc:
            lng_s, lat_s = loc.split(",", 1)
            try:
                lng = float(lng_s)
                lat = float(lat_s)
            except ValueError:
                pass
        place = g0.get("formatted_address") or address or "未知现场点"
        return {"lat": lat, "lng": lng, "place": str(place)}

    @staticmethod
    def _norm_route(data: Dict[str, Any], origin: str, destination: str) -> Optional[Dict[str, Any]]:
        route = data.get("route") if isinstance(data.get("route"), dict) else {}
        paths = route.get("paths") or []
        if not paths:
            return None
        p0 = paths[0] if isinstance(paths[0], dict) else {}
        distance_m = p0.get("distance")   # AMap V3: 米
        duration_s = p0.get("duration")   # 秒
        strategy = p0.get("strategy")
        distance_km = None
        duration_min = None
        if distance_m is not None:
            try:
                distance_km = round(float(distance_m) / 1000.0, 2)
            except (TypeError, ValueError):
                pass
        if duration_s is not None:
            try:
                duration_min = int(round(float(duration_s) / 60.0))
            except (TypeError, ValueError):
                pass
        summary = strategy or f"{origin} → {destination}"
        return {"distance_km": distance_km, "duration_min": duration_min,
                "summary": str(summary)}

    @staticmethod
    def _norm_poi(data: Dict[str, Any]) -> list:
        pois = data.get("pois") or []
        out = []
        for p in pois:
            if not isinstance(p, dict):
                continue
            name = p.get("name") or ""
            distance = p.get("distance")
            if distance is not None:  # 米 → 可读
                try:
                    d = float(distance)
                    distance = f"{int(d)}米" if d < 1000 else f"{round(d/1000, 1)}公里"
                except (TypeError, ValueError):
                    pass
            out.append({
                "name": name,
                "type": p.get("type"),
                "distance": distance,
                "address": p.get("address"),
            })
        return out


if __name__ == "__main__":
    t = MapTool()
    print(t.info())
    print("Provider(=mock 若未 set):", t.provider)
    print("live 需 env:", _MAP_PROVIDER_ENV, _MAP_API_KEY_ENV,
          _MAP_GEOCODE_URL_ENV, _MAP_ROUTE_URL_ENV)
    print(t.run(action="geocode", address="上海中心"))
    print(t.run(action="rescue_route", origin="现场", destination="急救站"))
