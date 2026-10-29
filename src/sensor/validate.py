"""传感器样本校验（阶段 A1）。

依据 SRS REQ-E7-02（无效读数不得生成告警/存储）。
不硬编码 DHT11 量程/告警阈值（未经确认）；量程硬边界不在 A1 实现。
"""

from __future__ import annotations

import math
from typing import Any

from src.sensor.model import Reading
from src.sensor.sensor_errors import SensorReadError


def _is_finite_number(value: Any) -> bool:
    """数值必须为 int/float、非 bool、有限。"""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def is_valid_sample(temperature: Any, humidity: Any) -> bool:
    """判断一次样本是否有效。

    - temperature 必填且为有限数值（排除 bool / NaN / ±inf）。
    - humidity 可为 None；若提供则须为有限数值。
    """
    if not _is_finite_number(temperature):
        return False
    if humidity is None:
        return True
    return _is_finite_number(humidity)


def build_reading(temperature: Any, humidity: Any, captured_at: str, /) -> Reading:
    """校验并构造 Reading；无效则抛 SensorReadError，不产生虚假有效读数。"""
    if not is_valid_sample(temperature, humidity):
        raise SensorReadError(
            f"无效传感器样本: temperature={temperature!r}, humidity={humidity!r}"
        )
    return Reading(
        temperature_c=float(temperature),
        humidity_pct=(
            float(humidity) if humidity is not None else None
        ),
        captured_at=captured_at,
    )