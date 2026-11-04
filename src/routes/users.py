"""User & role administration (admin only).

Thin layer over the Keycloak Admin API: Keycloak stays the single source of
truth for identity and roles — nothing is mirrored in our database.
"""

from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from dependencies.auth import CurrentUser, require_admin, require_editor
from schemas.users import RolesUpdate, UserOption, UserRead
from services import keycloak_admin

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("", response_model=List[UserRead])
async def list_users(
    search: Optional[str] = Query(None, description="Match username, email or name"),
    limit: int = Query(100, ge=1, le=500),
    _user: CurrentUser = Depends(require_admin),
):
    """Realm users with their effective roles, from Keycloak. Admin only."""
    try:
        return keycloak_admin.list_users(search=search, limit=limit)
    except keycloak_admin.KeycloakAdminError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/search", response_model=List[UserOption])
async def search_users(
    q: str = Query(..., min_length=2, description="Name or email fragment (min 2 chars)"),
    _user: CurrentUser = Depends(require_editor),
):
    """Minimal user lookup for the "authorized users" picker on a dataset ficha.

    Deliberately narrower than ``GET /users``: an editor needs to grant a colleague
    access to their dataset, so this returns only a display name and the email used
    as the access key — never roles, attributes or the full directory (a query is
    required, so it cannot be enumerated in one call).
    """
    try:
        users = keycloak_admin.list_users(search=q, limit=20, with_roles=False)
    except keycloak_admin.KeycloakAdminError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return [
        UserOption(
            email=u["email"],
            name=" ".join(filter(None, [u.get("first_name"), u.get("last_name")])) or u.get("username"),
            organization=u.get("organization"),
        )
        for u in users
        if u.get("email")
    ]


@router.get("/roles", response_model=List[str])
async def list_roles(_user: CurrentUser = Depends(require_admin)):
    """Roles this app manages, lowest privilege first."""
    return list(keycloak_admin.MANAGED_ROLES)


@router.put("/{user_id}/roles", response_model=List[str])
async def set_user_roles(
    user_id: str,
    payload: RolesUpdate = Body(...),
    user: CurrentUser = Depends(require_admin),
):
    """Replace the user's managed realm roles.

    An admin cannot drop their own ``admin`` role: doing so would lock them out
    of this screen mid-session, and possibly leave the realm with no admin.
    """
    unknown = [r for r in payload.roles if r not in keycloak_admin.MANAGED_ROLES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Rol(es) no permitido(s): {', '.join(unknown)}")

    if user_id == user.sub and "admin" not in payload.roles:
        raise HTTPException(status_code=409, detail="No puedes quitarte tu propio rol de admin.")

    try:
        return keycloak_admin.set_user_roles(user_id, payload.roles)
    except keycloak_admin.KeycloakAdminError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
