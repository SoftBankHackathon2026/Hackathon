"""One-use loopback form to pass a real native Chrome paste to the private FIFO.

This does not generate enrollment material, replace clipboard APIs, or call any
product endpoint. The receiver independently validates the exact command before
executing it. No command body is logged or written to a regular file.
"""

import hmac
import html
import os
import secrets
import stat
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ORIGIN = "http://localhost:18327"
PIPE = Path("/private/tmp/onprem-live-e2e/enrollment.pipe")
TOKEN = secrets.token_urlsafe(32)
READ_TIMEOUT = 5


class Handler(BaseHTTPRequestHandler):
    used = False

    def setup(self):
        super().setup()
        self.connection.settimeout(READ_TIMEOUT)

    def log_message(self, *_):
        pass

    def respond(self, status, body):
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path != "/" or self.headers.get("Host") != "localhost:18327":
            return self.respond(404, "없음")
        self.respond(
            200,
            f'''<!doctype html><html lang="ko"><meta charset="utf-8">
<title>실제 명령 전달 검사</title>
<style>body{{font:20px system-ui;padding:40px;max-width:700px}}input{{display:block;width:90%;padding:16px;margin:20px 0}}button{{padding:16px}}</style>
<h1>실제 명령 전달 검사</h1>
<p>Oneport에서 실제 복사한 명령을 붙여넣습니다. 값은 화면에 숨기며 로컬의 비공개 파이프로 한 번만 전달합니다. 일반 파일에 저장하지 않습니다.</p>
<form action="/transfer" method="post" autocomplete="off">
<input type="hidden" name="token" value="{html.escape(TOKEN)}">
<label>복사한 등록 명령<input type="password" name="command" required maxlength="256" autocomplete="off" spellcheck="false"></label>
<button type="submit">격리 Ubuntu 단말에 전달</button></form></html>''',
        )

    def do_POST(self):
        if (
            self.path != "/transfer"
            or self.headers.get("Origin") != ORIGIN
            or self.headers.get("Host") != "localhost:18327"
            or Handler.used
        ):
            return self.respond(403, "전달 거절")
        try:
            for header in ("Host", "Origin", "Content-Length", "Content-Type"):
                if len(self.headers.get_all(header, [])) != 1:
                    raise ValueError("duplicate or missing header")
            if self.headers.get("Transfer-Encoding") is not None:
                raise ValueError("unsupported transfer encoding")
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError("content type")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2048:
                raise ValueError("length")
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("truncated body")
            fields = parse_qs(body.decode(), strict_parsing=True, max_num_fields=2)
            if set(fields) != {"token", "command"} or any(
                len(v) != 1 for v in fields.values()
            ):
                raise ValueError("fields")
            if not hmac.compare_digest(fields["token"][0], TOKEN):
                raise ValueError("token")
            command = fields["command"][0].encode()
            if not 0 < len(command) <= 256:
                raise ValueError("command")
            meta = PIPE.lstat()
            if (
                not stat.S_ISFIFO(meta.st_mode)
                or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) != 0o600
            ):
                raise ValueError("pipe")
            fd = os.open(PIPE, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                opened = os.fstat(fd)
                if (
                    not stat.S_ISFIFO(opened.st_mode)
                    or opened.st_uid != os.getuid()
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or (opened.st_dev, opened.st_ino) != (meta.st_dev, meta.st_ino)
                ):
                    raise ValueError("opened pipe changed")
                if os.write(fd, command) != len(command):
                    raise ValueError("write")
            finally:
                os.close(fd)
            Handler.used = True
        except (ValueError, OSError, UnicodeError):
            return self.respond(400, "전달하지 못했습니다. 실행기 상태를 확인하세요.")
        self.respond(
            200,
            "<meta charset=utf-8><h1>실제 복사 명령을 전달했습니다</h1><p>실행기는 배포 주소와 명령 형식을 독립적으로 검증합니다. 단말 로그와 Oneport에서 진행 상태를 확인하세요.</p>",
        )


if __name__ == "__main__":
    print(
        "One-use native clipboard transfer listening on loopback port 18327", flush=True
    )
    HTTPServer(("127.0.0.1", 18327), Handler).serve_forever()
