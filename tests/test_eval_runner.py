from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evals.runner import run_suite  # noqa: E402


class EvaluationSuiteTest(unittest.TestCase):
    def test_case_file_has_broad_synthetic_coverage(self) -> None:
        case_path = PROJECT_ROOT / "evals" / "cases.jsonl"
        cases = [json.loads(line) for line in case_path.read_text(encoding="utf-8").splitlines()]
        self.assertGreaterEqual(len(cases), 20)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        tags = {tag for case in cases for tag in case.get("tags", [])}
        self.assertTrue(
            {
                "orders",
                "logistics",
                "products",
                "refund",
                "deduplication",
                "tenant-isolation",
                "prompt-injection",
                "trace",
            }.issubset(tags)
        )

    def test_offline_suite_passes_with_zero_safety_failures(self) -> None:
        report = run_suite(PROJECT_ROOT)
        self.assertGreaterEqual(report["total"], 20)
        self.assertEqual(report["passed"], report["total"])
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["safety_failures"], 0)
        self.assertEqual(report["pass_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
