"""Fail-closed publisher preflight tests; no signing keys or releases created."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).parent / "e2e/prepare_release.py"
SPEC = importlib.util.spec_from_file_location("e2e_release", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class E2EReleasePreflightTests(unittest.TestCase):
    def test_incomplete_build_never_creates_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            (root / "bin/.build-incomplete").touch()
            (root / "bin/migration-connector").write_bytes(b"previous-binary")
            with self.assertRaisesRegex(ValueError, "build is incomplete"):
                MODULE.prepare(root, "must-not-run", 2)
            self.assertFalse((root / "release").exists())

    def test_invalid_sequence_never_creates_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for sequence in (0, -1, True, 9_007_199_254_740_992):
                with (
                    self.subTest(sequence=sequence),
                    self.assertRaisesRegex(ValueError, "explicit positive"),
                ):
                    MODULE.prepare(root, "must-not-run", sequence)
            self.assertFalse((root / "release").exists())

    def test_equal_or_older_sequence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public").mkdir()
            (root / "public/release-metadata.json").write_text(
                json.dumps({"release_sequence": 2})
            )
            for sequence in (1, 2):
                with (
                    self.subTest(sequence=sequence),
                    self.assertRaisesRegex(ValueError, "must increase"),
                ):
                    MODULE.prepare(root, "must-not-run", sequence)
            self.assertFalse((root / "release").exists())

    def test_original_metadata_requires_sequence_above_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public").mkdir()
            (root / "public/release-metadata.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "must increase"):
                MODULE.prepare(root, "must-not-run", 1)
            self.assertFalse((root / "release").exists())
