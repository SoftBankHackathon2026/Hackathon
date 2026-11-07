"""Isolated real backend for the manual browser/native acceptance exercise.

Secrets stay in a protected temporary directory. No API or crypto readiness bypass.
"""

import argparse
import hashlib
import http.cookiejar
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DIR = Path("/private/tmp/onprem-live-e2e")


def protected(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
    path.chmod(0o600)


def prepare(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    password_file = directory / "postgres-password"
    if not password_file.exists():
        protected(password_file, secrets.token_urlsafe(32))
    password = password_file.read_text().strip()
    protected(
        directory / "database-url",
        f"postgresql://live_e2e:{password}@postgres:5432/live_e2e",
    )
    credentials = directory / "browser-credentials.json"
    if not credentials.exists():
        protected(
            credentials,
            json.dumps(
                {
                    "email": "owner@live-e2e.test",
                    "password": secrets.token_urlsafe(24),
                    "team": "live-e2e-team",
                }
            ),
        )
    (directory / "release").mkdir(mode=0o755, exist_ok=True)
    (directory / "release").chmod(0o755)


def environment(directory):
    env = os.environ.copy()
    env["LIVE_E2E_DIR"] = str(directory)
    metadata_path = directory / "public/release-metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    version = metadata.get("release_version", "live-e2e-1")
    if (
        not isinstance(version, str)
        or not version
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in version
        )
    ):
        raise ValueError("invalid release version")
    env["LIVE_E2E_RELEASE_VERSION"] = version
    env["LIVE_E2E_BOOTSTRAP_URL"] = (
        f"https://control.test/releases/{version}/bootstrap.sh"
    )
    bootstrap = directory / "release/releases" / version / "bootstrap.sh"
    env["BOOTSTRAP_SHA384"] = (
        hashlib.sha384(bootstrap.read_bytes()).hexdigest()
        if bootstrap.is_file()
        else ""
    )
    return env


def compose(directory, *arguments):
    subprocess.run(
        ["docker", "compose", "-f", str(HERE / "control.compose.yaml"), *arguments],
        env=environment(directory),
        check=True,
    )


def ensure_network():
    found = subprocess.run(
        ["docker", "network", "inspect", "mc-live-e2e"],
        capture_output=True,
        check=False,
    )
    if found.returncode:
        subprocess.run(
            [
                "docker",
                "network",
                "create",
                "--subnet",
                "172.30.87.0/24",
                "mc-live-e2e",
            ],
            check=True,
        )
    else:
        config = json.loads(found.stdout)[0]["IPAM"]["Config"]
        if not any(item.get("Subnet") == "172.30.87.0/24" for item in config):
            raise RuntimeError("Existing test network has an unexpected subnet")


def account(directory):
    credentials = json.loads((directory / "browser-credentials.json").read_text())
    jar = http.cookiejar.MozillaCookieJar(str(directory / "browser-session.cookies"))
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base = "http://127.0.0.1:18320/api/v1"
    for attempt in range(60):
        try:
            with opener.open(base + "/access", timeout=2) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    else:
        raise RuntimeError("Backend did not become reachable")

    def post(action, body):
        request = urllib.request.Request(
            base + "/auth/" + action,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://localhost:4327",
            },
        )
        with opener.open(request, timeout=10) as response:
            return None if response.status == 204 else json.load(response)

    try:
        identity = post("signup", credentials)
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise RuntimeError(f"Normal signup failed: HTTP {error.code}") from None
        identity = post(
            "login", {key: credentials[key] for key in ("email", "password")}
        )
    if identity.get("team") != "live-e2e-team" or identity.get("role") != "owner":
        raise RuntimeError(
            "Actual login did not establish the expected owner organization"
        )
    # Exercise a fresh normal login, not just the signup-created session.
    post("logout", {})
    post("login", {key: credentials[key] for key in ("email", "password")})
    protected(directory / "browser-session.cookies", "")
    jar.save(ignore_discard=True, ignore_expires=True)
    os.chmod(jar.filename, 0o600)
    print(
        "Real PostgreSQL-backed signup/login completed; credentials and cookies are protected files."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "build", "up", "account", "down"])
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if directory != DEFAULT_DIR:
        raise SystemExit(
            "This manual test is restricted to /private/tmp/onprem-live-e2e"
        )
    if args.action == "down":
        compose(directory, "down", "--volumes")
        return
    prepare(directory)
    if args.action == "build":
        compose(directory, "build", "backend")
    elif args.action == "up":
        for name in (
            "job-signing-key.pem",
            "runtime-token",
            "control.crt",
            "control.key",
            "ca.crt",
        ):
            if not (directory / name).is_file():
                raise RuntimeError(
                    "Required test key or certificate is missing: " + name
                )
        ensure_network()
        compose(directory, "up", "-d", "--no-build")
    elif args.action == "account":
        account(directory)


if __name__ == "__main__":
    main()
