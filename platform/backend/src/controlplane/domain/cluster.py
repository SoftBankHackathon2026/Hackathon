from dataclasses import asdict

from controlplane.domain.fleet import ClusterFacts, Resources
from controlplane.schemas.migrations import ClusterView, NodeView, ResourcesView


def _total(values: list[Resources | None]) -> ResourcesView | None:
    if not values or any(value is None for value in values):
        return None
    return ResourcesView(
        cpu_millicores=sum(value.cpu_millicores for value in values),
        memory_bytes=sum(value.memory_bytes for value in values),
    )


def cluster_view(cluster: ClusterFacts) -> ClusterView | None:
    if cluster.nodes is None and cluster.version is None:
        return None
    nodes = cluster.nodes or []
    readiness = [node.ready for node in nodes]
    return ClusterView(
        version=cluster.version,
        node_count=None if cluster.nodes is None else len(nodes),
        ready_nodes=None if None in readiness else sum(readiness),
        allocatable=_total([node.allocatable for node in nodes]),
        requests=_total([node.requests for node in nodes]),
        usage=_total([node.usage for node in nodes]),
        nodes=[NodeView(**asdict(node)) for node in nodes],
    )
