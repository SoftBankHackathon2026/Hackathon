"""시험용 ObjectStore — 메모리 dict 위의 버킷 하나."""

from collections.abc import Iterator

from crosscloud.audit_copy import object_store


class MemoryStore:
    """object_store.ObjectStore 계약을 메모리로 지킨다."""

    def __init__(self, scheme: str, bucket: str) -> None:
        self.scheme = scheme
        self.bucket = bucket
        self.objects: dict[str, object_store.StoredObject] = {}

    def uri(self, key: str) -> str:
        return f"{self.scheme}://{self.bucket}/{key}"

    def list_keys(self, prefix: str) -> Iterator[str]:
        yield from sorted(k for k in self.objects if k.startswith(prefix))

    def get(self, key: str) -> object_store.StoredObject | None:
        return self.objects.get(key)

    def put(self, key: str, stored: object_store.StoredObject) -> None:
        if key in self.objects:
            raise object_store.ObjectExistsError(self.uri(key))
        self.objects[key] = stored
