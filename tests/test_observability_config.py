from __future__ import annotations

import unittest
from pathlib import Path

import yaml


class ObservabilityConfigTests(unittest.TestCase):
    def test_alert_rules_have_unique_names_thresholds_and_severity(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        payload = yaml.safe_load(
            (repository / "config/observability/pilot107-alerts.yml").read_text(encoding="utf-8")
        )
        rules = [rule for group in payload["groups"] for rule in group["rules"]]
        names = [rule["alert"] for rule in rules]

        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(rules), 5)
        for rule in rules:
            self.assertTrue(str(rule["expr"]).strip())
            self.assertRegex(str(rule["for"]), r"^[1-9][0-9]*[ms]$")
            self.assertIn(rule["labels"]["severity"], {"warning", "critical"})
            self.assertTrue(rule["annotations"]["summary"])


if __name__ == "__main__":
    unittest.main()
