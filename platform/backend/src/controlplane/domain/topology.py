from collections import Counter
from dataclasses import dataclass, field

from controlplane.domain.fleet import (
    ClusterFacts,
    ComponentConfig,
    EnvironmentSnapshot,
    InventoryItem,
)
from controlplane.schemas.migrations import AppState, ComponentView

APP_HEALTH_STATUS = {
    "Healthy": "healthy",
    "Progressing": "degraded",
    "Suspended": "degraded",
    "Degraded": "down",
    "Missing": "down",
}
SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "local-path-storage"}
SYSTEM_PREFIXES = ("gke-", "gmp-", "kube-")
STORE_ROLE_SUFFIXES = ("-broker", "-controller", "-primary", "-replica", "-server")


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    layer: str
    kind: str
    prefixes: tuple[str, ...] = ()
    part_of: tuple[str, ...] = ()
    ingress_classes: tuple[str, ...] = ()

    def matches(self, item: InventoryItem) -> bool:
        if any(item.name.startswith(prefix) for prefix in self.prefixes):
            return True
        return item.labels.get("app.kubernetes.io/part-of") in self.part_of


PRODUCTS = (
    Product("cloudflared", "Cloudflare Tunnel", "entry", "cloudflared", ("cloudflared",)),
    Product("traefik", "Traefik Ingress", "entry", "ingress", ("traefik",), (), ("traefik",)),
    Product(
        "aws-lbc",
        "AWS Load Balancer Controller",
        "entry",
        "ingress",
        ("aws-load-balancer-controller",),
        (),
        ("alb",),
    ),
    Product(
        "ingress-nginx", "ingress-nginx", "entry", "ingress", ("ingress-nginx",), (), ("nginx",)
    ),
    Product("argocd", "Argo CD", "platform", "gitops", ("argocd-",), ("argocd",)),
    Product("argo-rollouts", "Argo Rollouts", "platform", "rollout", ("argo-rollouts",)),
    Product(
        "external-secrets",
        "External Secrets Operator",
        "platform",
        "secrets",
        ("external-secrets",),
    ),
    Product("cert-manager", "cert-manager", "platform", "certificates", ("cert-manager",)),
    Product(
        "prometheus",
        "Prometheus·Grafana",
        "platform",
        "monitoring",
        ("prometheus-", "monitoring-", "grafana"),
        ("kube-prometheus-stack", "kube-state-metrics", "prometheus-node-exporter", "grafana"),
    ),
    Product("harbor", "Harbor", "platform", "registry", ("harbor-",), ("harbor",)),
    Product("oneport", "Oneport", "platform", "platform", ("oneport-",), ("oneport-platform",)),
)
PRODUCT_IDS = {product.id for product in PRODUCTS}
CLASSIFY = (
    (("debezium", "kafka-connect", "-connect"), "migration", "migration"),
    (("openbao", "vault"), "platform", "secrets"),
    (("postgres", "postgresql"), "data", "postgres"),
    (("kafka",), "data", "kafka"),
    (("seaweedfs", "minio", "object-storage"), "data", "object_storage"),
    (("qdrant",), "data", "vector_db"),
    (("redis", "valkey"), "data", "cache"),
)


def _is_system(namespace: str) -> bool:
    return namespace in SYSTEM_NAMESPACES or namespace.startswith(SYSTEM_PREFIXES)


def _store_name(name: str) -> str:
    for suffix in STORE_ROLE_SUFFIXES:
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def _classify(item: InventoryItem) -> tuple[str, str]:
    if item.labels.get("app.kubernetes.io/part-of") == "migration":
        return "migration", "migration"
    images = [image.rsplit("/", 1)[-1] for image in item.images]
    haystack = " ".join([item.name, *images]).lower()
    for tokens, layer, kind in CLASSIFY:
        if any(token in haystack for token in tokens):
            return layer, kind
    return "app", "app"


def identify(item: InventoryItem) -> Product:
    for product in PRODUCTS:
        if product.matches(item):
            return product
    layer, kind = _classify(item)
    name = _store_name(item.name) if layer == "data" else item.name
    return Product(name, name, layer, kind)


@dataclass
class _Group:
    product: Product
    namespace: str
    id: str
    items: list[InventoryItem] = field(default_factory=list)


def _groups(inventory: list[InventoryItem]) -> list[_Group]:
    groups: dict[tuple[str, str], _Group] = {}
    for item in inventory:
        product = identify(item)
        if _is_system(item.namespace) and product.id not in PRODUCT_IDS:
            continue
        key = (product.id, item.namespace)
        groups.setdefault(key, _Group(product, item.namespace, product.id)).items.append(item)
    taken = Counter(product_id for product_id, _ in groups)
    for (product_id, namespace), group in groups.items():
        if taken[product_id] > 1:
            group.id = f"{product_id}-{namespace}"
    return list(groups.values())


