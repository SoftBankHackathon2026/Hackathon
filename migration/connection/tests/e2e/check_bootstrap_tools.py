"""Run only bootstrap preflight in disposable Linux containers; never enroll."""

import argparse
import subprocess
import uuid
from pathlib import Path

CHECK = r"""
import os
from pathlib import Path
import shutil
import subprocess

# Use the real kubectl shipped in the test image at the location used by K3s.
shutil.move('/usr/bin/kubectl', '/usr/local/bin/kubectl')
# Missing helper must no longer stop preflight. The isolated container has no
# network, so execution then stops at the genuine release download attempt.
shutil.move('/usr/local/libexec/migration-net-helper', '/tmp/helper-disabled')
env = dict(os.environ, MIGRATION_ENROLL='A' * 22 + '.' + 'B' * 43)

def run():
    result = subprocess.run(['bash', '/bootstrap.sh', '--foreground'], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0, 'preflight must stop before enrollment'
    assert env['MIGRATION_ENROLL'] not in result.stdout + result.stderr
    return result.stderr

error = run()
assert '초기 프로그램 다운로드에 실패했습니다.' in error
assert '관리자가 네트워크 보조 프로그램과 정책을 먼저 설치해야 합니다.' not in error
print('PASS: real kubectl in /usr/local/bin passes; missing helper does not block bootstrap')
Path('/usr/local/bin/kubectl').unlink()
assert '필수 단말 도구를 찾지 못했습니다: kubectl' in run()
print('PASS: missing kubectl is identified explicitly')
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", required=True, type=Path)
    parser.add_argument("--arch", choices=("amd64", "arm64"), default="amd64")
    parser.add_argument(
        "--image", help="Native test image built from the current source"
    )
    args = parser.parse_args()
    script = args.bootstrap.resolve(strict=True)
    image = args.image or (
        "mc-live-e2e-native:amd64"
        if args.arch == "amd64"
        else "mc-live-e2e-native:local"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            f"linux/{args.arch}",
            "--name",
            "mc-bootstrap-tools-" + uuid.uuid4().hex[:10],
            "--network",
            "none",
            "--label",
            "migration.connection.live-e2e=true",
            "--mount",
            f"type=bind,source={script},target=/bootstrap.sh,readonly",
            "--entrypoint",
            "python3",
            image,
            "-c",
            CHECK,
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
