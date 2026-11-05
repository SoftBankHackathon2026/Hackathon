"""Regression coverage: actual Postgres locking/checkpoints, simulated AWS faults."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

import pytest

from controlplane.providers.contracts import ProviderRejected
from oneport_aws.execution.oneport_ecs_apps import AppRegistry
from oneport_aws.execution.oneport_ecs_db import provision_step
from oneport_aws.providers.oneport_ecs.registry import EcrImageBuilder
from oneport_aws.providers.oneport_ecs.secrets import put_secret

from .aws_fakes import error
from .test_operations import FakeDocker, git_request


def registered(store, provider):
    registry = AppRegistry(store.dsn)
    registry.migrate()
    registry.register(provider.environment, port=8080, health_path="/health", database="postgresql")
    return registry


def advance_to_password(registry, provider):
    provision_step(registry, provider, settle_seconds=0)
    provision_step(registry, provider, settle_seconds=0)


def test_deleted_secret_prevents_any_password_reset(store, aws, provider):
    registry = registered(store, provider)
    advance_to_password(registry, provider)
    with patch.object(
        aws, "secrets_describe_secret", return_value={"ARN": "secret-arn", "DeletedDate": 123}
    ):
        assert provision_step(registry, provider).state == "failed"
        with pytest.raises(ProviderRejected, match="삭제 예약"):
            put_secret(provider, "db", "never-written")
    for _ in range(4):
        provision_step(registry, provider)
    # Re-running db-create/register must not silently restart failed provisioning.
    assert (
        registry.register(
            provider.environment, port=8080, health_path="/health", database="postgresql"
        ).db_state
        == "failed"
    )
    assert "rds.modify_db_instance" not in aws.calls
    assert "secrets.put_secret_value" not in aws.calls


def test_lost_secret_response_stops_password_retries(store, aws, provider):
    registry = registered(store, provider)
    advance_to_password(registry, provider)
    original = aws.secrets_create_secret

    def accepted_but_lost(**kwargs):
        original(**kwargs)  # Server accepted the secret, response lost in transit.
        raise error("ServiceUnavailableException")

    with patch.object(aws, "secrets_create_secret", side_effect=accepted_but_lost):
        assert provision_step(registry, provider).state == "uncertain"
    for _ in range(4):
        assert provision_step(registry, provider).state == "uncertain"
    assert aws.calls.count("rds.modify_db_instance") == 1
    assert aws.calls.count("secrets.create_secret") == 1
    assert not registry.refs("shop", "aws-oneport")  # Never advertise an unconfirmed binding.


def test_process_death_after_reset_intent_is_not_replayed(store, aws, provider):
    registry = registered(store, provider)
    advance_to_password(registry, provider)
    with patch.object(aws, "rds_modify_db_instance", side_effect=SystemExit):
        with pytest.raises(SystemExit):
            provision_step(registry, provider)
    assert registry.get("shop", "aws-oneport").db_state == "resetting"
    assert provision_step(registry, provider).state == "uncertain"
    assert aws.calls.count("rds.modify_db_instance") == 1


def test_worker_and_waiter_serialize_across_intent_commit(store, aws, provider):
    registry = registered(store, provider)
    advance_to_password(registry, provider)
    entered, release = Event(), Event()
    original = aws.rds_modify_db_instance

    def slow_write(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)

    with (
        patch.object(aws, "rds_modify_db_instance", side_effect=slow_write),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        active = pool.submit(provision_step, registry, provider)
        try:
            assert entered.wait(10)
            assert registry.get("shop", "aws-oneport").db_state == "resetting"
            assert pool.submit(provision_step, registry, provider).result(timeout=5) is None
            with pytest.raises(ValueError, match="진행 중"):
                registry.register(
                    provider.environment, port=8080, health_path="/health", database="postgresql"
                )
        finally:
            release.set()
        assert active.result(timeout=5).state == "configuring"
    assert aws.calls.count("rds.modify_db_instance") == 1


def test_required_database_rejects_registration_and_deploy(store, provider, make_request):
    env = provider.environment.model_copy(update={"required_database": "postgresql"})
    registry = AppRegistry(store.dsn)
    registry.migrate()
    with pytest.raises(ValueError, match="필수"):
        registry.register(env, port=8080, health_path="/health", database="none")
    assert registry.get(env.application, env.environment_id) is None
    with pytest.raises(ValueError, match="필수"):
        type(provider)(provider.aws, env).validate(make_request())


def test_parallel_builds_isolate_auth_and_cleanup(aws, environment):
    barrier = Barrier(2)
    configs = []
    runners = []

    def job(commit):
        docker = FakeDocker(aws, environment)
        runners.append(docker)

        def run(args, log, **kwargs):
            if args[0] == "docker":
                config = Path(args[args.index("--config") + 1])
                if "buildx" in args:
                    configs.append(config)
                    assert config.stat().st_mode & 0o777 == 0o700
                    contents = json.loads((config / "config.json").read_text())
                    assert contents["credsStore"] == ""
                    assert set(contents["auths"]) == {environment.ecr_registry}
                    assert (config / "config.json").stat().st_mode & 0o777 == 0o600
                    barrier.wait(timeout=10)
                assert config.exists()
            return docker(args, log, **kwargs)

        request = git_request(
            source={
                "kind": "git",
                "repository": "https://github.com/example/shop",
                "commit": commit,
            }
        )
        return EcrImageBuilder(aws.ecr, environment, run=run).build(request, log=lambda _: None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(job, ["a" * 40, "b" * 40]))
    assert len(set(configs)) == 2
    assert all(not p.exists() for p in configs)
    for runner in runners:
        docker_calls = [c for c in runner.commands if c[0] == "docker"]
        assert len(docker_calls) == 1
        assert "login" not in docker_calls[0]


def test_failed_build_removes_credentials(aws, environment):
    docker = FakeDocker(aws, environment, fail_on="buildx")
    with pytest.raises(ProviderRejected):
        EcrImageBuilder(aws.ecr, environment, run=docker).build(git_request(), log=lambda _: None)
    build = next(c for c in docker.commands if "buildx" in c)
    assert not Path(build[2]).exists()


def test_read_failure_is_retryable_without_write_intent(store, aws, provider):
    from controlplane.providers.contracts import ProviderUnavailable

    registry = registered(store, provider)
    aws.fail["rds.describe_db_instances"] = error("ServiceUnavailableException")
    with pytest.raises(ProviderUnavailable):
        provision_step(registry, provider)
    assert registry.get("shop", "aws-oneport").db_state == "requested"
    assert "rds.create_db_instance" not in aws.calls
    assert provision_step(registry, provider).state == "creating"


def test_lost_create_response_is_not_automatically_replayed(store, aws, provider):
    registry = registered(store, provider)
    original = aws.rds_create_db_instance

    def accepted_but_lost(**kwargs):
        original(**kwargs)
        raise error("ServiceUnavailableException")

    with patch.object(aws, "rds_create_db_instance", side_effect=accepted_but_lost):
        assert provision_step(registry, provider).state == "uncertain"
    assert provision_step(registry, provider).state == "uncertain"
    assert aws.calls.count("rds.create_db_instance") == 1
    assert "rds.modify_db_instance" not in aws.calls


def test_job_config_preserves_plugins_without_copying_credentials(
    aws, environment, tmp_path, monkeypatch
):
    original = tmp_path / "original"
    original.mkdir()
    (original / "config.json").write_text(
        json.dumps(
            {
                "auths": {"other-registry": {"auth": "must-not-copy"}},
                "credsStore": "desktop",
                "credHelpers": {environment.ecr_registry: "osxkeychain"},
                "cliPluginsExtraDirs": ["/opt/docker/plugins"],
            }
        )
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(original))
    job = tmp_path / "job"
    job.mkdir()
    EcrImageBuilder(aws.ecr, environment)._configure_auth(lambda _: None, job)
    result = json.loads((job / "config.json").read_text())
    assert result["cliPluginsExtraDirs"] == [str(original / "cli-plugins"), "/opt/docker/plugins"]
    assert "must-not-copy" not in (job / "config.json").read_text()
    assert "credHelpers" not in result and result["credsStore"] == ""
    assert json.loads((original / "config.json").read_text())["credsStore"] == "desktop"
