from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import ssl
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
    parse_http_list,
    parse_keqv_list,
)

from .config import DIGEST, Config, MigrationError, Robot, matches, require

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
DOCKER_INDEX = "application/vnd.docker.distribution.manifest.list.v2+json"
OCI_IMAGE = "application/vnd.oci.image.manifest.v1+json"
DOCKER_IMAGE = "application/vnd.docker.distribution.manifest.v2+json"
INDEX_TYPES = {OCI_INDEX, DOCKER_INDEX}
IMAGE_TYPES = {OCI_IMAGE, DOCKER_IMAGE}
IMAGE_CONFIGS = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}
ACCEPT = ", ".join(sorted(INDEX_TYPES | IMAGE_TYPES))
MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def encoded_robot(robot: Robot) -> str:
    return base64.b64encode(f"{robot.username}:{robot.secret}".encode()).decode("ascii")


def read_json(stream, limit: int) -> dict:
    data = stream.read(limit + 1)
    require(len(data) <= limit, "REGISTRY_RESPONSE_TOO_LARGE")
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeError):
        raise MigrationError("INVALID_REGISTRY_RESPONSE") from None
    require(isinstance(doc, dict), "INVALID_REGISTRY_RESPONSE")
    return doc


class Registry:
    def __init__(self, host: str, auth, ca_ref: Path | None = None):
        self.origin = f"https://{host}"
        self.auth = auth
        self.tokens: dict[str, str] = {}
        try:
            context = ssl.create_default_context()
            if ca_ref is not None:
                context.load_verify_locations(cafile=ca_ref)
        except (OSError, ValueError):
            raise MigrationError("INVALID_REGISTRY_CA") from None
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context))

    def open(self, url: str, headers: dict):
        try:
            return self.opener.open(Request(url, headers=headers), timeout=30)
        except HTTPError as exc:
            return exc
        except (OSError, URLError, http.client.HTTPException):
            raise MigrationError("REGISTRY_CONNECTION_FAILED") from None

    def token(self, challenge: str, repo: str) -> str:
        scheme, _, raw = challenge.partition(" ")
        require(scheme.lower() == "bearer", "REGISTRY_AUTH_FAILED")
        try:
            fields = parse_keqv_list(parse_http_list(raw))
            realm = urlsplit(fields["realm"])
            require(
                realm.scheme == "https"
                and f"https://{realm.netloc}" == self.origin
                and realm.path
                and not realm.fragment,
                "UNTRUSTED_TOKEN_ENDPOINT",
            )
            query = [
                (key, value)
                for key, value in parse_qsl(realm.query)
                if key not in {"scope", "service"}
            ]
            query.extend(
                (("service", fields["service"]), ("scope", f"repository:{repo}:pull"))
            )
            url = urlunsplit(realm._replace(query=urlencode(query)))
        except (KeyError, ValueError):
            raise MigrationError("INVALID_REGISTRY_CHALLENGE") from None
        with closing(
            self.open(url, {"Authorization": "Basic " + self.auth()})
        ) as response:
            require(response.code == 200, "REGISTRY_AUTH_FAILED")
            data = read_json(response, 1024 * 1024)
        token = data.get("token") or data.get("access_token")
        require(
            isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token),
            "REGISTRY_AUTH_FAILED",
        )
        return token

    def get(self, repo: str, path: str, *, blob: bool = False):
        url = f"{self.origin}/v2/{repo}/{path}"
        headers = {"Accept": ACCEPT} if not blob else {"Accept-Encoding": "identity"}
        headers["Authorization"] = (
            "Bearer " + self.tokens[repo]
            if repo in self.tokens
            else "Basic " + self.auth()
        )
        response = self.open(url, headers)
        if response.code == 401:
            with closing(response):
                challenge = response.headers.get("WWW-Authenticate", "")
            self.tokens[repo] = self.token(challenge, repo)
            headers["Authorization"] = "Bearer " + self.tokens[repo]
            response = self.open(url, headers)
        if not blob:
            return response
        # Storage redirects may carry a signed URL. Never send registry credentials to storage.
        for _ in range(5):
            if response.code not in {301, 302, 303, 307, 308}:
                return response
            with closing(response):
                location = response.headers.get("Location")
                require(bool(location), "INVALID_BLOB_REDIRECT")
                url = urljoin(url, location)
                dest = urlsplit(url)
                require(
                    dest.scheme == "https"
                    and dest.hostname
                    and not dest.username
                    and not dest.password,
                    "UNTRUSTED_BLOB_REDIRECT",
                )
            response = self.open(url, {"Accept-Encoding": "identity"})
        response.close()
        raise MigrationError("BLOB_REDIRECT_LIMIT")

    def manifest(self, repo: str, reference: str) -> tuple[str, dict] | None:
        try:
            with closing(self.get(repo, "manifests/" + reference)) as response:
                if response.code == 404:
                    errors = read_json(response, 64 * 1024).get("errors")
                    require(
                        isinstance(errors, list)
                        and errors
                        and all(
                            isinstance(error, dict)
                            and error.get("code")
                            in {"MANIFEST_UNKNOWN", "NAME_UNKNOWN"}
                            for error in errors
                        ),
                        "REGISTRY_MANIFEST_LOOKUP_FAILED",
                    )
                    return None
                if response.code in {429, 500, 502, 503, 504}:
                    raise MigrationError("REGISTRY_TEMPORARY_FAILURE")
                require(response.code == 200, "REGISTRY_MANIFEST_LOOKUP_FAILED")
                raw = response.read(MAX_MANIFEST_BYTES + 1)
                require(len(raw) <= MAX_MANIFEST_BYTES, "REGISTRY_RESPONSE_TOO_LARGE")
                actual = digest(raw)
                advertised = response.headers.get("Docker-Content-Digest", actual)
                require(advertised == actual, "MANIFEST_DIGEST_MISMATCH")
                require(
                    not matches(DIGEST, reference) or reference == actual,
                    "MANIFEST_DIGEST_MISMATCH",
                )
                doc = json.loads(raw)
                require(isinstance(doc, dict), "INVALID_MANIFEST")
                doc.setdefault("mediaType", response.headers.get_content_type())
                return actual, doc
        except (OSError, ValueError, http.client.HTTPException):
            raise MigrationError("REGISTRY_MANIFEST_LOOKUP_FAILED") from None

    def verify_blob(self, repo: str, expected: str, size: int) -> None:
        result = hashlib.sha256()
        read = 0
        try:
            with closing(self.get(repo, "blobs/" + expected, blob=True)) as response:
                require(response.code == 200, "BLOB_UNAVAILABLE")
                while chunk := response.read(1024 * 1024):
                    read += len(chunk)
                    require(read <= size, "BLOB_SIZE_MISMATCH")
                    result.update(chunk)
        except (OSError, ValueError, http.client.HTTPException):
            raise MigrationError("BLOB_READ_FAILED") from None
        require(read == size, "BLOB_SIZE_MISMATCH")
        require("sha256:" + result.hexdigest() == expected, "BLOB_DIGEST_MISMATCH")


