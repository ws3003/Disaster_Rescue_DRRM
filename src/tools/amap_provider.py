"""src/tools/amap_provider.py — V4.6 高德逆地理编码适配器。

职责: 输入 latitude/longitude, 返回人类可读 address(如 "江苏省南京市…")。
仅用于 DingTalk 报警显示地址, 不负责风险判断 / Agent / 报警。

网络/异常原则:
  - 惰性加载 requests, 调用方(LocationTool)负责捕获异常并回退 mock。
  - 本类 only 发起逆地理请求并解析; 失败抛/返回空串由调用方兜底。

配置: 环境变量 AMAP_API_KEY(进程环境, 不自动加载 .env)。
"""

from __future__ import annotations

import os
from typing import Optional

_AMAP_API_KEY_ENV = "AMAP_API_KEY"
_AMAP_REGEO_URL = "https://restapi.amap.com/v3/geocode/regeo"
_AMAP_TIMEOUT = 10.0


class AmapProvider:
    """高德逆地理编码(批量/单点 regeo)。"""

    name = "amap"

    @staticmethod
    def _key() -> str:
        return os.environ.get(_AMAP_API_KEY_ENV, "").strip()

    @staticmethod
    def regeo(latitude: float, longitude: float,
              key: Optional[str] = None) -> str:
        """逆地理编码: (lat, lng) -> address; 未配置 key/请求失败 → ""。

        调用方负责异常捕获与兜底。
        """
        if key is None:
            key = AmapProvider._key()
        if not key:
            return ""
        import requests  # 惰性加载

        resp = requests.get(
            _AMAP_REGEO_URL,
            params={
                "key": key,
                "location": f"{longitude},{latitude}",  # 高德要求 lng,lat
                "extensions": "base",
            },
            timeout=_AMAP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get("status") != "1":
            return ""
        regeo = data.get("regeocode") or {}
        if not isinstance(regeo, dict):
            return ""
        return str(regeo.get("formatted_address") or "").strip()


if __name__ == "__main__":
    print("AmapProvider regeo 需 AMAP_API_KEY; key 已配置:", bool(AmapProvider._key()))