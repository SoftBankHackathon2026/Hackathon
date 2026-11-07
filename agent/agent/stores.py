"""설정 저장소 어댑터. AI 는 여기서 환경변수 값을 읽고(마스킹해서 보고) 허용된 키만 고쳐 쓴다.

  온프렘  → HashiCorp Vault KV v2  (VAULT_TOKEN 환경변수)
  AWS     → AWS Secrets Manager     (표준 AWS 자격증명 체인: 환경변수/IRSA)
  개발    → 로컬 JSON 파일

쓰기는 항상 read-modify-write 이고, 읽은 버전(version)이 바뀌었으면 거부한다(낙관적 잠금).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from .config import StoreConfig, env_secret

log = logging.getLogger("agent.stores")


class StoreError(RuntimeError):
    """저장소 읽기/쓰기 실패."""


class VersionConflict(StoreError):
    """읽은 뒤 다른 쪽이 먼저 바꿨다. 다시 읽고 다시 판정한다."""


@dataclass(frozen=True)
class Snapshot:
    """한 번 읽은 설정. values 에는 비밀값도 들어 있으므로 masked() 만 밖으로 낸다."""

    values: Mapping[str, str]
    version: str
    location: str  # 사람이 읽는 위치 설명 (비밀값 없음)

    def masked(self, secret_keys: tuple[str, ...]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in self.values.items():
            out[k] = "***" if _is_secret_key(k, secret_keys) else v
        return out


def _is_secret_key(key: str, secret_keys: tuple[str, ...]) -> bool:
    lk = key.lower()
    return key in secret_keys or any(p in lk for p in ("password", "secret", "token", "dsn", "conninfo"))


class ConfigStore(Protocol):
    name: str

    def read(self) -> Snapshot: ...

    def write(self, values: Mapping[str, str], *, expected_version: str) -> Snapshot: ...


def _as_str_map(data: Any, where: str) -> dict[str, str]:
    if not isinstance(data, Mapping):
        raise StoreError(f"{where}: 값이 객체가 아니다")
    return {str(k): ("" if v is None else str(v)) for k, v in data.items()}


# ---------------------------------------------------------------------------
# 파일 (개발·테스트)
# ---------------------------------------------------------------------------


class FileStore:
    """JSON 파일 {key: value}. 버전은 파일 내용 digest."""

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _version(self, text: str) -> str:
        import hashlib

        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def read(self) -> Snapshot:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StoreError(f"파일 저장소 읽기 실패 {self.path}: {exc}") from None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise StoreError(f"파일 저장소가 JSON 이 아니다 {self.path}: {exc}") from None
        return Snapshot(_as_str_map(data, str(self.path)), self._version(text), f"file:{self.path.name}")

    def write(self, values: Mapping[str, str], *, expected_version: str) -> Snapshot:
        current = self.read()
        if current.version != expected_version:
            raise VersionConflict(f"파일이 읽은 뒤 바뀌었다 ({current.version} != {expected_version})")
        text = json.dumps(dict(values), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)
        return Snapshot(dict(values), self._version(text), current.location)


# ---------------------------------------------------------------------------
# Vault KV v2 (온프렘)
# ---------------------------------------------------------------------------


class VaultKvStore:
    """KV v2: GET/POST /v1/{mount}/data/{path}. 쓰기는 cas=version 으로 충돌을 막는다."""

    name = "vault"

    def __init__(self, addr: str, mount: str, path: str, *, token: str | None = None, timeout: float = 5.0,
                 namespace: str | None = None) -> None:
        self.addr = addr.rstrip("/")
        self.mount = mount.strip("/")
        self.path = path.strip("/")
        self._token = token if token is not None else env_secret("VAULT_TOKEN")
        self.timeout = timeout
        self.namespace = namespace or env_secret("VAULT_NAMESPACE")
        if not self._token:
            raise StoreError("VAULT_TOKEN 환경변수가 없다")

    @property
    def url(self) -> str:
        return f"{self.addr}/v1/{self.mount}/data/{self.path}"

    def _request(self, method: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"X-Vault-Token": self._token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        req = urllib.request.Request(self.url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - 설정된 내부 URL
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 400 and "check-and-set" in detail:
                raise VersionConflict(f"Vault cas 충돌: {detail}") from None
            raise StoreError(f"Vault HTTP {exc.code} ({method} {self.mount}/{self.path}): {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise StoreError(f"Vault 연결 실패 ({self.addr}): {exc}") from None
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError as exc:
            raise StoreError(f"Vault 응답이 JSON 이 아니다: {exc}") from None

    def read(self) -> Snapshot:
        doc = self._request("GET")
        data = (doc.get("data") or {})
        values = _as_str_map(data.get("data"), f"vault:{self.mount}/{self.path}")
        version = str((data.get("metadata") or {}).get("version", ""))
        if not version:
            raise StoreError("Vault 응답에 metadata.version 이 없다 (KV v2 가 아닌가?)")
        return Snapshot(values, version, f"vault:{self.mount}/{self.path}@v{version}")

    def write(self, values: Mapping[str, str], *, expected_version: str) -> Snapshot:
        doc = self._request("POST", {"data": dict(values), "options": {"cas": int(expected_version)}})
        new_version = str((doc.get("data") or {}).get("version", ""))
        if not new_version:
            raise StoreError("Vault 쓰기 응답에 version 이 없다")
        return Snapshot(dict(values), new_version, f"vault:{self.mount}/{self.path}@v{new_version}")


# ---------------------------------------------------------------------------
# AWS Secrets Manager (AWS)
# ---------------------------------------------------------------------------


class AwsSecretsManagerStore:
    """SecretString 이 JSON 객체인 secret 하나. 버전은 VersionId, 쓰기는 ClientRequestToken 으로 멱등."""

    name = "aws-secrets-manager"

    def __init__(self, secret_id: str, *, region: str | None = None, client: Any = None) -> None:
        self.secret_id = secret_id
        if client is None:
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError:
                raise StoreError("boto3 가 설치돼 있지 않다 (pip install 'ghost-agent[aws]')") from None
            client = boto3.client("secretsmanager", region_name=region)
        self._client = client

    def read(self) -> Snapshot:
        try:
            resp = self._client.get_secret_value(SecretId=self.secret_id)
        except Exception as exc:  # noqa: BLE001 - botocore 예외 계층을 그대로 감싼다
            raise StoreError(f"Secrets Manager 읽기 실패 ({self.secret_id}): {exc}") from None
        text = resp.get("SecretString")
        if not text:
            raise StoreError("SecretString 이 비어 있다 (바이너리 secret 은 지원하지 않는다)")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise StoreError(f"SecretString 이 JSON 객체가 아니다: {exc}") from None
        version = str(resp.get("VersionId", ""))
        return Snapshot(_as_str_map(data, self.secret_id), version, f"aws-sm:{self.secret_id}@{version[:8]}")

    def write(self, values: Mapping[str, str], *, expected_version: str) -> Snapshot:
        current = self.read()
        if current.version != expected_version:
            raise VersionConflict(f"secret 이 읽은 뒤 바뀌었다 ({current.version} != {expected_version})")
        import uuid

        token = str(uuid.uuid4())
        try:
            resp = self._client.put_secret_value(
                SecretId=self.secret_id,
                SecretString=json.dumps(dict(values), ensure_ascii=False, sort_keys=True),
                ClientRequestToken=token,
            )
        except Exception as exc:  # noqa: BLE001
            raise StoreError(f"Secrets Manager 쓰기 실패 ({self.secret_id}): {exc}") from None
        version = str(resp.get("VersionId", token))
        return Snapshot(dict(values), version, f"aws-sm:{self.secret_id}@{version[:8]}")


def build_store(cfg: StoreConfig) -> ConfigStore:
    if cfg.kind == "file":
        return FileStore(cfg.path or "target.json")
    if cfg.kind == "vault":
        return VaultKvStore(cfg.addr or "", cfg.mount, cfg.secret_path or "")
    if cfg.kind == "aws-secrets-manager":
        return AwsSecretsManagerStore(cfg.secret_id or "", region=cfg.region)
    raise StoreError(f"알 수 없는 저장소 종류: {cfg.kind}")
