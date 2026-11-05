from oneport_aws.execution.oneport_ecs_runtime import OneportEcsSettings


def test_aws_database_file_preserves_registered_environment(tmp_path, env_file):
    path = tmp_path / "database-url"
    path.write_text("postgresql://password@db/platform")
    path.chmod(0o600)
    common = {"env_file": env_file, "application": "shop", "environment_id": "aws-oneport"}
    inline = OneportEcsSettings(**common, database_url=path.read_text())
    mounted = OneportEcsSettings(**common, database_url_file=path)
    assert mounted.database_url == inline.database_url
    assert mounted.fingerprint() == inline.fingerprint()
