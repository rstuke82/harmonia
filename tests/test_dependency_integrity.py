"""Runtime pins are authenticated and understood by the startup preflight."""

import unittest
from pathlib import Path
from harmonia.bootstrap import _parse_requirements_file


class DependencyIntegrity(unittest.TestCase):
    def test_all_runtime_pins_have_sha256_digests(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "requirements.lock").read_text()
        self.assertIn("--require-hashes", text)
        entries = text.replace("\\\n", "").splitlines()
        pins = [line for line in entries if line and not line.startswith(("#", "--"))]
        self.assertEqual(len(pins), 28)
        for line in pins:
            self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[^\s]+\s+--hash=sha256:[a-f0-9]{64}")
        parsed = _parse_requirements_file(root / "requirements.txt")
        self.assertEqual(len(parsed), len(pins))
        self.assertFalse(any(row["parse_error"] for row in parsed))
        self.assertTrue(all(row["operator"] == "==" for row in parsed))
        self.assertNotRegex(text, r"(?i)fuzzywuzzy|python-levenshtein")
