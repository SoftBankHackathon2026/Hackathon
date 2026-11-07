import dataclasses
import datetime

import pytest

from crosscloud import model
from crosscloud.mapping import classify, service_table

_SUPPORTED_SCHEMA = "CREATE TABLE public.t (id uuid PRIMARY KEY);"
_HTTP_ENTRY = model.Entry(kind="http", source_id="tg-1", attributes=())


def _resource(kind, native_id, cloud="aws", **attributes):
    return model.Resource(
        kind=kind,
        cloud=cloud,
        native_id=native_id,
        region="ap-northeast-2",
        attributes=tuple(
            model.KeyValue(key=key, value=value)
            for key, value in attributes.items()
        ),
        source_of_truth="api",
    )


def _workload(
    name="request-api",
    cloud="aws",
    platform="ecs",
    entries=(_HTTP_ENTRY,),
    env=(),
):
    return model.Workload(
        name=name,
        cloud=cloud,
        platform=platform,
        native_id=f"id/{name}",
        identity_id="role/app",
        image=model.ImageRef(registry_repo="repo", digest="sha256:" + "a" * 64),
        code_repo=None,
        entries=entries,
        env=env,
        secret_refs=(),
        db_user=None,
        depends_on=(),
    )


def _inventory(
    cloud="aws", resources=(), workloads=(), couplings=(), omissions=()
):
    return model.Inventory(
        cloud=cloud,
        collected_at=datetime.datetime(2026, 11, 7, tzinfo=datetime.UTC),
        resources=resources,
        workloads=workloads,
        grants=(),
        restrictions=(),
        restriction_scans=(),
        couplings=couplings,
        omissions=omissions,
        attestation=model.CollectionAttestation(
            secret_value_calls=None,
            collector_can_read_secret_values=True,
            cai_viewer_bound=False,
            suspected_values_retained=0,
            evidence_ref="not_collected",
        ),
    )


def _aws_first_slice():
    return _inventory(
        resources=(
            _resource("postgres_instance", "db-1", engine="postgres"),
            _resource("secret", "arn:secret:db"),
            _resource("container_registry", "repo-1"),
            _resource("load_balancer", "alb-1", lb_type="application"),
            _resource("dns_record", "app.internal", private_zone=True),
        ),
        workloads=(_workload(),),
    )


def _verdicts(report):
    return {c.source_id: (c.verdict, c.row_id) for c in report.classifications}


def test_aws_first_slice_maps_every_resource_without_decisions():
    report = classify.classify(
        _aws_first_slice(), "aws_to_gcp", schema_sql=_SUPPORTED_SCHEMA
    )

    assert report.direction == "aws_to_gcp"
    assert _verdicts(report) == {
        "request-api": ("substitute", "aws_to_gcp/ecs_fargate-cloud_run"),
        "db-1": ("equivalent", "aws_to_gcp/rds_postgres-cloud_sql_postgres"),
        "arn:secret:db": (
            "equivalent",
            "aws_to_gcp/secrets_manager-secret_manager",
        ),
        "repo-1": ("equivalent", "aws_to_gcp/ecr-artifact_registry"),
        "alb-1": ("substitute", "aws_to_gcp/alb-external_application_lb"),
        "app.internal": ("retained", "aws_to_gcp/route53_private-retained"),
    }
    assert report.decisions == ()


def test_gcp_first_slice_maps_back_to_aws():
    inventory = _inventory(
        cloud="gcp",
        resources=(
            _resource("postgres_instance", "sql-1", "gcp", engine="postgres"),
            _resource("secret", "projects/p/secrets/db", "gcp"),
            _resource("container_registry", "ar-1", "gcp", format="DOCKER"),
            _resource(
                "load_balancer",
                "fr-1",
                "gcp",
                load_balancing_scheme="EXTERNAL_MANAGED",
            ),
        ),
        workloads=(_workload(cloud="gcp", platform="cloud_run"),),
    )

    report = classify.classify(
        inventory, "gcp_to_aws", schema_sql=_SUPPORTED_SCHEMA
    )

    assert {c.verdict for c in report.classifications} == {
        "equivalent",
        "substitute",
    }
    assert all(
        c.row_id.startswith("gcp_to_aws/") for c in report.classifications
    )
    assert report.decisions == ()


def test_direction_must_start_from_inventory_cloud():
    with pytest.raises(ValueError, match="aws"):
        classify.classify(_aws_first_slice(), "gcp_to_aws", schema_sql="")


def test_unmatched_resource_is_unclassified_human_decision():
    inventory = _inventory(
        resources=(
            _resource("dns_record", "www.example.com", private_zone=False),
        )
    )

    report = classify.classify(inventory, "aws_to_gcp", schema_sql=None)

    assert _verdicts(report) == {"www.example.com": ("unclassified", None)}
    (decision,) = report.decisions
    assert decision.subject == "www.example.com"
    assert decision.reason == "unclassified"
    assert decision.options == classify.HUMAN_CHOICES


