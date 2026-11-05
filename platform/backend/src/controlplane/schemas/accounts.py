import re
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from controlplane.domain.models import Model

Role = Literal["owner", "member", "viewer"]
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")


class Identity(Model):
    email: str
    team: str
    role: Role = "viewer"

    @property
    def can_deploy(self) -> bool:
        return self.role in ("owner", "member")


class Member(Model):
    email: str
    role: Role
    created_at: datetime


class Team(Model):
    team: str
    members: list[Member]


class RoleChange(Model):
    email: str = Field(max_length=320)
    role: Role


class Credentials(Model):
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("invalid email")
        return value


class SignUp(Credentials):
    team: str = Field(max_length=80)

    @field_validator("team")
    @classmethod
    def normalized_team(cls, value: str) -> str:
        value = " ".join(value.split()).casefold()
        if not 1 <= len(value) <= 40 or not value.isprintable():
            raise ValueError("invalid team")
        return value
