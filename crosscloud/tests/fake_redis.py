"""시험용 127.0.0.1 Redis 흉내. 명령 하나에 준비한 RESP 바이트로 답한다."""

import contextlib
import socket
import threading


@contextlib.contextmanager
def serve(reply):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    received = []
    stop = threading.Event()

    def run():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                received.append(connection.recv(65536))
                connection.sendall(reply)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield f"redis://127.0.0.1:{listener.getsockname()[1]}", received
    finally:
        stop.set()
        thread.join()
        listener.close()
