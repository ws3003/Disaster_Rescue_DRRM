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

from typing import Dict


class LocationTool:
    """设备位置抽象。get_current_location() 返回统一位置结构。"""

    name = "location_tool"

    # mock 坐标/地址(南京三江学院附近, 示例)
    _MOCK = {
        "latitude": 32.0617,
        "longitude": 118.7778,
        "address": "三江学院附近",
        "source": "mock",
    }

    def get_current_location(self) -> Dict[str, object]:
        """返回当前设备位置。

        结构:
            {"latitude", "longitude", "address", "source"}
        source 标识来源: mock(当前)。
        """
        return dict(self._MOCK)


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(LocationTool().get_current_location(),
                      ensure_ascii=False, indent=2))