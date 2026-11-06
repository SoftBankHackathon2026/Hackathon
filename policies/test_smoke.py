"""Actual OPA HTTP checks, including the 200-without-policy failure mode."""
import importlib.util
from pathlib import Path
import shutil
import socket
import subprocess
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("smoke", ROOT / "smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@unittest.skipUnless(shutil.which("opa"), "OPA executable required")
class SmokeIntegrationTest(unittest.TestCase):
    def run_server_check(self, policy):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        command = ["opa", "run", "--server", f"--addr=127.0.0.1:{port}",
                   "--disable-telemetry"]
        if policy:
            command.append(str(ROOT / "migration.rego"))
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen(url + "/health", timeout=0.2):
                        break
                except (urllib.error.URLError, TimeoutError):
                    if process.poll() is not None:
                        self.fail("OPA exited before readiness")
                    time.sleep(0.05)
            else:
                self.fail("OPA readiness timeout")
            return smoke.verify(url, ROOT / "examples", timeout=1)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def test_real_policy_all_outcomes(self):
        count, failures = self.run_server_check(True)
        self.assertEqual(count, 15)
        self.assertEqual(failures, [])

    def test_http_200_without_loaded_policy_fails(self):
        count, failures = self.run_server_check(False)
        self.assertEqual(len(failures), count)
        self.assertTrue(all("policy missing" in failure for failure in failures))

    def test_unreachable_server_fails(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            count, failures = smoke.verify(
                f"http://127.0.0.1:{sock.getsockname()[1]}", ROOT / "examples", timeout=0.1)
        self.assertEqual(len(failures), count)


if __name__ == "__main__":
    unittest.main()
