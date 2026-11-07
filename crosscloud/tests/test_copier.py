import gzip
import hashlib

import memory_store
import pytest

from crosscloud.audit_copy import copier, object_store

_LOG = object_store.StoredObject(
    data=gzip.compress(b'{"Records": []}'),
    content_type="application/json",
    content_encoding="gzip",
    metadata=(("signature", "ab"),),
)


def _stores() -> tuple[memory_store.MemoryStore, memory_store.MemoryStore]:
    source = memory_store.MemoryStore("s3", "trail")
    target = memory_store.MemoryStore("gs", "archive")
    source.objects["AWSLogs/1/a.json.gz"] = _LOG
    source.objects["AWSLogs/1/b.json.gz"] = object_store.StoredObject(data=b"b")
    source.objects["Other/c"] = object_store.StoredObject(data=b"c")
    return source, target


def test_copy_keeps_original_bytes_encoding_and_metadata():
    source, target = _stores()

    copier.copy_objects(source, "AWSLogs/", target, "ccx-r1/objects/")

    assert target.objects["ccx-r1/objects/AWSLogs/1/a.json.gz"] == _LOG


def test_copy_records_path_map_and_sha256_of_raw_bytes():
    source, target = _stores()

    copied = copier.copy_objects(source, "AWSLogs/", target, "ccx-r1/objects/")

    assert copied == (
        copier.CopiedObject(
            source_uri="s3://trail/AWSLogs/1/a.json.gz",
            target_uri="gs://archive/ccx-r1/objects/AWSLogs/1/a.json.gz",
            sha256=hashlib.sha256(_LOG.data).hexdigest(),
            size=len(_LOG.data),
            metadata=(("signature", "ab"),),
        ),
        copier.CopiedObject(
            source_uri="s3://trail/AWSLogs/1/b.json.gz",
            target_uri="gs://archive/ccx-r1/objects/AWSLogs/1/b.json.gz",
            sha256=hashlib.sha256(b"b").hexdigest(),
            size=1,
            metadata=(),
        ),
    )


class _AlteringStore(memory_store.MemoryStore):
    """쓰는 바이트를 바꿔 저장하는 대상 — 전송 중 변환을 흉내 낸다."""

    def put(self, key, stored):
        super().put(key, object_store.StoredObject(data=stored.data + b"!"))


def test_copy_fails_when_target_bytes_differ_after_write():
    source, _ = _stores()
    target = _AlteringStore("gs", "archive")

    with pytest.raises(copier.CopyIntegrityError, match="a.json.gz"):
        copier.copy_objects(source, "AWSLogs/", target, "ccx-r1/objects/")


class _VanishingStore(memory_store.MemoryStore):
    def get(self, key):
        return None


def test_copy_fails_when_listed_source_object_vanishes():
    source = _VanishingStore("s3", "trail")
    source.objects["AWSLogs/x"] = _LOG

    with pytest.raises(copier.CopyIntegrityError, match="AWSLogs/x"):
        copier.copy_objects(
            source, "AWSLogs/", memory_store.MemoryStore("gs", "a"), "p/"
        )
