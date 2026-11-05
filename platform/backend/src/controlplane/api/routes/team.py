from fastapi import APIRouter

from controlplane.api.deps import LoginAuth, OperatorIdentity, OwnerIdentity
from controlplane.schemas.accounts import Member, RoleChange, Team

router = APIRouter(prefix="/api/v1/team", tags=["team"])


@router.get("")
def team(auth: LoginAuth, identity: OperatorIdentity) -> Team:
    return Team(team=identity.team, members=auth.accounts.members(identity.team))


@router.post("/role")
def change_role(body: RoleChange, auth: LoginAuth, owner: OwnerIdentity) -> Member:
    return auth.accounts.set_role(owner, body)
