"""V4.4 阶段 A1 传感器基础模块单元测试（Windows Mock，无 GPIO/摄像头/DB/OSS）。"""

from __future__ import annotations

import importlib
import math

import pytest

from src.sensor.model import Reading
from src.sensor.reader import FakeReader, read_sample
from src.sensor.sensor_errors import SensorReadError
from src.sensor.validate import build_reading, is_valid_sample

TS = "2026-10-26T09:00:00+08:00"


def _pair(temp, humidity):
    return lambda: (temp, humidity)


class TestReadingModel:
    def test_frozen_fields(self):
        r = Reading(temperature_c=25.0, humidity_pct=40.0, captured_at=TS)
        assert r.temperature_c == 25.0
        assert r.humidity_pct == 40.0
        assert r.captured_at == TS
        with pytest.raises(Exception):
            r.temperature_c = 30.0  # frozen -> 不可变

    def test_humidity_none_allowed(self):
        r = Reading(temperature_c=25.0, humidity_pct=None, captured_at=TS)
        assert r.humidity_pct is None


class TestValidate:
    @pytest.mark.parametrize("kind", ["missing", "str", "nan", "posinf", "neginf", "bool"])
    def test_invalid(self, kind):
        val = {"missing": None, "str": "25", "nan": float("nan"),
               "posinf": math.inf, "neginf": -math.inf, "bool": True}[kind]
        # temperature 非法 → 拒绝
        assert is_valid_sample(val, None) is False

    @pytest.mark.parametrize("temp", [25.0, 25, -3.5, 1e-6])
    def test_valid_temperature(self, temp):
        assert is_valid_sample(temp, None) is True

    def test_humidity_optional(self):
        assert is_valid_sample(25.0, None) is True
        assert is_valid_sample(25.0, 40.0) is True

    def test_humidity_invalid(self):
        for bad in (True, float("nan"), math.inf, -math.inf, "40", [40]):
            assert is_valid_sample(25.0, bad) is False

    def test_repeat_consistent(self):
        a = is_valid_sample(25.0, 40.0)
        b = is_valid_sample(25.0, 40.0)
        assert a == b is True


class TestBuildReading:
    def test_valid_build(self):
        r = build_reading(25.0, 40.0, TS)
        assert r.temperature_c == 25.0 and r.humidity_pct == 40.0 and r.captured_at == TS

    def test_invalid_raises_no_reading(self):
        with pytest.raises(SensorReadError):
            build_reading(None, None, TS)
        with pytest.raises(SensorReadError):
            build_reading(float("nan"), 40.0, TS)
        with pytest.raises(SensorReadError):
            build_reading(25.0, True, TS)


class TestReader:
    def test_read_success(self):
        r = read_sample(_pair(25.0, 40.0), now=lambda: TS)
        assert isinstance(r, Reading)
        assert r.captured_at == TS

    def test_clock_injected(self):
        r = FakeReader(_pair(25.0, 40.0)).read(now=lambda: TS)
        assert r.captured_at == TS

    def test_default_clock_has_timezone(self):
        r = read_sample(_pair(25.0, 40.0))
        assert ("+" in r.captured_at) or r.captured_at.endswith("Z")

    def test_driver_exception_wrapped_with_cause(self):
        def boom():
            raise OSError("i2c failed")

        with pytest.raises(SensorReadError) as ei:
            read_sample(boom, now=lambda: TS)
        assert isinstance(ei.value.__cause__, OSError)

    def test_sensor_read_error_repropagates(self):
        def raise_own():
            raise SensorReadError("already failed")

        with pytest.raises(SensorReadError):
            read_sample(raise_own, now=lambda: TS)

    def test_validation_failure_no_reading(self):
        with pytest.raises(SensorReadError):
            read_sample(_pair(None, 40.0), now=lambda: TS)


class TestImportSafety:
    def test_import_no_hardware_side_effect(self):
        # reader 仅标准库；Windows 无 adafruit/board/GPIO 也可导入
        mod = importlib.import_module("src.sensor.reader")
        assert mod is not None
        # 顶层不应初始化任何硬件库
        for forbidden in ("adafruit", "board", "GPIO"):
            assert not hasattr(mod, forbidden)


class TestRangeValidation:
    """E7-02 读数量程（A1）：边界有效、越界无效、可配置。"""

    def test_default_boundaries_valid(self):
        assert is_valid_sample(-40.0, 0.0)
        assert is_valid_sample(85.0, 100.0)
        assert is_valid_sample(0.0, 50.0)

    def test_out_of_range_invalid(self):
        assert not is_valid_sample(-40.01, None)
        assert not is_valid_sample(85.01, None)
        assert not is_valid_sample(50.0, -0.01)
        assert not is_valid_sample(50.0, 100.01)

    def test_nan_inf_invalid(self):
        assert not is_valid_sample(float("nan"), None)
        assert not is_valid_sample(float("inf"), None)
        assert not is_valid_sample(20.0, float("-inf"))

    def test_custom_limits_change_result(self):
        assert is_valid_sample(90.0, None, temperature_max=100.0)
        assert not is_valid_sample(101.0, None, temperature_max=100.0)

    def test_humidity_none_valid(self):
        assert is_valid_sample(25.0, None)

    def test_build_reading_defers_range_to_loop(self):
        # Reader 层不预判配置量程：有限越界值仍构造 Reading；
        # 量程由 SensorLoop（is_valid_sample + SensorConfig）统一把关。
        r = build_reading(120.0, None, TS)
        assert r.temperature_c == 120.0
        assert is_valid_sample(r.temperature_c, None) is False  # 越界在业务层被拒

    def test_build_reading_rejects_non_finite(self):
        from src.sensor.validate import is_finite_sample
        assert is_finite_sample(25.0, None) is True
        assert is_finite_sample(float("nan"), None) is False
        assert is_finite_sample(float("inf"), 40.0) is False
        assert is_finite_sample("25", None) is False  # 类型非法（字符串）
        with pytest.raises(SensorReadError):
            build_reading(float("nan"), None, TS)
