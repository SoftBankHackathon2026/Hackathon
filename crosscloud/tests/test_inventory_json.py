import copy
import dataclasses
import datetime
import json

import pytest

from crosscloud import inventory_json, model


def _sample_inventory() -> model.Inventory:
    return model.Inventory(
        cloud="aws",
        collected_at=datetime.datetime(2026, 11, 7, 9, 30, tzinfo=datetime.UTC),
        resources=(
            model.Resource(
                kind="postgres_instance",
                cloud="aws",
                native_id="arn:aws:rds:ap-northeast-2:111122223333:db:app",
                region="ap-northeast-2",
                attributes=(
                    model.KeyValue(key="engine_version", value="16.4"),
                    model.KeyValue(key="iam_auth", value=True),
                    model.KeyValue(key="port", value=5432),
                    model.KeyValue(key="storage_gib", value=20.5),
                    model.KeyValue(key="kms_key", value=None),
                ),
                source_of_truth="api",
            ),
        ),
        workloads=(
            model.Workload(
                name="request-api",
                cloud="aws",
                platform="ecs",
                native_id="arn:aws:ecs:ap-northeast-2:111122223333:service/x",
                identity_id="arn:aws:iam::111122223333:role/request-api",
                image=model.ImageRef(
                    registry_repo="111122223333.dkr.ecr.example/request-api",
                    digest="sha256:" + "a" * 64,
                ),
                code_repo="https://example.invalid/sample-app.git",
                entries=(
                    model.Entry(
                        kind="http",
                        source_id="alb-listener-1",
                        attributes=(model.KeyValue(key="port", value=443),),
                    ),
                ),
                env=(
                    model.EnvEntry(
                        key="LOG_LEVEL",
                        classification="plain_nonsecret",
                        value="info",
                        confirmed_by=None,
                    ),
                    model.EnvEntry(
                        key="DATABASE_URL",
                        classification="secret_ref",
                        value=None,
                        confirmed_by=None,
                    ),
                    model.EnvEntry(
                        key="LEGACY_TOKEN",
                        classification="suspected_secret",
                        value=None,
                        confirmed_by=None,
                    ),
                ),
                secret_refs=(
                    model.SecretRef(
                        cloud="aws",
                        native_id="arn:aws:secretsmanager:ap-northeast-2:"
                        "111122223333:secret:app-db",
                        version="AWSCURRENT",
                    ),
                ),
                db_user="app",
                depends_on=("postgres_instance:app",),
            ),
        ),
        grants=(
            model.IdentityGrant(
                grant_id="g-1",
                principal_id="arn:aws:iam::111122223333:role/request-api",
                resource_id="arn:aws:rds-db:ap-northeast-2:111122223333:"
                "dbuser:db-ABC/app",
                action="rds-db:connect",
                effect="allow",
                condition_raw=None,
                origin="identity_policy",
            ),
        ),
        restrictions=(
            model.Restriction(
                restriction_id="p-scp-1",
                kind="scp",
                scope="ou-root",
                mode="deny",
                principals=("*",),
                actions=("rds:DeleteDBInstance",),
                resources=("*",),
                condition_raw='{"StringNotEquals": {}}',
                condition_parsed=False,
            ),
        ),
        restriction_scans=(
            model.RestrictionScan(
                scope="ou-root",
                kind="scp",
                coverage="unknown",
                reason="organizations:ListPolicies denied",
            ),
        ),
        couplings=(
            model.InfraCoupling(
                workload="request-api",
                location="packages/request-store/config.js:29",
                rule_id="db-auth-rds-iam-signer",
                category="db_auth_rds_iam",
                certainty="mandatory",
            ),
        ),
        omissions=(
            model.Omission(
                subject="AWS::ElasticLoadBalancingV2::Listener",
                detected_by="export_vs_api",
                acknowledged_by=None,
            ),
        ),
        attestation=model.CollectionAttestation(
            secret_value_calls=0,
            collector_can_read_secret_values=False,
            cai_viewer_bound=False,
            suspected_values_retained=0,
            evidence_ref="cloudtrail:query-1",
        ),
    )


def _sample_document() -> dict:
    return inventory_json.inventory_to_json(_sample_inventory())


def test_round_trip_returns_equal_inventory():
    inventory = _sample_inventory()

    document = inventory_json.inventory_to_json(inventory)
    restored = inventory_json.inventory_from_json(document)

    assert restored == inventory


def test_round_trip_survives_json_text():
    inventory = _sample_inventory()

    text = json.dumps(inventory_json.inventory_to_json(inventory))
    restored = inventory_json.inventory_from_json(json.loads(text))

    assert restored == inventory


def test_restored_collections_are_tuples():
    restored = inventory_json.inventory_from_json(_sample_document())

    assert isinstance(restored.workloads, tuple)
    assert isinstance(restored.workloads[0].env, tuple)
    assert isinstance(restored.workloads[0].depends_on, tuple)


