"""E7 连续恢复规则回归；DAO 用内存替身，不代表真实硬件或数据库验收。"""
from datetime import datetime, timedelta, timezone
import unittest

from src.sensor.alarm import HighTempAlarm, STATE_ACTIVE, STATE_CONFIRM_RECOVERY
from src.sensor.config import SensorConfig


class _MemoryAlarm(HighTempAlarm):
    """只替换持久化边界，实际 process 状态机保持原样。"""
    def __init__(self, config):
        super().__init__(config, object())
        self.rows = []

    def _find_active(self):
        return next((row for row in self.rows if row["status"] == "active"), None)

    def _create_alert(self, temp, captured_at, threshold):
        alert_id = f"alarm-{len(self.rows) + 1}"
        self.rows.append({"alert_id": alert_id, "status": "active"})
        return alert_id

    def _resolve_alert(self, alert_id, resolved_at):
        next(row for row in self.rows if row["alert_id"] == alert_id)["status"] = "resolved"


class TestRecoveryContinuity(unittest.TestCase):
    MODES = ("single_hysteresis", "level1", "level2")

    def make_alarm(self, mode):
        config = SensorConfig(
            sensor_id="test-only", temperature_threshold=40.0,
            level2_threshold=None if mode == "single_hysteresis" else 50.0,
            recovery_threshold=36.0, sample_period_seconds=1.0,
            confirm_high_count=3, confirm_recovery_count=3, cooldown_seconds=600.0,
        )
        alarm = _MemoryAlarm(config)
        self.clock = datetime(2026, 11, 3, tzinfo=timezone.utc)
        event = None
        for _ in range(3):
            event = self.sample(alarm, 55.0 if mode == "level2" else 45.0)
        self.assertEqual(event["type"], "alert_created")
        return alarm

    def sample(self, alarm, temperature):
        self.clock += timedelta(seconds=1)
        return alarm.process(temperature, None, self.clock.isoformat())

    def test_separated_low_readings_do_not_resolve(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                alarm = self.make_alarm(mode)
                alert_id = alarm.active_id
                for temperature in (30, 38, 30, 38, 30):
                    self.assertIsNone(self.sample(alarm, temperature))
                self.assertEqual(alarm.active_id, alert_id)
                self.assertEqual(alarm.rows[0]["status"], "active")

    def test_in_band_reading_cancels_recovery(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                alarm = self.make_alarm(mode)
                self.sample(alarm, 30)
                self.assertEqual(alarm.state, STATE_CONFIRM_RECOVERY)
                self.sample(alarm, 38)
                self.assertEqual(alarm.state, STATE_ACTIVE)
                self.assertEqual(alarm.recovery_count, 0)

    def test_exact_recovery_boundary_interrupts_low_streak(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                alarm = self.make_alarm(mode)
                for temperature in (30, 36, 30, 36, 30):
                    self.assertIsNone(self.sample(alarm, temperature))
                self.assertEqual(alarm.rows[0]["status"], "active")

    def test_level2_reading_returns_to_active_state(self):
        for mode in ("level1", "level2"):
            with self.subTest(mode=mode):
                alarm = self.make_alarm(mode)
                self.sample(alarm, 30)
                self.sample(alarm, 55)
                self.assertEqual(alarm.state, STATE_ACTIVE)
                self.assertEqual(alarm.recovery_count, 0)

    def test_three_consecutive_low_readings_still_resolve(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                alarm = self.make_alarm(mode)
                self.assertIsNone(self.sample(alarm, 30))
                self.assertIsNone(self.sample(alarm, 30))
                event = self.sample(alarm, 30)
                self.assertEqual(event["type"], "alert_resolved")
                self.assertEqual(alarm.rows[0]["status"], "resolved")

    def test_sustained_high_does_not_duplicate_alert(self):
        for mode in self.MODES:
            with self.subTest(mode=mode):
                alarm = self.make_alarm(mode)
                for _ in range(8):
                    self.assertIsNone(self.sample(alarm, 55 if mode == "level2" else 45))
                self.assertEqual(len(alarm.rows), 1)
                self.assertEqual(alarm.rows[0]["status"], "active")


if __name__ == "__main__":
    unittest.main()
