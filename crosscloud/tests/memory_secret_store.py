"""시험용 SecretStore — 메모리 dict 에 버전을 쌓는다(GCP 와 같은 불변 버전)."""

from crosscloud.secret_transfer import store


class MemorySecretStore:
    """secret_id → [(version_id, value, destroyed)] 를 메모리에 둔다."""

    def __init__(self, values: dict[str, bytes] | None = None) -> None:
        self.versions: dict[str, list[list]] = {}
        self.reads: list[str] = []
        self.fail_add_for: set[str] = set()
        self.fail_read_for: set[str] = set()
        self.fail_read_back_for: dict[str, Exception] = {}
        self.fail_retire_for: set[str] = set()
        self.corrupt_read_back_for: set[str] = set()
        for secret_id, value in (values or {}).items():
            self.versions[secret_id] = [[f"{secret_id}/1", value, False]]

    def _live(self, secret_id: str) -> list[list]:
        return [v for v in self.versions.get(secret_id, []) if not v[2]]

    def read_current(self, secret_id: str) -> bytes | None:
        self.reads.append(secret_id)
        if secret_id in self.fail_read_for:
            raise store.SecretStoreError("read_current", secret_id, "Denied")
        live = self._live(secret_id)
        return live[-1][1] if live else None

    def read_version(self, secret_id: str, version_id: str) -> bytes:
        self.reads.append(secret_id)
        if secret_id in self.fail_read_back_for:
            raise self.fail_read_back_for[secret_id]
        for vid, value, destroyed in self.versions.get(secret_id, []):
            if vid == version_id and not destroyed:
                if secret_id in self.corrupt_read_back_for:
                    return value + b"!"
                return value
        raise store.SecretStoreError("read_version", secret_id, "NotFound")

    def add_version(
        self, secret_id: str, value: bytes, token: str
    ) -> store.WrittenVersion:
        if secret_id in self.fail_add_for:
            raise store.SecretStoreError("add_version", secret_id, "Denied")
        entries = self.versions.setdefault(secret_id, [])
        version_id = f"{secret_id}/{len(entries) + 1}"
        entries.append([version_id, value, False])
        return store.WrittenVersion(
            secret_id=secret_id,
            version_id=version_id,
            created=True,
            prior_labels=(),
        )

    def retire_version(self, written: store.WrittenVersion) -> str:
        if not written.created:
            return "kept"
        if written.secret_id in self.fail_retire_for:
            raise store.SecretStoreError("retire", written.secret_id, "Denied")
        for entry in self.versions[written.secret_id]:
            if entry[0] == written.version_id:
                entry[2] = True
        return "retired"
