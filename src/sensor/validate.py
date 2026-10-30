"""传感器样本校验（阶段 A1，E7-02）。

依据 SRS REQ-E7-02（无效读数不得生成告警/存储）。

覆盖：
- 基本合法性：temperature 必填且为有限数值（排除 bool / NaN / ±inf）。
- 量程：温度/湿度必须在配置的可控边界内（默认 -40~85℃ / 0~100%RH，可经
  SensorConfig/SensorLoop 传入覆盖）。
- 边界值视为有效（如 -40℃、85℃、0%RH、100%RH 通过）。

量程默认值仅作开发默认，不声称已完成真实硬件规格确认；真实量程应写成配置。
"""

from __future__ import annotations

import math
from typing import Any

from src.sensor.model import Reading
from src.sensor.sensor_errors import SensorReadError

# 开发默认量程（可配置覆盖，见 SensorConfig；不代表已确认的硬件规格）
DEFAULT_TEMPERATURE_MIN = -40.0
DEFAULT_TEMPERATURE_MAX = 85.0
DEFAULT_HUMIDITY_MIN = 0.0
DEFAULT_HUMIDITY_MAX = 100.0


def _is_finite_number(value: Any) -> bool:
    """数值必须为 int/float、非 bool、有限。"""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _in_range(value: float, lo: float, hi: float) -> bool:
    """闭区间判界（含边界）。"""
    return lo <= value <= hi


def is_finite_sample(temperature: Any, humidity: Any) -> bool:
    """仅判断基本合法性（有限数值 / NaN / ±inf / 类型），不判断量程。

    供 Reader 层构造 Reading 使用：Reader 只把"非数值/NaN/inf"记为读取质量问题，
    不预判配置量程；量程由业务校验层（SensorLoop，按 SensorConfig）统一把关，
    以保证自定义量程在整个链路生效、且 Reader/Loop 不冲突。
    """
    if not _is_finite_number(temperature):
        return False
    if humidity is None:
        return True
    return _is_finite_number(humidity)


def is_valid_sample(
    temperature: Any,
    humidity: Any,
    /,
    *,
    temperature_min: float = DEFAULT_TEMPERATURE_MIN,
    temperature_max: float = DEFAULT_TEMPERATURE_MAX,
    humidity_min: float = DEFAULT_HUMIDITY_MIN,
    humidity_max: float = DEFAULT_HUMIDITY_MAX,
) -> bool:
    """判断一次样本是否合格（基本合法性 + 量程；业务校验层用）。

    - 基本合法：temperature 有限、humidity 有限或 None（见 is_finite_sample）。
    - 量程：temperature/humidity 在 [min, max] 闭区间内；边界有效。
    """
    if not is_finite_sample(temperature, humidity):
        return False
    if not _in_range(float(temperature), temperature_min, temperature_max):
        return False
    if humidity is not None and not isinstance(humidity, bool) and isinstance(humidity, (int, float)):
        if not _in_range(float(humidity), humidity_min, humidity_max):
            return False
    return True


def build_reading(
    temperature: Any,
    humidity: Any,
    captured_at: str,
    /,
) -> Reading:
    """构造 Reading（Reader 层）：仅校验基本合法性（非数值/NaN/inf）。

    注意：此处刻意不做量程预判，避免 Reader 的默认量程与 SensorConfig 自定义量程冲突；
    量程由 SensorLoop 按配置统一校验（见 is_valid_sample）。
    """
    if not is_finite_sample(temperature, humidity):
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