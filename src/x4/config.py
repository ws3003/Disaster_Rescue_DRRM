"""X4 配置（独立文件 config/x4.yaml；不重写 config/edge.yaml）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class X4ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class X4Config:
    enabled: bool = False            # 是否启用云端路径（无真实服务时默认禁用）
    request_timeout_seconds: float = 8.0
    max_attempts: int = 1            # 云端请求尝试次数（不做无限重试）
    cloud_endpoint: str = ""         # 真实云端地址；未配置则不接入真实服务
    model_id: str = "yolov26l"
    fallback_enabled: bool = True    # 云端失败时回退到边缘检测
    cloud_health_enabled: bool = False
    # 云端恢复探测（冷却/恢复态调度；云端正常/禁用时无开销）
    probe_interval_seconds: float = 10.0   # 冷却结束后恢复探测间隔
    probe_short_timeout_seconds: float = 2.0  # 探测专用短超时（不改变正常推理超时）


def _as_bool(v: Any, name: str, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _as_float(v: Any, name: str, lo: float, default: float) -> float:
    if v is None:
        return default
    f = float(v)
    if f < lo:
        raise X4ConfigError(f"配置非法 {name}={v!r}")
    return f


def _as_int(v: Any, name: str, lo: int, default: int) -> int:
    if v is None:
        return default
    i = int(v)
    if i < lo:
        raise X4ConfigError(f"配置非法 {name}={v!r}")
    return i


def load_x4_config(path: str | Path = "config/x4.yaml") -> X4Config:
    p = Path(path)
    if not p.exists():
        return X4Config()   # 缺失即安全默认（禁用云端）
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise X4ConfigError(f"无法读取配置: {p}") from exc
    cfg = raw.get("x4") or {}
    c = X4Config(
        enabled=_as_bool(cfg.get("enabled"), "enabled", False),
        request_timeout_seconds=_as_float(cfg.get("request_timeout_seconds"), "request_timeout_seconds", 0.001, 8.0),
        max_attempts=_as_int(cfg.get("max_attempts"), "max_attempts", 1, 1),
        cloud_endpoint=str(cfg.get("cloud_endpoint") or ""),
        model_id=str(cfg.get("model_id") or "yolov26l"),
        fallback_enabled=_as_bool(cfg.get("fallback_enabled"), "fallback_enabled", True),
        cloud_health_enabled=_as_bool(cfg.get("cloud_health_enabled"), "cloud_health_enabled", False),
        probe_interval_seconds=_as_float(cfg.get("probe_interval_seconds"), "probe_interval_seconds", 0.001, 10.0),
        probe_short_timeout_seconds=_as_float(cfg.get("probe_short_timeout_seconds"), "probe_short_timeout_seconds", 0.001, 2.0),
    )
    return c