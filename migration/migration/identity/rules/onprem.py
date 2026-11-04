"""A group is not itself evidence of container or cloud permissions."""
from ..models import BlockedError


def namespace_read(policies: list[dict]) -> list[dict]:
    expected = [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}]
    if len(policies) != 1 or policies[0].get("kind") != "kubernetes.rules" or policies[0].get("rules") != expected:
        raise BlockedError("explicit source pod-read entitlement evidence is required; Keycloak group membership is insufficient")
    return expected
