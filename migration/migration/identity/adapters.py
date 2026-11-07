"""Native, TLS-verified target APIs. Creation-only; existing differences are conflicts."""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from urllib.parse import quote

from .common import IdentityError, https_url, load, require, secret


def segment(value):
    return quote(value, safe="")


class Http:
    def __init__(
        self,
        base_url,
        token_env=None,
        ca_file=None,
        header="Authorization",
        *,
        token_value=None,
    ):
        import requests

        self.session = requests.Session()
        # Avoid ambient proxies, netrc authentication and credentials sent on redirects.
        self.session.trust_env = False
        self.base = base_url.rstrip("/")
        self.verify = ca_file or True
        token = token_value if token_value is not None else secret(token_env)
        self.headers = (
            {header: ("Bearer " + token) if header == "Authorization" else token}
            if token
            else {}
        )

    def request(self, method, path, *, payload=None, params=None, missing=False):
        try:
            response = self.session.request(
                method,
                self.base + path,
                headers=self.headers,
                json=payload,
                params=params,
                verify=self.verify,
                timeout=(10, 30),
                allow_redirects=False,
            )
        except Exception:  # noqa: BLE001 -- keep requests errors and tokens out of output.
            raise IdentityError("TARGET_TRANSPORT_ERROR", status="failed") from None
        if missing and response.status_code == 404:
            return None
        if response.status_code not in {200, 201, 204}:
            raise IdentityError(
                "TARGET_HTTP_" + str(response.status_code), status="failed"
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise IdentityError("TARGET_RESPONSE_SHAPE", status="failed") from None


def project(actual, expected):
    if actual is None:
        return None
    require(isinstance(actual, dict), "TARGET_RESPONSE_SHAPE")
    return {
        key: project(actual.get(key), value)
        if isinstance(value, dict)
        else actual.get(key)
        for key, value in expected.items()
    }


class KubernetesHttp(Http):
    """Stable APIs and static kubeconfig; no credential-plugin execution or SDK dependency."""

    def __init__(self, target, *, user_token=None):
        path = Path(target["kubeconfig"])
        raw = load(path)
        context = next(
            (
                x["context"]
                for x in raw.get("contexts", [])
                if x["name"] == target["context"]
            ),
            None,
        )
        require(context is not None, "KUBERNETES_CONTEXT_NOT_FOUND")
        cluster = next(
            (
                x["cluster"]
                for x in raw.get("clusters", [])
                if x["name"] == context["cluster"]
            ),
            None,
        )
        user = next(
            (
                x["user"]
                for x in raw.get("users", [])
                if x["name"] == context.get("user")
            ),
            {},
        )
        require(
            cluster is not None and not cluster.get("insecure-skip-tls-verify"),
            "KUBERNETES_TLS_REQUIRED",
        )
        self.temp = tempfile.TemporaryDirectory(prefix="identity-kube-")

        def material(field, source):
            if field + "-data" in source:
                out = Path(self.temp.name) / field
                data = base64.b64decode(source[field + "-data"], validate=True)
                require(len(data) <= 1048576, "KUBERNETES_CERTIFICATE_SIZE")
                out.write_bytes(data)
                out.chmod(0o600)
                return str(out)
            if field in source:
                file = Path(source[field])
                return str(file if file.is_absolute() else path.parent / file)
            return None

        ca = material("certificate-authority", cluster)
        if user_token is None:
            require(
                not set(user) & {"exec", "auth-provider", "username", "password"},
                "STATIC_KUBECONFIG_REQUIRED",
            )
            token = user.get("token", "")
            if user.get("tokenFile"):
                token_file = Path(user["tokenFile"])
                token = (
                    (
                        token_file
                        if token_file.is_absolute()
                        else path.parent / token_file
                    )
                    .read_text()
                    .strip()
                )
        else:
            token = user_token
        super().__init__(https_url(cluster["server"]), ca_file=ca, token_value=token)
        if user_token is None:
            cert, key = (
                material("client-certificate", user),
                material("client-key", user),
            )
            require(
                bool(token) or bool(cert and key), "KUBERNETES_CREDENTIALS_REQUIRED"
            )
            if cert and key:
                self.session.cert = (cert, key)


def kube_resource_path(r):
    p = r["payload"]["metadata"]
    collection = "roles" if r["kind"] == "k8s_role" else "rolebindings"
    return (
        "/apis/rbac.authorization.k8s.io/v1/namespaces/"
        + segment(p["namespace"])
        + "/"
        + collection
    )


class Targets:
    def __init__(self, config, plan):
        self.config = config
        self.resources = {r["id"]: r for r in plan["resources"]}
        c = config["targets"]["keycloak"]
        self.kc = Http(c["base_url"], c["admin_token_env"], c.get("ca_file"))
        self.realm = "/admin/realms/" + segment(c["realm"])
        b = config["targets"].get("openbao")
        self.bao = (
            Http(b["base_url"], b["token_env"], b.get("ca_file"), "X-Vault-Token")
            if b
            else None
        )
        self._kube = None

    def list_kc(self, path, params=None):
        items = []
        for start in range(0, 100000, 100):
            page = self.kc.request(
                "GET",
                path,
                params={**(params or {}), "first": start, "max": 100},
                missing=True,
            )
            if page is None:
                return []
            require(isinstance(page, list), "KEYCLOAK_RESPONSE_SHAPE")
            items.extend(page)
            if len(page) < 100:
                return items
        raise IdentityError("KEYCLOAK_PAGINATION_LIMIT")

    def find(self, kind, name):
        r = self.resources[kind + ":" + name]
        if kind == "kc_user":
            field, value, collection = "username", r["payload"]["username"], "users"
        elif kind == "kc_group":
            field, value, collection = "name", r["payload"]["name"], "groups"
        else:
            field, value, collection = "clientId", name, "clients"
        params = (
            {"username": value, "exact": "true"}
            if collection == "users"
            else {"search": value, "exact": "true", "briefRepresentation": "false"}
            if collection == "groups"
            else {"clientId": value}
        )
        items = self.list_kc(self.realm + "/" + collection, params)
        found = [item for item in items if item.get(field) == value]
        require(len(found) <= 1, "AMBIGUOUS_KEYCLOAK_IDENTITY")
        return found[0] if found else None

    def kc_address(self, r):
        kind, p = r["kind"], r["payload"]
        if kind == "kc_realm":
            return self.realm
        if kind in {"kc_user", "kc_group", "kc_client"}:
            item = self.find(kind, r["name"])
            collection = {
                "kc_user": "users",
                "kc_group": "groups",
                "kc_client": "clients",
            }[kind]
            return (
                self.realm + "/" + collection + "/" + segment(item["id"])
                if item
                else None
            )
        if kind == "kc_membership":
            user, group = (
                self.find("kc_user", p["user"]),
                self.find("kc_group", p["group"]),
            )
            return (
                self.realm
                + "/users/"
                + segment(user["id"])
                + "/groups/"
                + segment(group["id"])
                if user and group
                else None
            )
        client = self.find("kc_client", p["client"])
        if client is None:
            return None
        client_path = self.realm + "/clients/" + segment(client["id"])
        if kind == "kc_role":
            return client_path + "/roles/" + segment(p["name"])
        if kind == "kc_mapper":
            found = self.kc.request("GET", client_path + "/protocol-mappers/models")
            found = [x for x in found if x["name"] == p["name"]]
            require(len(found) <= 1, "AMBIGUOUS_MAPPER")
            return (
                client_path + "/protocol-mappers/models/" + segment(found[0]["id"])
                if found
                else None
            )
        if kind == "kc_group_role":
            group = self.find("kc_group", p["group"])
            return (
                self.realm
                + "/groups/"
                + segment(group["id"])
                + "/role-mappings/clients/"
                + segment(client["id"])
                if group
                else None
            )
        raise IdentityError("UNSUPPORTED_RESOURCE_KIND")

    def read(self, r):
        kind, p = r["kind"], r["payload"]
        if kind.startswith("kc_"):
            path = self.kc_address(r)
            if path is None:
                return None
            if kind == "kc_membership":
                user = self.find("kc_user", p["user"])
                group = self.find("kc_group", p["group"])
                found = self.list_kc(
                    self.realm + "/users/" + segment(user["id"]) + "/groups"
                )
                return p if any(g["id"] == group["id"] for g in found) else None
            if kind == "kc_group_role":
                found = self.kc.request("GET", path)
                return p if any(x["name"] == p["role"] for x in found) else None
            expected = {k: v for k, v in p.items() if k != "client"}
            raw = self.kc.request("GET", path, missing=True)
            value = project(raw, expected)
            if value is not None and isinstance(value.get("redirectUris"), list):
                value["redirectUris"] = sorted(value["redirectUris"])
            if value is not None and "client" in p:
                value["client"] = p["client"]
            return value
        if kind.startswith("bao_"):
            require(self.bao is not None, "OPENBAO_TARGET_UNAVAILABLE")
            if kind == "bao_mount":
                mounts = self.bao.request("GET", "/v1/sys/auth")["data"]
                raw = mounts.get(r["name"] + "/")
                return project(raw, p)
            if kind == "bao_policy":
                mounts = self.bao.request("GET", "/v1/sys/mounts")["data"]
                for item in p["paths"]:
                    mount = mounts.get(item["path"].split("/", 1)[0] + "/", {})
                    require(
                        mount.get("type") == "kv"
                        and mount.get("options", {}).get("version") == "2",
                        "OPENBAO_KV2_MOUNT_REQUIRED",
                    )
            path = self.bao_path(r)
            raw = self.bao.request("GET", path, missing=True)
            if raw is None:
                return None
            if kind == "bao_policy":
                policy = raw["data"]["policy"]
                return p if policy == self.policy_text(p) else {"conflict": True}
            actual = raw["data"]
            if kind == "bao_config" and not any(
                actual.get(field)
                for field in (
                    "oidc_discovery_url",
                    "jwks_url",
                    "jwt_validation_pubkeys",
                    "bound_issuer",
                )
            ):
                # Enabling the auth mount exposes an empty default config. It is
                # still unconfigured, rather than drift from the absent parent snapshot.
                return None
            # OpenBao normalizes duration strings to integral seconds.
            return project(actual, p)
        if kind.startswith("k8s_"):
            return self.kube_read(r)
        raise IdentityError("UNSUPPORTED_RESOURCE_KIND")

    def write(self, r):
        kind, p = r["kind"], r["payload"]
        if kind.startswith("kc_"):
            data = {k: v for k, v in p.items() if k != "client"}
            if kind == "kc_realm":
                self.kc.request("POST", "/admin/realms", payload=data)
            elif kind in {"kc_user", "kc_group", "kc_client"}:
                collection = {
                    "kc_user": "users",
                    "kc_group": "groups",
                    "kc_client": "clients",
                }[kind]
                if kind == "kc_user":
                    data.update(
                        enabled=False,
                        emailVerified=False,
                        requiredActions=["UPDATE_PASSWORD", "CONFIGURE_TOTP"],
                    )
                self.kc.request("POST", self.realm + "/" + collection, payload=data)
            elif kind == "kc_membership":
                self.kc.request("PUT", self.kc_address(r))
            elif kind in {"kc_role", "kc_mapper"}:
                client = self.find("kc_client", p["client"])
                suffix = "/roles" if kind == "kc_role" else "/protocol-mappers/models"
                self.kc.request(
                    "POST",
                    self.realm + "/clients/" + segment(client["id"]) + suffix,
                    payload=data,
                )
            elif kind == "kc_group_role":
                role = self.role_representation(p)
                self.kc.request("POST", self.kc_address(r), payload=[role])
            else:
                raise IdentityError("UNSUPPORTED_RESOURCE_KIND")
        elif kind.startswith("bao_"):
            if kind == "bao_mount":
                self.bao.request(
                    "POST", "/v1/sys/auth/" + segment(r["name"]), payload=p
                )
            elif kind == "bao_policy":
                self.bao.request(
                    "PUT", self.bao_path(r), payload={"policy": self.policy_text(p)}
                )
            else:
                self.bao.request("POST", self.bao_path(r), payload=p)
        elif kind.startswith("k8s_"):
            self.kube().request("POST", kube_resource_path(r), payload=p)
        else:
            raise IdentityError("UNSUPPORTED_RESOURCE_KIND")

    def role_representation(self, p):
        client = self.find("kc_client", p["client"])
        return self.kc.request(
            "GET",
            self.realm
            + "/clients/"
            + segment(client["id"])
            + "/roles/"
            + segment(p["role"]),
        )

    @staticmethod
    def policy_text(payload):
        return "\n".join(
            "path "
            + json.dumps(p["path"])
            + " { capabilities = "
            + json.dumps(p["capabilities"])
            + " }"
            for p in payload["paths"]
        )

    @staticmethod
    def bao_path(r):
        kind, name = r["kind"], r["name"]
        if kind == "bao_policy":
            return "/v1/sys/policies/acl/" + segment(name)
        if kind == "bao_config":
            return "/v1/auth/" + segment(name) + "/config"
        mount, role = name.split("/", 1)
        return "/v1/auth/" + segment(mount) + "/role/" + segment(role)

    def kube(self):
        if self._kube is None:
            self._kube = KubernetesHttp(self.config["targets"]["kubernetes"])
        return self._kube

    def kube_read(self, r):
        api = self.kube()
        p = r["payload"]
        # An absent namespace is a prerequisite failure, not an absent role.
        api.request("GET", "/api/v1/namespaces/" + segment(p["metadata"]["namespace"]))
        value = api.request(
            "GET",
            kube_resource_path(r) + "/" + segment(p["metadata"]["name"]),
            missing=True,
        )
        if value is None:
            return None
        value["metadata"] = project(value["metadata"], p["metadata"])
        return project(value, p)

    def delete(self, r):
        # Rollback only removes newly-created access grants and their OpenBao policy.
        kind = r["kind"]
        require(
            kind
            in {
                "kc_membership",
                "kc_group_role",
                "k8s_binding",
                "bao_role",
                "bao_policy",
            },
            "ROLLBACK_RETAINS_IDENTITY_AND_CONTAINERS",
        )
        if kind == "kc_group_role":
            self.kc.request(
                "DELETE",
                self.kc_address(r),
                payload=[self.role_representation(r["payload"])],
            )
        elif kind == "kc_membership":
            self.kc.request("DELETE", self.kc_address(r))
        elif kind.startswith("bao_"):
            self.bao.request("DELETE", self.bao_path(r))
        else:
            self.kube().request(
                "DELETE",
                kube_resource_path(r) + "/" + segment(r["payload"]["metadata"]["name"]),
            )
