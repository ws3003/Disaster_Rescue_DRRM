"""E7-01 DS18B20Reader 单元测试（A2）。

用临时目录 + monkeypatch 将默认设备目录指向临时路径，模拟 w1_slave 内容；
不连接真实 Raspberry Pi / 不访问 /sys/bus/w1。
覆盖：正常 CRC+温度、CRC 错、缺目录、缺文件、缺字段、格式错、指定设备、自动发现、摄氏度换算。
"""

from __future__ import annotations

import os

import pytest

from src.sensor.ds18b20_reader import DEFAULT_W1_DIR, DS18B20Reader
from src.sensor.sensor_errors import SensorReadError

TS = "2026-10-26T09:00:00+08:00"


def _write_slave(root, device_id, first, second):
    dev = root / device_id
    dev.mkdir()
    slave = dev / "w1_slave"
    slave.write_text(f"{first}\n{second}\n", encoding="utf-8")
    return root


def _reader(root, device_id=None):
    return DS18B20Reader(device_dir=str(root), device_id=device_id)


class TestParse:
    def test_normal_crc_and_temp(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-0224e33f1e4e",
                     "6e 01 4b 46 7f ff 04 10 8b : crc=8b YES",
                     "6e 01 4b 46 7f ff 04 10 8b t=26750")
        r = _reader(root, device_id="28-0224e33f1e4e").read(now=lambda: TS)
        assert r.temperature_c == pytest.approx(26.75)
        assert r.humidity_pct is None
        assert r.captured_at == TS

    def test_negative_temperature(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-aaaa",
                     "ff ff ff ff ff ff ff ff ff : crc=ff YES",
                     "ff ff ff ff ff ff ff ff ff t=-5050")
        r = _reader(root, device_id="28-aaaa").read(now=lambda: TS)
        assert r.temperature_c == pytest.approx(-5.05)

    def test_crc_no_yes_rejected(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-bbbb",
                     "6e 01 4b 46 7f ff 04 10 8b : crc=00 NO",
                     "6e 01 4b 46 7f ff 04 10 8b t=26750")
        with pytest.raises(SensorReadError):
            _reader(root, device_id="28-bbbb").read(now=lambda: TS)

    def test_missing_t_field_rejected(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-cccc",
                     "6e 01 4b 46 7f ff 04 10 8b : crc=8b YES",
                     "6e 01 4b 46 7f ff 04 10 8b")
        with pytest.raises(SensorReadError):
            _reader(root, device_id="28-cccc").read(now=lambda: TS)

    def test_bad_t_value_rejected(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-dddd",
                     "6e 01 4b 46 7f ff 04 10 8b : crc=8b YES",
                     "6e 01 4b 46 7f ff 04 10 8b t=abc")
        with pytest.raises(SensorReadError):
            _reader(root, device_id="28-dddd").read(now=lambda: TS)


class TestDeviceLocate:
    def test_directory_missing(self, tmp_path):
        missing = str(tmp_path / "nope")
        with pytest.raises(SensorReadError):
            DS18B20Reader(device_dir=missing).read(now=lambda: TS)

    def test_specific_device_file_missing(self, tmp_path):
        root = tmp_path
        dev = root / "28-eeee"
        dev.mkdir()  # 目录在但无 w1_slave
        with pytest.raises(SensorReadError):
            _reader(root, device_id="28-eeee").read(now=lambda: TS)

    def test_autodiscover_first_device(self, tmp_path):
        root = tmp_path
        _write_slave(root, "28-1111",
                     "xx : crc=AA YES", "xx t=20000")
        _write_slave(root, "28-2222",
                     "xx : crc=AA YES", "xx t=30000")
        r = _reader(root).read(now=lambda: TS)  # 无 device_id → 自动发现
        assert r.temperature_c == pytest.approx(20.0)

    def test_no_device_found(self, tmp_path):
        root = tmp_path
        (root / "some-other").mkdir()
        with pytest.raises(SensorReadError):
            _reader(root).read(now=lambda: TS)