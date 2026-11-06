import pytest

from src.onenet.config import OneNetConfigError, config_from_dict


def test_disabled_requires_no_credentials():
    cfg = config_from_dict({"enabled": False}, env={})
    assert cfg.enabled is False
    assert cfg.property_topic.startswith("$sys/")


def test_enabled_uses_environment_credentials_without_serializing_secret():
    cfg = config_from_dict(
        {"enabled": True, "product_id": "ignored", "device_name": "ignored"},
        env={
            "ONENET_PRODUCT_ID": "pid-1",
            "ONENET_DEVICE_NAME": "001",
            "ONENET_ACCESS_TOKEN": "secret-token",
        },
    )
    assert cfg.property_topic == "$sys/pid-1/001/thing/property/post"
    assert cfg.access_token == "secret-token"


def test_enabled_fails_closed_when_token_missing():
    with pytest.raises(OneNetConfigError, match="ONENET_ACCESS_TOKEN"):
        config_from_dict(
            {"enabled": True, "product_id": "pid-1", "device_name": "001"},
            env={},
        )
