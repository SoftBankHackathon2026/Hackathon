"""Publisher-tool tests. No production address/key/artifact is generated."""

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tool(name):
    spec = importlib.util.spec_from_file_location(
        "release_" + name, ROOT / "releases" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseToolsTests(unittest.TestCase):
    def test_bootstrap_render_rejects_shell_injection(self):
        render = tool("render_bootstrap").render
        for url in (
            "http://example.com",
            "https://x/$(touch /tmp/x)",
            "https://x/';exit;#",
            "https://user:pass@example.com",
            "https://x/../other",
            "https://x?a=b",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                render(url, "a" * 96, "b" * 96)
        with self.assertRaises(ValueError):
            render("https://example.com/releases/1", "a" * 95, "b" * 96)

    def test_unrendered_bootstrap_fails_before_download_without_secret(self):
        result = subprocess.run(
            ["bash", str(ROOT / "bootstrap/install.sh.in")],
            env={**os.environ, "MIGRATION_ENROLL": "request-0123456789." + "x" * 43},
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"request-0123456789", result.stdout + result.stderr)
        self.assertIn("배포 주소".encode(), result.stderr)

    def test_rendered_script_syntax_and_seed_not_external_argument(self):
        rendered = tool("render_bootstrap").render(
            "https://example.com/releases/1", "a" * 96, "b" * 96
        )
        result = subprocess.run(
            ["bash", "-n"],
            input=rendered.encode(),
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertLess(
            rendered.index("unset MIGRATION_ENROLL"), rendered.index("curl -qfsS")
        )
        self.assertNotIn("@RELEASE_BASE_URL@", rendered)

    def test_inventory_archive_is_deterministic_and_self_contained(self):
        build = tool("build_inventory").build
        with tempfile.TemporaryDirectory() as directory:
            one, two = Path(directory) / "one.pyz", Path(directory) / "two.pyz"
            build(one)
            build(two)
            self.assertEqual(
                hashlib.sha384(one.read_bytes()).digest(),
                hashlib.sha384(two.read_bytes()).digest(),
            )
            with zipfile.ZipFile(one) as archive:
                self.assertTrue(
                    all(name.endswith(".py") for name in archive.namelist())
                )
                self.assertNotIn(
                    "migration_connection/control/__init__.py", archive.namelist()
                )
            result = subprocess.run(
                [sys.executable, "-I", str(one), "--help"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_manifest_signing_requires_complete_artifacts(self):
        sign = tool("sign_manifest").sign
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            sign(
                Path(directory), 1, 9_000_000_000_000, Path("missing"), Path("missing")
            )

    def test_host_install_assets_are_signed_as_a_pair(self):
        publisher = tool("sign_manifest")
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            self.assertEqual(publisher.artifact_names(bundle), publisher.ARTIFACTS)
            (bundle / "migration-net-helper").write_bytes(b"helper")
            with self.assertRaisesRegex(ValueError, "both helper and client policy"):
                publisher.artifact_names(bundle)
            (bundle / "net-policy.json").write_text("{}")
            self.assertEqual(
                publisher.artifact_names(bundle),
                publisher.ARTIFACTS + publisher.HOST_ARTIFACTS,
            )

    def test_host_install_symlink_is_rejected_before_signing(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            for name in tool("sign_manifest").ARTIFACTS:
                (bundle / name).write_bytes(b"fixture")
            (bundle / "migration-net-helper").symlink_to(bundle / "migration-connector")
            (bundle / "net-policy.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "regular release artifact"):
                tool("sign_manifest").sign(
                    bundle, 1, 9_000_000_000_000, Path("missing"), Path("missing")
                )


if __name__ == "__main__":
    unittest.main()
