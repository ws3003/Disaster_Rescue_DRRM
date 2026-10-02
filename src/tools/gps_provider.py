"""src/tools/gps_provider.py — V4.7 真实 GPS 读取 (NMEA → lat/lng)。

职责: 从串口 GPS 设备读取 NMEA 并解析出设备坐标。只负责坐标; 地址富化由
LocationTool→AmapProvider 完成。

支持 GPGGA / GPRMC; 无效定位/坏 NMEA/设备异常一律返回 {}, 禁止抛异常。
遵循: pyserial 惰性 import, 单次读取, timeout 控制, 不开后台线程。

配置: 环境变量 GPS_DEVICE(可选, 默认 /dev/ttyUSB0), 不提交真实设备信息。
"""

from __future__ import annotations

import os
from typing import Dict, Optional

_GPS_DEVICE_ENV = "GPS_DEVICE"
_DEFAULT_DEVICE = "/dev/ttyUSB0"
_DEFAULT_TIMEOUT = 2.0


def _nmea_to_deg(value: str) -> Optional[float]:
    """NMEA ddmm.mmmm → 十进制度数。"""
    if not value:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    deg = int(f // 100)
    minutes = f - deg * 100
    return deg + minutes / 60.0


def _convert(lat_str: str, ns: str, lon_str: str, ew: str):
    """坐标转换; 南纬/西经置负。解析失败返回 (None,None)。"""
    lat = _nmea_to_deg(lat_str)
    lon = _nmea_to_deg(lon_str)
    if lat is None or lon is None:
        return None, None
    if ns.strip().upper() == "S":
        lat = -lat
    if ew.strip().upper() == "W":
        lon = -lon
    return lat, lon


class GPSProvider:
    """单次读取 GPS 设备, 返回 {latitude, longitude, source:"gps"} 或 {}。"""

    def __init__(self, device: Optional[str] = None, timeout: float = _DEFAULT_TIMEOUT):
        self.device = device or os.environ.get(_GPS_DEVICE_ENV, "").strip() or _DEFAULT_DEVICE
        self.timeout = timeout

    def get_location(self) -> Dict[str, object]:
        """读取并解析一条有效 NMEA。失败/无定位/异常 → {}。禁止抛异常。"""
        try:
            import serial  # 惰性 import

            with serial.Serial(self.device, baudrate=9600, timeout=self.timeout) as s:
                for _ in range(200):  # 限次数读取, 避免无限等待
                    try:
                        raw = s.readline()
                        line = raw.decode("ascii", errors="ignore").strip()
                    except Exception:  # noqa: BLE001
                        line = ""
                    loc = self.parse(line)
                    if loc:
                        return loc
        except Exception:  # noqa: BLE001 — 设备不存在/权限/打开失败
            pass
        return {}

    @staticmethod
    def parse(line: str) -> Dict[str, object]:
        """解析单行 NMEA; 返回坐标 dict 或 {}。含校验和校验。"""
        line = (line or "").strip()
        if not line.startswith("$"):
            return {}
        body = line
        if "*" in line:
            body, cs = line.rsplit("*", 1)
            calc = 0
            for ch in body[1:]:
                calc ^= ord(ch)
            if cs.strip().upper() != "%02X" % calc:
                return {}
        fields = body.split(",")
        if len(fields) < 7:
            return {}
        kind = fields[0]
        if kind == "$GPGGA":
            quality = fields[6] if len(fields) > 6 else ""
            if not quality or str(quality).strip() == "0":  # 无定位
                return {}
            lat, lon = _convert(fields[2], fields[3], fields[4], fields[5])
            if lat is None or lon is None:
                return {}
            return {"latitude": lat, "longitude": lon}
        if kind == "$GPRMC":
            status = fields[2] if len(fields) > 2 else ""
            if str(status).strip().upper() != "A":  # 无效定位
                return {}
            lat, lon = _convert(fields[3], fields[4], fields[5], fields[6])
            if lat is None or lon is None:
                return {}
            return {"latitude": lat, "longitude": lon}
        return {}


if __name__ == "__main__":
    print("GPSProvider device:", GPSProvider().device)