def _group_status(items: list[InventoryItem]) -> tuple[str, str]:
    ready = sum(min(item.ready, item.desired) for item in items)
    desired = sum(item.desired for item in items)
    count = f"워크로드 {len(items)}개 · " if len(items) > 1 else ""
    if desired <= 0:
        return "idle", f"{count}복제본 0개로 줄여 둠"
    detail = f"{count}준비 {ready}/{desired}"
    lagging = [item.name for item in items if item.ready < item.desired]
    if not lagging:
        return "healthy", detail
    if ready == 0:
        return "down", detail
    return "degraded", f"{detail} · 준비 안 됨: {', '.join(lagging[:3])}"


@dataclass(frozen=True)
class _Links:
    owner: dict[str, str]
    entry_by_class: dict[str, str]
    sole_entry: str | None

    @classmethod
    def of(cls, groups: list[_Group]) -> "_Links":
        present = {group.id for group in groups}
        entries = [g.id for g in groups if g.product.kind == "ingress"]
        return cls(
            owner={item.key: group.id for group in groups for item in group.items},
            entry_by_class={
                name: p.id for p in PRODUCTS for name in p.ingress_classes if p.id in present
            },
            sole_entry=entries[0] if len(entries) == 1 else None,
        )

    def depends_on(self, group: _Group) -> list[str]:
        found: set[str] = set()
        for item in group.items:
            found.update(self.owner[key] for key in item.uses if key in self.owner)
            found.update(item.uses_external)
            for ingress_class in item.ingress_classes:
                entry = self.entry_by_class.get(ingress_class) or self.sole_entry
                if entry:
                    found.add(entry)
        found.discard(group.id)
        return sorted(found)


def discovered_components(
    inventory: list[InventoryItem], cluster_path: list[str]
) -> list[ComponentView]:
    groups = _groups(inventory)
    links = _Links.of(groups)
    views = []
    for group in groups:
        status, detail = _group_status(group.items)
        views.append(
            ComponentView(
                id=group.id,
                name=group.product.name,
                layer=group.product.layer,
                kind=group.product.kind,
                managed=False,
                status=status,
                detail=detail,
                depends_on=links.depends_on(group),
                placement=[*cluster_path, f"ns {group.namespace}"],
            )
        )
    return views


def _workload_status(workload: str, cluster: ClusterFacts) -> tuple[str, str]:
    if not cluster.connected:
        return "unknown", "클러스터 미연결"
    if workload not in cluster.workload_ready:
        return "unknown", "상태를 읽지 못함"
    ready, desired = cluster.workload_ready[workload]
    if desired <= 0:
        return "down", "워크로드 없음"
    if ready >= desired:
        return "healthy", f"준비 {ready}/{desired}"
    return ("down" if ready == 0 else "degraded"), f"준비 {ready}/{desired}"


def _app_status(name: str, apps: list[AppState]) -> tuple[str, str]:
    app = next((a for a in apps if a.name == name), None)
    if app is None:
        return "unknown", "Argo CD 앱을 찾지 못함"
    return APP_HEALTH_STATUS.get(app.health, "unknown"), f"{app.sync} · {app.health}"


def component_view(component: ComponentConfig, cluster: ClusterFacts) -> ComponentView:
    if component.workload:
        status, detail = _workload_status(component.workload, cluster)
    elif component.argo_app:
        status, detail = _app_status(component.argo_app, cluster.apps)
    else:
        status, detail = "unknown", "관리형 서비스 상태는 아직 수집하지 않음"
    if component.note:
        detail = f"{detail} · {component.note}"
    return ComponentView(
        id=component.id,
        name=component.name,
        layer=component.layer,
        kind=component.kind,
        managed=component.managed,
        status=status,
        detail=detail,
        depends_on=component.depends_on,
        placement=component.placement,
    )


def _external_components(snapshot: EnvironmentSnapshot, taken: set[str]) -> list[ComponentView]:
    return [
        component_view(c, snapshot.cluster)
        for c in snapshot.config.components
        if not (c.workload or c.argo_app) and c.id not in taken
    ]


def environment_components(snapshot: EnvironmentSnapshot) -> list[ComponentView]:
    config, cluster = snapshot.config, snapshot.cluster
    if cluster.inventory is None:
        return [component_view(c, cluster) for c in config.components]
    found = discovered_components(cluster.inventory, config.cluster_path)
    views = [*found, *_external_components(snapshot, {view.id for view in found})]
    ids = {view.id for view in views}
    return [
        view.model_copy(update={"depends_on": [d for d in view.depends_on if d in ids]})
        for view in views
    ]
