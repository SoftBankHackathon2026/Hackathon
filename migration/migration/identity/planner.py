"""Deterministic plan generation: incomplete evidence remains a blocker."""
from . import RULE_VERSION, SCHEMA_VERSION
from .adapters.base import observation_key
from .models import BlockedError, Inventory, MigrationSpec, Operation, Plan, digest, primitive, ordered_evidence
from .normalize import normalize
from .rules import rule_data
from .rules.aws_gcp import aws_permissions, ec2_trust, gcp_permissions
from .rules.onprem import namespace_read

COVERAGE = {"identity", "identity_policies", "resource_policy", "boundary", "organization", "session", "resource_constraints"}


def make_plan(spec: MigrationSpec, inventory: Inventory) -> Plan:
    inventory = Inventory.from_dict(ordered_evidence(primitive(inventory)))
    blockers, warnings, operations = [], [], []
    if inventory.spec_digest != digest(spec):
        blockers.append("inventory is bound to a different specification")
    for label, endpoint, side in (("source", spec.source, inventory.source), ("target", spec.target, inventory.target)):
        if side["endpoint"] != primitive(endpoint):
            blockers.append(f"{label} inventory endpoint mismatch")
        for key in sorted(COVERAGE):
            if side["coverage"].get(key) not in {"complete", "not_applicable"}:
                blockers.append(f"{label} coverage {key}: {side['coverage'].get(key, 'missing')}")
        if label == "source" and (not side["identities"] or any(not (x.get("id") or x.get("stable_id")) for x in side["identities"])):
            blockers.append("source immutable identity is missing")
        if any(x.get("provider", endpoint.provider) != endpoint.provider or x.get("scope", endpoint.scope) != endpoint.scope for x in side["identities"]):
            blockers.append(f"{label} collected identity crosses provider or scope")
        if label == "source" and not any(endpoint.identity in {x.get("id"), x.get("stable_id"), x.get("name"), x.get("arn")} for x in side["identities"]):
            blockers.append("source identity evidence does not identify the declared principal")
        if any(not isinstance(p, dict) for p in side["policies"] + side["trust"] + side["identities"]):
            blockers.append(f"{label} malformed policy or identity")
    if rule_data()["version"] != RULE_VERSION:
        blockers.append("installed rule version mismatch")
    if any(set(ep.options).intersection({"pool_id", "provider_id", "federation_mode", "attribute_mapping", "attribute_condition"}) for ep in (spec.source,spec.target)):
        blockers.append("federation configuration and direct/impersonation paths are unsupported")
    if inventory.target["policies"]:
        warnings.append("preexisting target access is preserved and may be broader; inspect target policy evidence separately from the proposed grant")

    def add(kind, resource, desired, dependencies=()):
        operation_id = f"{len(operations)+1:02d}-{kind}"
        expected = inventory.target["observations"].get(observation_key(kind, resource))
        if expected is None:
            blockers.append(f"target observation missing: {kind}|{resource}")
        if kind not in spec.allowed_changes:
            blockers.append(f"operation is outside allowed_changes: {kind}")
        operations.append(Operation(operation_id, spec.target.provider, kind, spec.target.scope, resource, desired, expected, list(dependencies)))
        return operation_id

    try:
        pair = (spec.source.provider, spec.target.provider, spec.direction, spec.workload)
        if pair == ("aws", "gcp", "aws-to-gcp", "object-read"):
            aws_permissions(inventory.source["policies"], spec.source.resource)
            if not inventory.source["trust"] or any(not ec2_trust(t) for t in inventory.source["trust"]):
                raise BlockedError("source trust must be a collected, exact EC2 execution trust")
            role_id = spec.target.options.get("role_id", "MigrationObjectRead")
            name = f"projects/{spec.target.scope}/roles/{role_id}"
            account_id = spec.target.identity.split("@")[0]
            if spec.target.identity != f"{account_id}@{spec.target.scope}.iam.gserviceaccount.com":
                raise BlockedError("target service account does not belong to target project")
            identity_op = add("gcp.service_account", spec.target.identity, {"account_id": account_id})
            role_op = add("gcp.custom_role", name, {"included_permissions": sorted(rule_data()["aws_to_gcp"].values())})
            add("gcp.bucket_binding", spec.target.resource, {"role": name, "member": f"serviceAccount:{spec.target.identity}"}, [identity_op, role_op])
            warnings.append("Compute Engine execution identity attachment and application authentication changes require separate deployment and live verification")
        elif pair == ("gcp", "aws", "gcp-to-aws", "object-read"):
            gcp_permissions(inventory.source["policies"], spec.source.identity, spec.source.resource)
            trust = spec.target.options.get("trust_policy")
            if not isinstance(trust, dict) or not ec2_trust(trust):
                raise BlockedError("target exact EC2 trust policy must be explicitly supplied")
            role_op = add("aws.role", spec.target.identity, {"trust_policy": trust})
            bucket = f"arn:aws:s3:::{spec.target.resource}"
            policy = {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": bucket},
                {"Effect": "Allow", "Action": "s3:GetObject", "Resource": bucket + "/*"}]}
            add("aws.inline_policy", spec.target.identity, {"policy_name": spec.target.options.get("policy_name", "MigrationObjectRead"), "policy": policy}, [role_op])
            warnings.append("instance profile attachment, PassRole and execution deployment are separate; AWS APIs lack compare-and-swap for these changes")
        elif pair == ("keycloak", "kubernetes", "keycloak-to-kubernetes", "namespace-read"):
            rules = namespace_read(inventory.source["policies"])
            if spec.target.options.get("group_claim_confirmed") is not True:
                raise BlockedError("actual login group claim and cluster prefix are not confirmed")
            namespace = spec.target.scope
            if spec.target.resource != namespace:
                raise BlockedError("target resource must equal the permitted namespace")
            role_name = spec.target.options.get("role_name", "migration-namespace-read")
            binding_name = spec.target.options.get("role_binding_name", "migration-namespace-read")
            role_op = add("kubernetes.role", f"{namespace}/{role_name}", {"rules": rules})
            add("kubernetes.role_binding", f"{namespace}/{binding_name}", {"role_name": role_name, "group": spec.target.identity}, [role_op])
            warnings.append("actual user login, nested permissions and group removal timing require live verification")
        else:
            raise BlockedError("unsupported direction/workload: federation direct and impersonation, generic onprem/cloud and OpenStack translation require product-specific rules")
    except BlockedError as exc:
        blockers.append(str(exc))
        operations = []
    except (TypeError, KeyError, ValueError, AttributeError):
        blockers.append("malformed or unsupported source policy structure")
        operations = []
    graph = normalize(inventory)
    graph["access_changes"] = {"added": [{"kind": op.kind, "resource": op.resource, "desired": op.desired} for op in operations],
                               "removed": [], "unknown": sorted(set(blockers)),
                               "preexisting_target_policy_evidence": inventory.target["policies"]}
    for op in operations:
        graph["edges"].append({"kind": "resource_role_binding" if "binding" in op.kind else "identity_creation" if op.kind.endswith(("service_account", ".role")) else "permission_definition", "operation": op.id, "resource": op.resource, "desired": op.desired})
    graph["edges"].append({"kind": "execution_identity_attachment", "status": "external_prerequisite", "identity": spec.target.identity})
    return Plan(SCHEMA_VERSION, RULE_VERSION, spec, inventory, operations, sorted(set(blockers)), sorted(set(warnings)), graph).sealed()
