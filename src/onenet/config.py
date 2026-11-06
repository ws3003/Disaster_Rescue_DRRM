"""OneNET MQTT configuration with secret values sourced from environment only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


class OneNetConfigError(ValueError):
    """Raised when enabled OneNET configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class OneNetConfig:
    enabled: bool = False
    host: str = "mqttstls.heclouds.com"
    port: int = 8883
    tls: bool = True
    # OneNET MQTTS uses a platform CA that is not always present in the OS
    # trust store. Keep the certificate outside the repository and provide
    # its absolute path at deployment time.
    ca_cert_path: str = ""
    keepalive_seconds: int = 60
    qos: int = 1
    queue_size: int = 256
    reconnect_seconds: float = 5.0
    product_id: str = ""
    device_name: str = ""
    access_token: str = ""
    temperature_identifier: str = "temperature"
    humidity_identifier: str = "humidity"

    @property
    def property_topic(self) -> str:
        return f"$sys/{self.product_id}/{self.device_name}/thing/property/post"

    @property
    def reply_topic(self) -> str:
        return self.property_topic + "/reply"


def _bool(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise OneNetConfigError(f"{name} 必须为布尔值")


def _int(value: Any, *, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise OneNetConfigError(f"{name} 必须为不小于 {minimum} 的整数")
    if maximum is not None and value > maximum:
        raise OneNetConfigError(f"{name} 必须不大于 {maximum}")
    return value


def config_from_dict(data: Mapping[str, Any], env: Mapping[str, str] | None = None) -> OneNetConfig:
    env = os.environ if env is None else env
    enabled = _bool(data.get("enabled", False), name="onenet.enabled")
    cfg = OneNetConfig(
        enabled=enabled,
        host=str(data.get("host") or "mqttstls.heclouds.com").strip(),
        port=_int(data.get("port", 8883), name="onenet.port", minimum=1, maximum=65535),
        tls=_bool(data.get("tls", True), name="onenet.tls"),
        ca_cert_path=str(data.get("ca_cert_path") or "").strip(),
        keepalive_seconds=_int(
            data.get("keepalive_seconds", 60), name="onenet.keepalive_seconds", minimum=10
        ),
        qos=_int(data.get("qos", 1), name="onenet.qos", minimum=0, maximum=1),
        queue_size=_int(data.get("queue_size", 256), name="onenet.queue_size", minimum=1),
        reconnect_seconds=float(data.get("reconnect_seconds", 5.0)),
        product_id=str(env.get("ONENET_PRODUCT_ID") or data.get("product_id") or "").strip(),
        device_name=str(env.get("ONENET_DEVICE_NAME") or data.get("device_name") or "").strip(),
        access_token=str(env.get("ONENET_ACCESS_TOKEN") or "").strip(),
        temperature_identifier=str(data.get("temperature_identifier") or "temperature").strip(),
        humidity_identifier=str(data.get("humidity_identifier") or "humidity").strip(),
    )
    if cfg.reconnect_seconds <= 0:
        raise OneNetConfigError("onenet.reconnect_seconds 必须大于 0")
    if cfg.enabled:
        missing = [
            name for name, value in (
                ("host", cfg.host),
                ("product_id/ONENET_PRODUCT_ID", cfg.product_id),
                ("device_name/ONENET_DEVICE_NAME", cfg.device_name),
                ("ONENET_ACCESS_TOKEN", cfg.access_token),
                ("temperature_identifier", cfg.temperature_identifier),
            ) if not value
        ]
        if missing:
            raise OneNetConfigError("OneNET 已启用但缺少: " + ", ".join(missing))
    return cfg


def load_onenet_config(path: str | Path, env: Mapping[str, str] | None = None) -> OneNetConfig:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise OneNetConfigError(f"无法读取 OneNET 配置: {p}") from exc
    section = raw.get("onenet") or {}
    if not isinstance(section, dict):
        raise OneNetConfigError("配置中的 onenet 必须是对象")
    return config_from_dict(section, env=env)
