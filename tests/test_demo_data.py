"""Public D1 package validation, without access to local evidence files."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from evals.validate_demo_data import DEFAULT_PACKAGE, comparison, load_package, read_json, validate_case


class DemoDataTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_package()

    def test_three_cases_and_confirmed_tradeoffs(self):
        a, b, c = self.cases
        self.assertEqual([case["demo_case_id"] for case in self.cases], ["A", "B", "C"])
        self.assertEqual(a["new_risk"], {"package_count": 0, "stop_count": 0})
        self.assertEqual(b["review_status"], "REJECTED")
        self.assertEqual(b["comparisons"]["total_operation_seconds"]["delta"], 224.7)
        self.assertEqual(b["comparisons"]["waiting_seconds"]["delta"], 1347.7)
        self.assertIsNone(b["new_risk"]["package_count"])
        self.assertEqual(c["new_risk"], {"package_count": 2, "stop_count": 1})
        self.assertIsNone(a["run_id"])
        self.assertIsNone(c["run_id"])

    def test_time_total_delta_percent_and_case_classification_tampering(self):
        for target, value in (
            (("historical", "total_operation_seconds"), 1),
            (("comparisons", "driving_seconds", "delta"), 0),
            (("comparisons", "total_operation_seconds", "change_percent"), 100),
            (("demo_case_id",), "B"),
            (("new_risk", "package_count"), 1),
            (("suggested", "risk_stop_count"), -1),
        ):
            with self.subTest(target=target):
                case = deepcopy(self.cases[0])
                node = case
                for field in target[:-1]:
                    node = node[field]
                node[target[-1]] = value
                with self.assertRaises(ValueError):
                    validate_case(case)

    def test_zero_baseline_and_missing_values_are_not_filled(self):
        self.assertIsNone(comparison(0, 4)["change_percent"])
        self.assertEqual(comparison(0, 4)["delta"], 4)
        self.assertEqual(comparison(None, 4), {"delta": None, "change_percent": None, "direction": None})

    def test_nonstandard_json_and_duplicate_fields_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": 1, "a": 2}'):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_json(path)

    def test_private_fields_and_credentials_are_rejected(self):
        for field in ("review_note", "stops", "travel_matrix", "api_key"):
            case = deepcopy(self.cases[0])
            case[field] = "unexpected"
            with self.assertRaises(ValueError):
                validate_case(case)
        case = deepcopy(self.cases[0])
        case["description"] = "Authorization: Bearer test-credential"
        with self.assertRaises(ValueError):
            validate_case(case)

    def test_case_fingerprints_and_extra_files_rejected(self):
        with TemporaryDirectory() as directory:
            package = Path(directory) / "demo_data"
            shutil.copytree(DEFAULT_PACKAGE, package)
            (package / "private.sqlite3").write_bytes(b"SQLite format 3\x00")
            with self.assertRaises(ValueError):
                load_package(package)
            (package / "private.sqlite3").unlink()
            case = package / "cases/A.json"
            case.write_bytes(case.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                load_package(package)

    def test_standalone_loading_without_site_packages_or_private_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "demo_data"
            shutil.copytree(DEFAULT_PACKAGE, package)
            script = root / "validate.py"
            shutil.copyfile(Path(__file__).resolve().parents[1] / "evals/validate_demo_data.py", script)
            result = subprocess.run(
                [sys.executable, "-I", "-S", str(script), "--package-dir", str(package)],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Validated 3", result.stdout)
            self.assertEqual({p.name for p in root.iterdir()}, {"demo_data", "validate.py"})
