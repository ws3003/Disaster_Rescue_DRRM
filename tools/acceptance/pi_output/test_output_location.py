"""Output routing tests only: no hardware, Git writes, or production helper edits."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("testtest_launcher", HERE / "run_sensor_validation.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class OutputRouting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="testtest_route_", dir=HERE.parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.assertTrue(self.base.is_relative_to(HERE.parent.resolve()))
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "untouched.txt").write_bytes(b"ORIGINAL\n")
        self.destination = self.base / "testtest"

    def run_case(self, destination=None):
        def perform(args, out, result):
            # Fixture verifies where the existing helper would write all outputs.
            (out / "media").mkdir()
            (out / "media/test.mp4").write_bytes(b"TEST_NOT_A_REAL_VIDEO")
            (out / "TEST_ONLY.yaml").write_text("test_only: true\n")
            raise RuntimeError("TEST: preserve a failed run's result, do not fake a pass")
        helper = SimpleNamespace(perform=perform, assess=lambda result: ["runtime_error"])
        argv = ["launcher", "--repo", str(self.repo), "--scenario", "level2",
                "--output-root", str(destination or self.destination)]
        with patch.object(launcher, "load_original", return_value=helper), patch.object(launcher.sys, "argv", argv), redirect_stdout(io.StringIO()):
            return launcher.main()

    def test_generated_artifacts_and_failure_evidence_stay_in_requested_root(self):
        before = {p.name: p.read_bytes() for p in self.repo.iterdir()}
        self.assertEqual(self.run_case(), 1)
        results = list((self.destination / "results").iterdir())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].resolve().is_relative_to(self.destination.resolve()))
        report = json.loads((results[0] / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(report["failures"], ["runtime_error"])
        self.assertTrue((results[0] / "media/test.mp4").is_file())
        self.assertEqual({p.name: p.read_bytes() for p in self.repo.iterdir()}, before)

    def test_existing_files_and_previous_results_are_not_overwritten(self):
        self.destination.mkdir()
        old = self.destination / "user-file.txt"
        old.write_bytes(b"KEEP\n")
        self.run_case()
        self.run_case()
        self.assertEqual(len(list((self.destination / "results").iterdir())), 2)
        self.assertEqual(old.read_bytes(), b"KEEP\n")

    def test_repository_output_refused_before_writing(self):
        with self.assertRaises(SystemExit) as stopped, redirect_stdout(io.StringIO()):
            self.run_case(self.repo / "testtest")
        self.assertEqual(stopped.exception.code, 2)
        self.assertFalse((self.repo / "testtest").exists())

    def test_changed_original_helper_is_refused(self):
        source = self.repo / "tools/acceptance/verify_sensor_recording.py"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"raise RuntimeError('must never execute')\n")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            launcher.load_original(self.repo)

    def test_original_assessment_and_hardware_implementation_are_reused(self):
        real = HERE.parents[2]
        module = launcher.load_original(real)
        source = (real / "tools/acceptance/verify_sensor_recording.py").read_bytes()
        self.assertEqual(launcher.hashlib.sha256(source.replace(b"\r\n", b"\n")).hexdigest(), launcher.EXPECTED_HELPER_SHA256)
        self.assertTrue(callable(module.perform))
        self.assertTrue(callable(module.assess))


if __name__ == "__main__":
    unittest.main()
