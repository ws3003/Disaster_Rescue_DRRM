"""src/tools/location_tool.py — V4.5 设备位置获取抽象层。

作用: 统一提供"当前设备位置"。本阶段为 mock 实现(不接 GPS 硬件/网络/GPS/高德),
未来真实定位模块接入时, 仅替换内部实现, 接口不变。

结构(mock for now):
  LocationTool
    ├── MockLocationProvider(当前默认)
    ├── GPSProvider(未来)
    └── AmapProvider(未来)

约束:
  - 不读取数据库
  - 不调用网络 API
  - 不依赖 GPS 硬件 / 高德 / API key
"""

from __future__ import annotations

import os
from typing import Dict


class LocationTool:
    """设备位置抽象。get_current_location() 返回统一位置结构。"""

    name = "location_tool"

    def __init__(
        self,
        latitude: float = 32.0617,
        longitude: float = 118.7778,
        address: str = "三江学院附近",
    ):
        # mock 坐标/地址(南京三江学院附近, 示例)
        self._MOCK = {
            "latitude": latitude,
            "longitude": longitude,
            "address": address,
            "source": "mock",
        }

    def get_current_location(self) -> Dict[str, object]:
        """返回当前设备位置(接口不变)。

        结构:
            {"latitude", "longitude", "address", "source"}
        source: gps(GPS 坐标) / amap(仅高德富化地址 / GPS失败+mock坐标) / mock(兜底)。

        V4.7 优先真实 GPS(GPSProvider); GPS 失败回退 mock 坐标; 地址均由 Amap 富化,
        失败则保留 mock 地址。所有异常被吞掉, 不影响主流程。
        """
        lat, lng, gps_ok = self._coordinates()
        addr = self._amap_address(lat, lng)
        if gps_ok:
            return {"latitude": lat, "longitude": lng,
                    "address": addr, "source": "gps"}
        base = dict(self._MOCK)
        if addr:
            base["address"] = addr
            base["source"] = "amap"
        return base

    def _coordinates(self):
        """坐标来源: GPSProvider → {lat,lng,gps_ok} | mock 回退。"""
        try:
            from src.tools.gps_provider import GPSProvider
            g = GPSProvider().get_location()
            if isinstance(g, dict) and g.get("latitude") is not None \
                    and g.get("longitude") is not None:
                return g["latitude"], g["longitude"], True
        except Exception:  # noqa: BLE001
            pass
        return (self._MOCK["latitude"], self._MOCK["longitude"], False)

    @staticmethod
    def _amap_address(lat: float, lng: float) -> str:
        """高德逆地理富化地址; 无 key/失败 → ""。"""
        amap_key = os.environ.get("AMAP_API_KEY", "").strip()
        if not amap_key:
            return ""
        try:
            from src.tools.amap_provider import AmapProvider
            return AmapProvider.regeo(lat, lng, key=amap_key)
        except Exception:  # noqa: BLE001
            return ""


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(LocationTool().get_current_location(),
                      ensure_ascii=False, indent=2))