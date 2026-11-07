"""Consume the real browser clipboard command through a private FIFO.

No seed is written to a regular file, argv, or this runner's environment.
The exact signed-release URL is checked before sending the command to the
isolated Ubuntu user's shell. The shell command is the product's clipboard
command, not a replacement enrollment protocol.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path


def request_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{22}", value):
        raise argparse.ArgumentTypeError(
            "a 22-character public request identifier is required"
        )
    return value


def command_matches(
    command: bytearray, bootstrap_url: str, request_id: str | None = None
) -> bool:
    pattern = (
        rb"curl -qfsS --proto '=https' "
        + re.escape(bootstrap_url.encode())
        + rb" \| MIGRATION_ENROLL='(?P<request_id>[A-Za-z0-9_-]{22})\.[A-Za-z0-9_-]{43}' bash\s*"
    )
    if len(command) > 256:
        return False
    matched = re.fullmatch(pattern, command)
    return matched is not None and (
        request_id is None
        or matched.group("request_id") == request_identifier(request_id).encode()
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request-id",
        type=request_identifier,
        help="Public request ID observed in the product's copy-completed URL",
    )
    args = parser.parse_args()
    os.umask(0o077)
    root = Path("/private/tmp/onprem-live-e2e")
    metadata = json.loads((root / "public/release-metadata.json").read_text())
    pipe = root / "enrollment.pipe"
    if pipe.exists():
        raise RuntimeError("existing enrollment receiver must be inspected first")
    os.mkfifo(pipe, 0o600)
    print("Waiting for the real browser clipboard command on private FIFO", flush=True)
    command = bytearray()
    try:
        if not stat.S_ISFIFO(pipe.lstat().st_mode):
            raise RuntimeError("private FIFO required")
        with pipe.open("rb") as source:
            command.extend(source.read(513))
    finally:
        pipe.unlink(missing_ok=True)
    if not command_matches(command, metadata["bootstrap_url"], args.request_id):
        command[:] = b"\0" * len(command)
        raise RuntimeError(
            "unexpected clipboard command or request identifier; refused execution"
        )
    log = root / f"client-enrollment-{time.time_ns()}.log"
    (root / "client-log-path").write_text(str(log))
    with log.open("xb") as output:
        child = subprocess.Popen(
            ["docker", "exec", "-i", "-u", "connector", "mc-live-e2e-client", "bash"],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            if child.stdin is None:
                raise RuntimeError("private input unavailable")
            # This isolated container does not run systemd. Preserve the
            # validated clipboard command and add the explicit test mode.
            while command and command[-1] in b" \t\r\n":
                command.pop()
            child.stdin.write(command)
            child.stdin.write(b" -s -- --foreground\n")
            child.stdin.close()
        finally:
            command[:] = b"\0" * len(command)
        print(
            "Validated clipboard command sent to isolated Ubuntu in foreground test mode",
            flush=True,
        )
        result = child.wait()
    print(f"Connector process exited: {result}", flush=True)
    raise SystemExit(result)


if __name__ == "__main__":
    main()
