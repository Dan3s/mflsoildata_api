"""Keycloak JWT authentication and role-based authorization.

Validates the Bearer access token issued by Keycloak (RS256) against the realm's
JWKS, and exposes role guards:

- ``get_current_user``      -> any authenticated user (Viewer+). Use for reads.
- ``require_editor``         -> editor, database_editor or admin. Use for writes (add data values).
- ``require_database_editor``-> database_editor or admin. Use for catalog/schema writes
                                (new variables and EAV attribute definitions).
- ``require_admin``          -> admin only. Use for deletes.

Realm roles arrive in the token under ``realm_access.roles``.
"""

import os
from functools import lru_cache
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


def _lan_issuer(base: str) -> str:
    """LAN usability testing: a token minted by Keycloak carries whatever host
    its realm "Frontend URL" is set to, so once that's pointed at this
    machine's LAN IP (for a coworker's browser to reach it), the `iss` claim
    we validate against has to match — even though Keycloak itself still runs
    on localhost as far as this API is concerned. Set KEYCLOAK_LOCALTESTS to
    that same IP (optionally "ip:port") to swap it in here; leave it unset and
    nothing changes. KEYCLOAK_JWKS_URL deliberately keeps the original
    (localhost) host below — fetching signing keys is a same-machine call,
    unaffected by what the browser-facing issuer says."""
    lan_host = os.getenv("KEYCLOAK_LOCALTESTS", "").strip()
    if not lan_host:
        return base
    parts = urlsplit(base)
    host, _, port = lan_host.partition(":")
    netloc = f"{host}:{port}" if port else f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


_KEYCLOAK_ISSUER_BASE = os.getenv("KEYCLOAK_ISSUER", "http://localhost:8080/realms/mfl")
KEYCLOAK_ISSUER = _lan_issuer(_KEYCLOAK_ISSUER_BASE)
KEYCLOAK_AUDIENCE = os.getenv("KEYCLOAK_AUDIENCE")  # e.g. "mfl-api" (optional)
KEYCLOAK_VERIFY_AUD = os.getenv("KEYCLOAK_VERIFY_AUD", "false").lower() in {"1", "true", "yes"}
KEYCLOAK_JWKS_URL = os.getenv(
    "KEYCLOAK_JWKS_URL", f"{_KEYCLOAK_ISSUER_BASE}/protocol/openid-connect/certs"
)

_bearer = HTTPBearer(auto_error=True)
# Optional variant: does not 401 when the Authorization header is missing, so
# endpoints can log the caller when present without requiring authentication.
_bearer_optional = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def _jwk_client() -> "jwt.PyJWKClient":
    # PyJWKClient fetches and caches the realm signing keys.
    return jwt.PyJWKClient(KEYCLOAK_JWKS_URL)


class CurrentUser:
    def __init__(
        self,
        sub: Optional[str],
        email: Optional[str],
        username: Optional[str],
        roles: List[str],
        organization: Optional[str] = None,
        country: Optional[str] = None,
    ):
        self.sub = sub
        self.email = email
        self.username = username
        self.roles = roles
        # Keycloak custom user attributes (require a protocol mapper into the token).
        self.organization = organization
        self.country = country

    def has_any(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)


def _user_from_claims(decoded: dict) -> CurrentUser:
    roles = (decoded.get("realm_access") or {}).get("roles", []) or []
    return CurrentUser(
        sub=decoded.get("sub"),
        email=decoded.get("email"),
        username=decoded.get("preferred_username"),
        roles=roles,
        organization=decoded.get("organization"),
        country=decoded.get("country"),
    )


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

    return _user_from_claims(decoded)


def get_optional_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_optional),
) -> Optional[CurrentUser]:
    """Return the caller if a valid token is present, else ``None`` (no 401).

    Use for endpoints that should work anonymously but want to record who acted
    when a token is available (e.g. the download audit log).
    """
    if creds is None:
        return None
    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(creds.credentials).key
        decoded = jwt.decode(
            creds.credentials,
            signing_key,
            algorithms=["RS256"],
            issuer=KEYCLOAK_ISSUER,
            audience=KEYCLOAK_AUDIENCE if KEYCLOAK_VERIFY_AUD else None,
            options={"verify_aud": KEYCLOAK_VERIFY_AUD},
        )
    except Exception:
        return None

    return _user_from_claims(decoded)


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


# Adding data *values* (measurements, EAV values, uploads): editors, database editors, admins.
require_editor = require_roles("editor", "database_editor", "admin")
# Extending the *catalog* (new variables and EAV attribute definitions): database editors, admins.
require_database_editor = require_roles("database_editor", "admin")
require_admin = require_roles("admin")
