"""Keycloak JWT authentication and role-based authorization.

Validates the Bearer access token issued by Keycloak (RS256) against the realm's
JWKS, and exposes role guards:

- ``get_current_user``  -> any authenticated user (Viewer+). Use for reads.
- ``require_editor``     -> editor or admin. Use for writes (add data).
- ``require_admin``      -> admin only. Use for deletes.

Realm roles arrive in the token under ``realm_access.roles``.
"""

import os
from functools import lru_cache
from typing import List, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

KEYCLOAK_ISSUER = os.getenv("KEYCLOAK_ISSUER", "http://localhost:8080/realms/mfl")
KEYCLOAK_AUDIENCE = os.getenv("KEYCLOAK_AUDIENCE")  # e.g. "mfl-api" (optional)
KEYCLOAK_VERIFY_AUD = os.getenv("KEYCLOAK_VERIFY_AUD", "false").lower() in {"1", "true", "yes"}
KEYCLOAK_JWKS_URL = os.getenv(
    "KEYCLOAK_JWKS_URL", f"{KEYCLOAK_ISSUER}/protocol/openid-connect/certs"
)

_bearer = HTTPBearer(auto_error=True)


@lru_cache(maxsize=1)
def _jwk_client() -> "jwt.PyJWKClient":
    # PyJWKClient fetches and caches the realm signing keys.
    return jwt.PyJWKClient(KEYCLOAK_JWKS_URL)


class CurrentUser:
    def __init__(self, sub: Optional[str], email: Optional[str], username: Optional[str], roles: List[str]):
        self.sub = sub
        self.email = email
        self.username = username
        self.roles = roles

    def has_any(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> CurrentUser:
    token = creds.credentials
    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(token).key
        decoded = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=KEYCLOAK_ISSUER,
            audience=KEYCLOAK_AUDIENCE if KEYCLOAK_VERIFY_AUD else None,
            options={"verify_aud": KEYCLOAK_VERIFY_AUD},
        )
    except Exception as exc:  # invalid signature, expired, wrong issuer, etc.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token inválido o expirado: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    roles = (decoded.get("realm_access") or {}).get("roles", []) or []
    return CurrentUser(
        sub=decoded.get("sub"),
        email=decoded.get("email"),
        username=decoded.get("preferred_username"),
        roles=roles,
    )


def require_roles(*allowed: str):
    """Dependency factory: require at least one of ``allowed`` realm roles."""

    def checker(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has_any(*allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tiene permisos suficientes para realizar esta acción.",
            )
        return user

    return checker


require_editor = require_roles("editor", "admin")
require_admin = require_roles("admin")
