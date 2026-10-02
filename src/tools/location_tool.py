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
        """返回当前设备位置。

        结构:
            {"latitude", "longitude", "address", "source"}
        source: mock(兜底) / amap(高德逆地理富化)。

        V4.6: 若配置 AMAP_API_KEY, 尝试用 amap 逆地理把地址富化为真实地址;
        失败/无 key → 保持 mock 兜底。所有异常被吞掉, 不影响主流程。
        """
        base = dict(self._MOCK)
        amap_key = os.environ.get("AMAP_API_KEY", "").strip()
        if not amap_key:
            return base
        try:
            from src.tools.amap_provider import AmapProvider
            addr = AmapProvider.regeo(
                base["latitude"], base["longitude"], key=amap_key)
        except Exception:  # noqa: BLE001 — 逆地理失败回退 mock
            addr = ""
        if addr:
            base["address"] = addr
            base["source"] = "amap"
        return base


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(LocationTool().get_current_location(),
                      ensure_ascii=False, indent=2))