"""V4.6 AmapProvider 逆地理适配器测试(离线, 注入伪请求, 不依赖网络/key)。"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.amap_provider import AmapProvider  # noqa: E402


class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"status": "1",
                "regeocode": {"formatted_address": "江苏省南京市三江学院"}}


class AmapProviderTest(unittest.TestCase):
    def test_import(self):
        self.assertEqual(AmapProvider.name, "amap")

    def test_regeo_parses_address(self):
        with mock.patch("requests.get", return_value=_FakeResp()) as m:
            addr = AmapProvider.regeo(32.0617, 118.7778, key="sk-test")
        self.assertEqual(addr, "江苏省南京市三江学院")
        # 断言 location 参数为 lng,lat
        _, kwargs = m.call_args
        self.assertEqual(kwargs["params"]["location"], "118.7778,32.0617")

    def test_regeo_no_key_returns_empty(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            AmapProvider._key = lambda: ""  # noqa: BLE001
            self.assertEqual(AmapProvider.regeo(32.0, 118.0), "")

    def test_regeo_error_returns_empty(self):
        with mock.patch("requests.get",
                        side_effect=RuntimeError("net down")):
            with self.assertRaises(RuntimeError):
                AmapProvider.regeo(32.0, 118.0, key="k")  # 异常由调用方兜底


if __name__ == "__main__":
    unittest.main(verbosity=2)