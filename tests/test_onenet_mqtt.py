import json
import time

from src.onenet.config import OneNetConfig
from src.onenet.mqtt_publisher import OneNetMqttPublisher, build_property_payload


class _Info:
    rc = 0

    def wait_for_publish(self, timeout=None):
        return True


class _Client:
    def __init__(self):
        self.calls = []

    def username_pw_set(self, username, password):
        self.auth = (username, password)

    def tls_set(self):
        self.tls = True

    def connect(self, host, port, keepalive):
        self.connection = (host, port, keepalive)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def publish(self, topic, payload, qos, retain):
        self.calls.append((topic, payload, qos, retain))
        return _Info()


def _cfg(**overrides):
    values = dict(
        enabled=True, product_id="pid", device_name="001", access_token="token",
        reconnect_seconds=0.01,
    )
    values.update(overrides)
    return OneNetConfig(**values)


def test_onejson_payload_contains_real_values_and_millisecond_time():
    payload = json.loads(build_property_payload(
        _cfg(), temperature=26.125, humidity=52.5,
        captured_at="2026-11-06T12:00:00+08:00", message_id="R-123456789012345",
    ))
    assert payload["version"] == "1.0"
    assert len(payload["id"]) <= 13
    assert payload["params"]["temperature"]["value"] == 26.125
    assert payload["params"]["humidity"]["value"] == 52.5
    assert isinstance(payload["params"]["temperature"]["time"], int)


def test_async_publish_uses_official_property_topic_and_qos1():
    client = _Client()
    publisher = OneNetMqttPublisher(_cfg(), client_factory=lambda _: client)
    publisher.start()
    assert publisher.publish_reading(
        sensor_id="ds18b20-01", temperature=27.0, humidity=None,
        captured_at="2026-11-06T12:00:00+08:00", reading_id="R-1",
    )
    deadline = time.time() + 2
    while publisher.published < 1 and time.time() < deadline:
        time.sleep(0.01)
    assert publisher.close()
    assert publisher.published == 1
    assert client.auth == ("pid", "token")
    topic, payload, qos, retain = client.calls[0]
    assert topic == "$sys/pid/001/thing/property/post"
    assert qos == 1 and retain is False
    assert "humidity" not in json.loads(payload)["params"]
