import tempfile

from src.database.db import Database
from src.sensor.alarm import HighTempAlarm
from src.sensor.config import sensor_config_from_dict
from src.sensor.loop import SampledSensorLoop
from src.sensor.reader import FakeReader


class _Telemetry:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def publish_reading(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("mqtt down")
        return True


def _cfg():
    return sensor_config_from_dict({
        "sensor_id": "s1", "reader": "fake",
        "temperature_threshold": 40, "level2_threshold": 50,
        "recovery_threshold": 36, "sample_period_seconds": 1,
        "confirm_high_count": 3, "confirm_recovery_count": 3,
        "cooldown_seconds": 60,
    })


def test_valid_reading_is_persisted_then_sent_to_onenet():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Database(db_path=td + "/test.db")
        telemetry = _Telemetry()
        cfg = _cfg()
        loop = SampledSensorLoop(
            cfg, FakeReader(lambda: (26.5, 48.0)), HighTempAlarm(cfg, db), db,
            sleep=lambda _: None, telemetry=telemetry,
        )
        result = loop.step(now=lambda: "2026-11-06T12:00:00+08:00")
        assert result["status"] == "valid"
        assert telemetry.calls[0]["temperature"] == 26.5
        assert telemetry.calls[0]["reading_id"]
        assert db.count_readings() == 1


def test_mqtt_failure_does_not_rollback_local_reading():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Database(db_path=td + "/test.db")
        cfg = _cfg()
        loop = SampledSensorLoop(
            cfg, FakeReader(lambda: (26.5, None)), HighTempAlarm(cfg, db), db,
            sleep=lambda _: None, telemetry=_Telemetry(fail=True),
        )
        result = loop.step(now=lambda: "2026-11-06T12:00:00+08:00")
        assert result["status"] == "valid"
        assert db.count_readings() == 1
