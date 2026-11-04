"""V4.4 E7a config 模块单元测试。"""

from __future__ import annotations

import math
import os
import tempfile

import pytest

from src.sensor.config import (
    SensorConfig,
    SensorConfigError,
    load_sensor_config,
    sensor_config_from_dict,
    validate_config,
)


def _ok() -> dict:
    return {
        "sensor_id": "dht11-01",
        "temperature_threshold": 40.0,
        "sample_period_seconds": 5.0,
        "confirm_high_count": 2,
        "confirm_recovery_count": 2,
        "cooldown_seconds": 60.0,
    }


class TestConfig:
    def test_valid(self):
        cfg = sensor_config_from_dict(_ok())
        assert cfg.sensor_id == "dht11-01"
        assert validate_config(cfg) is cfg

    def test_missing_key(self):
        d = _ok(); d.pop("temperature_threshold")
        with pytest.raises(SensorConfigError):
            sensor_config_from_dict(d)

    @pytest.mark.parametrize("key,val", [
        ("temperature_threshold", True), ("temperature_threshold", float("nan")),
        ("temperature_threshold", -1.0), ("sample_period_seconds", -1.0),
        ("cooldown_seconds", -1.0), ("confirm_high_count", 0),
        ("confirm_recovery_count", 0), ("offline_after_failures", 0),
        ("temperature_threshold", "40"),
    ])
    def test_invalid(self, key, val):
        d = _ok(); d[key] = val
        with pytest.raises(SensorConfigError):
            sensor_config_from_dict(d)

    def test_yaml_load_ok(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "cfg.yaml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("sensor:\n" + "".join(f"  {k}: {v}\n" for k, v in _ok().items()))
            cfg = load_sensor_config(p)
            assert cfg.temperature_threshold == 40.0

    def test_yaml_missing_section(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "cfg.yaml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("other: 1\n")
            with pytest.raises(SensorConfigError):
                load_sensor_config(p)


class TestA1A2Config:
    """E7-01/02 新增可选配置（量程 + reader/device）。"""

    def _base(self):
        return {
            "sensor_id": "ds18b20-01", "temperature_threshold": 40.0,
            "sample_period_seconds": 10.0, "confirm_high_count": 3,
            "confirm_recovery_count": 3, "cooldown_seconds": 600.0,
        }

    def test_optional_range_and_reader_parsed(self):
        data = {**self._base(), "temperature_min": -20.0, "temperature_max": 60.0,
                "humidity_min": 5.0, "humidity_max": 95.0,
                "reader": "ds18b20", "device_id": "28-abc", "device_dir": "/sys/bus/w1/devices"}
        cfg = sensor_config_from_dict(data)
        assert cfg.temperature_min == -20.0 and cfg.temperature_max == 60.0
        assert cfg.humidity_min == 5.0 and cfg.humidity_max == 95.0
        assert cfg.reader == "ds18b20" and cfg.device_id == "28-abc"
        assert cfg.device_dir == "/sys/bus/w1/devices"

    def test_defaults_when_absent(self):
        cfg = sensor_config_from_dict(self._base())
        assert cfg.temperature_min == -40.0 and cfg.temperature_max == 85.0
        assert cfg.humidity_min == 0.0 and cfg.humidity_max == 100.0
        assert cfg.reader == "fake" and cfg.device_id is None

    def test_bad_range_rejected(self):
        with pytest.raises(SensorConfigError):
            sensor_config_from_dict({**self._base(), "temperature_min": 90.0, "temperature_max": 10.0})

    def test_bad_reader_rejected(self):
        with pytest.raises(SensorConfigError):
            sensor_config_from_dict({**self._base(), "reader": "sht31"})

    def test_edge_yaml_loads_new_fields(self):
        # 确保集成的 config/edge.yaml 可被现有加载器解析（含新增字段）
        cfg = load_sensor_config("config/edge.yaml")
        assert cfg.reader == "ds18b20"
        assert cfg.device_id == "28-0224e33f1e4e"
        assert int(cfg.temperature_max) == 85


class TestDHT11Config:
    """V4.4 E7 改造：DHT11 + GPIO pin 配置解析与校验。"""

    def _dht(self, **extra):
        d = {
            "sensor_id": "dht11-01", "temperature_threshold": 40.0,
            "sample_period_seconds": 10.0, "confirm_high_count": 3,
            "confirm_recovery_count": 3, "cooldown_seconds": 600.0,
            "reader": "dht11", "pin": 4,
        }
        d.update(extra)
        return d

    def test_dht11_with_pin_parsed(self):
        cfg = sensor_config_from_dict(self._dht())
        assert cfg.reader == "dht11"
        assert cfg.pin == 4

    def test_dht11_missing_pin_rejected(self):
        with pytest.raises(SensorConfigError):
            sensor_config_from_dict(self._dht(pin=None))

    def test_dht11_pin_absent_key_rejected(self):
        d = self._dht(); d.pop("pin")
        with pytest.raises(SensorConfigError):
            sensor_config_from_dict(d)

    @pytest.mark.parametrize("bad", ["17", True, 0, -1, 4.0, 3.5])
    def test_dht11_invalid_pin_rejected(self, bad):
        with pytest.raises(SensorConfigError):
            sensor_config_from_dict(self._dht(pin=bad))

    def test_ds18b20_allows_pin_none(self):
        # 原有 DS18B20 配置不受 pin 字段影响（pin 仅 dht11 必填）
        base = {
            "sensor_id": "ds18b20-01", "temperature_threshold": 40.0,
            "sample_period_seconds": 10.0, "confirm_high_count": 3,
            "confirm_recovery_count": 3, "cooldown_seconds": 600.0,
            "reader": "ds18b20", "device_id": "28-abc",
        }
        cfg = sensor_config_from_dict(base)
        assert cfg.pin is None
        assert cfg.reader == "ds18b20"

    def test_fake_allows_pin_none(self):
        # 默认 fake 仍可无 pin 通过，且不带 pin
        cfg = sensor_config_from_dict(TestA1A2Config()._base())
        assert cfg.reader == "fake"
        assert cfg.pin is None

    def test_unknown_reader_still_rejected(self):
        with pytest.raises(SensorConfigError):
            sensor_config_from_dict(self._dht(reader="sht31"))
