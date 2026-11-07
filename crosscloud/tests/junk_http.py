"""시험용 127.0.0.1 서버. HTTP 가 아닌 줄을 돌려준다(BadStatusLine)."""

import contextlib
import socket
import threading


@contextlib.contextmanager
def serve():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def run():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.recv(65536)
                connection.sendall(b"garbage\r\n\r\n")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        stop.set()
        thread.join()
        listener.close()
