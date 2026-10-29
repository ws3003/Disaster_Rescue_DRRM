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