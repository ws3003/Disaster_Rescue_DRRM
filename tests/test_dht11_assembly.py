"""V4.4 E7 改造：build_sensor_reader 的 DHT11 装配测试（Windows，无真实 GPIO）。"""

from __future__ import annotations

import pytest

from src.sensor.config import SensorConfig
from src.sensor.reader import FakeReader
from src.sensor.runner import build_sensor_reader
from src.sensor.sensor_errors import SensorReadError


def _cfg(**over):
    base = dict(
        sensor_id="dht11-01",
        temperature_threshold=40.0, sample_period_seconds=10.0,
        confirm_high_count=3, confirm_recovery_count=3, cooldown_seconds=600.0,
        reader="dht11", pin=4, read_retry_count=2,
    )
    base.update(over)
    return SensorConfig(**base)


def _ds18b20_cfg(**over):
    base = dict(
        sensor_id="ds18b20-01",
        temperature_threshold=40.0, sample_period_seconds=10.0,
        confirm_high_count=3, confirm_recovery_count=3, cooldown_seconds=600.0,
        reader="ds18b20", device_id="28-abc", device_dir="/sys/bus/w1/devices",
    )
    base.update(over)
    return SensorConfig(**base)


def _fake_cfg(**over):
    base = dict(
        sensor_id="s1",
        temperature_threshold=40.0, sample_period_seconds=10.0,
        confirm_high_count=3, confirm_recovery_count=3, cooldown_seconds=600.0,
        reader="fake",
    )
    base.update(over)
    return SensorConfig(**base)


class TestDHT11Assembly:
    def test_dht11_assembles_to_dht11_reader(self):
        reader = build_sensor_reader(_cfg())
        from src.sensor.dht11_reader import DHT11Reader
        assert isinstance(reader, DHT11Reader)

    def test_gpio_pin_passed_through(self):
        reader = build_sensor_reader(_cfg(pin=17))
        assert reader._pin == 17

    def test_retry_count_passed_through(self):
        reader = build_sensor_reader(_cfg(read_retry_count=3))
        assert reader._retry_count == 3
        # 默认 0（单次尝试），保持兼容
        assert build_sensor_reader(_cfg(read_retry_count=0))._retry_count == 0

    def test_assembly_does_not_touch_gpio(self):
        # 构造即装配，不触发驱动导入/引脚访问（Windows 无 adafruit 也可）
        from src.sensor import dht11_reader as m
        build_sensor_reader(_cfg())
        assert not hasattr(m, "adafruit_dht")


class TestExistingAssemblyPreserved:
    def test_ds18b20_still_assembles(self):
        from src.sensor.ds18b20_reader import DS18B20Reader
        reader = build_sensor_reader(_ds18b20_cfg())
        assert isinstance(reader, DS18B20Reader)

    def test_fake_requires_provider(self):
        with pytest.raises(SensorReadError):
            build_sensor_reader(_fake_cfg())

    def test_fake_with_provider_still_works(self):
        reader = build_sensor_reader(_fake_cfg(), provider=lambda: (25.0, 40.0))
        assert isinstance(reader, FakeReader)
        rd = reader.read()
        assert rd.temperature_c == 25.0 and rd.humidity_pct == 40.0