def descriptor(value: object) -> tuple[str, int]:
    require(isinstance(value, dict), "INVALID_DESCRIPTOR")
    identity, size = value.get("digest"), value.get("size")
    require(
        matches(DIGEST, identity) and type(size) is int and size >= 0,
        "INVALID_DESCRIPTOR",
    )
    return identity, size


def child_descriptors(manifest: dict) -> list[tuple[str, int]]:
    entries = manifest.get("manifests")
    require(isinstance(entries, list) and bool(entries), "INVALID_INDEX")
    return [descriptor(entry) for entry in entries]


class Verifier:
    def __init__(self, registry: Registry):
        self.registry = registry
        self.manifests: set[tuple[str, str]] = set()
        self.blobs: dict[tuple[str, str], int] = {}

    def verify(self, repo: str, reference: str, expected: str) -> None:
        result = self.registry.manifest(repo, reference)
        require(result is not None, "TARGET_MANIFEST_MISSING")
        require(result[0] == expected, "TARGET_DIGEST_MISMATCH")
        self.walk(repo, result[0], result[1], 0)

    def walk(self, repo: str, identity: str, manifest: dict, depth: int) -> None:
        key = (repo, identity)
        if key in self.manifests:
            return
        require(depth <= 32, "INDEX_DEPTH_LIMIT")
        if manifest.get("mediaType") in INDEX_TYPES:
            for child, _ in child_descriptors(manifest):
                if (repo, child) not in self.manifests:
                    result = self.registry.manifest(repo, child)
                    require(
                        result is not None and result[0] == child,
                        "TARGET_CHILD_MISSING",
                    )
                    self.walk(repo, child, result[1], depth + 1)
        else:
            require(
                manifest.get("mediaType") in IMAGE_TYPES, "UNSUPPORTED_IMAGE_FORMAT"
            )
            layers = manifest.get("layers")
            require(isinstance(layers, list), "INVALID_MANIFEST")
            for entry in [manifest.get("config"), *layers]:
                blob, size = descriptor(entry)
                blob_key = (repo, blob)
                if blob_key not in self.blobs:
                    self.registry.verify_blob(repo, blob, size)
                    self.blobs[blob_key] = size
                require(self.blobs[blob_key] == size, "BLOB_SIZE_MISMATCH")
        self.manifests.add(key)


