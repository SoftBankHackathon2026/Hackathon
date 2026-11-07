import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

EC2 = Path(__file__).resolve().parents[2] / "deploy" / "ec2"

SHIMS = {
    "docker": """#!/usr/bin/env bash
echo "docker $*" >> "$SHIM_LOG"
[[ "$1" == login ]] && cat > /dev/null
if [[ "$1 $2" == "compose up" ]]; then
  echo "up tag=$(sed -n 's/^ONEPORT_TAG=//p' .env)" >> "$SHIM_LOG"
fi
exit 0
""",
    "aws": "#!/usr/bin/env bash\necho token\n",
    "modprobe": "#!/usr/bin/env bash\nexit 0\n",
    "curl": "#!/usr/bin/env bash\nexit 0\n",
    "install": """#!/usr/bin/env bash
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
done
exec /usr/bin/install "${args[@]}"
""",
}

FAKE_CHECK = """#!/usr/bin/env bash
echo "check wait=$3" >> "$SHIM_LOG"
queue="$ONEPORT_ROOT/.check_queue"
line=$(head -n1 "$queue" 2>/dev/null || true)
sed -i.bak '1d' "$queue" 2>/dev/null; rm -f "$queue.bak"
case "$line" in fail:*) echo "${line#fail:}" >&2; exit 1 ;; *) exit 0 ;; esac
"""


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def server(tmp_path):
    shims = tmp_path / "shims"
    shims.mkdir()
    for name, text in SHIMS.items():
        executable(shims / name, text)
    if shutil.which("sha256sum") is None:
        executable(shims / "sha256sum", '#!/usr/bin/env bash\nexec shasum -a 256 "$@"\n')

    root = tmp_path / "oneport"
    for name in ("postgres-password", "database-url", "ingest-token"):
        (root / "secrets").mkdir(parents=True, exist_ok=True)
        (root / "secrets" / name).write_text("x")
    (root / "settings.env").write_text("ONEPORT_DOMAIN=platform.example\n")
    (root / ".env").write_text("ONEPORT_TAG=old\n")
    (root / "connection" / "broker").mkdir(parents=True)
    (root / "connection" / "server.env").write_text("ONEPORT_CONNECTION_ENABLED=true\n")
    (root / "connection" / "broker" / "broker.json").write_text('{"release_version": "conn-1"}')

    release = tmp_path / "release"
    release.mkdir()
    for name in ("compose.yaml", "Caddyfile", "fleet.json", "cost.json"):
        (release / name).write_text("x")
    executable(release / "check-connection.sh", FAKE_CHECK)
    return tmp_path


def deploy(server, *, broker_tag="broker-1", checks=(), release_has_check=True):
    root, release, log = server / "oneport", server / "release", server / "calls.log"
    if not release_has_check:
        (release / "check-connection.sh").unlink()
    (root / ".check_queue").write_text("".join(f"{line}\n" for line in checks))
    log.write_text("")
    result = subprocess.run(
        ["bash", str(EC2 / "deploy.sh"), str(release), "registry.example", "tag123", broker_tag],
        env={
            **os.environ,
            "PATH": f"{server / 'shims'}:{os.environ['PATH']}",
            "ONEPORT_ROOT": str(root),
            "SHIM_LOG": str(log),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, log.read_text().splitlines()


def env_value(server, key):
    match = re.search(rf"^{key}=(.*)$", (server / "oneport" / ".env").read_text(), re.M)
    return match.group(1) if match else None


def test_without_connection_the_check_never_runs(server):
    result, calls = deploy(server, broker_tag="")
    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("check") for call in calls)
    assert env_value(server, "COMPOSE_PROFILES") == ""
    assert env_value(server, "ONEPORT_CONNECTION_CONFIG_HASH") == "none"


def test_with_connection_the_check_runs_before_and_after(server):
    result, calls = deploy(server, checks=["ok", "ok"])
    assert result.returncode == 0, result.stderr
    assert [c for c in calls if c.startswith("check")] == ["check wait=0", "check wait=120"]
    assert env_value(server, "COMPOSE_PROFILES") == "connection"
    assert re.fullmatch(r"[0-9a-f]{16}", env_value(server, "ONEPORT_CONNECTION_CONFIG_HASH"))
    assert env_value(server, "ONEPORT_TAG") == "tag123"


def test_a_regression_after_the_deploy_rolls_back(server):
    result, calls = deploy(server, checks=["ok", "fail:연결 기능이 꺼져 있습니다"])
    assert result.returncode == 1
    assert "배포 실패" in result.stderr and "연결 기능이 꺼져 있습니다" in result.stderr
    assert [c for c in calls if c.startswith("up")] == ["up tag=tag123", "up tag=old"]
    assert env_value(server, "ONEPORT_TAG") == "old"


def test_a_problem_that_was_already_there_only_warns(server):
    result, calls = deploy(server, checks=["fail:이미 꺼져 있었음", "fail:여전히 꺼져 있음"])
    assert result.returncode == 0, result.stderr
    assert "경고" in result.stderr and "여전히 꺼져 있음" in result.stderr
    assert [c for c in calls if c.startswith("up")] == ["up tag=tag123"]
    assert env_value(server, "ONEPORT_TAG") == "tag123"


def test_an_old_release_without_the_check_script_still_deploys(server):
    result, calls = deploy(server, release_has_check=False)
    assert result.returncode == 0, result.stderr
    assert not any(call.startswith("check") for call in calls)


def test_the_config_hash_follows_the_broker_files_only(server):
    root = server / "oneport"
    deploy(server, checks=["ok", "ok"])
    first = env_value(server, "ONEPORT_CONNECTION_CONFIG_HASH")

    deploy(server, checks=["ok", "ok"])
    assert env_value(server, "ONEPORT_CONNECTION_CONFIG_HASH") == first

    (root / "connection" / "server.env").write_text("ONEPORT_CONNECTION_ENABLED=false\n")
    (root / "secrets" / "ingest-token").write_text("changed")
    deploy(server, checks=["ok", "ok"])
    assert env_value(server, "ONEPORT_CONNECTION_CONFIG_HASH") == first

    (root / "connection" / "broker" / "broker.json").write_text('{"release_version": "conn-2"}')
    deploy(server, checks=["ok", "ok"])
    assert env_value(server, "ONEPORT_CONNECTION_CONFIG_HASH") != first

    (root / "connection" / "broker" / "gateway-key.pem").write_text("new key")
    changed = env_value(server, "ONEPORT_CONNECTION_CONFIG_HASH")
    deploy(server, checks=["ok", "ok"])
    assert env_value(server, "ONEPORT_CONNECTION_CONFIG_HASH") != changed


def test_the_broker_is_not_restarted_when_the_api_is_recreated():
    services = yaml.safe_load((EC2 / "compose.yaml").read_text())["services"]
    broker = services["connection-broker"]
    assert "oneport-api" not in broker["depends_on"]
    assert (
        broker["environment"]["ONEPORT_BROKER_CONFIG_REVISION"]
        == "${ONEPORT_CONNECTION_CONFIG_HASH:-none}"
    )
