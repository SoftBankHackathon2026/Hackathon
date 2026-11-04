"""Narrow object-read rules. These never substitute a broader predefined role."""
from ..models import BlockedError


def sequence(value):
    return value if isinstance(value, list) else [value]


def aws_permissions(policies: list[dict], bucket: str) -> set[str]:
    permissions = set()
    if not policies:
        raise BlockedError("source identity has no collected permission policies")
    for raw in policies:
        doc = raw.get("document", raw.get("PolicyDocument", raw))
        if not isinstance(doc, dict) or set(doc) - {"Version", "Statement", "Id"} or "Statement" not in doc:
            raise BlockedError("unsupported AWS policy document")
        for statement in sequence(doc["Statement"]):
            if not isinstance(statement, dict) or set(statement) - {"Sid", "Effect", "Action", "Resource"}:
                raise BlockedError("AWS condition, exclusion, principal or unknown policy field requires alternative design")
            if statement.get("Effect") != "Allow":
                raise BlockedError("AWS deny or unknown effect cannot be translated")
            actions, resources = sequence(statement.get("Action")), sequence(statement.get("Resource"))
            if not actions or not resources or any(not isinstance(a, str) for a in actions + resources):
                raise BlockedError("invalid AWS actions or resources")
            for action in actions:
                expected = {"s3:ListBucket": f"arn:aws:s3:::{bucket}", "s3:GetObject": f"arn:aws:s3:::{bucket}/*"}.get(action)
                if expected is None:
                    raise BlockedError("unsupported AWS action or wildcard")
                # AWS allows the common two-actions/two-resources statement. Each
                # action must have its exact applicable ARN and no other scope.
                allowed = {f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"}
                if expected not in resources or not set(resources) <= allowed:
                    raise BlockedError("unsupported AWS object prefix or resource scope")
                permissions.add(action)
    if permissions != {"s3:ListBucket", "s3:GetObject"}:
        raise BlockedError("both exact bucket listing and all-object reads are required")
    return permissions


def ec2_trust(trust: dict) -> bool:
    if set(trust) - {"Version", "Statement"}:
        return False
    statements = sequence(trust.get("Statement"))
    return len(statements) == 1 and isinstance(statements[0], dict) and set(statements[0]) <= {"Sid", "Effect", "Principal", "Action"} and statements[0].get("Effect") == "Allow" and statements[0].get("Principal") == {"Service": "ec2.amazonaws.com"} and sequence(statements[0].get("Action")) == ["sts:AssumeRole"]


def gcp_permissions(policies: list[dict], identity: str, bucket: str) -> set[str]:
    definitions = {p.get("name"): p.get("included_permissions") for p in policies if p.get("kind") == "gcp.role"}
    result = set()
    found = False
    for item in policies:
        if item.get("kind") == "gcp.role":
            continue
        if item.get("kind") != "gcp.allow_policy":
            raise BlockedError("unknown GCP policy kind")
        policy = item.get("policy", {})
        if set(policy) - {"bindings", "etag", "version", "kind", "resourceId"}:
            raise BlockedError("unknown GCP policy field")
        if policy.get("kind", "storage#policy") != "storage#policy" or policy.get("resourceId", f"projects/_/buckets/{bucket}") != f"projects/_/buckets/{bucket}":
            raise BlockedError("GCP storage policy metadata mismatch")
        for binding in policy.get("bindings", []):
            members = binding.get("members", [])
            if not isinstance(members, list) or any(not isinstance(m, str) for m in members):
                raise BlockedError("invalid GCP binding members")
            if any(m.startswith(("group:", "domain:", "principalSet:", "principal:")) for m in members):
                raise BlockedError("GCP indirect group or federation applicability is unresolved")
            if f"serviceAccount:{identity}" not in members and not {"allUsers", "allAuthenticatedUsers"}.intersection(members):
                continue
            if set(binding) != {"role", "members"}:
                raise BlockedError("GCP conditions cannot be translated")
            if item.get("resource") not in {bucket, f"buckets/{bucket}", f"//storage.googleapis.com/projects/_/buckets/{bucket}"}:
                raise BlockedError("source GCP inherited or broader binding needs alternative design")
            permissions = definitions.get(binding["role"])
            if not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions):
                raise BlockedError("GCP role definition is missing")
            found = True
            result.update(permissions)
    if not found or result != {"storage.objects.get", "storage.objects.list"}:
        raise BlockedError("GCP source must have only the two supported object-read permissions")
    return result
