"""Python runner client -> actual Node receipt APIs -> two disposable Redis servers.

SAMPLE_APP_SOURCE points to a checkout with npm ci completed. No production
network or DB is used; the app's DB adapter is an in-memory contract fixture.
"""
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

from migration.full_migration import ReceiptHandoff
from migration.config import parse_config
from tests.test_full_migration import full_raw


def read_line(proc):
    assert select.select([proc.stdout], [], [], 15)[0], "fixture did not respond"
    return proc.stdout.readline().strip()


def request(url, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def committed(url):
    for _ in range(150):
        status, body = request(url)
        if status == 200 and body.get("status") == "committed":
            return body
        time.sleep(0.02)
    pytest.fail("queued request did not commit")


def test_python_client_hands_off_receipts_and_destination_survives_source_shutdown(tmp_path):
    root = os.getenv("SAMPLE_APP_SOURCE")
    if not root or not shutil.which("redis-server") or not shutil.which("node"):
        pytest.skip("SAMPLE_APP_SOURCE + local redis-server/node required")
    processes = []
    try:
        urls = []
        for n in range(2):
            with socket.socket() as port:
                port.bind(("127.0.0.1", 0))
                number = port.getsockname()[1]
            proc = subprocess.Popen(["redis-server", "--bind", "127.0.0.1", "--port", str(number),
                                     "--save", "", "--appendonly", "no", "--dir", str(tmp_path)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(proc)
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", number), timeout=.1):
                        break
                except OSError:
                    time.sleep(.02)
            urls.append(f"redis://127.0.0.1:{number}")
        fixture = Path(__file__).parent / "fixtures" / "receipt_pair.cjs"
        node = subprocess.Popen(["node", str(fixture), str(Path(root).resolve()), *urls],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        processes.append(node)
        locations = json.loads(read_line(node))
        client = ReceiptHandoff(parse_config(full_raw()), env={
            "INGRESS_CONTROL_TOKEN": "contract-control", "CLOUD_CONTROL_TOKEN": "contract-control",
            "CLOUD_API_TOKEN": "contract-api"})
        # Production config requires HTTPS. Only this isolated test replaces
        # transport endpoints with dynamically allocated loopback HTTP ports.
        client.urls = {key: locations[key] for key in client.urls}
        def row(number):
            return {"request_id": str(uuid.uuid4()), "run_id": "contract", "phase": "test",
                    "request_no": str(number), "payload": "unchanged", "created_at": "2026-11-08T00:00:00.000Z"}
        first, pending, after = row(1), row(2), row(3)
        source = locations["sourceApi"]
        assert request(source + "/api/requests", first)[0] == 202
        client._call("source", "POST", "/resume", {"expected_epoch": "0"})
        committed(source + "/api/requests/" + first["request_id"])
        client._call("source", "POST", "/pause", {})
        assert request(source + "/api/requests", pending)[0] == 202
        sealed = client.seal(client.precheck())
        assert sealed["manifest"]["count"] == 2
        assert request(source + "/api/requests", after)[0] == 503
        assert client.transfer(sealed)["count"] == 2
        client.activate(sealed)
        client.forward(sealed)
        assert client.verify_destination(sealed)["app_version"] == "fixture-v1"
        cloud = locations["receipt"]
        committed(cloud + "/api/requests/" + pending["request_id"])
        assert request(source + "/api/requests", first)[0] == 200
        node.stdin.write("stop-source\n"); node.stdin.flush()
        assert read_line(node) == "stopped"
        assert request(cloud + "/api/requests", after)[0] == 202
        assert committed(cloud + "/api/requests/" + after["request_id"])["request"] == after
        assert request(cloud + "/api/requests", first)[0] == 200
        assert request(cloud + "/api/requests", {**first, "payload": "different"})[0] == 409
    finally:
        for proc in reversed(processes):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
