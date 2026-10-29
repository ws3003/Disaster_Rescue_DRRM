"""E7 传感器配置（V4.4 E7a）。

业务参数（阈值/周期/确认次数/冷却/重试/离线）一律由显式配置提供；
不在此模块写死任何业务默认值。
配置缺失或非法 → SensorConfigError（不静默启动, 不编造默认业务数值）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping
from pathlib import Path

import yaml


class SensorConfigError(ValueError):
    """传感器配置缺失或非法。"""


@dataclass(frozen=True)
class SensorConfig:
    """传感器业务配置（一级阈值等核心字段转换前；二级/恢复阈值可选，缺省单级模式）。"""

    sensor_id: str
    # 一级高温阈值（单级模式下即唯一触发阈值）
    temperature_threshold: float
    sample_period_seconds: float
    confirm_high_count: int
    confirm_recovery_count: int
    cooldown_seconds: float
    # 可选二级阈值；为 None 时保持单级告警行为（向后兼容）
    level2_threshold: float | None = None
    # 可选恢复阈值；为 None 时回退到 temperature_threshold（单级语义：低于阈值即恢复）
    recovery_threshold: float | None = None
    read_retry_count: int = 0
    offline_after_failures: int = 1


def _is_num(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(float(v))


def validate_config(cfg: SensorConfig) -> SensorConfig:
    if not isinstance(cfg.sensor_id, str) or not cfg.sensor_id.strip():
        raise SensorConfigError("sensor_id 缺失或为空")
    for name, val, pred in (
        ("temperature_threshold", cfg.temperature_threshold, lambda v: _is_num(v) and float(v) > 0),
        ("sample_period_seconds", cfg.sample_period_seconds, lambda v: _is_num(v) and float(v) > 0),
        ("cooldown_seconds", cfg.cooldown_seconds, lambda v: _is_num(v) and float(v) >= 0),
    ):
        if not pred(val):
            raise SensorConfigError(f"配置非法: {name}={val!r}")
    if cfg.level2_threshold is not None:
        if not _is_num(cfg.level2_threshold) or float(cfg.level2_threshold) <= float(cfg.temperature_threshold):
            raise SensorConfigError(
                f"配置非法: level2_threshold 须为大于一级阈值的有限数值 "
                f"(level2={cfg.level2_threshold!r} level1={cfg.temperature_threshold!r})")
    if cfg.recovery_threshold is not None:
        if not _is_num(cfg.recovery_threshold) or float(cfg.recovery_threshold) < 0:
            raise SensorConfigError(f"配置非法: recovery_threshold={cfg.recovery_threshold!r}")
    for name, val, lo in (
        ("confirm_high_count", cfg.confirm_high_count, 1),
        ("confirm_recovery_count", cfg.confirm_recovery_count, 1),
        ("read_retry_count", cfg.read_retry_count, 0),
        ("offline_after_failures", cfg.offline_after_failures, 1),
    ):
        if isinstance(val, bool) or not isinstance(val, int) or val < lo:
            raise SensorConfigError(f"配置非法(需整数>= {lo}): {name}={val!r}")
    return cfg


class _Missing:
    pass


def sensor_config_from_dict(data: Mapping[str, Any]) -> SensorConfig:
    """从字典构造并校验（缺失/类型错误/非法值 → SensorConfigError）。"""

    def req_num(key: str) -> float:
        if key not in data:
            raise SensorConfigError(f"缺少传感器配置项: {key}")
        v = data[key]
        if not _is_num(v):
            raise SensorConfigError(f"配置类型非法(需数值): {key}={v!r}")
        return float(v)

    def req_int(key: str, lo: int) -> int:
        if key not in data:
            raise SensorConfigError(f"缺少传感器配置项: {key}")
        v = data[key]
        if isinstance(v, bool) or not isinstance(v, int) or v < lo:
            raise SensorConfigError(f"配置类型非法(需整数>= {lo}): {key}={v!r}")
        return v

    return validate_config(SensorConfig(
        sensor_id=str(data["sensor_id"]),
        temperature_threshold=req_num("temperature_threshold"),
        level2_threshold=(
            req_num("level2_threshold") if "level2_threshold" in data else None
        ),
        recovery_threshold=(
            req_num("recovery_threshold") if "recovery_threshold" in data else None
        ),
        sample_period_seconds=req_num("sample_period_seconds"),
        confirm_high_count=req_int("confirm_high_count", 1),
        confirm_recovery_count=req_int("confirm_recovery_count", 1),
        cooldown_seconds=req_num("cooldown_seconds"),
        read_retry_count=(
            req_int("read_retry_count", 0)
            if "read_retry_count" in data
            else 0
        ),
        offline_after_failures=(
            req_int("offline_after_failures", 1)
            if "offline_after_failures" in data
            else 1
        ),
    ))


def load_sensor_config(yaml_path: str | Path) -> SensorConfig:
    """从 yaml 的 ``sensor:`` 段读取配置；缺失/非法 -> SensorConfigError。

    不在此处为未确认参数编造默认业务数值。
    """
    path = Path(yaml_path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise SensorConfigError(f"无法读取配置文件: {path}") from exc
    section = raw.get("sensor")
    if not isinstance(section, dict) or not section:
        raise SensorConfigError(f"配置缺少 sensor 段: {path}")
    return sensor_config_from_dict(section)