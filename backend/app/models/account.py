"""Request bodies for sign-in, user accounts, projects and API tokens."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Role = Literal["viewer", "tester", "admin"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Secret = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: Secret


class PasswordChange(BaseModel):
    current_password: Secret
    new_password: Secret


class TokenIn(BaseModel):
    name: Name


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: Secret
    role: Role = "tester"
    projects: list[str] = Field(default_factory=lambda: ["p_default"])


class UserUpdate(BaseModel):
    """Only the fields that are sent change."""
    role: Role | None = None
    disabled: bool | None = None
    password: Secret | None = None
    projects: list[str] | None = None


class ProjectIn(BaseModel):
    name: Name
    members: list[str] = []
