"""Test the hardware helper's isolation/assessment only; no hardware exercised."""
import copy
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

HANDOVER = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("sensor_hardware_helper", HANDOVER / "verify_sensor_recording.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class TestProfile(unittest.TestCase):
    def setUp(self):
        self.original = {
            "sensor": {"sensor_id": "ds18b20-01", "reader": "ds18b20", "enabled": False,
                       "temperature_threshold": 40.0, "level2_threshold": 50.0,
                       "recovery_threshold": 36.0, "sample_period_seconds": 10.0,
                       "confirm_high_count": 3, "confirm_recovery_count": 3, "cooldown_seconds": 600.0},
            "e8": {"enabled": False, "width": 1280, "height": 720, "fps": 15, "record_seconds": 10},
            "server": {"api_url": "not-needed-for-this-local-test"},
        }

    def test_level1_profile_preserves_original_and_confirmation_rules(self):
        before = copy.deepcopy(self.original)
        profile = helper.make_profile(self.original, 26, "level1", Path("results"))
        self.assertEqual(self.original, before)
        sensor = profile["sensor"]
        self.assertLess(sensor["temperature_threshold"], 26)
        self.assertGreater(sensor["level2_threshold"], 26)
        self.assertEqual(sensor["confirm_high_count"], 3)
        self.assertEqual(sensor["confirm_recovery_count"], 3)
        self.assertEqual(sensor["cooldown_seconds"], 600)
        self.assertNotIn("server", profile)

    def test_level2_profile_is_test_only_and_below_real_baseline(self):
        profile = helper.make_profile(self.original, 26, "level2", Path("results"))
        self.assertTrue(profile["acceptance"]["test_only"])
        self.assertLess(profile["sensor"]["level2_threshold"], 26)
        self.assertLess(profile["sensor"]["recovery_threshold"], profile["sensor"]["temperature_threshold"])
        self.assertEqual(profile["sensor"]["reader"], "ds18b20")
        self.assertEqual(profile["e8"]["record_seconds"], 10)

    def test_invalid_baseline_is_not_used_for_thresholds(self):
        for value in (True, None, "26", float("nan"), float("inf"), 5, 80):
            with self.subTest(value=value), self.assertRaises(ValueError):
                helper.make_profile(self.original, value, "level2", Path("results"))

    def test_fake_reader_is_not_silently_accepted(self):
        self.original["sensor"]["reader"] = "fake"
        with self.assertRaisesRegex(ValueError, "real sensor"):
            helper.make_profile(self.original, 26, "level2", Path("results"))

    def test_dht11_profile_accepted(self):
        d = copy.deepcopy(self.original)
        d["sensor"].update({"reader": "dht11", "pin": 4})
        profile = helper.make_profile(d, 26, "level1", Path("results"))
        self.assertEqual(profile["sensor"]["reader"], "dht11")
        self.assertEqual(profile["sensor"]["pin"], 4)
        # DS18B20 原有断言保持：确认次数/冷却/量级
        self.assertEqual(profile["sensor"]["confirm_high_count"], 3)

    def test_dht11_missing_pin_is_refused(self):
        d = copy.deepcopy(self.original)
        d["sensor"].update({"reader": "dht11"})  # 缺 pin
        with self.assertRaisesRegex(ValueError, "GPIO pin"):
            helper.make_profile(d, 26, "level2", Path("results"))

    def test_dht11_invalid_pin_is_refused(self):
        for bad in (True, "17", 0, -1):
            with self.subTest(bad=bad):
                d = copy.deepcopy(self.original)
                d["sensor"].update({"reader": "dht11", "pin": bad})
                with self.assertRaisesRegex(ValueError, "GPIO pin"):
                    helper.make_profile(d, 26, "level1", Path("results"))


class TestAssessment(unittest.TestCase):
    def result(self, scenario="level2"):
        value = {"test_only": True, "scenario": scenario, "confirm_high_count": 3,
                 "camera_fault": None, "e7e8_closed": True, "camera_closed_state": "CLOSED",
                 "reader_errors": [], "runner_dropped": [],
                 "status_before": "expected existing changes", "status_after": "expected existing changes",
                 "sample_progress": [{"samples": count} for count in (2, 4, 6, 8)],
                 "test_profile": {"sensor": {"temperature_threshold": 24.0, "level2_threshold": 25.0}},
                 "database": {"readings": [{"reading_id": str(i)} for i in range(8)],
                              "alerts": [{"alert_id": "actual-id", "status": "active",
                                          "threshold": 24.0 if scenario == "level1" else 25.0}],
                              "media": [], "outbox_count": 9, "verify_count": 0},
                 "record_calls": []}
        if scenario == "level2":
            value["database"]["media"] = [{"source": "sensor", "ref_id": "actual-id", "media_type": "video",
                                          "capture_result": "ok", "upload_status": None, "status": "processing"}]
            value["record_calls"] = [{"completed": True, "samples_during_recording": 5}]
            value["video_duration_seconds"] = 10.2
        return value

    def test_local_capture_acceptance_does_not_require_or_claim_upload(self):
        self.assertEqual(helper.assess(self.result()), [])

    def test_level1_no_video_is_the_expected_result(self):
        self.assertEqual(helper.assess(self.result("level1")), [])

    def test_missing_test_label_is_rejected(self):
        value = self.result()
        value["test_only"] = False
        self.assertIn("missing_test_only_label", helper.assess(value))

    def test_short_video_fails_even_if_capture_was_marked_ok(self):
        value = self.result()
        value["video_duration_seconds"] = 1.0
        self.assertIn("video_duration_not_approximately_10_seconds", helper.assess(value))

    def test_blocked_sampler_is_not_accepted(self):
        value = self.result()
        value["record_calls"][0]["samples_during_recording"] = 0
        self.assertIn("sampling_not_observed_during_recording", helper.assess(value))

    def test_stalled_sampler_near_end_is_not_accepted(self):
        value = self.result()
        value["sample_progress"] = [{"samples": 8}] * 4
        self.assertIn("sampling_not_observed_near_test_end", helper.assess(value))

    def test_wrong_alert_link_is_not_accepted(self):
        value = self.result()
        value["database"]["media"][0]["ref_id"] = "other-alert"
        self.assertIn("media_not_linked_to_actual_alert", helper.assess(value))

    def test_upload_success_is_not_faked_without_uploader(self):
        value = self.result()
        value["database"]["media"][0]["upload_status"] = "success"
        self.assertIn("unexpected_upload_success_without_uploader", helper.assess(value))

    def test_wrong_threshold_is_not_accepted(self):
        value = self.result()
        value["database"]["alerts"][0]["threshold"] = 50
        self.assertIn("unexpected_alert_threshold", helper.assess(value))

    def test_level1_unexpected_video_fails(self):
        value = self.result("level1")
        value["record_calls"] = [{"completed": True}]
        self.assertIn("level1_unexpectedly_recorded", helper.assess(value))

    def test_unclosed_pipeline_or_camera_fault_fails(self):
        value = self.result()
        value["e7e8_closed"] = False
        value["camera_fault"] = "capture failure"
        failures = helper.assess(value)
        self.assertIn("lifecycle_not_closed", failures)
        self.assertIn("camera_fault", failures)

    def test_input_quarantine_and_changed_worktree_are_reported(self):
        value = self.result()
        value["database"]["verify_count"] = 1
        value["status_after"] = "new user changes"
        failures = helper.assess(value)
        self.assertIn("local_outbox_not_complete", failures)
        self.assertIn("worktree_changed_during_test", failures)


class TestDatabaseInspection(unittest.TestCase):
    def test_inspection_closes_connection_and_preserves_database(self):
        with tempfile.TemporaryDirectory(prefix="sensor_inspection_test_", dir=HANDOVER) as folder:
            root = Path(folder).resolve()
            self.assertTrue(root.is_relative_to(HANDOVER.resolve()))
            path = root / "test.db"
            conn = sqlite3.connect(path)
            try:
                for name in ("readings", "sensor_alerts", "media_evidence", "sync_outbox", "c4_verify"):
                    conn.execute(f"CREATE TABLE {name}(id TEXT)")
                conn.commit()
            finally:
                conn.close()
            before = path.read_bytes()
            self.assertEqual(helper.inspect_test_db(path)["outbox_count"], 0)
            self.assertEqual(path.read_bytes(), before)
            path.unlink()  # Windows also verifies the inspection connection was released.


class TestBuildRealReader(unittest.TestCase):
    """V4.4 E7 改造：验收构造器同时支持 DS18B20 与 DHT11（不触 GPIO）。"""

    def test_ds18b20_returns_real_reader(self):
        r = helper._build_real_reader({"reader": "ds18b20", "device_id": "28-abc",
                                       "device_dir": "/sys/bus/w1/devices"})
        self.assertEqual(type(r).__name__, "DS18B20Reader")

    def test_dht11_returns_reader_with_pin_and_retry(self):
        r = helper._build_real_reader({"reader": "dht11", "pin": 4, "read_retry_count": 2})
        self.assertEqual(r._pin, 4)
        self.assertEqual(r._retry_count, 2)

    def test_dht11_missing_or_invalid_pin_fails(self):
        for pin in (None, True, "17", 0, -1):
            with self.subTest(pin=pin):
                with self.assertRaisesRegex(ValueError, "GPIO pin"):
                    helper._build_real_reader({"reader": "dht11", "pin": pin})

    def test_unknown_reader_refused(self):
        with self.assertRaisesRegex(ValueError, "DS18B20 or DHT11"):
            helper._build_real_reader({"reader": "fake"})


if __name__ == "__main__":
    unittest.main()
