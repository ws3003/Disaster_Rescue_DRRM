"""Non-blocking OneNET MQTT OneJSON property publisher.

This is an optional telemetry side channel. MQTT failures are recorded and retried
in memory, but never roll back the authoritative SQLite/C4 E7 flow.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .config import OneNetConfig

_LOGGER = logging.getLogger("onenet.mqtt")


def _epoch_ms(value: str | None) -> int:
    if value:
        try:
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except (TypeError, ValueError):
            pass
    return int(time.time() * 1000)


def build_property_payload(
    cfg: OneNetConfig,
    *,
    temperature: float,
    humidity: float | None,
    captured_at: str | None,
    message_id: str,
) -> str:
    ts = _epoch_ms(captured_at)
    params: dict[str, Any] = {
        cfg.temperature_identifier: {"value": float(temperature), "time": ts}
    }
    if humidity is not None and cfg.humidity_identifier:
        params[cfg.humidity_identifier] = {"value": float(humidity), "time": ts}
    return json.dumps(
        {"id": str(message_id)[-13:], "version": "1.0", "params": params},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class OneNetMqttPublisher:
    """Bounded asynchronous publisher; does not block E7 sampling."""

    def __init__(
        self,
        cfg: OneNetConfig,
        *,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self._client_factory = client_factory
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=cfg.queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self.published = 0
        self.dropped = 0
        self.failed = 0
        self.last_error: str | None = None

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self.cfg.device_name)
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("缺少 paho-mqtt；请安装项目 requirements") from exc
        return mqtt.Client(client_id=self.cfg.device_name, protocol=mqtt.MQTTv311)

    def start(self) -> None:
        if not self.cfg.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="onenet-mqtt", daemon=True)
        self._thread.start()

    def publish_reading(
        self,
        *,
        sensor_id: str,
        temperature: float,
        humidity: float | None,
        captured_at: str | None,
        reading_id: str | None = None,
    ) -> bool:
        if not self.cfg.enabled:
            return False
        item = {
            "sensor_id": sensor_id,
            "temperature": temperature,
            "humidity": humidity,
            "captured_at": captured_at,
            "message_id": reading_id or str(time.time_ns()),
        }
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            self.dropped += 1
            self.last_error = "queue_full"
            _LOGGER.warning("OneNET 队列已满，丢弃本次遥测（本地 E7 数据不受影响）")
            return False

    def _connect(self) -> Any:
        client = self._new_client()
        client.username_pw_set(self.cfg.product_id, self.cfg.access_token)
        if self.cfg.tls:
            client.tls_set()
        client.connect(self.cfg.host, self.cfg.port, self.cfg.keepalive_seconds)
        client.loop_start()
        return client

    def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.loop_stop()
        finally:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        pending: dict[str, Any] | None = None
        while not self._stop.is_set():
            try:
                if self._client is None:
                    self._client = self._connect()
                if pending is None:
                    try:
                        pending = self._queue.get(timeout=0.2)
                    except queue.Empty:
                        continue
                payload = build_property_payload(
                    self.cfg,
                    temperature=pending["temperature"],
                    humidity=pending["humidity"],
                    captured_at=pending["captured_at"],
                    message_id=pending["message_id"],
                )
                info = self._client.publish(
                    self.cfg.property_topic, payload, qos=self.cfg.qos, retain=False
                )
                if hasattr(info, "wait_for_publish"):
                    info.wait_for_publish(timeout=10.0)
                if getattr(info, "rc", 0) != 0:
                    raise RuntimeError(f"MQTT publish rc={info.rc}")
                self.published += 1
                self._queue.task_done()
                pending = None
                self.last_error = None
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                _LOGGER.warning("OneNET 发布失败，保留待重试；本地链路继续: %s", self.last_error)
                self._disconnect()
                self._stop.wait(self.cfg.reconnect_seconds)
        self._disconnect()

    def close(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
        return thread is None or not thread.is_alive()
