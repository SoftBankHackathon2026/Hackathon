"""Opt-in synthetic OCI checks and temporary Kubernetes pull probes."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .ansible.module_utils.harbor_core import Api, HarborError, check, read
from .handoff import binding, state_directory


def registry(api, method, path, token, payload=None, content_type="", expected=(200,)):
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.oci.image.manifest.v1+json",
    }
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(
        api.endpoint + path, data=payload, headers=headers, method=method
    )
    try:
        with api.opener.open(req, timeout=20) as response:
            status, data, response_headers = (
                response.status,
                response.read(1024 * 1024 + 1),
                dict(response.headers),
            )
    except urllib.error.HTTPError as exc:
        status, data, response_headers = exc.code, b"", dict(exc.headers)
        exc.close()
    except HarborError:
        raise
    except Exception:
        raise HarborError("REGISTRY_CONNECTION_FAILED") from None
    check(status in expected and len(data) <= 1024 * 1024, "REGISTRY_OPERATION_FAILED")
    return data, response_headers


def token(api, credentials, repository, actions):
    auth_api = Api(api.endpoint, "", credentials["username"], credentials["secret"])
    auth_api.opener = api.opener
    query = urllib.parse.urlencode(
        {
            "service": "harbor-registry",
            "scope": "repository:" + repository + ":" + actions,
        }
    )
    reply = auth_api.call("GET", "/service/token?" + query)
    value = reply.get("token") or reply.get("access_token")
    check(isinstance(value, str) and 0 < len(value) < 65536, "REGISTRY_TOKEN_MISSING")
    return value


def same_origin_upload(api, location):
    parsed = urllib.parse.urlsplit(location)
    endpoint = urllib.parse.urlsplit(api.endpoint)
    check(
        not parsed.username and not parsed.password and not parsed.fragment,
        "UPLOAD_LOCATION_REJECTED",
    )
    check(
        not parsed.netloc
        or parsed.scheme == endpoint.scheme
        and parsed.netloc == endpoint.netloc,
        "UPLOAD_ORIGIN_REJECTED",
    )
    check(
        parsed.path.startswith("/v2/")
        and not parsed.path.startswith("//")
        and "\\" not in parsed.path,
        "UPLOAD_LOCATION_REJECTED",
    )
    return parsed.path + ("?" + parsed.query if parsed.query else "")


def runtime_probe(c, project, repository, image_digest, config_digest, credentials):
    """No business workload edits; a namespace and explicit probe opt-in are required."""
    from kubernetes import client, config
    from kubernetes.client.exceptions import ApiException

    cfg = client.Configuration()
    config.load_kube_config(
        c["cluster"]["kubeconfig_ref"],
        context=c["cluster"]["context"],
        client_configuration=cfg,
        persist_config=False,
    )
    check(cfg.verify_ssl, "CLUSTER_TLS_REQUIRED")
    core = client.CoreV1Api(client.ApiClient(cfg))
    namespace = c["verification"]["test_namespace"]
    checks = []
    for node in c["trust"]["targets"]:
        name = "harbor-probe-" + uuid.uuid4().hex[:20]
        labels = {"app.kubernetes.io/managed-by": "oneport-harbor-probe"}
        authority = urllib.parse.urlsplit(c["endpoint"]["url"]).netloc
        secret_data = json.dumps(
            {
                "auths": {
                    authority: {
                        "username": credentials["username"],
                        "password": credentials["secret"],
                        "auth": base64.b64encode(
                            (
                                credentials["username"] + ":" + credentials["secret"]
                            ).encode()
                        ).decode(),
                    }
                }
            }
        )
        created_secret = created_pod = None
        try:
            created_secret = core.create_namespaced_secret(
                namespace,
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": name, "labels": labels},
                    "type": "kubernetes.io/dockerconfigjson",
                    "stringData": {".dockerconfigjson": secret_data},
                },
                _request_timeout=20,
            )
            image = authority + "/" + project + "/" + repository + "@" + image_digest
            spec = {
                "nodeName": node,
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "activeDeadlineSeconds": 120,
                "imagePullSecrets": [{"name": name}],
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 65534,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "image-only",
                        "image": image,
                        "imagePullPolicy": "Always",
                        "command": ["/image-pull-probe-only"],
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "16Mi"},
                            "limits": {"cpu": "50m", "memory": "32Mi"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }
                ],
            }
            created_pod = core.create_namespaced_pod(
                namespace,
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": name, "labels": labels},
                    "spec": spec,
                },
                _request_timeout=20,
            )
            verified = False
            for _ in range(40):
                pod = core.read_namespaced_pod(name, namespace, _request_timeout=10)
                image_ids = [
                    x.image_id or "" for x in pod.status.container_statuses or []
                ]
                if any(
                    image_digest in value or config_digest in value
                    for value in image_ids
                ):
                    verified = True
                    break
                time.sleep(2)
            check(verified, "NODE_RUNTIME_PULL_NOT_VERIFIED")
            checks.append(
                {"name": "node_image_pull", "node_name": node, "status": "passed"}
            )
        except ApiException:
            raise HarborError("KUBERNETES_PROBE_FAILED") from None
        finally:
            # Try both deletions even if one fails; never delete replacements.
            cleanup_failed = False
            for kind, created in (("pod", created_pod), ("secret", created_secret)):
                if not created:
                    continue
                try:
                    body = {"preconditions": {"uid": created.metadata.uid}}
                    if kind == "pod":
                        body["gracePeriodSeconds"] = 0
                        core.delete_namespaced_pod(
                            name, namespace, body=body, _request_timeout=20
                        )
                    else:
                        core.delete_namespaced_secret(
                            name, namespace, body=body, _request_timeout=20
                        )
                except Exception:
                    cleanup_failed = True
            check(not cleanup_failed, "KUBERNETES_PROBE_CLEANUP_FAILED")
    return checks


def image_checks(api, c, accounts):
    checks = []
    for project in c["projects"]:
        name = project["name"]
        repository = "harbor-infra-probe-" + uuid.uuid4().hex
        repo = name + "/" + repository
        writer, reader = accounts[name + "/writer"], accounts[name + "/reader"]
        write_token = token(api, writer, repo, "pull,push")
        read_token = token(api, reader, repo, "pull,push")
        created_digest = None
        try:
            # The fresh name is also checked before writing; never overwrite an existing tag.
            registry(
                api,
                "HEAD",
                "/v2/" + repo + "/manifests/probe",
                write_token,
                expected=(404,),
            )
            config_bytes = json.dumps(
                {
                    "architecture": "amd64",
                    "os": "linux",
                    "config": {},
                    "rootfs": {"type": "layers", "diff_ids": []},
                    "history": [],
                    "oneport_probe_nonce": repository,
                },
                separators=(",", ":"),
            ).encode()
            config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
            _, headers = registry(
                api,
                "POST",
                "/v2/" + repo + "/blobs/uploads/",
                write_token,
                b"",
                expected=(202,),
            )
            location = headers.get("Location") or headers.get("location")
            check(isinstance(location, str), "UPLOAD_LOCATION_MISSING")
            upload = same_origin_upload(api, location)
            upload += ("&" if "?" in upload else "?") + urllib.parse.urlencode(
                {"digest": config_digest}
            )
            registry(
                api,
                "PUT",
                upload,
                write_token,
                config_bytes,
                "application/octet-stream",
                (201,),
            )
            manifest = json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {
                        "mediaType": "application/vnd.oci.image.config.v1+json",
                        "digest": config_digest,
                        "size": len(config_bytes),
                    },
                    "layers": [],
                },
                separators=(",", ":"),
            ).encode()
            created_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
            registry(
                api,
                "PUT",
                "/v2/" + repo + "/manifests/probe",
                write_token,
                manifest,
                "application/vnd.oci.image.manifest.v1+json",
                (201,),
            )
            result, _ = registry(
                api, "GET", "/v2/" + repo + "/manifests/" + created_digest, read_token
            )
            check(
                hashlib.sha256(result).hexdigest() == created_digest.split(":")[1],
                "MANIFEST_DIGEST_MISMATCH",
            )
            registry(
                api,
                "POST",
                "/v2/" + repo + "/blobs/uploads/",
                read_token,
                b"",
                expected=(401, 403),
            )
            checks.extend(
                [
                    {
                        "name": "writer_push_reader_pull_digest",
                        "project": name,
                        "status": "passed",
                    },
                    {"name": "reader_push_denied", "project": name, "status": "passed"},
                ]
            )
            other = next(
                (
                    v
                    for k, v in accounts.items()
                    if k.endswith("/reader") and v.get("project") != name
                ),
                None,
            )
            if other:
                other_token = token(api, other, repo, "pull")
                registry(
                    api,
                    "GET",
                    "/v2/" + repo + "/manifests/" + created_digest,
                    other_token,
                    expected=(401, 403),
                )
            checks.append(
                {
                    "name": "cross_project_pull_denied",
                    "project": name,
                    "status": "passed" if other else "not_applicable",
                }
            )
            if c["verification"]["runtime_pull"]:
                checks.extend(
                    runtime_probe(
                        c, name, repository, created_digest, config_digest, reader
                    )
                )
        finally:
            if created_digest:
                api.call(
                    "DELETE",
                    "/api/v2.0/projects/"
                    + urllib.parse.quote(name, safe="")
                    + "/repositories/"
                    + repository
                    + "/artifacts/"
                    + created_digest,
                    expected=(200, 202, 404),
                )
    return checks


def verify(c, values):
    checks = []
    ep = c["endpoint"]
    api = Api(
        ep["url"],
        values.get("endpoint_ca", ""),
        ep["admin_username"],
        values.get("admin_password", ""),
    )
    health = api.call("GET", "/api/v2.0/health", authenticated=False)
    checks.append(
        {
            "name": "harbor_components_healthy",
            "status": "passed" if health.get("status") == "healthy" else "failed",
        }
    )
    root = state_directory(c)
    state = read(root / "managed.json")
    credentials = read(root / "credentials.json")
    check(
        state
        and state.get("configured")
        and state["binding"] == binding(c)
        and credentials
        and credentials["binding"] == binding(c),
        "CONFIGURATION_NOT_READY",
    )
    cfg = api.call("GET", "/api/v2.0/configurations")
    from .ansible.module_utils.harbor_core import config_value, oidc_settings

    checks.append(
        {
            "name": "oidc_enabled",
            "status": "passed"
            if all(
                config_value(cfg, key) == expected
                for key, expected in oidc_settings(c["keycloak"]).items()
            )
            else "failed",
        }
    )
    checks.append({"name": "oidc_browser_login", "status": "not_run"})
    for project in c["projects"]:
        remote_project = api.call(
            "GET", "/api/v2.0/projects/" + urllib.parse.quote(project["name"], safe="")
        )
        valid_project = (
            state["projects"].get(project["name"]) == remote_project.get("project_id")
            and remote_project.get("metadata", {}).get("public") == "false"
            and not remote_project.get("registry_id")
        )
        checks.append(
            {
                "name": "private_destination_project",
                "project": project["name"],
                "status": "passed" if valid_project else "failed",
            }
        )
    from .ansible.module_utils.harbor_core import normalized_permissions, permissions

    for key, account in credentials["accounts"].items():
        remote = api.call("GET", "/api/v2.0/robots/" + str(account["id"]))
        valid = (
            remote.get("level") == "project"
            and not remote.get("disable")
            and remote.get("name") == account["username"]
            and remote.get("expires_at", 0) > time.time()
            and normalized_permissions(remote.get("permissions", []))
            == normalized_permissions(
                permissions(account["project"], account["role"] == "writer")
            )
        )
        checks.append(
            {
                "name": "robot_policy",
                "account": key,
                "status": "passed" if valid else "failed",
            }
        )
    if c["verification"]["image_tests"]:
        checks.extend(image_checks(api, c, credentials["accounts"]))
    else:
        checks.append({"name": "image_permission_tests", "status": "not_run"})
    if c["trust"]["targets"] and not c["verification"]["runtime_pull"]:
        checks.append({"name": "node_runtime_image_pull", "status": "not_run"})
    receipt = read(root / "export-record.json")
    checks.append(
        {
            "name": "credential_handoff",
            "status": "passed"
            if receipt and receipt.get("acknowledged")
            else "not_run",
        }
    )
    required = [v for v in checks if v["name"] != "oidc_browser_login"]
    return {
        "status": "ready"
        if all(v["status"] in ("passed", "not_applicable") for v in required)
        else "partially_ready",
        "checks": checks,
        "reason_codes": []
        if all(v["status"] in ("passed", "not_applicable") for v in required)
        else ["REQUIRED_VERIFICATION_INCOMPLETE"],
    }