class Skopeo:
    def __init__(self, config: Config):
        self.config = config
        # Keep runtime paths and proxy settings, but never pass AWS credentials to the copier.
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key
            in {"PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "XDG_CONFIG_HOME", "HOME"}
            or key.lower() in {"http_proxy", "https_proxy", "no_proxy", "all_proxy"}
        }
        try:
            version = subprocess.check_output(
                ["skopeo", "--version"],
                env=self.env,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).decode("ascii", "replace")
        except (OSError, subprocess.SubprocessError):
            raise MigrationError("SKOPEO_UNAVAILABLE") from None
        require(
            re.match(
                r"^skopeo version " + re.escape(config.skopeo_version) + r"(?:\s|$)",
                version,
            ),
            "SKOPEO_VERSION_MISMATCH",
        )

    def copy(self, source: str, target: str, source_auth: str, robot: Robot) -> None:
        config = self.config
        self.transfer(
            source,
            target,
            (config.source_host, source_auth),
            (config.target_host, encoded_robot(robot)),
            target_ca=config.ca_ref,
        )

    def transfer(
        self,
        source: str,
        target: str,
        source_auth: tuple[str, str],
        target_auth: tuple[str, str],
        *,
        source_ca: Path | None = None,
        target_ca: Path | None = None,
    ) -> None:
        config = self.config
        with tempfile.TemporaryDirectory(prefix="oneport-registry-") as directory:
            root = Path(directory)
            src, dst = root / "source.json", root / "target.json"
            for file, (host, value) in ((src, source_auth), (dst, target_auth)):
                with os.fdopen(
                    os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
                ) as out:
                    json.dump({"auths": {host: {"auth": value}}}, out)
            registries = root / "registries.conf"
            registries.write_text("")
            cert = []
            for flag, name, ca in (
                ("--src-cert-dir", "source-certs", source_ca),
                ("--dest-cert-dir", "target-certs", target_ca),
            ):
                if ca is not None:
                    cert_dir = root / name
                    cert_dir.mkdir(mode=0o700)
                    (cert_dir / "registry-ca.crt").write_bytes(ca.read_bytes())
                    cert += [flag, str(cert_dir)]
            try:
                result = subprocess.run(
                    [
                        "skopeo",
                        "--registries-conf",
                        str(registries),
                        "copy",
                        "--all",
                        "--preserve-digests",
                        "--remove-signatures",
                        "--src-tls-verify=true",
                        "--dest-tls-verify=true",
                        "--src-authfile",
                        str(src),
                        "--dest-authfile",
                        str(dst),
                        *cert,
                        "docker://" + source,
                        "docker://" + target,
                    ],
                    env=self.env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=config.copy_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise MigrationError("COPY_TIMED_OUT") from None
            except OSError:
                raise MigrationError("SKOPEO_UNAVAILABLE") from None
            if result.returncode:
                # The external error can contain a signed URL. Translate to fixed reason codes.
                error = result.stderr.lower()
                if any(
                    marker in error
                    for marker in (b"unauthorized", b"authentication required", b"401")
                ):
                    raise MigrationError("COPY_AUTH_FAILED")
                if any(
                    marker in error
                    for marker in (
                        b"429",
                        b"500 internal",
                        b"502 bad gateway",
                        b"503 service unavailable",
                        b"504 gateway",
                        b"i/o timeout",
                        b"connection reset",
                        b"unexpected eof",
                    )
                ):
                    raise MigrationError("COPY_TEMPORARY_FAILURE")
                raise MigrationError("COPY_FAILED")
