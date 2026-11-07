"""Read-only, paginated workforce inventory with account-specific policy documents.

This is an assignment inventory, NOT an effective AWS authorization evaluator.
Organizations/resource/session policies and workload identities are explicit gaps.
"""

from __future__ import annotations

import json
from urllib.parse import unquote

from .common import IdentityError, digest, now, require


def pages(client, operation, field, **kwargs):
    token = None
    seen = set()
    while True:
        response = getattr(client, operation)(
            **kwargs, **({"NextToken": token} if token else {})
        )
        require(isinstance(response.get(field), list), "AWS_RESPONSE_SHAPE")
        yield from response[field]
        token = response.get("NextToken")
        if not token:
            break
        require(token not in seen, "AWS_PAGINATION_LOOP")
        seen.add(token)


def policy_document(value):
    if isinstance(value, str):
        value = json.loads(unquote(value))
    require(isinstance(value, dict), "POLICY_DOCUMENT_SHAPE")
    return value


def managed_document(client, arn):
    policy = client.get_policy(PolicyArn=arn)["Policy"]
    doc = client.get_policy_version(PolicyArn=arn, VersionId=policy["DefaultVersionId"])
    return {
        "arn": arn,
        "version": policy["DefaultVersionId"],
        "document": policy_document(doc["PolicyVersion"]["Document"]),
    }


