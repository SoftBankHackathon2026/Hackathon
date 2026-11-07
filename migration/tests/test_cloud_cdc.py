"""Opt-in real Debezium test using the disposable cloud test Docker network."""

import os
from dataclasses import replace

import pytest
from psycopg import sql
from test_cloud_pg import insert  # reuse disposable databases/roles

from migration.cdc import ConnectClient
from migration.cloud.postgres import Postgres
from migration.cloud.runner import Runner
from migration.cutover import ApprovalRequired
from migration.state import CutoverStore

pytestmark = pytest.mark.skipif(
    not os.getenv("CLOUD_CONNECT_URL"), reason="CLOUD_CONNECT_URL not set"
)


class DockerConnect(ConnectClient):
    def put_config(self, name, config):
        # Test runner connects through published localhost port; Connect runs
        # inside Docker and reaches the same test database by its network alias.
        config = dict(config)
        if "database.hostname" in config:
            config.update({"database.hostname": "cloud-pg", "database.port": "5432"})
        if "connection.url" in config:
            dbname = config["connection.url"].split("/", 3)[-1]
            config["connection.url"] = "jdbc:postgresql://cloud-pg:5432/" + dbname
        return super().put_config(name, config)


class Application:
    def __init__(self, connection, names, roles):
        self.connection, self.names, self.roles = connection, names, roles
        self.state, self.target = "RUNNING", "aws"

    def status(self):
        return {
            "state": self.state,
            "target": self.target,
            "in_flight": 0,
            "known_targets": ["aws", "gcp"],
        }

    def pause(self):
        self.state = "PAUSED"
        return self.status()

    def drain(self, timeout):
        return {"settled": True}

    def switch_target(self, target):
        self.target = target
        return self.status()

    def resume(self):
        with self.connection(self.names[1], self.roles["app"]) as c:
            c.execute("INSERT INTO orders(tenant) VALUES ('a')")
        self.state = "RUNNING"
        return self.status()

    def healthz(self):
        with self.connection(self.names[1], self.roles["app"]) as c:
            healthy = c.execute("SELECT count(*) FROM orders").fetchone()[0] == 3
        return {"healthy": healthy, "target": self.target}


def test_real_snapshot_updates_deletes_and_cutover(pg, tmp_path, monkeypatch):
    database, connection, names, roles = pg
    # Docker Connect provider environment names, not stored passwords.
    c = database.plan.config
    targets = {}
    for name, target in c.targets.items():
        users = dict(target.users)
        for role in ("cdc", "sink"):
            users[role] = replace(
                users[role], password_env="MIGRATION_" + role.upper() + "_PASSWORD"
            )
        targets[name] = replace(target, users=users)
    plan = replace(
        database.plan,
        config=replace(
            c,
            targets=targets,
            cutover=replace(
                c.cutover, stage_timeout_seconds=180, poll_interval_seconds=1
            ),
        ),
    )
    database = Postgres(plan)
    with connection("postgres") as admin:
        admin.execute(
            sql.SQL("ALTER ROLE {} REPLICATION").format(sql.Identifier(roles["cdc"]))
        )
        admin.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                sql.Identifier(names[0]), sql.Identifier(roles["cdc"])
            )
        )
    with connection(names[0], roles["app"]) as src:
        insert(src)
        src.execute("INSERT INTO orders(tenant,amount) VALUES ('delete',1)")
    connect = DockerConnect(os.environ["CLOUD_CONNECT_URL"])
    app = Application(connection, names, roles)
    store = CutoverStore(tmp_path)
    runner = Runner(plan, store, database, connect, app)
    try:
        with pytest.raises(ApprovalRequired):
            runner.run()
        assert store.load(plan.config.job.job_id).stage == "READY"
        with connection(names[0], roles["app"]) as src:
            src.execute("UPDATE orders SET optional='changed' WHERE tenant='a'")
            src.execute("DELETE FROM orders WHERE tenant='delete'")
            src.execute("INSERT INTO orders(tenant,amount) VALUES ('new',2.123456789)")
        runner.approve = "local-test"
        result = runner.run()
        assert result.stage == "COMPLETED"
        assert all(
            not any(row[k] for k in ("missing", "extra", "changed"))
            for row in result.checkpoint["verify_after_stop"]["tables"].values()
        )
        with connection(names[1]) as tgt:
            assert (
                tgt.execute(
                    "SELECT optional FROM orders WHERE tenant='a' AND id=1"
                ).fetchone()[0]
                == "changed"
            )
            assert (
                tgt.execute(
                    "SELECT count(*) FROM orders WHERE tenant='delete'"
                ).fetchone()[0]
                == 0
            )
    finally:
        for name in runner.docs:
            connect.delete(name)
        # Dropping disposable source DB removes its slot after connector shutdown.
        store.close()