def test_optional_nested_object_round_trips_as_none():
    workload = dataclasses.replace(
        _sample_inventory().workloads[0], image=None, code_repo=None
    )
    inventory = dataclasses.replace(_sample_inventory(), workloads=(workload,))

    restored = inventory_json.inventory_from_json(
        inventory_json.inventory_to_json(inventory)
    )

    assert restored.workloads[0].image is None


def test_gcp_attestation_without_secret_call_count_round_trips():
    attestation = dataclasses.replace(
        _sample_inventory().attestation, secret_value_calls=None
    )
    inventory = dataclasses.replace(
        _sample_inventory(), cloud="gcp", attestation=attestation
    )

    restored = inventory_json.inventory_from_json(
        inventory_json.inventory_to_json(inventory)
    )

    assert restored.attestation.secret_value_calls is None


def test_from_json_rejects_retained_suspected_values():
    document = _sample_document()
    document["attestation"]["suspected_values_retained"] = 1

    with pytest.raises(
        inventory_json.InvalidInventoryError, match="attestation"
    ):
        inventory_json.inventory_from_json(document)


def test_to_json_rejects_retained_suspected_values():
    attestation = dataclasses.replace(
        _sample_inventory().attestation, suspected_values_retained=2
    )
    inventory = dataclasses.replace(
        _sample_inventory(), attestation=attestation
    )

    with pytest.raises(inventory_json.InvalidInventoryError):
        inventory_json.inventory_to_json(inventory)


@pytest.mark.parametrize(
    "classification", ["secret_ref", "suspected_secret", "unknown", "empty"]
)
def test_rejects_env_value_kept_outside_plain_nonsecret(classification):
    document = _sample_document()
    env = document["workloads"][0]["env"][0]
    env["classification"] = classification
    env["value"] = "kept-value"

    with pytest.raises(inventory_json.InvalidInventoryError, match="env"):
        inventory_json.inventory_from_json(document)


def test_rejects_unknown_field():
    document = _sample_document()
    document["resources"][0]["password"] = "x"

    with pytest.raises(inventory_json.InvalidInventoryError):
        inventory_json.inventory_from_json(document)


def test_rejects_missing_field():
    document = _sample_document()
    del document["workloads"][0]["db_user"]

    with pytest.raises(inventory_json.InvalidInventoryError):
        inventory_json.inventory_from_json(document)


def test_rejects_value_outside_literal():
    document = _sample_document()
    document["workloads"][0]["platform"] = "fargate"

    with pytest.raises(inventory_json.InvalidInventoryError, match="platform"):
        inventory_json.inventory_from_json(document)


def test_rejects_negative_secret_value_calls():
    document = _sample_document()
    document["attestation"]["secret_value_calls"] = -1

    with pytest.raises(inventory_json.InvalidInventoryError):
        inventory_json.inventory_from_json(document)


def test_rejects_boolean_where_integer_expected():
    document = _sample_document()
    document["attestation"]["secret_value_calls"] = False

    with pytest.raises(inventory_json.InvalidInventoryError):
        inventory_json.inventory_from_json(document)


def test_rejects_collected_at_without_timezone():
    inventory = dataclasses.replace(
        _sample_inventory(),
        collected_at=datetime.datetime(2026, 11, 7, 9, 30),
    )

    with pytest.raises(inventory_json.InvalidInventoryError):
        inventory_json.inventory_to_json(inventory)


def test_rejects_non_object_document():
    with pytest.raises(inventory_json.InvalidInventoryError):
        inventory_json.inventory_from_json(["not", "an", "object"])


def test_from_json_does_not_modify_input_document():
    document = _sample_document()
    snapshot = copy.deepcopy(document)

    inventory_json.inventory_from_json(document)

    assert document == snapshot


def test_error_message_does_not_echo_rejected_value():
    canary = "canary-7f3a9c-should-not-leak"
    document = _sample_document()
    env = document["workloads"][0]["env"][0]
    env["classification"] = "suspected_secret"
    env["value"] = canary

    with pytest.raises(inventory_json.InvalidInventoryError) as excinfo:
        inventory_json.inventory_from_json(document)

    assert canary not in str(excinfo.value)
    assert canary not in repr(excinfo.value.args)


@pytest.mark.parametrize(
    "collected_at",
    [
        "2026-13-45T25:61:61Z",
        "2026-11-07T09:30:00Z\n",
        "٢٠٢٦-11-07T09:30:00Z",
    ],
    ids=["out_of_range", "trailing_newline", "non_ascii_digits"],
)
def test_rejects_collected_at_that_is_not_a_real_time(collected_at):
    document = _sample_document()
    document["collected_at"] = collected_at

    with pytest.raises(inventory_json.InvalidInventoryError) as excinfo:
        inventory_json.inventory_from_json(document)

    assert collected_at.strip() not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
