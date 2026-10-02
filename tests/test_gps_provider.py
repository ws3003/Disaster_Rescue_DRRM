"""V4.7 GPSProvider 测试(离线, 注入 NMEA, 不依赖真实设备)。"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.gps_provider import GPSProvider  # noqa: E402


class GpsProviderTest(unittest.TestCase):
    # 标准示例含正确校验和 *47
    _GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
    # GPRMC, status=A, 校验和 *6A
    _RMC = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"

    def test_gga_parse(self):
        r = GPSProvider.parse(self._GGA)
        self.assertEqual(round(r["latitude"], 4), round(48.1173, 4))   # 48+07.038/60
        self.assertEqual(round(r["longitude"], 6), round(11.516667, 6))  # 11+31/60

    def test_gga_no_quality_returns_empty(self):
        # quality=0 → 无定位 → {}
        r = GPSProvider.parse(
            "$GPGGA,123519,4807.038,N,01131.000,E,0,08,0.9,545.4,M,46.9,M,,*62")
        self.assertEqual(r, {})

    def test_rmc_parse(self):
        r = GPSProvider.parse(self._RMC)
        self.assertEqual(round(r["latitude"], 4), round(48.1173, 4))
        self.assertEqual(round(r["longitude"], 6), round(11.516667, 6))

    def test_bad_nmea_returns_empty(self):
        self.assertEqual(GPSProvider.parse("not a nmea"), {})
        self.assertEqual(GPSProvider.parse("$GPGGA,123"), {})       # 字段不足
        self.assertEqual(GPSProvider.parse("$GPGGA,,,,"), {})

    def test_checksum_error_returns_empty(self):
        bad = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*00"
        self.assertEqual(GPSProvider.parse(bad), {})

    def test_device_missing_returns_empty(self):
        # 设备不存在(且不依赖 pyserial 是否安装): 一律安全返回 {}
        self.assertEqual(GPSProvider(device="/definitely/missing/ttyUSB9",
                                     timeout=0.1).get_location(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)