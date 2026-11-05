"""Keycloak Admin REST client for user/role management.

Only what the admin UI needs: list users, read their realm roles, and change
them. Authenticates as a **service account** (client_credentials) so no human
password is ever stored here; that client needs the ``realm-management`` roles
``view-users`` and ``manage-users`` (see docs/KEYCLOAK_PRODUCCION.md).

Env:
  KEYCLOAK_ISSUER               http://host:8080/realms/mfl  (already used by auth)
  KEYCLOAK_ADMIN_CLIENT_ID      service-account client id
  KEYCLOAK_ADMIN_CLIENT_SECRET  its secret
"""

import os
import time
from typing import Any, Dict, List, Optional

import httpx

KEYCLOAK_ISSUER = os.getenv("KEYCLOAK_ISSUER", "http://localhost:8080/realms/mfl")
ADMIN_CLIENT_ID = os.getenv("KEYCLOAK_ADMIN_CLIENT_ID", "")
ADMIN_CLIENT_SECRET = os.getenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "")

# Roles this app knows about. Anything else in the realm (default-roles-*,
# offline_access, uma_authorization, realm-management roles…) is never shown
# nor touched, so the UI cannot accidentally strip Keycloak's own plumbing.
MANAGED_ROLES = ("viewer", "editor", "database_editor", "admin")

# ``{issuer}`` is ``{base}/realms/{realm}`` — split it to reach the admin API.
_base, _, _realm = KEYCLOAK_ISSUER.rpartition("/realms/")
BASE_URL = _base.rstrip("/")
REALM = _realm
ADMIN_URL = f"{BASE_URL}/admin/realms/{REALM}"
TOKEN_URL = f"{KEYCLOAK_ISSUER}/protocol/openid-connect/token"


class KeycloakAdminError(Exception):
    """Keycloak admin API is unreachable, unconfigured or refused the call."""


def is_configured() -> bool:
    return bool(ADMIN_CLIENT_ID and ADMIN_CLIENT_SECRET and BASE_URL and REALM)


_token_cache: Dict[str, Any] = {"value": None, "expires_at": 0.0}


