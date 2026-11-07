"""시험용 127.0.0.1 HTTP 서버. 경로마다 (상태 코드, 본문)을 돌려준다."""

import contextlib
import http.server
import threading


@contextlib.contextmanager
def serve(routes):
    """routes: {경로: (코드, 본문 bytes) 또는 그것을 돌려주는 함수}."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            route = routes.get(self.path)
            if route is None:
                code, body = 404, b"{}"
            else:
                code, body = route() if callable(route) else route
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
