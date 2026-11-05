"""Create a dedicated local demo database without printing its password."""

import json
import secrets
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".data"
DATA.mkdir(exist_ok=True, mode=0o700)
path = DATA / "database.json"
if path.exists():
    config = json.loads(path.read_text())
else:
    password = secrets.token_urlsafe(32)
    config = {"database_url": f"postgresql://launchpad:{password}@127.0.0.1:55433/launchpad"}
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    env = DATA / "postgres.env"
    env.write_text(
        f"POSTGRES_USER=launchpad\nPOSTGRES_PASSWORD={password}\nPOSTGRES_DB=launchpad\n"
    )
    env.chmod(0o600)
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "oneport-platform-postgres",
            "--label",
            "project=oneport-platform",
            "--env-file",
            str(env),
            "-p",
            "127.0.0.1:55433:5432",
            "-v",
            "oneport-platform-postgres:/var/lib/postgresql/data",
            "postgres:16-alpine",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
subprocess.run(
    ["docker", "start", "oneport-platform-postgres"], check=True, stdout=subprocess.DEVNULL
)
for attempt in range(30):
    result = subprocess.run(
        [
            "docker",
            "exec",
            "oneport-platform-postgres",
            "pg_isready",
            "-U",
            "launchpad",
            "-d",
            "launchpad",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        break
    time.sleep(1)
else:
    raise SystemExit("PostgreSQL did not become ready within 30 seconds")
print("Dedicated local PostgreSQL ready on 127.0.0.1:55433")
