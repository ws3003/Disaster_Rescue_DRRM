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
    # ---- E7-02 读数量程（A1；开发默认，真实规格以配置为准）----
    temperature_min: float = -40.0
    temperature_max: float = 85.0
    humidity_min: float = 0.0
    humidity_max: float = 100.0
    # ---- E7-01 读取器选择（A2）："fake" | "ds18b20" | "dht11" ----
    reader: str = "fake"
    device_id: str | None = None
    device_dir: str | None = None
    # DHT11 数据引脚（BCM 编号，正整数）；无未经核实的默认值，须确认接线后注入。
    # 仅 reader=="dht11" 时强制要求；ds18b20/fake 忽略该项。
    pin: int | None = None


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
    # 量程交叉校验（E7-02）
    if not (_is_num(cfg.temperature_min) and _is_num(cfg.temperature_max)
            and cfg.temperature_min < cfg.temperature_max):
        raise SensorConfigError(
            f"配置非法: 温度量程需 min<max 且为有限数值 "
            f"(min={cfg.temperature_min!r} max={cfg.temperature_max!r})")
    if not (_is_num(cfg.humidity_min) and _is_num(cfg.humidity_max)
            and cfg.humidity_min < cfg.humidity_max):
        raise SensorConfigError(
            f"配置非法: 湿度量程需 min<max 且为有限数值 "
            f"(min={cfg.humidity_min!r} max={cfg.humidity_max!r})")
    if cfg.reader not in ("fake", "ds18b20", "dht11"):
        raise SensorConfigError(f"配置非法: reader 仅支持 fake|ds18b20|dht11, got {cfg.reader!r}")
    # pin：空（None）或正整数；禁止 bool/字符串/0/-1
    if cfg.pin is not None and (
        isinstance(cfg.pin, bool) or not isinstance(cfg.pin, int) or cfg.pin <= 0
    ):
        raise SensorConfigError(f"配置非法: pin 须为正整数或 None, got {cfg.pin!r}")
    # reader=dht11 时 pin 为必填；缺失明确报错，不回退 FakeReader
    if cfg.reader == "dht11" and cfg.pin is None:
        raise SensorConfigError("配置非法: reader=dht11 必须指定 pin（GPIO BCM 编号）")
    if cfg.device_id is not None and (
        not isinstance(cfg.device_id, str) or not cfg.device_id.strip()
    ):
        raise SensorConfigError(
            f"配置非法: device_id 须为非空字符串或 None, got {cfg.device_id!r}")
    if cfg.device_dir is not None and not isinstance(cfg.device_dir, str):
        raise SensorConfigError(f"配置非法: device_dir 须为字符串或 None, got {cfg.device_dir!r}")
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
        temperature_min=(
            req_num("temperature_min") if "temperature_min" in data else -40.0
        ),
        temperature_max=(
            req_num("temperature_max") if "temperature_max" in data else 85.0
        ),
        humidity_min=(
            req_num("humidity_min") if "humidity_min" in data else 0.0
        ),
        humidity_max=(
            req_num("humidity_max") if "humidity_max" in data else 100.0
        ),
        reader=str(data.get("reader", "fake")),
        device_id=data.get("device_id") if isinstance(data.get("device_id"), str) else None,
        device_dir=data.get("device_dir") if isinstance(data.get("device_dir"), str) else None,
        # 原样传入 pin，交由 validate_config 判定（bool/str/非法值会被拒绝）
        pin=data.get("pin"),
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