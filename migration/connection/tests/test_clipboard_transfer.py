"""Isolated loopback/FIFO regression tests; never contact the live transfer port."""

import http.client
import importlib.util
import os
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

SOURCE = Path(__file__).parent / "e2e/clipboard_transfer.py"


class ClipboardTransferTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("isolated_transfer", SOURCE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.directory = tempfile.TemporaryDirectory()
        self.pipe = Path(self.directory.name) / "isolated.pipe"
        os.mkfifo(self.pipe, 0o600)
        self.module.PIPE = self.pipe
        self.reader = os.open(self.pipe, os.O_RDONLY | os.O_NONBLOCK)
        self.server = HTTPServer(("127.0.0.1", 0), self.module.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        os.close(self.reader)
        self.directory.cleanup()

    def request(
        self,
        fields=None,
        *,
        headers=None,
        body=None,
        extra_headers=(),
        method="POST",
        path="/transfer",
    ):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=2
        )
        payload = (
            body
            if body is not None
            else urlencode(
                fields
                or {
                    "token": self.module.TOKEN,
                    "command": "artificial non-secret command",
                }
            ).encode()
        )
        values = {
            "Host": "localhost:18327",
            "Origin": self.module.ORIGIN,
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(payload)),
        }
        values.update(headers or {})
        connection.putrequest(method, path, skip_host=True)
        for name, value in (*values.items(), *extra_headers):
            connection.putheader(name, value)
        connection.endheaders(payload)
        response = connection.getresponse()
        status, data, response_headers = (
            response.status,
            response.read(),
            dict(response.getheaders()),
        )
        connection.close()
        return status, data, response_headers

    def assert_pipe_empty(self):
        try:
            self.assertEqual(os.read(self.reader, 512), b"")
        except BlockingIOError:
            pass

    def test_actual_fifo_transfer_is_one_use_and_not_echoed(self):
        command = "artificial non-secret command"
        status, body, headers = self.request(
            fields={"token": self.module.TOKEN, "command": command}
        )
        self.assertEqual(status, 200)
        self.assertEqual(os.read(self.reader, 512), command.encode())
        self.assertNotIn(command.encode(), body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.request()[0], 403)
        self.assert_pipe_empty()

    def test_wrong_origin_host_or_nonce_never_reaches_fifo(self):
        for headers in (
            {"Origin": "https://untrusted.invalid"},
            {"Host": "untrusted.invalid"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        self.assertEqual(
            self.request(fields={"token": "wrong", "command": "artificial"})[0], 400
        )
        self.assert_pipe_empty()

    def test_duplicate_fields_and_headers_are_rejected(self):
        fields = [("token", self.module.TOKEN), ("command", "one"), ("command", "two")]
        self.assertEqual(self.request(fields=fields)[0], 400)
        for name, value in (
            ("Host", "localhost:18327"),
            ("Origin", self.module.ORIGIN),
            ("Content-Length", "1"),
            ("Content-Type", "application/x-www-form-urlencoded"),
        ):
            with self.subTest(header=name):
                self.assertEqual(self.request(extra_headers=((name, value),))[0], 400)
        self.assert_pipe_empty()

    def test_wrong_type_encoding_and_size_are_rejected(self):
        for headers in (
            {"Content-Type": "application/json"},
            {"Transfer-Encoding": "chunked"},
            {"Content-Length": "2049"},
            {"Content-Length": "-1"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 400)
        self.assertEqual(self.request(body=b"\xff")[0], 400)
        self.assertEqual(
            self.request(fields={"token": self.module.TOKEN, "command": "x" * 257})[0],
            400,
        )
        self.assert_pipe_empty()

    def test_unsafe_fifo_mode_and_regular_file_are_rejected(self):
        self.pipe.chmod(0o644)
        self.assertEqual(self.request()[0], 400)
        self.pipe.unlink()
        self.pipe.write_bytes(b"unchanged")
        self.pipe.chmod(0o600)
        self.assertEqual(self.request()[0], 400)
        self.assertEqual(self.pipe.read_bytes(), b"unchanged")

    def test_file_substitution_after_path_check_never_writes_regular_file(self):
        original = os.open

        def replace_then_open(path, flags, *args, **kwargs):
            self.pipe.unlink()
            self.pipe.write_bytes(b"unchanged")
            self.pipe.chmod(0o600)
            return original(path, flags, *args, **kwargs)

        with patch.object(self.module.os, "open", side_effect=replace_then_open):
            self.assertEqual(self.request()[0], 400)
        self.assertEqual(self.pipe.read_bytes(), b"unchanged")

    def test_symlink_and_missing_reader_are_rejected(self):
        self.pipe.unlink()
        target = Path(self.directory.name) / "regular"
        target.write_bytes(b"unchanged")
        self.pipe.symlink_to(target)
        self.assertEqual(self.request()[0], 400)
        self.assertEqual(target.read_bytes(), b"unchanged")
        self.pipe.unlink()
        os.mkfifo(self.pipe, 0o600)
        self.assertEqual(self.request()[0], 400)

    def test_stalled_request_times_out_without_fifo_write(self):
        self.module.READ_TIMEOUT = 0.1
        self.assertEqual(
            self.request(body=b"x", headers={"Content-Length": "20"})[0], 400
        )
        self.assert_pipe_empty()

    def test_form_has_hidden_input_and_no_store_headers(self):
        status, body, headers = self.request(method="GET", path="/", body=b"")
        self.assertEqual(status, 200)
        self.assertIn(b'type="password"', body)
        self.assertIn(b'autocomplete="off"', body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
