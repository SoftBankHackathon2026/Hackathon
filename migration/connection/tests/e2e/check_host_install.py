"""Exercise administrator-approved installation in a new disposable Ubuntu container.

Only the uniquely named test container is changed. Supply a real signed bundle,
including its architecture-specific helper and client network policy. No enrollment,
gateway, remote cluster, or running acceptance-test container is used.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import re
import secrets
import select
import subprocess
import time
import uuid
from pathlib import Path

PREPARE = r"""
import os
from pathlib import Path
import subprocess

subprocess.run(['apt-get', 'purge', '-y', 'iproute2', 'wireguard-tools', 'nftables'],
               check=True, stdout=subprocess.DEVNULL)
for path in ['/usr/local/libexec/migration-net-helper',
             '/etc/migration-connector/net-policy.json',
             '/etc/sudoers.d/migration-connector',
             '/etc/sudoers.d/migration-connector-1001']:
    Path(path).unlink(missing_ok=True)
Path('/etc/sudoers.d/admin-install-test').write_text('connector ALL=(root) ALL\n')
Path('/etc/sudoers.d/admin-install-test').chmod(0o440)
Path('/home/connector/.local/state/migration-connector').mkdir(parents=True, exist_ok=True)
subprocess.run(['chown', '-R', '1001:1001', '/home/connector'], check=True)
for path in [Path('/home/connector/bundle'),
             Path('/home/connector/.local/state/migration-connector')]:
    path.chmod(0o700)
for path in Path('/home/connector/bundle').iterdir():
    path.chmod(0o700 if path.name == 'migration-connector' else 0o600)
"""

SNAPSHOT = r"""
import hashlib
import json
from pathlib import Path
import subprocess

files = {}
for text in ['/usr/local/libexec/migration-net-helper',
             '/etc/migration-connector/net-policy.json',
             '/etc/migration-connector/release-sequence']:
    path = Path(text)
    if path.exists():
        stat = path.lstat()
        files[text] = [stat.st_uid, stat.st_gid, stat.st_mode,
                       hashlib.sha384(path.read_bytes()).hexdigest()]
    else:
        files[text] = None
for path in sorted(Path('/etc/sudoers.d').glob('migration-*')):
    stat = path.lstat()
    files[str(path)] = [stat.st_uid, stat.st_gid, stat.st_mode,
                        hashlib.sha384(path.read_bytes()).hexdigest()]
packages = subprocess.run(['dpkg-query', '-W', '-f=${binary:Package} ${db:Status-Abbrev}\n',
                           'iproute2', 'wireguard-tools', 'nftables'],
                          text=True, capture_output=True).stdout
print(json.dumps({'files': files, 'packages': packages}, sort_keys=True))
"""

CHECK = r"""
import json
from pathlib import Path
import subprocess

for path, mode in [('/usr/local/libexec/migration-net-helper', 0o755),
                   ('/etc/migration-connector/net-policy.json', 0o644),
                   ('/etc/sudoers.d/migration-connector-1001', 0o440)]:
    stat = Path(path).lstat()
    assert stat.st_uid == 0 and stat.st_gid == 0 and stat.st_mode & 0o777 == mode, path
policy = json.loads(Path('/etc/migration-connector/net-policy.json').read_text())
assert policy['uid'] == 1001 and policy['role'] == 'client'
for package in ['iproute2', 'wireguard-tools', 'nftables']:
    result = subprocess.run(['dpkg-query', '-W', '-f=${db:Status-Status}', package],
                            capture_output=True, text=True, check=True)
    assert result.stdout == 'installed', package
subprocess.run(['visudo', '-cf', '/etc/sudoers.d/migration-connector-1001'],
               check=True, stdout=subprocess.DEVNULL)
Path('/etc/sudoers.d/admin-install-test').unlink()
request = json.dumps({'operation':'check', 'connection_id':'preflight0000000000',
                      'epoch':0, 'private_key':None, 'public_key':None,
                      'psk':None, 'key_expires_at_ms':0})
result = subprocess.run(['runuser', '-u', 'connector', '--', 'sudo', '-n',
                         '/usr/local/libexec/migration-net-helper'],
                        input=request, capture_output=True, text=True, check=True)
assert json.loads(result.stdout)['ready'] is True
denied = subprocess.run(['runuser', '-u', 'connector', '--', 'sudo', '-n',
                         '/usr/local/libexec/migration-net-helper', 'unexpected'],
                        input=request, capture_output=True, text=True)
