import gzip
import io
from unittest import mock

import boto3
import pytest
from botocore import response, stub
from google.api_core import exceptions as google_exceptions

from crosscloud.audit_copy import object_store

_GZIP_BODY = gzip.compress(b'{"Records": []}')


@pytest.fixture
def s3_client():
    client = boto3.client(
        "s3",
        region_name="ap-northeast-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    with stub.Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def _body(data: bytes) -> response.StreamingBody:
    return response.StreamingBody(io.BytesIO(data), len(data))


def test_s3_uri_names_bucket_and_key(s3_client):
    client, _ = s3_client
    store = object_store.S3ObjectStore(client, "src")

    assert store.uri("a/b.json.gz") == "s3://src/a/b.json.gz"


def test_s3_list_keys_follows_pages(s3_client):
    client, stubber = s3_client
    stubber.add_response(
        "list_objects_v2",
        {
            "Contents": [{"Key": "p/1"}],
            "IsTruncated": True,
            "NextContinuationToken": "t",
        },
        {"Bucket": "src", "Prefix": "p/"},
    )
    stubber.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "p/2"}], "IsTruncated": False},
        {"Bucket": "src", "Prefix": "p/", "ContinuationToken": "t"},
    )
    store = object_store.S3ObjectStore(client, "src")

    assert list(store.list_keys("p/")) == ["p/1", "p/2"]


def test_s3_list_keys_of_empty_prefix_is_empty(s3_client):
    client, stubber = s3_client
    stubber.add_response(
        "list_objects_v2",
        {"IsTruncated": False},
        {"Bucket": "src", "Prefix": "p/"},
    )

    assert list(object_store.S3ObjectStore(client, "src").list_keys("p/")) == []


def test_s3_get_returns_stored_bytes_without_decompressing(s3_client):
    client, stubber = s3_client
    stubber.add_response(
        "get_object",
        {
            "Body": _body(_GZIP_BODY),
            "ContentType": "application/json",
            "ContentEncoding": "gzip",
            "Metadata": {
                "signature": "ab",
                "signature-algorithm": "SHA256withRSA",
            },
        },
        {"Bucket": "src", "Key": "d.json.gz"},
    )
    store = object_store.S3ObjectStore(client, "src")

    stored = store.get("d.json.gz")

    assert stored == object_store.StoredObject(
        data=_GZIP_BODY,
        content_type="application/json",
        content_encoding="gzip",
        metadata=(
            ("signature", "ab"),
            ("signature-algorithm", "SHA256withRSA"),
        ),
    )


def test_s3_get_of_absent_key_is_none(s3_client):
    client, stubber = s3_client
    stubber.add_client_error("get_object", service_error_code="NoSuchKey")

    assert object_store.S3ObjectStore(client, "src").get("x") is None


def test_s3_get_reraises_other_errors(s3_client):
    client, stubber = s3_client
    stubber.add_client_error("get_object", service_error_code="AccessDenied")

    with pytest.raises(Exception, match="AccessDenied"):
        object_store.S3ObjectStore(client, "src").get("x")


def test_s3_put_writes_raw_bytes_and_never_overwrites(s3_client):
    client, stubber = s3_client
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": "dst",
            "Key": "k",
            "Body": _GZIP_BODY,
            "ContentType": "application/json",
            "ContentEncoding": "gzip",
            "Metadata": {"signature": "ab"},
            "IfNoneMatch": "*",
            "ChecksumAlgorithm": "SHA256",
        },
    )
    store = object_store.S3ObjectStore(client, "dst")

    store.put(
        "k",
        object_store.StoredObject(
            data=_GZIP_BODY,
            content_type="application/json",
            content_encoding="gzip",
            metadata=(("signature", "ab"),),
        ),
    )


def test_s3_put_without_type_or_encoding_sends_only_body(s3_client):
    client, stubber = s3_client
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": "dst",
            "Key": "k",
            "Body": b"x",
            "Metadata": {},
            "IfNoneMatch": "*",
            "ChecksumAlgorithm": "SHA256",
        },
    )

    object_store.S3ObjectStore(client, "dst").put(
        "k", object_store.StoredObject(data=b"x")
    )


