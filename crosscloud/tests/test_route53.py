import boto3
import pytest
from botocore import stub

from crosscloud.traffic import route53

_ZONE = "Z0PRIVATE"
_NAME = "app.ccx.internal."
_AWS_RECORD = {
    "Name": _NAME,
    "Type": "A",
    "SetIdentifier": "aws",
    "Weight": 100,
    "AliasTarget": {
        "HostedZoneId": "ZALB",
        "DNSName": "ccx-alb-1.ap-northeast-2.elb.amazonaws.com.",
        "EvaluateTargetHealth": False,
    },
}
_GCP_RECORD = {
    "Name": _NAME,
    "Type": "A",
    "SetIdentifier": "gcp",
    "Weight": 0,
    "TTL": 30,
    "ResourceRecords": [{"Value": "34.111.2.3"}],
}
_OTHER_RECORD = {
    "Name": "other.ccx.internal.",
    "Type": "A",
    "TTL": 60,
    "ResourceRecords": [{"Value": "10.0.0.1"}],
}
_LIST_PARAMS = {
    "HostedZoneId": _ZONE,
    "StartRecordName": _NAME,
    "StartRecordType": "A",
    "MaxItems": "10",
}


class _FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def client():
    return boto3.client(
        "route53",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _route(client):
    return route53.WeightedRoute(client, _ZONE, "app.ccx.internal", "A")


def _list_response(*records):
    return {
        "ResourceRecordSets": list(records),
        "IsTruncated": False,
        "MaxItems": "10",
    }


def test_read_returns_weights_by_label_and_ttl(client):
    with stub.Stubber(client) as stubber:
        stubber.add_response(
            "list_resource_record_sets",
            _list_response(_AWS_RECORD, _GCP_RECORD, _OTHER_RECORD),
            _LIST_PARAMS,
        )

        snapshot = _route(client).read()

    assert snapshot.weights == {"aws": 100, "gcp": 0}
    assert snapshot.ttl_seconds == 30


def test_read_refuses_when_a_label_record_is_missing(client):
    with stub.Stubber(client) as stubber:
        stubber.add_response(
            "list_resource_record_sets",
            _list_response(_AWS_RECORD),
            _LIST_PARAMS,
        )

        with pytest.raises(route53.RoutingError, match="gcp"):
            _route(client).read()


def test_set_weights_upserts_both_records_keeping_their_targets(client):
    expected_changes = [
        {"Action": "UPSERT", "ResourceRecordSet": {**_AWS_RECORD, "Weight": 0}},
        {
            "Action": "UPSERT",
            "ResourceRecordSet": {**_GCP_RECORD, "Weight": 100},
        },
    ]
    with stub.Stubber(client) as stubber:
        stubber.add_response(
            "list_resource_record_sets",
            _list_response(_AWS_RECORD, _GCP_RECORD),
            _LIST_PARAMS,
        )
        stubber.add_response(
            "change_resource_record_sets",
            {
                "ChangeInfo": {
                    "Id": "/change/C1",
                    "Status": "PENDING",
                    "SubmittedAt": "2026-11-07T00:00:00Z",
                }
            },
            {
                "HostedZoneId": _ZONE,
                "ChangeBatch": {
                    "Comment": "crosscloud aws=0 gcp=100",
                    "Changes": expected_changes,
                },
            },
        )

        change_id = _route(client).set_weights({"aws": 0, "gcp": 100})

    assert change_id == "/change/C1"


def test_set_weights_rejects_weights_not_summing_to_100(client):
    with pytest.raises(ValueError, match="100"):
        _route(client).set_weights({"aws": 50, "gcp": 60})


def _change(status):
    return {
        "ChangeInfo": {
            "Id": "/change/C1",
            "Status": status,
            "SubmittedAt": "2026-11-07T00:00:00Z",
        }
    }


def test_wait_insync_polls_until_insync(client):
    fake = _FakeTime()
    with stub.Stubber(client) as stubber:
        stubber.add_response(
            "get_change", _change("PENDING"), {"Id": "/change/C1"}
        )
        stubber.add_response(
            "get_change", _change("INSYNC"), {"Id": "/change/C1"}
        )

        insync = _route(client).wait_insync(
            "/change/C1",
            timeout_seconds=60,
            interval=5,
            clock=fake.clock,
            sleep=fake.sleep,
        )

    assert insync
    assert fake.now == 5


def test_wait_insync_gives_up_at_timeout(client):
    fake = _FakeTime()
    with stub.Stubber(client) as stubber:
        for _ in range(3):
            stubber.add_response(
                "get_change", _change("PENDING"), {"Id": "/change/C1"}
            )

        insync = _route(client).wait_insync(
            "/change/C1",
            timeout_seconds=10,
            interval=5,
            clock=fake.clock,
            sleep=fake.sleep,
        )

    assert not insync


def test_change_record_round_trip(tmp_path):
    record = route53.ChangeRecord(tmp_path / "traffic.json")

    assert record.load() is None
    record.save("/change/C9")

    assert route53.ChangeRecord(tmp_path / "traffic.json").load() == (
        "/change/C9"
    )


def test_change_record_refuses_corrupt_file(tmp_path):
    (tmp_path / "traffic.json").write_text("{")

    with pytest.raises(route53.RoutingError):
        route53.ChangeRecord(tmp_path / "traffic.json").load()


def _fully_on_stubs(stubber, gcp_weight, change_status):
    stubber.add_response(
        "list_resource_record_sets",
        _list_response(
            {**_AWS_RECORD, "Weight": 100 - gcp_weight},
            {**_GCP_RECORD, "Weight": gcp_weight},
        ),
        _LIST_PARAMS,
    )
    if change_status:
        stubber.add_response(
            "get_change", _change(change_status), {"Id": "/change/C1"}
        )


@pytest.mark.parametrize(
    ("gcp_weight", "change_status", "saved", "expected"),
    [
        (100, "INSYNC", True, True),
        (100, "PENDING", True, False),
        (90, None, True, False),
        (100, None, False, False),
    ],
)
def test_fully_on_needs_100_and_insync_last_change(
    client, tmp_path, gcp_weight, change_status, saved, expected
):
    record = route53.ChangeRecord(tmp_path / "traffic.json")
    if saved:
        record.save("/change/C1")
    with stub.Stubber(client) as stubber:
        _fully_on_stubs(stubber, gcp_weight, change_status)

        result = route53.fully_on(_route(client), record, "gcp")

    assert result is expected
