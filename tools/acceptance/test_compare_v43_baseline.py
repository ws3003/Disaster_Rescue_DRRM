"""Comparison-tool guards only; no hardware, real model, or Git writes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HANDOVER = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("v43_compare", HANDOVER / "compare_v43_baseline.py")
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)

LABELS = [{"file": "001.jpg", "gt_state": "Standing"},
          {"file": "002.jpg", "gt_state": "Fall"}]
REPORT = "001.jpg Standing Standing ✓ 正确\n002.jpg Fall Sitting ✗ 错误\n"


def result(predictions):
    rows = [{"file": label["file"], "expected": label["gt_state"], "predicted": predicted}
            for label, predicted in zip(LABELS, predictions)]
    return {"rows": rows, "correct": sum(r["expected"] == r["predicted"] for r in rows),
            "total": len(rows)}


class TestReportParser(unittest.TestCase):
    def test_existing_report_format(self):
        rows = compare.parse_report(REPORT, LABELS)
        self.assertEqual(rows[1]["predicted"], "Sitting")

    def test_no_detection_is_retained_as_failure(self):
        text = REPORT.replace("Fall Sitting", "Fall 未检出")
        self.assertEqual(compare.parse_report(text, LABELS)[1]["predicted"], "未检出")

    def test_partial_report_is_not_accepted(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            compare.parse_report(REPORT.splitlines()[0], LABELS)

    def test_duplicate_report_row_is_not_accepted(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            compare.parse_report(REPORT + REPORT.splitlines()[0], LABELS)

    def test_different_ground_truth_is_not_accepted(self):
        with self.assertRaisesRegex(ValueError, "changed"):
            compare.parse_report(REPORT.replace("002.jpg Fall", "002.jpg Lying"), LABELS)


class TestVerdict(unittest.TestCase):
    def test_equal_errors_are_still_failed(self):
        bad = result(["Standing", "Sitting"])
        value = compare.compare_results([bad, bad], [bad, bad])
        self.assertEqual(value["exit_code"], 1)
        self.assertTrue(value["paired_predictions_equal"])
        self.assertFalse(value["all_five_cases_correct"])

    def test_stable_difference_requires_review(self):
        good, bad = result(["Standing", "Fall"]), result(["Standing", "Sitting"])
        value = compare.compare_results([good, good], [bad, bad])
        self.assertEqual(value["exit_code"], 2)
        self.assertEqual(value["verdict"], "DIFFERENCE_FOUND_NEEDS_CAUSE_ANALYSIS")

    def test_unstable_repeats_are_not_called_baseline_failure(self):
        good, bad = result(["Standing", "Fall"]), result(["Standing", "Sitting"])
        value = compare.compare_results([good, bad], [good, bad])
        self.assertEqual(value["verdict"], "UNSTABLE_RESULTS_REVIEW_REQUIRED")

    def test_all_correct_same_predictions(self):
        good = result(["Standing", "Fall"])
        self.assertEqual(compare.compare_results([good, good], [good, good])["exit_code"], 0)


class TestSnapshotsAndExecution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v43_compare_test_", dir=HANDOVER)
        self.path = Path(self.temp.name).resolve()
        self.assertTrue(self.path.is_relative_to(HANDOVER.resolve()))
        self.addCleanup(self.temp.cleanup)
        self.repo = self.path / "repo"
        self.out = self.path / "out"
        self.repo.mkdir()
        self.out.mkdir()
        self.blobs = {name: b"VALUE = 1\n" for name in compare.SOURCES}
        self.blobs[compare.CONFIG] = b"model:\n  path: models/disaster_best.pt\n"
        self.blobs[compare.LABELS] = json.dumps({"images": LABELS}).encode()
        for label in LABELS:
            self.blobs["data/test_images/" + label["file"]] = b"image-fixture-" + label["file"].encode()
        for relative, data in self.blobs.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def tree(self):
        return {p.relative_to(self.repo).as_posix(): p.read_bytes()
                for p in self.repo.rglob("*") if p.is_file()}

    def test_snapshots_do_not_modify_source_and_share_inputs(self):
        before = self.tree()
        labels, fingerprints = compare.prepare_snapshots(self.repo, self.out, self.blobs.__getitem__)
        self.assertEqual(self.tree(), before)
        self.assertEqual(labels, LABELS)
        self.assertTrue(all(v.get("equal_ignoring_crlf", v.get("equal")) for v in fingerprints.values()))
        self.assertEqual((self.out / "shared/images/001.jpg").read_bytes(),
                         self.blobs["data/test_images/001.jpg"])

    def test_changed_labels_require_review_without_source_changes(self):
        path = self.repo / compare.LABELS
        path.write_text(json.dumps({"images": LABELS[::-1]}))
        before = self.tree()
        with self.assertRaisesRegex(ValueError, "labels differ"):
            compare.prepare_snapshots(self.repo, self.out, self.blobs.__getitem__)
        self.assertEqual(self.tree(), before)

    def test_changed_image_requires_review(self):
        (self.repo / "data/test_images/001.jpg").write_bytes(b"user-image")
        before = self.tree()
        with self.assertRaisesRegex(ValueError, "Image differs"):
            compare.prepare_snapshots(self.repo, self.out, self.blobs.__getitem__)
        self.assertEqual(self.tree(), before)

    def test_path_traversal_label_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            compare.checked_labels(json.dumps({"images": [{"file": "../private.jpg", "gt_state": "Fall"}]}))

    def run_stub(self, code):
        (self.repo / "tests/evaluate_states.py").write_text(code, encoding="utf-8")
        bootstrap = "import runpy,sys; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
        with patch.object(compare, "BOOTSTRAP", bootstrap):
            return compare.run_evaluation(self.repo, self.out, self.repo / compare.CONFIG,
                                          self.out / "raw.log", LABELS, 10)

    def test_exit_one_with_valid_failed_report_is_not_hidden(self):
        evaluated = self.run_stub("import sys\nprint(" + repr(REPORT) + ")\nsys.exit(1)\n")
        self.assertEqual(evaluated["returncode"], 1)
        self.assertEqual(evaluated["correct"], 1)

    def test_crashed_evaluation_is_not_compared_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "crashed"):
            self.run_stub("import sys\nsys.exit(3)\n")

    def test_exit_zero_with_incorrect_predictions_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "disagrees"):
            self.run_stub("print(" + repr(REPORT) + ")\n")


if __name__ == "__main__":
    unittest.main()
