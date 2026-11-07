from __future__ import annotations

import re
import time

from ..identity.collect import pages
from ..identity.common import IdentityError, require
from .common import primary_email

try:
    from botocore.exceptions import BotoCoreError, ClientError

    AWS_ERRORS = (BotoCoreError, ClientError)
except ImportError:
    AWS_ERRORS = ()


def error_code(error):
    code = getattr(error, "response", None)
    code = code.get("Error", {}).get("Code") if isinstance(code, dict) else None
    return code if isinstance(code, str) else ""


def failure(error):
    code = error_code(error)
    if re.fullmatch(r"[A-Za-z]{1,64}", code):
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", code).upper()
        return IdentityError("AWS_" + name, status="failed")
    return IdentityError("AWS_REQUEST_FAILED", status="failed")


def aws_session(target, session_factory=None):
    if session_factory is None:
        try:
            from boto3 import Session
        except ImportError:
            raise IdentityError("INSTALL_IDENTITY_AWS_DEPENDENCIES") from None
        session_factory = Session
    try:
        session = session_factory(
            profile_name=target.get("profile"), region_name=target["region"]
        )
    except AWS_ERRORS:
        raise IdentityError("AWS_SESSION_UNAVAILABLE", status="failed") from None
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
    try:
        return session.client("sso-admin", **options), session.client(
            "identitystore", **options
        )
    except AWS_ERRORS:
        raise IdentityError("AWS_SESSION_UNAVAILABLE", status="failed") from None


class IdentityCenter:
    def __init__(
        self,
        config,
        session_factory=None,
        *,
        sleep=time.sleep,
        poll_interval=2,
        poll_attempts=90,
    ):
        target = config["target"]
        self.sso, self.store = aws_session(target, session_factory)
        self.instance = target["instance_arn"]
        self.store_id = target["identity_store_id"]
        self.sleep = sleep
        self.poll_interval = poll_interval
        self.poll_attempts = poll_attempts

    def call(self, client, operation, *, missing=False, **kwargs):
        try:
            return getattr(client, operation)(**kwargs)
        except AWS_ERRORS as error:
            if missing and error_code(error) == "ResourceNotFoundException":
                return None
            raise failure(error) from None

    def list(self, client, operation, field, **kwargs):
        try:
            return list(pages(client, operation, field, **kwargs))
        except AWS_ERRORS as error:
            raise failure(error) from None

    def check_instance(self):
        instances = self.list(self.sso, "list_instances", "Instances")
        require(
            any(
                i.get("InstanceArn") == self.instance
                and i.get("IdentityStoreId") == self.store_id
                for i in instances
            ),
            "IDENTITY_STORE_INSTANCE_MISMATCH",
        )

    def permission_sets(self):
        result = {}
        for arn in self.list(
            self.sso,
            "list_permission_sets",
            "PermissionSets",
            InstanceArn=self.instance,
        ):
            ps = self.call(
                self.sso,
                "describe_permission_set",
                InstanceArn=self.instance,
                PermissionSetArn=arn,
            )["PermissionSet"]
            result[arn] = ps["Name"]
        return result

    def lookup(self, operation, field, path, value):
        found = self.call(
            self.store,
            operation,
            missing=True,
            IdentityStoreId=self.store_id,
            AlternateIdentifier={
                "UniqueAttribute": {"AttributePath": path, "AttributeValue": value}
            },
        )
        return None if found is None else found[field]

    def user_id(self, username):
        return self.lookup("get_user_id", "UserId", "userName", username)

    def group_id(self, name):
        return self.lookup("get_group_id", "GroupId", "displayName", name)

    def read(self, r):
        kind, p = r["kind"], r["payload"]
        if kind == "user":
            uid = self.user_id(p["UserName"])
            if uid is None:
                email = primary_email(p["Emails"])
                other = email and self.lookup(
                    "get_user_id", "UserId", "emails.value", email
                )
                return {"email_used_by": "another_user"} if other else None
            u = self.call(
                self.store,
                "describe_user",
                missing=True,
                IdentityStoreId=self.store_id,
                UserId=uid,
            )
            if u is None:
                return None
            return {
                "user_name": u["UserName"].lower(),
                "email": primary_email(u.get("Emails")),
            }
        if kind == "group":
            gid = self.group_id(p["DisplayName"])
            if gid is None:
                return None
            g = self.call(
                self.store,
                "describe_group",
                missing=True,
                IdentityStoreId=self.store_id,
                GroupId=gid,
            )
            if g is None:
                return None
            return {
                "display_name": g.get("DisplayName"),
                "description": g.get("Description"),
            }
        if kind == "membership":
            gid, uid = self.group_id(p["group"]), self.user_id(p["user"])
            if gid is None or uid is None:
                return None
            found = self.call(
                self.store,
                "get_group_membership_id",
                missing=True,
                IdentityStoreId=self.store_id,
                GroupId=gid,
                MemberId={"UserId": uid},
            )
            return {"member": True} if found else None
        if kind == "assignment":
            gid = self.group_id(p["group"])
            if gid is None:
                return None
            assignments = self.list(
                self.sso,
                "list_account_assignments",
                "AccountAssignments",
                InstanceArn=self.instance,
                AccountId=p["account_id"],
                PermissionSetArn=p["permission_set_arn"],
            )
            return (
                {"assigned": True}
                if any(
                    a.get("PrincipalType") == "GROUP" and a.get("PrincipalId") == gid
                    for a in assignments
                )
                else None
            )
        raise IdentityError("UNSUPPORTED_RESOURCE_KIND")

    def write(self, r):
        kind, p = r["kind"], r["payload"]
        if kind == "user":
            self.call(self.store, "create_user", IdentityStoreId=self.store_id, **p)
        elif kind == "group":
            self.call(self.store, "create_group", IdentityStoreId=self.store_id, **p)
        elif kind == "membership":
            gid, uid = self.group_id(p["group"]), self.user_id(p["user"])
            require(
                gid is not None and uid is not None, "MEMBERSHIP_DEPENDENCY_MISSING"
            )
            self.call(
                self.store,
                "create_group_membership",
                IdentityStoreId=self.store_id,
                GroupId=gid,
                MemberId={"UserId": uid},
            )
        elif kind == "assignment":
            gid = self.group_id(p["group"])
            require(gid is not None, "ASSIGNMENT_GROUP_MISSING")
            response = self.call(
                self.sso,
                "create_account_assignment",
                InstanceArn=self.instance,
                TargetId=p["account_id"],
                TargetType="AWS_ACCOUNT",
                PermissionSetArn=p["permission_set_arn"],
                PrincipalType="GROUP",
                PrincipalId=gid,
            )
            self.wait(response.get("AccountAssignmentCreationStatus"))
        else:
            raise IdentityError("UNSUPPORTED_RESOURCE_KIND")

    def wait(self, status):
        for _ in range(self.poll_attempts):
            require(isinstance(status, dict), "AWS_RESPONSE_SHAPE")
            state = status.get("Status")
            if state == "SUCCEEDED":
                return
            if state != "IN_PROGRESS":
                raise IdentityError("ACCOUNT_ASSIGNMENT_FAILED", status="failed")
            self.sleep(self.poll_interval)
            status = self.call(
                self.sso,
                "describe_account_assignment_creation_status",
                InstanceArn=self.instance,
                AccountAssignmentCreationRequestId=status.get("RequestId"),
            ).get("AccountAssignmentCreationStatus")
        raise IdentityError("ACCOUNT_ASSIGNMENT_PENDING", status="failed")
