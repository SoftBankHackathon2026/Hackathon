"""S3·GCS 버킷을 같은 계약으로 읽고 쓰는 객체 저장소.

두 구현 모두 원본 바이트를 그대로 다룬다. gzip `Content-Encoding` 객체도
압축을 풀지 않고 받고 올린다. GCS 는 raw_download 로 받아 압축 해제
변환(decompressive transcoding)을 피한다. 쓰기는 덮어쓰지 않는다 — 같은
키가 있으면 ObjectExistsError 다(S3 IfNoneMatch, GCS if_generation_match=0).

  store = object_store.S3ObjectStore(boto3.client("s3"), "trail-bucket")
  for key in store.list_keys("AWSLogs/"):
      stored = store.get(key)
"""

import dataclasses
from collections.abc import Iterator
from typing import Any, Protocol

from botocore import exceptions as botocore_exceptions
from google.api_core import exceptions as google_exceptions
from google.cloud import storage

_OCTET_STREAM = "application/octet-stream"


@dataclasses.dataclass(frozen=True)
class StoredObject:
    """객체 하나의 원본 바이트와, 복사할 때 함께 옮기는 속성.

    Attributes:
        data: 저장된 그대로의 바이트(압축 해제하지 않음).
        content_type: Content-Type. 없으면 None.
        content_encoding: Content-Encoding(예: gzip). 없으면 None.
        metadata: 사용자 메타데이터(S3 x-amz-meta-*, GCS metadata). 키 순.
    """

    data: bytes
    content_type: str | None = None
    content_encoding: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()


class ObjectExistsError(Exception):
    """쓰려는 키에 이미 객체가 있다. 덮어쓰지 않았다."""


class ObjectStore(Protocol):
    """버킷 하나를 가리키는 객체 저장소 계약."""

    def uri(self, key: str) -> str:
        """키의 전체 경로(s3://버킷/키, gs://버킷/키)."""
        ...

    def list_keys(self, prefix: str) -> Iterator[str]:
        """접두사 아래 모든 키."""
        ...

    def get(self, key: str) -> StoredObject | None:
        """객체를 원본 바이트로 읽는다. 없으면 None."""
        ...

    def put(self, key: str, stored: StoredObject) -> None:
        """객체를 쓴다. 이미 있으면 ObjectExistsError."""
        ...


class S3ObjectStore:
    """boto3 S3 클라이언트로 버킷 하나를 다룬다."""

    def __init__(self, client: Any, bucket: str) -> None:
        """S3 버킷 하나를 가리킨다.

        Args:
            client: boto3.client("s3").
            bucket: 버킷 이름.
        """
        self._client = client
        self._bucket = bucket

    def uri(self, key: str) -> str:
        """s3://버킷/키."""
        return f"s3://{self._bucket}/{key}"

    def list_keys(self, prefix: str) -> Iterator[str]:
        """list_objects_v2 를 끝 페이지까지 따라간다."""
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for entry in page.get("Contents", ()):
                yield entry["Key"]

    def get(self, key: str) -> StoredObject | None:
        """get_object 본문을 그대로 읽는다(botocore 는 압축을 풀지 않는다)."""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except botocore_exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        return StoredObject(
            data=response["Body"].read(),
            content_type=response.get("ContentType"),
            content_encoding=response.get("ContentEncoding"),
            metadata=tuple(sorted(response.get("Metadata", {}).items())),
        )

    def put(self, key: str, stored: StoredObject) -> None:
        """조건부 쓰기(IfNoneMatch)와 SHA-256 체크섬으로 올린다."""
        optional = {
            "ContentType": stored.content_type,
            "ContentEncoding": stored.content_encoding,
        }
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=stored.data,
                Metadata=dict(stored.metadata),
                IfNoneMatch="*",
                ChecksumAlgorithm="SHA256",
                **{k: v for k, v in optional.items() if v is not None},
            )
        except botocore_exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "PreconditionFailed":
                raise ObjectExistsError(self.uri(key)) from e
            raise


class GcsObjectStore:
    """google-cloud-storage Bucket 하나를 다룬다."""

    def __init__(self, bucket: storage.Bucket) -> None:
        """GCS 버킷 하나를 가리킨다.

        Args:
            bucket: storage.Client().bucket(이름).
        """
        self._bucket = bucket

    def uri(self, key: str) -> str:
        """gs://버킷/키."""
        return f"gs://{self._bucket.name}/{key}"

    def list_keys(self, prefix: str) -> Iterator[str]:
        """접두사 아래 blob 이름."""
        for blob in self._bucket.list_blobs(prefix=prefix):
            yield blob.name

    def get(self, key: str) -> StoredObject | None:
        """raw_download 로 받아 gzip 객체도 저장된 바이트 그대로 읽는다."""
        blob = self._bucket.get_blob(key)
        if blob is None:
            return None
        return StoredObject(
            data=blob.download_as_bytes(raw_download=True),
            content_type=blob.content_type,
            content_encoding=blob.content_encoding,
            metadata=tuple(sorted((blob.metadata or {}).items())),
        )

    def put(self, key: str, stored: StoredObject) -> None:
        """if_generation_match=0 으로, 같은 이름이 없을 때만 올린다."""
        blob = self._bucket.blob(key)
        blob.content_encoding = stored.content_encoding
        blob.metadata = dict(stored.metadata)
        try:
            blob.upload_from_string(
                stored.data,
                content_type=stored.content_type or _OCTET_STREAM,
                if_generation_match=0,
            )
        except google_exceptions.PreconditionFailed as e:
            raise ObjectExistsError(self.uri(key)) from e
