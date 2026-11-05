import hashlib
import io
import tarfile
from pathlib import Path

from controlplane.providers.contracts import ProviderRejected
from controlplane.providers.onprem.helm.models import ChartArtifact

MAX_PACKAGE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 128


def _inspect_archive(artifact: ChartArtifact, payload: bytes) -> None:
    name = artifact.name
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        entries = archive.getmembers()
        if len(entries) > MAX_ARCHIVE_ENTRIES or sum(e.size for e in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError()
        for entry in entries:
            parts = Path(entry.name).parts
            if (
                entry.issym()
                or entry.islnk()
                or not (entry.isfile() or entry.isdir())
                or not parts
                or parts[0] != name
                or ".." in parts
            ):
                raise ValueError()
        metadata = archive.extractfile(name + "/Chart.yaml").read().decode()
        if f"name: {name}\n" not in metadata or f"version: {artifact.version}\n" not in metadata:
            raise ValueError()


def load_chart_package(artifact: ChartArtifact) -> bytes:
    path = artifact.path
    if not path.is_file() or path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ProviderRejected("Chart package is absent or too large")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise ProviderRejected("Chart package checksum differs from the reviewed artifact")
    try:
        _inspect_archive(artifact, payload)
    except (tarfile.TarError, KeyError, ValueError, UnicodeError, AttributeError):
        raise ProviderRejected("Invalid reviewed chart archive") from None
    return payload
