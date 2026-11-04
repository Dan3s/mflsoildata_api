"""Schemas for user/role administration (data comes from Keycloak, not our DB)."""

from typing import List, Optional

from pydantic import BaseModel


class UserRead(BaseModel):
    id: str
    username: Optional[str] = None
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    enabled: bool = True
    organization: Optional[str] = None
    country: Optional[str] = None
    roles: List[str] = []


class UserOption(BaseModel):
    """Minimal identity for the authorized-users picker (no roles, no attributes)."""

    email: str
    name: Optional[str] = None
    organization: Optional[str] = None


class RolesUpdate(BaseModel):
    """The user's managed roles after the change (replace, not merge)."""

    roles: List[str] = []