def collect(config, session_factory=None):
    if session_factory is None:
        try:
            from boto3 import Session
        except ImportError:
            raise IdentityError("INSTALL_IDENTITY_AWS_DEPENDENCIES") from None
        session_factory = Session
    source = config["source"]
    session = session_factory(
        profile_name=source.get("profile"), region_name=source["region"]
    )
    try:
        from botocore.config import Config

        options = {
            "config": Config(
                connect_timeout=10,
                read_timeout=30,
                retries={"mode": "standard", "max_attempts": 3},
            )
        }
    except ImportError:
        options = {}
    sso = session.client("sso-admin", **options)
    store = session.client("identitystore", **options)
    instance = source["instance_arn"]
    store_id = source["identity_store_id"]
    inv = {
        "schema_version": 1,
        "kind": "identity_inventory",
        "collected_at": now(),
        "source": {
            k: source[k]
            for k in (
                "provider",
                "region",
                "instance_arn",
                "identity_store_id",
                "account_ids",
            )
        },
        "users": [],
        "groups": [],
        "memberships": [],
        "permission_sets": [],
        "assignments": [],
        "policies": [],
        "iam_principals": [],
        "iam_groups": [],
        "coverage": [],
        "limitations": [
            "AWS_EFFECTIVE_PERMISSIONS_NOT_EVALUATED",
            "RESOURCE_AND_ORGANIZATION_CONTROLS_NOT_COLLECTED",
            "WORKLOAD_IDENTITIES_REQUIRE_SEPARATE_ADAPTER",
            "PASSWORDS_AND_MFA_NOT_EXPORTED",
        ],
    }

    def capture(scope, fn):
        try:
            result = fn()
            inv["coverage"].append({"scope": scope, "status": "complete"})
            return result
        except Exception:  # noqa: BLE001 -- SDK errors must not expose raw request/credential text.
            # Never serialize SDK exception text, request IDs, tokens or credentials.
            inv["coverage"].append(
                {"scope": scope, "status": "unknown", "reason": "SOURCE_READ_FAILED"}
            )
            return None

    instances = capture(
        "instance", lambda: list(pages(sso, "list_instances", "Instances"))
    )
    require(
        instances is not None
        and any(
            i["InstanceArn"] == instance and i["IdentityStoreId"] == store_id
            for i in instances
        ),
        "IDENTITY_STORE_INSTANCE_MISMATCH",
    )
    users = capture(
        "users",
        lambda: list(pages(store, "list_users", "Users", IdentityStoreId=store_id)),
    )
    for u in users or []:
        inv["users"].append(
            {
                "id": u["UserId"],
                "username": u["UserName"],
                "display_name": u.get("DisplayName", u["UserName"]),
                "emails": u.get("Emails", []),
            }
        )
    groups = capture(
        "groups",
        lambda: list(pages(store, "list_groups", "Groups", IdentityStoreId=store_id)),
    )
    for g in groups or []:
        inv["groups"].append({"id": g["GroupId"], "name": g["DisplayName"]})
        members = capture(
            "memberships:" + g["GroupId"],
            lambda g=g: list(
                pages(
                    store,
                    "list_group_memberships",
                    "GroupMemberships",
                    IdentityStoreId=store_id,
                    GroupId=g["GroupId"],
                )
            ),
        )
        inv["memberships"].extend(
            {"group_id": g["GroupId"], "user_id": m["MemberId"]["UserId"]}
            for m in members or []
        )

    for account in source["account_ids"]:

        def iam_inventory(account=account):
            profile = source.get("account_profiles", {}).get(
                account, source.get("profile")
            )
            scoped = session_factory(profile_name=profile, region_name=source["region"])
            require(
                scoped.client("sts", **options).get_caller_identity()["Account"]
                == account,
                "ACCOUNT_CREDENTIAL_MISMATCH",
            )
            iam = scoped.client("iam", **options)
            principals, iam_groups, marker, seen = [], [], None, set()
            while True:
                response = iam.get_account_authorization_details(
                    **({"Marker": marker} if marker else {})
                )
                for field, kind in (
                    ("UserDetailList", "iam_user"),
                    ("RoleDetailList", "iam_role"),
                ):
                    for item in response.get(field, []):
                        principals.append(
                            {
                                "kind": kind,
                                "account_id": account,
                                "arn": item["Arn"],
                                "id": item[
                                    "UserId" if kind == "iam_user" else "RoleId"
                                ],
                                "name": item[
                                    "UserName" if kind == "iam_user" else "RoleName"
                                ],
                                "inline_policies": item.get(
                                    "UserPolicyList"
                                    if kind == "iam_user"
                                    else "RolePolicyList",
                                    [],
                                ),
                                "managed_policies": item.get(
                                    "AttachedManagedPolicies", []
                                ),
                                "trust_policy": item.get("AssumeRolePolicyDocument"),
                                "permissions_boundary": item.get("PermissionsBoundary"),
                                "groups": item.get("GroupList", []),
                                "migration_status": "requires_separate_workload_or_iam_user_mapping",
                            }
                        )
                for item in response.get("GroupDetailList", []):
                    iam_groups.append(
                        {
                            "account_id": account,
                            "arn": item["Arn"],
                            "id": item["GroupId"],
                            "name": item["GroupName"],
                            "inline_policies": item.get("GroupPolicyList", []),
                            "managed_policies": item.get("AttachedManagedPolicies", []),
                        }
                    )
                if not response.get("IsTruncated"):
                    break
                marker = response.get("Marker")
                require(marker and marker not in seen, "IAM_PAGINATION_LOOP")
                seen.add(marker)
            return principals, iam_groups

        account_inventory = capture("iam_principals:" + account, iam_inventory)
        if account_inventory is not None:
            inv["iam_principals"].extend(account_inventory[0])
            inv["iam_groups"].extend(account_inventory[1])
    arns = capture(
        "permission_sets",
        lambda: list(
            pages(sso, "list_permission_sets", "PermissionSets", InstanceArn=instance)
        ),
    )
    for arn in arns or []:
        args = {"InstanceArn": instance, "PermissionSetArn": arn}

        def details(args=args, arn=arn):
            p = sso.describe_permission_set(**args)["PermissionSet"]
            inline = sso.get_inline_policy_for_permission_set(**args).get(
                "InlinePolicy"
            )
            aws = list(
                pages(
                    sso,
                    "list_managed_policies_in_permission_set",
                    "AttachedManagedPolicies",
                    **args,
                )
            )
            customer = list(
                pages(
                    sso,
                    "list_customer_managed_policy_references_in_permission_set",
                    "CustomerManagedPolicyReferences",
                    **args,
                )
            )
            try:
                boundary = sso.get_permissions_boundary_for_permission_set(**args).get(
                    "PermissionsBoundary"
                )
            except Exception as error:
                if (
                    getattr(error, "response", {}).get("Error", {}).get("Code")
                    != "ResourceNotFoundException"
                ):
                    raise
                boundary = None
            return {
                "arn": arn,
                "name": p["Name"],
                "session_duration": p.get("SessionDuration"),
                "inline": policy_document(inline) if inline else None,
                "aws_managed": [m["Arn"] for m in aws],
                "customer_managed": customer,
                "boundary": boundary,
            }

        p = capture("permission_set:" + arn, details)
        if p is None:
            continue
        inv["permission_sets"].append(p)
        for account in source["account_ids"]:
            assignments = capture(
                "assignments:" + account + ":" + arn,
                lambda account=account, args=args: list(
                    pages(
                        sso,
                        "list_account_assignments",
                        "AccountAssignments",
                        AccountId=account,
                        **args,
                    )
                ),
            )
            inv["assignments"].extend(
                {
                    "account_id": account,
                    "permission_set_arn": arn,
                    "principal_type": a["PrincipalType"],
                    "principal_id": a["PrincipalId"],
                }
                for a in assignments or []
            )
            if not assignments:
                continue

            def documents(account=account, p=p, arn=arn):
                profile = source.get("account_profiles", {}).get(
                    account, source.get("profile")
                )
                account_session = session_factory(
                    profile_name=profile, region_name=source["region"]
                )
                require(
                    account_session.client("sts", **options).get_caller_identity()[
                        "Account"
                    ]
                    == account,
                    "ACCOUNT_CREDENTIAL_MISMATCH",
                )
                iam = account_session.client("iam", **options)
                docs = [managed_document(iam, a) for a in p["aws_managed"]]
                partition = instance.split(":")[1]
                for ref in p["customer_managed"]:
                    policy_arn = f"arn:{partition}:iam::{account}:policy{ref.get('Path', '/')}{ref['Name']}"
                    docs.append(managed_document(iam, policy_arn))
                if p["inline"]:
                    docs.append(
                        {
                            "arn": "inline:" + arn,
                            "version": "inline",
                            "document": p["inline"],
                        }
                    )
                return {
                    "account_id": account,
                    "permission_set_arn": arn,
                    "documents": docs,
                    "boundary": p["boundary"],
                }

            docs = capture("policies:" + account + ":" + arn, documents)
            if docs is not None:
                inv["policies"].append(docs)
    inv["status"] = (
        "partial"
        if any(c["status"] == "unknown" for c in inv["coverage"])
        else "complete"
    )
    inv["inventory_hash"] = digest(inv)
    return inv


def validate_inventory(inv):
    require(
        inv.get("kind") == "identity_inventory" and inv.get("schema_version") == 1,
        "INVALID_INVENTORY",
    )
    raw = {k: v for k, v in inv.items() if k != "inventory_hash"}
    require(inv.get("inventory_hash") == digest(raw), "INVENTORY_HASH_MISMATCH")
    for field in (
        "users",
        "groups",
        "memberships",
        "permission_sets",
        "assignments",
        "policies",
        "coverage",
    ):
        require(isinstance(inv.get(field), list), "INVALID_INVENTORY")
    for field, key in (("users", "id"), ("groups", "id"), ("permission_sets", "arn")):
        ids = [v[key] for v in inv[field]]
        require(len(ids) == len(set(ids)), "DUPLICATE_SOURCE_ID")
    return inv
