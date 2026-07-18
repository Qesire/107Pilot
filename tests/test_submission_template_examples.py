from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = ROOT / "data/submission_templates/examples"


class SubmissionTemplateExampleTests(unittest.TestCase):
    def test_all_examples_validate_and_materialize_to_bash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ContractStore(Path(temporary) / "pilot107.db")
            service = ContractService(catalog=RecipeCatalog(store=store), store=store)

            examples = sorted(EXAMPLE_DIR.glob("*.contract.json"))
            self.assertEqual(len(examples), 3)
            for path in examples:
                with self.subTest(path=path.name):
                    result = service.validate(json.loads(path.read_text()))
                    self.assertEqual(
                        result.status,
                        "OK",
                        [(item.code, item.message) for item in result.findings],
                    )
                    script = result.effective_request["script"]
                    self.assertIsInstance(script, str)
                    self.assertNotIn("{{", script)
                    syntax = subprocess.run(
                        ["bash", "-n"],
                        input=script,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(syntax.returncode, 0, syntax.stderr)


if __name__ == "__main__":
    unittest.main()