assert denied.returncode != 0, 'additional helper arguments must be denied'
"""


class Container:
    def __init__(self, name: str):
        self.name = name

    def run(self, *args: str, input: str | None = None, check: bool = True):
        return subprocess.run(
            ["docker", "exec", "-i", self.name, *args],
            input=input,
            capture_output=True,
            text=True,
            timeout=600,
            check=check,
        )

    def python(self, source: str):
        return self.run("python3", "-c", source)

    def snapshot(self):
        return json.loads(self.python(SNAPSHOT).stdout)

    def command(self):
        return [
            "docker",
            "exec",
            "-i",
            "--user",
            "1001:1001",
            "--env",
            "HOME=/home/connector",
            self.name,
            "/home/connector/bundle/migration-connector",
            "prepare-host",
            "/home/connector/bundle",
        ]

    def without_tty(self):
        return subprocess.run(
            self.command(), capture_output=True, text=True, timeout=30, check=False
        )

    def with_tty(self, approve: bool, password: str):
        """Use an actual pseudo-terminal; never print the password or raw transcript."""
        master, slave = pty.openpty()
        command = self.command()
        command.insert(3, "-t")
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        transcript = bytearray()
        consent_sent = False
        password_sent = False
        deadline = time.monotonic() + 600
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "administrator installation exceeded ten minutes"
                    )
                readable, _, _ = select.select([master], [], [], 0.2)
                if not readable:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                transcript.extend(chunk)
                # Keep the consent marker ASCII and stable in the product prompt.
                if not consent_sent and b"[y/N]" in transcript:
                    os.write(master, b"yes\n" if approve else b"no\n")
                    consent_sent = True
                if approve and consent_sent and not password_sent:
                    text = transcript.decode(errors="replace")
                    if re.search(r"\[(?:sudo|migration-admin)\][^\r\n]*:\s*$", text):
                        os.write(master, password.encode() + b"\n")
                        password_sent = True
            result = process.wait(timeout=10)
            if not consent_sent:
                raise AssertionError(
                    "the explicit administrator consent prompt was not shown"
                )
            if password.encode() in transcript:
                raise AssertionError(
                    "administrator password was echoed in terminal output"
                )
            return result, password_sent
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.wait()
            for index in range(len(transcript)):
                transcript[index] = 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--image", default="mc-live-e2e-native:local")
    parser.add_argument("--arch", choices=("arm64", "amd64"), default="arm64")
    args = parser.parse_args()
    bundle = args.bundle.resolve(strict=True)
    for name in [
        "migration-connector",
        "migration-net-helper",
        "net-policy.json",
        "manifest.json",
    ]:
        path = bundle / name
        if path.is_symlink() or not path.is_file():
            raise SystemExit("signed regular installation artifact required: " + name)
    name = "mc-admin-install-test-" + uuid.uuid4().hex[:10]
    container = Container(name)
    password = secrets.token_urlsafe(24)
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--platform",
                "linux/" + args.arch,
                "--name",
                name,
                "--label",
                "migration.connection.admin-install-test=true",
                "--cap-add",
                "NET_ADMIN",
                args.image,
            ],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["docker", "cp", str(bundle), name + ":/home/connector/bundle"],
            capture_output=True,
            check=True,
        )
        container.python(PREPARE)
        container.run("chpasswd", input="connector:" + password + "\n")
        before = container.snapshot()
        container.python(
            "from pathlib import Path\n"
            "p=Path('/home/connector/bundle/migration-net-helper')\n"
            "p.with_suffix('.original').write_bytes(p.read_bytes())\n"
            "p.write_bytes(p.read_bytes()+b'tampered')\n"
        )
        assert container.without_tty().returncode != 0
        assert container.snapshot() == before
        container.python(
            "from pathlib import Path\n"
            "p=Path('/home/connector/bundle/migration-net-helper')\n"
            "p.write_bytes(p.with_suffix('.original').read_bytes())\n"
            "p.with_suffix('.original').unlink()\n"
        )
        print("PASS: tampered helper is rejected before administrator installation")
        assert container.without_tty().returncode != 0
        assert container.snapshot() == before, "no terminal must leave host unchanged"
        print("PASS: no terminal rejects installation without host changes")
        status, prompted = container.with_tty(False, password)
        assert status != 0 and not prompted
        assert container.snapshot() == before, "declining must leave host unchanged"
        print("PASS: administrator decline leaves packages and root files unchanged")
        status, prompted = container.with_tty(True, password)
        assert status == 0 and prompted, (
            "real administrator authentication and installation required"
        )
        container.python(CHECK)
        print(
            "PASS: real sudo authentication, apt packages, root helper/policy and restricted sudo rule"
        )
        after = container.snapshot()
        assert container.without_tty().returncode == 0
        assert container.snapshot() == after, (
            "healthy rerun must not reinstall or alter policy"
        )
        print("PASS: healthy rerun needs no terminal or administrator prompt")
        # A conflicting administrator-owned policy must not be silently replaced.
        container.python(
            "from pathlib import Path\nimport json\n"
            "p=Path('/etc/migration-connector/net-policy.json')\n"
            "v=json.loads(p.read_text()); v['uid']=2002; p.write_text(json.dumps(v))\n"
            "p=Path('/etc/sudoers.d/admin-install-test')\n"
            "p.write_text('connector ALL=(root) ALL\\n'); p.chmod(0o440)\n"
        )
        conflict = container.snapshot()
        status, _ = container.with_tty(True, password)
        assert status != 0
        assert container.snapshot() == conflict
        print("PASS: conflicting administrator policy is retained and rejected")
    except subprocess.CalledProcessError as error:
        # Never dump command arguments, stdin, environment, or raw output.
        raise SystemExit(
            "isolated host-install check failed, exit " + str(error.returncode)
        ) from None
    finally:
        password = ""
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()
