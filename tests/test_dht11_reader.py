"""V4.4 A2 真实 DHT11 适配器 单元测试（Windows Mock，注入驱动，无真实硬件）。"""

from __future__ import annotations

import builtins
import importlib

import pytest

from src.sensor.dht11_reader import DHT11Reader
from src.sensor.model import Reading
from src.sensor.sensor_errors import SensorReadError

TS = "2026-10-26T09:00:00+08:00"


class _FakeDriver:
    """模拟 adafruit_dht 驱动对象（属性 .temperature/.humidity可抛异常）。"""

    def __init__(self, temp=None, humid=None):
        self._t = temp
        self._h = humid

    @property
    def temperature(self):
        if isinstance(self._t, Exception):
            raise self._t
        return self._t

    @property
    def humidity(self):
        if isinstance(self._h, Exception):
            raise self._h
        return self._h


class TestDHT11Reader:
    def test_valid_reading(self):
        r = DHT11Reader(driver_factory=lambda: _FakeDriver(25.0, 40.0))
        rd = r.read(now=lambda: TS)
        assert isinstance(rd, Reading)
        assert rd.temperature_c == 25.0
        assert rd.humidity_pct == 40.0
        assert rd.captured_at == TS

    def test_humidity_none(self):
        r = DHT11Reader(driver_factory=lambda: _FakeDriver(25.0, None))
        rd = r.read(now=lambda: TS)
        assert rd.humidity_pct is None

    def test_invalid_temp_rejected(self):
        r = DHT11Reader(driver_factory=lambda: _FakeDriver(None, 40.0))
        with pytest.raises(SensorReadError):
            r.read(now=lambda: TS)

    def test_invalid_temp_type_rejected(self):
        r = DHT11Reader(driver_factory=lambda: _FakeDriver("25", 40.0))
        with pytest.raises(SensorReadError):
            r.read(now=lambda: TS)

    def test_driver_exception_cause_preserved(self):
        r = DHT11Reader(
            driver_factory=lambda: _FakeDriver(OSError("bus time out"), 40.0)
        )
        with pytest.raises(SensorReadError) as ei:
            r.read(now=lambda: TS)
        assert isinstance(ei.value.__cause__, OSError)

    def test_dependency_missing_no_fake_fallback(self, monkeypatch):
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "adafruit_dht":
                raise ImportError("no adafruit_dht")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        r = DHT11Reader(pin=17)  # pin 值在依赖缺失时不会被使用
        with pytest.raises(SensorReadError) as ei:
            r.read(now=lambda: TS)
        assert "依赖缺失" in str(ei.value)

    def test_pin_not_set_when_dependency_present(self, monkeypatch):
        # 依赖已"存在"但未给 pin → 报"未指定引脚"而非初始化 GPIO
        real_import = builtins.__import__

        class _FakeDHTModule:
            class DHT11:
                def __init__(self, pin):
                    if pin is None:
                        raise AssertionError("已给 pin")

        def fake_import(name, *a, **k):
            if name == "adafruit_dht":
                return _FakeDHTModule
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        r = DHT11Reader(pin=None)
        with pytest.raises(SensorReadError) as ei:
            r.read(now=lambda: TS)
        assert "引脚" in str(ei.value)


class TestImportSafety:
    def test_import_has_no_hardware_side_effect(self):
        mod = importlib.import_module("src.sensor.dht11_reader")
        assert mod is not None
        # 导入仅定义类; 不初始化 GPIO/设备/读数
        assert not hasattr(mod, "adafruit_dht")
        assert not hasattr(mod, "board")
        assert not hasattr(mod, "GPIO")

    def test_construct_does_not_touch_hardware(self):
        DHT11Reader()  # 仅构造, 不应创建设备/读取
        DHT11Reader(pin=None)