def test_s3_put_over_existing_key_raises_object_exists(s3_client):
    client, stubber = s3_client
    stubber.add_client_error(
        "put_object",
        service_error_code="PreconditionFailed",
        http_status_code=412,
    )

    with pytest.raises(object_store.ObjectExistsError):
        object_store.S3ObjectStore(client, "dst").put(
            "k", object_store.StoredObject(data=b"x")
        )


def _gcs_bucket(name: str = "dst") -> mock.MagicMock:
    bucket = mock.MagicMock()
    bucket.name = name
    return bucket


def test_gcs_uri_names_bucket_and_key():
    store = object_store.GcsObjectStore(_gcs_bucket("arc"))

    assert store.uri("a/b") == "gs://arc/a/b"


def test_gcs_list_keys_returns_blob_names():
    bucket = _gcs_bucket()
    first, second = mock.MagicMock(), mock.MagicMock()
    first.name, second.name = "p/1", "p/2"
    bucket.list_blobs.return_value = iter([first, second])

    keys = list(object_store.GcsObjectStore(bucket).list_keys("p/"))

    assert keys == ["p/1", "p/2"]
    bucket.list_blobs.assert_called_once_with(prefix="p/")


def test_gcs_get_downloads_raw_bytes_to_avoid_transcoding():
    bucket = _gcs_bucket()
    blob = bucket.get_blob.return_value
    blob.download_as_bytes.return_value = _GZIP_BODY
    blob.content_type = "application/json"
    blob.content_encoding = "gzip"
    blob.metadata = {"signature": "ab"}

    stored = object_store.GcsObjectStore(bucket).get("k")

    blob.download_as_bytes.assert_called_once_with(raw_download=True)
    assert stored == object_store.StoredObject(
        data=_GZIP_BODY,
        content_type="application/json",
        content_encoding="gzip",
        metadata=(("signature", "ab"),),
    )


def test_gcs_get_of_absent_key_is_none():
    bucket = _gcs_bucket()
    bucket.get_blob.return_value = None

    assert object_store.GcsObjectStore(bucket).get("k") is None


def test_gcs_get_without_metadata_has_empty_metadata():
    bucket = _gcs_bucket()
    blob = bucket.get_blob.return_value
    blob.download_as_bytes.return_value = b"x"
    blob.content_type = None
    blob.content_encoding = None
    blob.metadata = None

    stored = object_store.GcsObjectStore(bucket).get("k")

    assert stored == object_store.StoredObject(data=b"x")


def test_gcs_put_keeps_encoding_and_never_overwrites():
    bucket = _gcs_bucket()
    blob = bucket.blob.return_value

    object_store.GcsObjectStore(bucket).put(
        "k",
        object_store.StoredObject(
            data=_GZIP_BODY,
            content_type="application/json",
            content_encoding="gzip",
            metadata=(("signature", "ab"),),
        ),
    )

    bucket.blob.assert_called_once_with("k")
    assert blob.content_encoding == "gzip"
    assert blob.metadata == {"signature": "ab"}
    blob.upload_from_string.assert_called_once_with(
        _GZIP_BODY, content_type="application/json", if_generation_match=0
    )


def test_gcs_put_without_type_uses_octet_stream():
    bucket = _gcs_bucket()

    object_store.GcsObjectStore(bucket).put(
        "k", object_store.StoredObject(data=b"x")
    )

    bucket.blob.return_value.upload_from_string.assert_called_once_with(
        b"x", content_type="application/octet-stream", if_generation_match=0
    )


def test_gcs_put_over_existing_key_raises_object_exists():
    bucket = _gcs_bucket()
    bucket.blob.return_value.upload_from_string.side_effect = (
        google_exceptions.PreconditionFailed("exists")
    )

    with pytest.raises(object_store.ObjectExistsError):
        object_store.GcsObjectStore(bucket).put(
            "k", object_store.StoredObject(data=b"x")
        )
