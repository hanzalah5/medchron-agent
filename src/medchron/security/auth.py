"""Authentication and role-based access control.

Two modes, chosen with ``MEDCHRON_AUTH_MODE``:

- ``oidc`` (production): a bearer JWT verified against the identity
  provider's JWKS. Issuer, audience and signature are all checked. Roles
  come from a configurable claim (Keycloak puts them in
  ``realm_access.roles``).
- ``dev`` (local demo and tests): ``X-Dev-User`` and ``X-Dev-Roles``
  headers. Dev mode refuses to start unless ``MEDCHRON_ENV`` is
  ``development`` or ``test``, so it can't be switched on by accident in a
  deployed environment.

Roles are fixed: paralegal, attorney, admin and auditor. Matter-level access
(ethical walls) sits on top of roles. See ``medchron.security.access``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Any

import jwt
from fastapi import HTTPException, Request


class Role(StrEnum):
    PARALEGAL = "paralegal"
    ATTORNEY = "attorney"
    ADMIN = "admin"
    AUDITOR = "auditor"


# What each role may do. Anything not listed is denied.
PERMISSIONS: dict[str, set[Role]] = {
    "matter.create": {Role.PARALEGAL, Role.ATTORNEY, Role.ADMIN},
    "matter.read": {Role.PARALEGAL, Role.ATTORNEY},
    "matter.members": {Role.ADMIN, Role.ATTORNEY},
    "events.review": {Role.PARALEGAL, Role.ATTORNEY},
    "index.confirm": {Role.PARALEGAL, Role.ATTORNEY},
    "issues.triage": {Role.ATTORNEY},
    "memo.draft": {Role.ATTORNEY},
    "memo.approve": {Role.ATTORNEY},
    "audit.read": {Role.AUDITOR, Role.ADMIN},
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    roles: frozenset[Role]

    def can(self, permission: str) -> bool:
        return bool(self.roles & PERMISSIONS.get(permission, set()))


SYSTEM = Principal(user_id="__system__", roles=frozenset())

# "demo" is the public synthetic-data demo: self-chosen identities, no real data,
# and live LLM runs gated by an access code (see api.app.create_matter).
DEV_AUTH_ENVS = ("development", "test", "demo")


class AuthError(HTTPException):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(
            status, detail, headers={"WWW-Authenticate": "Bearer"} if status == 401 else None
        )


def auth_mode() -> str:
    mode = os.environ.get("MEDCHRON_AUTH_MODE", "dev")
    if mode == "dev" and os.environ.get("MEDCHRON_ENV", "development") not in (
        "development",
        "test",
    ):
        raise RuntimeError("dev auth mode is only allowed when MEDCHRON_ENV is development or test")
    if mode not in ("dev", "oidc"):
        raise RuntimeError("MEDCHRON_AUTH_MODE must be 'dev' or 'oidc'")
    return mode


def _roles(values: Any) -> frozenset[Role]:
    out = set()
    for v in values if isinstance(values, list | tuple | set) else str(values).split(","):
        try:
            out.add(Role(str(v).strip().lower()))
        except ValueError:
            continue  # unknown roles grant nothing
    return frozenset(out)


@lru_cache(maxsize=4)
def _jwks(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=600)


def _claim(payload: dict[str, Any], path: str) -> Any:
    cur: Any = payload
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def verify_token(token: str) -> Principal:
    issuer = os.environ["MEDCHRON_OIDC_ISSUER"]
    audience = os.environ["MEDCHRON_OIDC_AUDIENCE"]
    jwks_url = os.environ.get(
        "MEDCHRON_OIDC_JWKS_URL", issuer.rstrip("/") + "/protocol/openid-connect/certs"
    )
    try:
        key = _jwks(jwks_url).get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise AuthError(401, f"invalid token: {type(e).__name__}") from e
    roles = _roles(
        _claim(payload, os.environ.get("MEDCHRON_OIDC_ROLES_CLAIM", "realm_access.roles")) or []
    )
    user = (
        payload.get(os.environ.get("MEDCHRON_OIDC_USER_CLAIM", "preferred_username"))
        or payload["sub"]
    )
    return Principal(user_id=str(user), roles=roles)


def authenticate(request: Request) -> Principal:
    """Bearer header, or a cookie for requests browsers can't add headers to
    (EventSource, <img>). In production the cookie is HttpOnly, Secure and
    SameSite=Strict, and is set by the login flow."""
    if auth_mode() == "oidc":
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else None
        token = token or request.cookies.get("medchron_token")
        if not token:
            raise AuthError(401, "missing bearer token")
        return verify_token(token)
    user = request.headers.get("x-dev-user")
    roles = request.headers.get("x-dev-roles", "")
    if not user and (cookie := request.cookies.get("medchron_dev")):
        user, _, roles = cookie.partition(":")
    if not user:
        raise AuthError(401, "not signed in (dev mode: X-Dev-User header or medchron_dev cookie)")
    return Principal(user_id=user[:128], roles=_roles(roles))


def require(principal: Principal, permission: str) -> None:
    if not principal.can(permission):
        raise AuthError(403, f"role does not allow {permission}")
