"""Notion "플랫폼 개발 범위" items beyond the deploy path: image build/push,
secrets, logs, status, restore (bake 이후 롤백)."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from controlplane.domain.models import DeploymentRequest, ImageSource, Runtime
from controlplane.providers.contracts import ReleaseKey
from oneport_aws.providers.oneport_ecs import logs as app_logs
from oneport_aws.providers.oneport_ecs.registry import BuildFailed, EcrImageBuilder
from oneport_aws.providers.oneport_ecs.secrets import put_secret

COMMIT = "a" * 40


def git_request(**fields):
    base = {
        "application": "shop",
        "environment_id": "aws-oneport",
        "cpu": "0.5",
        "memory_mib": 1024,
        "source": {
            "kind": "git",
            "repository": "https://github.com/example/shop",
            "commit": COMMIT,
        },
    }
    return DeploymentRequest(**{**base, **fields})


class FakeDocker:
    """Records commands; on `buildx --push` pretends the registry stored the image."""

    def __init__(self, aws, environment, fail_on=None):
        self.aws, self.environment, self.fail_on, self.commands = aws, environment, fail_on, []

    def __call__(self, args, log, *, timeout, stdin=None, cwd=None):
        self.commands.append(args)
        if self.fail_on and self.fail_on in args:
            raise BuildFailed("`docker buildx` 실패 (exit 1)", "ERROR: failed to solve")
        if args[:2] == ["git", "checkout"]:
            (Path(cwd) / "Dockerfile").write_text("FROM scratch\n")
        if args[0] == "docker" and "buildx" in args:
            digest = self.aws.push(
                self.environment.repository, tag=args[args.index("-t") + 1].rsplit(":", 1)[1]
            )
            metadata = Path(args[args.index("--metadata-file") + 1])
            metadata.write_text(json.dumps({"containerimage.digest": digest}))


# ---------------------------------------------------------------- build / push


def test_build_pushes_commit_tag_for_amd64(aws, environment):
    docker = FakeDocker(aws, environment)
    built = EcrImageBuilder(aws.ecr, environment, run=docker).build(
        git_request(), log=lambda _: None
    )

    assert isinstance(built.source, ImageSource)
    assert built.source.image.startswith(f"{environment.repository_uri}@sha256:")
    buildx = next(c for c in docker.commands if c[0] == "docker" and "buildx" in c)
    assert buildx[buildx.index("--platform") + 1] == "linux/amd64"
    assert buildx[buildx.index("-t") + 1] == f"{environment.repository_uri}:{COMMIT}"
    assert not any("login" in c for c in docker.commands)
    assert "fake-registry-password" not in str(docker.commands)


def test_already_pushed_commit_is_reused_not_rebuilt(aws, environment):
    digest = aws.push(environment.repository, tag=COMMIT)
    docker = FakeDocker(aws, environment)
    built = EcrImageBuilder(aws.ecr, environment, run=docker).build(
        git_request(), log=lambda _: None
    )
    assert built.source.image.endswith(digest)
    assert docker.commands == []  # IMMUTABLE tag: no login, no build, no push


def test_build_failure_is_rejected_with_output_tail(aws, environment):
    docker = FakeDocker(aws, environment, fail_on="buildx")
    with pytest.raises(BuildFailed) as exc:
        EcrImageBuilder(aws.ecr, environment, run=docker).build(git_request(), log=lambda _: None)
    assert "failed to solve" in exc.value.detail


def test_built_request_passes_deploy_validation(aws, environment, provider):
    built = EcrImageBuilder(aws.ecr, environment, run=FakeDocker(aws, environment)).build(
        git_request(), log=lambda _: None
    )
    provider.validate(built)


# ---------------------------------------------------------------- secrets


def test_secret_is_written_never_read_and_pinned(aws, provider):
    ref = put_secret(provider, "db-url", "postgres://user:pw@host/db")
    name, version = "oneport/shop/db-url", ref.rsplit(":", 1)[1]
    assert ref.startswith(aws.secret_store[name]["ARN"]) and ref.count(":") == 9
    assert aws.secret_store[name]["versions"][version] == "postgres://user:pw@host/db"

    ref2 = put_secret(provider, "db-url", "postgres://rotated")
    assert (
        ref2 != ref and len(aws.secret_store[name]["versions"]) == 2
    )  # old release keeps old version
    assert "secrets.get_secret_value" not in aws.calls


def test_pinned_secret_ref_is_accepted_by_task_definition(aws, provider, make_request):
    ref = put_secret(provider, "api-key", "s3cr3t")
    env = provider.environment.model_copy(update={"secret_refs": {"api-key": ref}})
    p = type(provider)(aws, env)
    request = make_request(secret_bindings=[{"name": "API_KEY", "binding": "api-key"}])
    p.initialize(request)
    td = aws.task_definitions[p.register_task(request, ReleaseKey(value="9" * 24), delay=0)]
    assert td["containerDefinitions"][0]["secrets"] == [{"name": "API_KEY", "valueFrom": ref}]
    assert "s3cr3t" not in json.dumps(td)


def test_secret_binding_name_rules(provider):
    with pytest.raises(ValueError):
        put_secret(provider, "Bad Name", "x")
    with pytest.raises(ValueError):
        put_secret(provider, "ok", "")


# ---------------------------------------------------------------- logs / status


def test_logs_are_scoped_to_the_app(aws, provider):
    aws.log_events = [
        {"timestamp": 1_700_000_000_000, "logStreamName": "shop/app/t1", "message": "hello\n"},
        {"timestamp": 1_700_000_000_000, "logStreamName": "blog/app/t2", "message": "other app"},
    ]
    assert [line.message for line in app_logs.recent(provider)] == ["hello"]
    assert app_logs.task_log(provider, "t1")[0].stream == "shop/app/t1"
    rows = app_logs.query(provider, filter_text="ERROR")
    assert rows == [{"@message": "boom"}]
    assert 'filter app = "shop"' in aws.last_query and 'like "ERROR"' in aws.last_query
    with pytest.raises(ValueError):
        app_logs.query(provider, filter_text='x" | stats count(*)')


def test_latest_logs_keep_the_newest_lines_of_the_window(aws, provider):
    aws.log_page_size = 2
    aws.log_events = [
        {"timestamp": 1_700_000_000_000 + i, "logStreamName": "shop/app/t1", "message": f"m{i}"}
        for i in range(5)
    ] + [{"timestamp": 1_700_000_000_009, "logStreamName": "blog/app/t2", "message": "m9"}]
    lines, truncated = app_logs.latest(provider, limit=3)
    assert [line.message for line in lines] == ["m2", "m3", "m4"] and not truncated
    lines, truncated = app_logs.latest(provider, limit=3, max_pages=2)
    assert [line.message for line in lines] == ["m1", "m2", "m3"] and truncated
    lines, _ = app_logs.latest(provider, filter_text="m3")
    assert [line.message for line in lines] == ["m3"]
    with pytest.raises(ValueError):
        app_logs.latest(provider, filter_text='m" OR "x')


def test_status_reads_service_deployment_and_traffic(aws, provider, make_request):
    assert provider.status().exists is False
    request = make_request()
    routing = provider.initialize(request)
    key = ReleaseKey(value="1" * 24)
    td = provider.register_task(request, key, delay=0)
    provider.submit(request, key, td, routing)
    aws.services["shop"].update(
        desiredCount=1,
        runningCount=1,
        pendingCount=0,
        deployments=[{"status": "PRIMARY", "rolloutState": "IN_PROGRESS"}],
    )
    aws.target_health[routing["blue"]] = ["healthy"]
    aws.advance("shop", "IN_PROGRESS", "BAKE_TIME")

    status = provider.status()
    assert status.release == key.value and status.running == 1
    assert asdict(status.deployment) == {
        "status": "IN_PROGRESS",
        "stage": "BAKE_TIME",
        "reason": None,
    }
    assert asdict(status.traffic["oneport-shop-blue"]) == {
        "weight": 100,
        "healthy": 1,
        "targets": 1,
    }


# ---------------------------------------------------------------- restore after bake


@pytest.fixture
def restore_harness(store, aws, environment, env_file, make_request):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from oneport_aws.candidates import CandidateGateway
    from oneport_aws.execution.oneport_ecs_apps import AppRegistry
    from oneport_aws.execution.oneport_ecs_runtime import OneportEcsSettings
    from oneport_aws.execution.oneport_ecs_worker import OneportEcsWorker
    from oneport_aws.providers.oneport_ecs import OneportEcsCandidateDriver, OneportEcsProvider

    settings = OneportEcsSettings(
        env_file=env_file,
        application="shop",
        environment_id="aws-oneport",
        secret_refs=environment.secret_refs,
        database_url=store.dsn,
        required_health_samples=2,
    )
    provider = OneportEcsProvider(aws, settings.environment())
    registry = AppRegistry(store.dsn)
    registry.migrate()
    registry.register(settings.environment(), port=8080, health_path="/health")
    driver = OneportEcsCandidateDriver(provider)
    runtime = SimpleNamespace(
        settings=settings,
        provider=provider,
        driver=driver,
        connect=Mock(),
        probe=Mock(return_value=True),
    )
    runtime.gateway = CandidateGateway({("shop", "aws-oneport"): driver})
    worker = OneportEcsWorker(store, runtime)

    def release(request, key, restore_of=None):
        row = store.enqueue(request, key, settings.fingerprint(), runtime=Runtime.AWS_ECS)
        if restore_of:  # what `operator restore --release <id>` records
            registry.set_restore(row["id"], restore_of)
        for _ in range(30):
            with store.connect() as conn:
                conn.execute("UPDATE releases SET next_attempt_at=now()")
            worker.tick()
            current = store.get(row["id"])
            if current["state"] == "waiting":
                aws.advance("shop", "SUCCESSFUL")
            if current["state"] in {"succeeded", "failed", "uncertain"}:
                return current
        raise AssertionError(current["state"])

    return SimpleNamespace(release=release, runtime=runtime, make_request=make_request)


def test_restore_reuses_the_previous_revision(restore_harness, aws, store):
    """Notion "롤백: update_service(이전 revision)" after bake time."""
    h = restore_harness
    v1 = h.release(h.make_request(), "release-key-v1-0001")
    v1_td = aws.services["shop"]["taskDefinition"]
    h.release(h.make_request(), "release-key-v2-0001")
    revisions = len(aws.task_definitions)

    old = DeploymentRequest.model_validate(store.get(v1["id"])["request"])
    h.runtime.probe.reset_mock()
    v3 = h.release(old, "release-key-restore-1", restore_of=v1["id"])

    assert v3["state"] == "succeeded"
    assert aws.services["shop"]["taskDefinition"] == v1_td  # exactly the earlier revision
    assert len(aws.task_definitions) == revisions  # nothing new registered
    checked = {call.args[1].value for call in h.runtime.probe.call_args_list}
    assert checked == {v1["id"]}  # the app answers with the release id baked into that revision
    assert any("revision" in e["message"] and "그대로" in e["message"] for e in v3["events"])


def test_restore_reregisters_when_the_revision_was_cleaned_up(restore_harness, aws, store):
    h = restore_harness
    v1 = h.release(h.make_request(), "release-key-v1-0002")
    v1_td = aws.services["shop"]["taskDefinition"]
    h.release(h.make_request(), "release-key-v2-0002")
    aws.task_definitions[v1_td]["status"] = "INACTIVE"  # deregistered by cleanup

    old = DeploymentRequest.model_validate(store.get(v1["id"])["request"])
    v3 = h.release(old, "release-key-restore-2", restore_of=v1["id"])

    assert v3["state"] == "succeeded"
    new_td = aws.task_definitions[aws.services["shop"]["taskDefinition"]]
    assert new_td["taskDefinitionArn"] != v1_td
    assert new_td["containerDefinitions"][0]["image"] == old.source.image  # same image and settings


# ---------------------------------------------------------------- app registration


def test_registration_allocates_unique_priorities(store, environment):
    from oneport_aws.execution.oneport_ecs_apps import AppRegistry

    registry = AppRegistry(store.dsn)
    registry.migrate()
    shop = registry.register(environment, port=8080, health_path="/health", taken={100})
    blog = registry.register(
        environment.model_copy(update={"application": "blog"}),
        port=3000,
        health_path="/health",
        taken={100},
    )
    assert (shop.rule_priority, blog.rule_priority) == (
        101,
        102,
    )  # 100 already on the listener
    assert shop.domain == "shop.app.yubin.dev"
    again = registry.register(environment, port=9090, health_path="/ready")
    assert again.rule_priority == 101 and again.port == 9090  # update keeps the priority


def test_worker_rejects_unregistered_or_mismatched_app(
    store, aws, environment, env_file, make_request
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from oneport_aws.candidates import CandidateGateway
    from oneport_aws.execution.oneport_ecs_apps import AppRegistry
    from oneport_aws.execution.oneport_ecs_runtime import OneportEcsSettings
    from oneport_aws.execution.oneport_ecs_worker import OneportEcsWorker
    from oneport_aws.providers.oneport_ecs import OneportEcsCandidateDriver, OneportEcsProvider

    settings = OneportEcsSettings(
        env_file=env_file,
        application="shop",
        environment_id="aws-oneport",
        secret_refs=environment.secret_refs,
        database_url=store.dsn,
    )
    provider = OneportEcsProvider(aws, settings.environment())
    runtime = SimpleNamespace(
        settings=settings, provider=provider, connect=Mock(), probe=Mock(return_value=True)
    )
    runtime.gateway = CandidateGateway(
        {("shop", "aws-oneport"): OneportEcsCandidateDriver(provider)}
    )
    worker, registry = OneportEcsWorker(store, runtime), AppRegistry(store.dsn)
    registry.migrate()

    row = store.enqueue(
        make_request(), "unregistered-app-key", settings.fingerprint(), runtime=Runtime.AWS_ECS
    )
    worker.tick()
    assert store.get(row["id"])["error_code"] == "app_not_registered" and not aws.target_groups

    registry.register(settings.environment(), port=3000, health_path="/health")
    row = store.enqueue(
        make_request(), "mismatched-port-key1", settings.fingerprint(), runtime=Runtime.AWS_ECS
    )
    worker.tick()
    assert store.get(row["id"])["error_code"] == "app_registration_mismatch"

    registry.register(settings.environment(), port=8080, health_path="/health")
    row = store.enqueue(
        make_request(), "registered-app-key-1", settings.fingerprint(), runtime=Runtime.AWS_ECS
    )
    worker.tick()
    rule = next(r for r in aws.rules if r["Priority"] != "default")
    assert rule["Priority"] == str(registry.get("shop", "aws-oneport").rule_priority)


# ---------------------------------------------------------------- database (RDS)


def run_db(provider, state="requested", limit=10):
    from oneport_aws.providers.oneport_ecs import database

    steps = []
    for _ in range(limit):
        step = database.advance(provider, state, settle_seconds=0, before_write=lambda _: None)
        steps.append(step)
        state = step.state
        if state in {"ready", "failed"}:
            break
    return steps


def test_db_steps_follow_notion_and_never_read_the_secret(aws, provider):
    steps = run_db(provider)
    assert [s.state for s in steps] == [
        "creating",
        "creating",
        "configuring",
        "configuring",
        "ready",
    ]
    kwargs = aws.db["kwargs"]
    assert aws.db["DBInstanceIdentifier"] == "oneport-db-shop"
    assert kwargs["DBSubnetGroupName"] == "oneport-data" and kwargs["VpcSecurityGroupIds"] == [
        "sg-data"
    ]
    assert kwargs["StorageEncrypted"] and kwargs["Engine"] == "postgres"
    ref = next(s.ref for s in steps if s.ref)
    url = aws.secret_store["oneport/shop/db"]["versions"][ref.rsplit(":", 1)[1]]
    # the URL holds the password set by ModifyDBInstance, not the discarded creation password
    assert url == (
        f"postgresql://app_admin:{aws.db['password']}@oneport-db-shop.c1.ap-northeast-2"
        ".rds.amazonaws.com:5432/shop?sslmode=require"
    )
    assert "secrets.get_secret_value" not in aws.calls


def test_registered_db_is_provisioned_in_background(store, aws, provider):
    from oneport_aws.execution.oneport_ecs_apps import AppRegistry
    from oneport_aws.execution.oneport_ecs_db import provision_step

    registry = AppRegistry(store.dsn)
    registry.migrate()
    row = registry.register(
        provider.environment, port=8080, health_path="/health", database="postgresql"
    )
    assert row.db_state == "requested"
    for _ in range(10):
        result = provision_step(registry, provider, settle_seconds=0)
        if result.state == "ready":
            break
    assert registry.get("shop", "aws-oneport").db_state == "ready"
    assert set(registry.refs("shop", "aws-oneport")) == {"db"}
    with registry.connect() as conn:  # e.g. a re-provision is in progress
        registry.set_db(conn, "shop", "aws-oneport", "creating")
    with pytest.raises(ValueError, match="끌 수 없"):
        registry.register(provider.environment, port=8080, health_path="/health", database="none")


def test_db_app_deploys_with_injected_database_url(store, aws, environment, env_file, make_request):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from oneport_aws.candidates import CandidateGateway
    from oneport_aws.execution.oneport_ecs_apps import AppRegistry
    from oneport_aws.execution.oneport_ecs_runtime import OneportEcsSettings
    from oneport_aws.execution.oneport_ecs_worker import OneportEcsWorker
    from oneport_aws.providers.oneport_ecs import OneportEcsCandidateDriver, OneportEcsProvider

    settings = OneportEcsSettings(
        env_file=env_file,
        application="shop",
        environment_id="aws-oneport",
        database_url=store.dsn,
        required_health_samples=2,
        db_settle_seconds=0,
        legacy_dedicated_provisioning=True,
    )
    provider = OneportEcsProvider(aws, settings.environment())
    runtime = SimpleNamespace(
        settings=settings, provider=provider, connect=Mock(), probe=Mock(return_value=True)
    )
    runtime.gateway = CandidateGateway(
        {("shop", "aws-oneport"): OneportEcsCandidateDriver(provider)}
    )
    worker, registry = OneportEcsWorker(store, runtime), AppRegistry(store.dsn)
    registry.migrate()
    registry.register(
        settings.environment(), port=8080, health_path="/health", database="postgresql"
    )

    request = make_request(resources={"database": "postgresql"})  # the user only ticked "DB 사용"
    early = store.enqueue(
        request, "db-app-too-early-key", settings.fingerprint(), runtime=Runtime.AWS_ECS
    )
    worker.tick()  # one DB step + the release: DB not ready yet
    assert store.get(early["id"])["error_code"] == "db_not_ready"

    for _ in range(10):  # worker keeps provisioning in the background
        worker.tick()
        if registry.get("shop", "aws-oneport").db_state == "ready":
            break
    assert registry.get("shop", "aws-oneport").db_state == "ready"

    row = store.enqueue(
        request, "db-app-release-key-1", settings.fingerprint(), runtime=Runtime.AWS_ECS
    )
    for _ in range(30):
        with store.connect() as conn:
            conn.execute("UPDATE releases SET next_attempt_at=now()")
        worker.tick()
        if store.get(row["id"])["state"] == "waiting":
            aws.advance("shop", "SUCCESSFUL")
        if store.get(row["id"])["state"] in {"succeeded", "failed", "uncertain"}:
            break
    assert store.get(row["id"])["state"] == "succeeded"
    td = aws.task_definitions[aws.services["shop"]["taskDefinition"]]
    assert td["containerDefinitions"][0]["secrets"] == [
        {"name": "DATABASE_URL", "valueFrom": registry.refs("shop", "aws-oneport")["db"]}
    ]


def test_db_requests_need_a_ready_db(provider, make_request):
    from controlplane.providers.contracts import ProviderRejected

    request = make_request(resources={"database": "postgresql"})
    provider.validate(request)  # selection itself is valid
    provider.initialize(request)
    with pytest.raises(ProviderRejected, match="준비"):
        provider.register_task(request, ReleaseKey(value="7" * 24), delay=0)


def test_db_delete_keeps_final_snapshot_and_app_delete_keeps_db(aws, provider, make_request):
    from oneport_aws.providers.oneport_ecs import database

    run_db(provider)
    provider.initialize(make_request())
    provider.delete_app()
    assert aws.db["DBInstanceStatus"] == "available"  # app delete never touches the DB
    snapshot = database.delete(provider, suffix="-1")
    assert snapshot == "oneport-db-shop-final-1" and aws.db["final_snapshot"] == snapshot


# ---------------------------------------------------------------- auth


def test_credentials_are_key_or_profile(env_file):
    from oneport_aws.providers.oneport_ecs.models import AwsCredentials, credentials_from_env_file

    assert credentials_from_env_file(env_file).access_key_id is not None
    role = "arn:aws:iam::111122223333:role/oneport-deployer"
    assert (
        AwsCredentials(profile="oneport-deployer", deployer_role_arn=role).profile
        == "oneport-deployer"
    )
    with pytest.raises(ValueError):
        AwsCredentials(deployer_role_arn=role)  # neither
    with pytest.raises(ValueError):
        AwsCredentials(
            profile="p", access_key_id="a", secret_access_key="b", deployer_role_arn=role
        )  # both
