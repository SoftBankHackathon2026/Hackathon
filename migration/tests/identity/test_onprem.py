"""Mock provider contracts; no real login, resource access or role changes occur."""
import copy
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from migration.identity.adapters.base import observation
from migration.identity.adapters.keycloak import KeycloakAdapter
from migration.identity.adapters.kubernetes import KubernetesAdapter
from migration.identity.adapters.openstack import OpenStackAdapter
from migration.identity.adapters import keycloak as kc_module, kubernetes as kube_module, openstack as os_module
from migration.identity.models import (BlockedError, ConfigError, ConflictError, Endpoint,
                                       Operation, UnknownOutcome, UnsupportedError, canonical)


class APIError(Exception):
    def __init__(self, status):
        self.status = self.status_code = self.http_status = status
        super().__init__("SECRET provider error response")


def kube_endpoint(**opts):
    return Endpoint("kubernetes", "reports", "oidc:readers", "reports", {"context": "isolated-test", "url": "https://cluster.example.invalid:6443", "group_claim_confirmed": True, **opts})


def kube_op(kind="kubernetes.role", **kwargs):
    desired = {"rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}]} if kind == "kubernetes.role" else {"role_name": "migration-namespace-read", "group": "oidc:readers"}
    return Operation("kube-test", "kubernetes", kind, "reports", "reports/migration-namespace-read", desired, observation(), **kwargs)


class KubeFake:
    def __init__(self):
        self.roles, self.bindings, self.calls = {}, {}, []
        self.lost = self.conflict = self.denied = self.repeated = False

    def _read(self, collection, name, namespace):
        if self.denied:
            raise APIError(403)
        if name not in collection:
            raise APIError(404)
        return copy.deepcopy(collection[name])

    def read_namespaced_role(self, name, namespace):
        return self._read(self.roles, name, namespace)

    def read_namespaced_role_binding(self, name, namespace):
        return self._read(self.bindings, name, namespace)

    def _create(self, collection, namespace, body):
        self.calls.append(copy.deepcopy(body))
        if self.conflict:
            raise APIError(409)
        raw = copy.deepcopy(body)
        raw["metadata"].update(uid="uid-1", resourceVersion="1")
        collection[raw["metadata"]["name"]] = raw
        if self.lost:
            raise TimeoutError("SECRET response lost")
        return copy.deepcopy(raw)

    def create_namespaced_role(self, namespace, body):
        return self._create(self.roles, namespace, body)

    def create_namespaced_role_binding(self, namespace, body):
        return self._create(self.bindings, namespace, body)

    def _list(self, collection, **kwargs):
        if self.denied:
            raise APIError(403)
        if self.repeated:
            return {"metadata": {"continue": "same"}, "items": []}
        return {"metadata": {}, "items": list(copy.deepcopy(collection).values())}

    def list_namespaced_role(self, **kwargs):
        return self._list(self.roles, **kwargs)

    def list_namespaced_role_binding(self, **kwargs):
        return self._list(self.bindings, **kwargs)


@pytest.mark.parametrize("kind", ["kubernetes.role", "kubernetes.role_binding"])
def test_kube_additive_creation_and_duplicate(kind):
    fake = KubeFake()
    adapter = KubernetesAdapter(kube_endpoint(), fake, context_verified=True)
    op = kube_op(kind)
    before = adapter.observe(op)
    after = adapter.apply(op, before)
    assert adapter.satisfied(op, after)
    assert adapter.apply(op, after) == after
    assert len(fake.calls) == 1


def test_kube_resource_version_conflict_and_existing_rules_preserved():
    fake = KubeFake()
    adapter = KubernetesAdapter(kube_endpoint(), fake, context_verified=True)
    op = kube_op()
    old = adapter.apply(op, observation())
    fake.roles["migration-namespace-read"]["metadata"]["resourceVersion"] = "2"
    with pytest.raises(ConflictError, match="resourceVersion"):
        adapter.apply(op, old)
    current = adapter.observe(op)
    changed = replace(op, desired={"rules": [{"apiGroups": [""], "resources": ["services"], "verbs": ["get"]}]})
    with pytest.raises(ConflictError, match="updates are unsupported"):
        adapter.apply(changed, current)
    assert fake.roles["migration-namespace-read"]["rules"] == op.desired["rules"]


def test_kube_lost_response_is_unknown_and_reobserved():
    fake = KubeFake()
    fake.lost = True
    adapter = KubernetesAdapter(kube_endpoint(), fake, context_verified=True)
    with pytest.raises(UnknownOutcome) as err:
        adapter.apply(kube_op(), observation())
    assert "SECRET" not in str(err.value)
    assert adapter.satisfied(kube_op(), adapter.observe(kube_op()))


@pytest.mark.parametrize("change", [{"scope": "other"}, {"resource": "other/migration-namespace-read"}, {"resource": "reports/unapproved"}, {"kind": "kubernetes.cluster_role_binding"}])
def test_kube_scope_escape(change):
    adapter = KubernetesAdapter(kube_endpoint(), KubeFake(), context_verified=True)
    with pytest.raises(BlockedError):
        adapter.observe(replace(kube_op(), **change))


def test_kube_group_and_context_must_be_confirmed():
    adapter = KubernetesAdapter(kube_endpoint(), KubeFake())
    with pytest.raises(BlockedError, match="context"):
        adapter.apply(kube_op(), observation())
    with pytest.raises(BlockedError, match="subject"):
        adapter.observe(replace(kube_op("kubernetes.role_binding"), desired={"role_name": "migration-namespace-read", "group": "other"}))


def test_kube_paging_denied_and_repeated_token():
    fake = KubeFake()
    fake.denied = True
    adapter = KubernetesAdapter(kube_endpoint(), fake)
    inventory = adapter.collect()
    assert inventory["coverage"]["resource_policy"] == "denied"
    assert "SECRET" not in canonical(inventory)
    fake.denied, fake.repeated = False, True
    assert adapter.collect()["coverage"]["resource_policy"] == "failed"


def test_kube_all_pages_and_snapshot_conflict():
    fake = KubeFake()
    adapter = KubernetesAdapter(kube_endpoint(), fake)
    calls = []

    def pages(**kwargs):
        calls.append(kwargs)
        page = 2 if kwargs.get("_continue") else 1
        return {"metadata": {"resourceVersion": "20", "continue": "next" if page == 1 else ""},
                "items": [{"metadata": {"name": f"read-{page}", "namespace": "reports", "uid": f"uid-{page}", "resourceVersion": "19"},
                           "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}]}]}

    assert len(adapter._list(pages, "kubernetes.role")) == 2
    assert calls[1]["_continue"] == "next"

    def changed(**kwargs):
        raw = pages(**kwargs)
        if kwargs.get("_continue"):
            raw["metadata"]["resourceVersion"] = "21"
        return raw

    with pytest.raises(BlockedError, match="snapshot"):
        adapter._list(changed, "kubernetes.role")


def test_kube_reject_write_and_wildcard_permissions():
    adapter = KubernetesAdapter(kube_endpoint(), KubeFake())
    for verbs, resources in [(["create"], ["pods"]), (["get"], ["*"])]:
        op = replace(kube_op(), desired={"rules": [{"apiGroups": [""], "resources": resources, "verbs": verbs}]})
        with pytest.raises(UnsupportedError):
            adapter.observe(op)


def test_kube_pinned_server_and_no_sdk_retries(monkeypatch):
    configuration = SimpleNamespace(verify_ssl=True, host="https://changed.example.invalid", retries=3)
    config = SimpleNamespace(list_kube_config_contexts=lambda: ([{"name": "isolated-test", "context": {"namespace": "reports", "cluster": "test-cluster"}}], None),
                             load_kube_config=lambda **kwargs: None)
    created = []

    def api_client(**kwargs):
        created.append(kwargs["configuration"])
        return kwargs["configuration"]

    client = SimpleNamespace(Configuration=lambda: configuration, ApiClient=api_client, RbacAuthorizationV1Api=lambda _: KubeFake())
    monkeypatch.setitem(sys.modules, "kubernetes", SimpleNamespace(client=client, config=config))
    with pytest.raises(BlockedError, match="pinned URL"):
        kube_module.from_environment(kube_endpoint())
    assert not created
    configuration.host = "https://cluster.example.invalid:6443"
    adapter = kube_module.from_environment(kube_endpoint())
    assert adapter.context_verified
    assert created[0].retries == 0


def test_kube_read_name_and_uid_checked():
    fake = KubeFake()
    adapter = KubernetesAdapter(kube_endpoint(), fake, context_verified=True)
    adapter.apply(kube_op(), observation())
    fake.roles["migration-namespace-read"]["metadata"]["name"] = "other-name"
    with pytest.raises(BlockedError, match="different object name"):
        adapter.observe(kube_op())
    fake.roles["migration-namespace-read"]["metadata"]["name"] = "migration-namespace-read"
    fake.roles["migration-namespace-read"]["metadata"]["uid"] = None
    with pytest.raises(BlockedError, match="immutable"):
        adapter.observe(kube_op())


def test_kube_pagination_has_finite_limit(monkeypatch):
    monkeypatch.setattr(kube_module, "MAX_PAGES", 2)
    adapter = KubernetesAdapter(kube_endpoint(), KubeFake())
    counter = 0

    def endless(**kwargs):
        nonlocal counter
        counter += 1
        return {"metadata": {"resourceVersion": "1", "continue": str(counter)}, "items": []}

    with pytest.raises(BlockedError, match="page limit"):
        adapter._list(endless, "kubernetes.role")
    assert counter == 2


def kc_endpoint(identity="group-1", **opts):
    return Endpoint("keycloak", "demo", identity, "/readers", {"url": "https://id.example.invalid", "version": "26.4.2", **opts})


def kc_op():
    return Operation("kc-test", "keycloak", "keycloak.group", "demo", "/readers", {"name": "readers", "attributes": {}}, observation())


class Response:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        return copy.deepcopy(self.body)


class KeycloakFake:
    def __init__(self, exists=True):
        self.groups = {"group-1": {"id": "group-1", "name": "readers", "path": "/readers", "attributes": {}}} if exists else {}
        self.calls, self.denied, self.lost, self.users = [], False, False, []

    def request(self, method, url, **kwargs):
        assert kwargs["follow_redirects"] is False
        self.calls.append((method, url, kwargs))
        if self.denied:
            return Response(403, {"secret": "SHOULD NEVER BE STORED"})
        path = url.removeprefix("https://id.example.invalid")
        params = kwargs.get("params", {})
        if path == "/admin/serverinfo":
            return Response(200, {"systemInfo": {"version": "26.4.2"}, "password": "SERVER SECRET"})
        if path == "/admin/realms/demo":
            return Response(200, {"realm": "demo", "id": "realm-1", "password": "REALM SECRET"})
        if path == "/admin/realms/demo/groups" and method == "POST":
            self.groups["group-1"] = {"id": "group-1", "path": "/readers", **kwargs["json"]}
            if self.lost:
                raise TimeoutError("SECRET response lost")
            return Response(201)
        if path == "/admin/realms/demo/groups":
            return Response(200, list(self.groups.values())[params.get("first", 0):params.get("first", 0) + params.get("max", 100)])
        if path.endswith("/children") or path.endswith("/members") or path.endswith("/roles"):
            return Response(200, [])
        if path.endswith("/users"):
            return Response(200, self.users[params.get("first", 0):params.get("first", 0) + params.get("max", 100)])
        if path.endswith("/clients"):
            return Response(200, [{"id": "client-1", "clientId": "example", "secret": "CLIENT SECRET"}])
        if path.startswith("/admin/realms/demo/groups/"):
            gid = path.rsplit("/", 1)[1]
            return Response(200, self.groups[gid]) if gid in self.groups else Response(404)
        return Response(404)


def test_keycloak_paging_and_secret_allowlist():
    fake = KeycloakFake()
    fake.users = [{"id": f"user-{i}", "username": f"u{i}", "credentials": [{"value": "SECRET"}]} for i in range(101)]
    inventory = KeycloakAdapter(kc_endpoint(), fake).collect()
    assert len(inventory["identities"]) == 102
    assert "SECRET" not in canonical(inventory)
    assert inventory["coverage"]["resource_policy"] == "complete"
    assert inventory["coverage"]["identity_policies"] == "unknown"
    assert any(call[2].get("params", {}).get("first") == 100 for call in fake.calls)


def test_keycloak_new_group_is_idempotent():
    fake = KeycloakFake(exists=False)
    adapter = KeycloakAdapter(kc_endpoint("new:/readers"), fake)
    after = adapter.apply(kc_op(), observation())
    assert adapter.satisfied(kc_op(), after)
    assert adapter.apply(kc_op(), after) == after
    assert sum(method == "POST" for method, _, _ in fake.calls) == 1


def test_keycloak_existing_group_is_never_modified():
    fake = KeycloakFake()
    adapter = KeycloakAdapter(kc_endpoint(), fake)
    current = adapter.observe(kc_op())
    changed = replace(kc_op(), desired={"name": "readers", "attributes": {"groups": ["admins"]}})
    with pytest.raises(ConflictError):
        adapter.apply(changed, current)
    assert not any(method == "POST" for method, _, _ in fake.calls)


def test_keycloak_immutable_id_and_version_checked():
    with pytest.raises(ConflictError):
        KeycloakAdapter(kc_endpoint("other-id"), KeycloakFake()).observe(kc_op())
    with pytest.raises(UnsupportedError):
        KeycloakAdapter(kc_endpoint(version="25.0.0"), KeycloakFake()).apply(kc_op(), observation())
    with pytest.raises(BlockedError, match="version"):
        KeycloakAdapter(kc_endpoint(version="26.4.3"), KeycloakFake()).apply(kc_op(), observation())


def test_keycloak_denied_collection_not_empty_success_and_error_redacted():
    fake = KeycloakFake()
    fake.denied = True
    inventory = KeycloakAdapter(kc_endpoint(), fake).collect()
    assert inventory["coverage"]["resource_policy"] == "denied"
    assert "SHOULD NEVER" not in canonical(inventory)


def test_keycloak_lost_response_reconciles_without_duplicate():
    fake = KeycloakFake(exists=False)
    fake.lost = True
    adapter = KeycloakAdapter(kc_endpoint("new:/readers"), fake)
    with pytest.raises(UnknownOutcome) as err:
        adapter.apply(kc_op(), observation())
    assert "SECRET" not in str(err.value)
    after = adapter.observe(kc_op())
    assert adapter.satisfied(kc_op(), after)


def test_keycloak_scope_and_https():
    with pytest.raises(ConfigError):
        KeycloakAdapter(kc_endpoint(url="http://id.example.invalid"), KeycloakFake())
    with pytest.raises(BlockedError):
        KeycloakAdapter(kc_endpoint(), KeycloakFake()).observe(replace(kc_op(), resource="/other"))


def test_keycloak_paging_ignored_is_blocked():
    fake = KeycloakFake()
    fake.users = [{"id": f"user-{i}"} for i in range(100)]
    request = fake.request

    def ignoring_offset(method, url, **kwargs):
        if url.endswith("/users"):
            kwargs["params"]["first"] = 0
        return request(method, url, **kwargs)

    fake.request = ignoring_offset
    inv = KeycloakAdapter(kc_endpoint(), fake).collect()
    assert inv["coverage"]["resource_policy"] == "failed"


def test_keycloak_page_limit(monkeypatch):
    monkeypatch.setattr(kc_module, "MAX_PAGES", 1)
    fake = KeycloakFake()
    fake.users = [{"id": f"user-{i}"} for i in range(100)]
    assert KeycloakAdapter(kc_endpoint(), fake).collect()["coverage"]["resource_policy"] == "failed"


def os_endpoint(**opts):
    return Endpoint("openstack", "project-1", "group-1", "project-1", {"domain_id": "domain-1", "role_id": "role-reader", **opts})


def os_op():
    return Operation("os-test", "openstack", "openstack.role_assignment", "project-1", "project-1", {"group_id": "group-1", "role_id": "role-reader", "project_id": "project-1"}, observation())


class OpenStackFake:
    def __init__(self):
        self.identity = self
        self.current_project_id = "project-1"
        self.assigned, self.lost, self.denied, self.domain = False, False, False, "domain-1"
        self.writes = 0

    def get_project(self, project):
        return {"id": project, "domain_id": self.domain, "is_enabled": True}

    def get_group(self, group):
        return {"id": group, "domain_id": self.domain, "name": "readers"}

    def get_role(self, role):
        return {"id": role, "name": "reader"}

    def validate_group_has_project_role(self, project, group, role, inherited):
        assert inherited is False
        return self.assigned

    def assign_project_role_to_group(self, project, group, role, inherited):
        assert (project, group, role, inherited) == ("project-1", "group-1", "role-reader", False)
        self.writes += 1
        self.assigned = True
        if self.lost:
            raise TimeoutError("SECRET write response lost")

    def role_assignments(self, **kwargs):
        assert kwargs == {"group_id": "group-1", "scope_project_id": "project-1"}
        if self.denied:
            raise APIError(403)
        # Generator exhaustion tests collection beyond the first result/page.
        for role in ("reader", "auditor"):
            yield {"group": {"id": "group-1"}, "scope": {"project": {"id": "project-1"}}, "role": {"id": role}}


def test_openstack_explicit_assignment_additive_idempotent():
    fake = OpenStackFake()
    adapter = OpenStackAdapter(os_endpoint(), fake)
    after = adapter.apply(os_op(), observation())
    assert adapter.satisfied(os_op(), after)
    assert adapter.apply(os_op(), after) == after
    assert fake.writes == 1


def test_openstack_project_and_domain_checked():
    fake = OpenStackFake()
    fake.current_project_id = "other-project"
    with pytest.raises(BlockedError, match="project"):
        OpenStackAdapter(os_endpoint(), fake).observe(os_op())
    fake.current_project_id, fake.domain = "project-1", "other-domain"
    with pytest.raises(BlockedError, match="domain"):
        OpenStackAdapter(os_endpoint(), fake).observe(os_op())


@pytest.mark.parametrize("desired", [
    {"group_id": "other", "role_id": "role-reader", "project_id": "project-1"},
    {"group_id": "group-1", "role_id": "admin", "project_id": "project-1"},
    {"group_id": "group-1", "role_id": "role-reader", "project_id": "other"},
])
def test_openstack_assignment_scope_escape(desired):
    with pytest.raises(BlockedError):
        OpenStackAdapter(os_endpoint(), OpenStackFake()).observe(replace(os_op(), desired=desired))


def test_openstack_paging_and_permission_denied():
    fake = OpenStackFake()
    adapter = OpenStackAdapter(os_endpoint(), fake)
    inventory = adapter.collect()
    assert len(inventory["relationships"]) == 2
    assert inventory["coverage"]["organization"] == "unknown"
    fake.denied = True
    inventory = adapter.collect()
    assert inventory["coverage"]["resource_policy"] == "denied"
    assert "SECRET" not in canonical(inventory)


def test_openstack_partial_generator_failure_is_not_complete():
    fake = OpenStackFake()

    def partial(**kwargs):
        yield {"group": {"id": "group-1"}, "scope": {"project": {"id": "project-1"}}, "role": {"id": "reader"}}
        raise APIError(403)

    fake.role_assignments = partial
    inv = OpenStackAdapter(os_endpoint(), fake).collect()
    assert inv["coverage"]["resource_policy"] == "denied"
    assert "SECRET" not in canonical(inv)


def test_openstack_record_limit_and_returned_project(monkeypatch):
    monkeypatch.setattr(os_module, "MAX_ASSIGNMENTS", 1)
    fake = OpenStackFake()
    adapter = OpenStackAdapter(os_endpoint(), fake)
    assert adapter.collect()["coverage"]["resource_policy"] == "failed"
    fake.role_assignments = lambda **kwargs: iter([{"group": {"id": "group-1"}, "scope": {"project": {"id": "other"}}, "role": {"id": "reader"}}])
    assert adapter.collect()["coverage"]["resource_policy"] == "failed"


def test_openstack_lost_response_reobserve_and_unknown_federation_kind():
    fake = OpenStackFake()
    fake.lost = True
    adapter = OpenStackAdapter(os_endpoint(), fake)
    with pytest.raises(UnknownOutcome) as err:
        adapter.apply(os_op(), observation())
    assert "SECRET" not in str(err.value)
    assert adapter.satisfied(os_op(), adapter.observe(os_op()))
    with pytest.raises(UnsupportedError):
        adapter.observe(replace(os_op(), kind="openstack.federation_mapping"))


def test_real_kubernetes_sdk_serialization_without_network():
    client = pytest.importorskip("kubernetes.client")
    fake = KubeFake()
    role = client.V1Role(metadata=client.V1ObjectMeta(name="migration-namespace-read", namespace="reports", uid="sdk-uid", resource_version="12"),
                         rules=[client.V1PolicyRule(api_groups=[""], resources=["pods"], verbs=["get", "list", "watch"])])
    fake.read_namespaced_role = lambda **kwargs: role
    adapter = KubernetesAdapter(kube_endpoint(), fake)
    observed = adapter.observe(kube_op())
    assert observed["version"] == "12"
    assert adapter.satisfied(kube_op(), observed)
    pages = client.V1RoleList(items=[role], metadata=client.V1ListMeta(resource_version="12"))
    assert len(adapter._list(lambda **kwargs: pages, "kubernetes.role")) == 1
    binding = client.V1RoleBinding(metadata=client.V1ObjectMeta(name="migration-namespace-read", namespace="reports", uid="sdk-binding", resource_version="13"),
                                   role_ref=client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name="migration-namespace-read"),
                                   subjects=[client.RbacV1Subject(api_group="rbac.authorization.k8s.io", kind="Group", name="oidc:readers")])
    fake.read_namespaced_role_binding = lambda **kwargs: binding
    assert adapter.satisfied(kube_op("kubernetes.role_binding"), adapter.observe(kube_op("kubernetes.role_binding")))


def test_real_openstack_sdk_resources_without_network():
    pytest.importorskip("openstack")
    from openstack.identity.v3.group import Group
    from openstack.identity.v3.project import Project
    from openstack.identity.v3.role import Role
    from openstack.identity.v3.role_assignment import RoleAssignment
    from openstack.identity.v3._proxy import Proxy
    import inspect

    fake = OpenStackFake()
    fake.get_project = lambda project: Project.existing(id=project, domain_id="domain-1", is_enabled=True)
    fake.get_group = lambda group: Group.existing(id=group, domain_id="domain-1", name="readers")
    fake.get_role = lambda role: Role.existing(id=role, name="reader")
    fake.role_assignments = lambda **kwargs: iter([RoleAssignment.existing(group={"id": "group-1"}, scope={"project": {"id": "project-1"}}, role={"id": "role-reader"})])
    adapter = OpenStackAdapter(os_endpoint(), fake)
    assert adapter.collect()["coverage"]["resource_policy"] == "complete"
    assert adapter.apply(os_op(), observation())["exists"]
    # Verify the installed SDK accepts our exact additive, non-inherited call.
    for method in (Proxy.assign_project_role_to_group, Proxy.validate_group_has_project_role):
        inspect.signature(method).bind(None, "project-1", "group-1", "role-reader", inherited=False)


def test_real_httpx_transport_no_redirect_and_no_secret_artifact():
    httpx = pytest.importorskip("httpx")
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer secret-only-in-request"
        return httpx.Response(302, headers={"Location": "https://unapproved.example.invalid"})

    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer secret-only-in-request"})
    inv = KeycloakAdapter(kc_endpoint(), client).collect()
    assert len(seen) == 1
    assert inv["coverage"]["resource_policy"] == "failed"
    assert "secret-only-in-request" not in canonical(inv)