def test_row_without_citations_is_unclassified():
    uncited = tuple(
        dataclasses.replace(row, citations=())
        for row in service_table.SERVICE_ROWS
    )
    inventory = _inventory(resources=(_resource("secret", "arn:secret:db"),))

    report = classify.classify(
        inventory, "aws_to_gcp", schema_sql=None, table=uncited
    )

    assert _verdicts(report) == {"arn:secret:db": ("unclassified", None)}
    assert report.decisions[0].reason == "unclassified"


@pytest.mark.parametrize(
    "entries",
    [
        (),
        (
            model.Entry(kind="http", source_id="tg-1", attributes=()),
            model.Entry(kind="queue_consumer", source_id="q-1", attributes=()),
        ),
    ],
)
def test_workload_with_non_http_entry_needs_decision(entries):
    inventory = _inventory(workloads=(_workload(entries=entries),))

    report = classify.classify(inventory, "aws_to_gcp", schema_sql=None)

    assert [d.reason for d in report.decisions] == ["non_http_entry"]


def test_env_classifications_raise_decisions():
    env = (
        model.EnvEntry("LOG_LEVEL", "plain_nonsecret", "info", None),
        model.EnvEntry("LEGACY_TOKEN", "suspected_secret", None, None),
        model.EnvEntry("AUTH_HEADER", "unknown", None, None),
    )
    inventory = _inventory(workloads=(_workload(env=env),))

    report = classify.classify(inventory, "aws_to_gcp", schema_sql=None)

    assert [(d.subject, d.reason) for d in report.decisions] == [
        ("request-api/LEGACY_TOKEN", "plaintext_secret_in_env"),
        ("request-api/AUTH_HEADER", "env_value_unconfirmed"),
    ]


def test_potential_only_coupling_needs_confirmation():
    def coupling(certainty, workload="request-api"):
        return model.InfraCoupling(
            workload=workload,
            location="config.js:1",
            rule_id="r",
            category="db_auth_rds_iam",
            certainty=certainty,
        )

    inventory = _inventory(
        workloads=(_workload(), _workload(name="worker")),
        couplings=(
            coupling("potential"),
            coupling("potential", "worker"),
            coupling("mandatory", "worker"),
        ),
    )

    report = classify.classify(inventory, "aws_to_gcp", schema_sql=None)

    assert [(d.subject, d.reason) for d in report.decisions] == [
        ("request-api", "coupling_unconfirmed"),
    ]


def test_omission_needs_acknowledgement():
    inventory = _inventory(
        omissions=(
            model.Omission("rds:mysql-1", "unsupported_type", None),
            model.Omission("rds:old", "unsupported_type", "kty"),
        )
    )

    report = classify.classify(inventory, "aws_to_gcp", schema_sql=None)

    assert [(d.subject, d.reason) for d in report.decisions] == [
        ("rds:mysql-1", "omission"),
    ]


def test_database_without_schema_cannot_pass_support_check():
    inventory = _inventory(
        resources=(_resource("postgres_instance", "db-1", engine="postgres"),)
    )

    report = classify.classify(inventory, "aws_to_gcp", schema_sql=None)

    (decision,) = report.decisions
    assert decision.reason == "transition_support_limit"
    assert decision.subject == "db-1"
    assert decision.signal == "schema_not_provided"


def test_support_limits_become_decisions():
    inventory = _inventory(
        resources=(_resource("postgres_instance", "db-1", engine="postgres"),)
    )
    schema_sql = (
        "CREATE TABLE a (id int PRIMARY KEY, b int REFERENCES b(id));"
        "CREATE TABLE c (id int);"
    )

    report = classify.classify(inventory, "aws_to_gcp", schema_sql=schema_sql)

    assert [(d.subject, d.signal) for d in report.decisions] == [
        ("db-1", "foreign_key: public.a"),
        ("db-1", "missing_primary_key: public.c"),
    ]
    assert {d.reason for d in report.decisions} == {"transition_support_limit"}


def test_every_mapped_row_cites_both_clouds():
    for row in service_table.SERVICE_ROWS:
        if row.verdict not in ("equivalent", "substitute"):
            continue
        urls = [citation.url for citation in row.citations]
        assert any("docs.aws.amazon.com" in url for url in urls), row.row_id
        assert any("cloud.google.com" in url for url in urls), row.row_id


def test_row_ids_are_unique():
    row_ids = [row.row_id for row in service_table.SERVICE_ROWS]

    assert len(row_ids) == len(set(row_ids))