def _token() -> str:
    """Service-account access token, cached until shortly before it expires."""
    if not is_configured():
        raise KeycloakAdminError(
            "Keycloak admin no configurado: falta KEYCLOAK_ADMIN_CLIENT_ID / "
            "KEYCLOAK_ADMIN_CLIENT_SECRET en el .env de la API."
        )
    if _token_cache["value"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["value"]

    try:
        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": ADMIN_CLIENT_ID,
                "client_secret": ADMIN_CLIENT_SECRET,
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise KeycloakAdminError(f"No se pudo autenticar contra Keycloak: {exc}") from exc

    payload = response.json()
    _token_cache["value"] = payload["access_token"]
    # Refresh 30s early to avoid using a token that expires mid-request.
    _token_cache["expires_at"] = time.time() + max(payload.get("expires_in", 60) - 30, 10)
    return _token_cache["value"]


def _request(method: str, path: str, **kwargs) -> Optional[Any]:
    try:
        response = httpx.request(
            method,
            f"{ADMIN_URL}{path}",
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=15.0,
            **kwargs,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            raise KeycloakAdminError(
                "Keycloak rechazó la operación (403). El service account necesita los "
                "roles 'view-users' y 'manage-users' del cliente realm-management."
            ) from exc
        raise KeycloakAdminError(f"Keycloak respondió {exc.response.status_code}: {exc.response.text[:200]}") from exc
    except httpx.HTTPError as exc:
        raise KeycloakAdminError(f"No se pudo contactar a Keycloak: {exc}") from exc

    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _role_reps_for_user(user_id: str) -> Dict[str, Dict[str, Any]]:
    """name -> role representation, for every realm role this user could hold.

    Read through the *user's* role-mapping endpoints (assigned + available) on
    purpose. The obvious ``GET /roles/{name}`` needs the realm-wide ``view-realm``
    permission, which would mean granting this service account far more than
    managing users; ``/users/{id}/role-mappings/realm[/available]`` only needs
    ``view-users``/``manage-users``.
    """
    assigned = _request("GET", f"/users/{user_id}/role-mappings/realm") or []
    available = _request("GET", f"/users/{user_id}/role-mappings/realm/available") or []
    return {role["name"]: role for role in [*assigned, *available]}


def list_users(
    search: Optional[str] = None, limit: int = 100, with_roles: bool = True
) -> List[Dict[str, Any]]:
    """Users, with their effective managed realm roles unless ``with_roles`` is off.

    Reading roles costs one request per user, so the picker (which only needs a
    name and an email) skips it.
    """
    params: Dict[str, Any] = {"max": limit, "briefRepresentation": False}
    if search:
        params["search"] = search
    users = _request("GET", "/users", params=params) or []

    result: List[Dict[str, Any]] = []
    for user in users:
        attributes = user.get("attributes") or {}
        # ``/composite`` = effective roles. ``viewer`` normally arrives inherited
        # through the ``default-roles-<realm>`` composite, so the direct mapping
        # list would wrongly show a brand-new user as having no role at all.
        roles = (
            [
                role["name"]
                for role in (_request("GET", f"/users/{user['id']}/role-mappings/realm/composite") or [])
                if role["name"] in MANAGED_ROLES
            ]
            if with_roles
            else []
        )
        result.append(
            {
                "id": user["id"],
                "username": user.get("username"),
                "email": user.get("email"),
                "first_name": user.get("firstName"),
                "last_name": user.get("lastName"),
                "enabled": user.get("enabled", True),
                # Custom attributes arrive as lists.
                "organization": (attributes.get("organization") or [None])[0],
                "country": (attributes.get("country") or [None])[0],
                "roles": roles,
            }
        )
    return result


def list_reviewer_emails() -> List[str]:
    """Emails of enabled users who can review submissions (database_editor/admin).

    Best-effort and silent: an unconfigured or unreachable Keycloak admin client
    returns an empty list rather than raising, so a caller notifying reviewers on
    a new submission never fails the request that's waiting on it.
    """
    if not is_configured():
        return []
    try:
        users = list_users(limit=200, with_roles=True)
    except KeycloakAdminError:
        return []
    reviewer_roles = {"database_editor", "admin"}
    return [
        u["email"]
        for u in users
        if u.get("email") and u.get("enabled", True) and reviewer_roles.intersection(u.get("roles") or [])
    ]


def set_user_roles(user_id: str, roles: List[str]) -> List[str]:
    """Make the user's managed realm roles exactly ``roles``.

    Only roles in ``MANAGED_ROLES`` are added or removed; every other realm role
    the user has is left untouched.
    """
    wanted = {r for r in roles if r in MANAGED_ROLES}
    reps = _role_reps_for_user(user_id)

    # Two different views, and the difference matters:
    #  - direct:    what is actually assigned to the user (the only thing we can remove)
    #  - effective: direct + inherited via composites, e.g. viewer through default-roles
    direct_all = _request("GET", f"/users/{user_id}/role-mappings/realm") or []
    direct = {r["name"] for r in direct_all if r["name"] in MANAGED_ROLES}
    effective = {
        r["name"]
        for r in (_request("GET", f"/users/{user_id}/role-mappings/realm/composite") or [])
        if r["name"] in MANAGED_ROLES
    }

    missing = wanted - reps.keys() - effective
    if missing:
        raise KeycloakAdminError(
            f"El realm '{REALM}' no tiene el/los rol(es): {', '.join(sorted(missing))}. "
            "Créalos en Realm roles."
        )

    # Don't re-add something already inherited, and only remove direct assignments:
    # trying to strip an inherited role would fail or silently do nothing.
    to_add = wanted - effective
    to_remove = direct - wanted

    if to_add:
        _request("POST", f"/users/{user_id}/role-mappings/realm", json=[reps[n] for n in to_add])
    if to_remove:
        _request(
            "DELETE",
            f"/users/{user_id}/role-mappings/realm",
            json=[r for r in direct_all if r["name"] in to_remove],
        )

    # Re-read rather than predicting: inherited roles make the result non-obvious.
    return sorted(
        r["name"]
        for r in (_request("GET", f"/users/{user_id}/role-mappings/realm/composite") or [])
        if r["name"] in MANAGED_ROLES
    )
