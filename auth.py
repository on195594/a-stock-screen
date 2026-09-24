"""Authentication and actor authorization boundaries."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

AUTH_EXPIRY_SECONDS = 8 * 3600  # 8 hours maximum business authorization


class AuthError(RuntimeError):
    pass


@dataclass
class Actor:
    user_id: str
    role: Literal["owner", "demo"]
    expires_at: float
    revoked: bool = False

    @property
    def is_valid(self) -> bool:
        return not self.revoked and time.monotonic() < self.expires_at

    def revoke(self) -> None:
        self.revoked = True


def create_demo_actor() -> Actor:
    """Create an authorized actor for loopback demo mode."""
    return Actor(
        user_id="demo_user",
        role="demo",
        expires_at=time.monotonic() + AUTH_EXPIRY_SECONDS,
    )


def create_owner_actor(github_user_id: str | int | None, allowed_user_id: str | None) -> Actor:
    """Authenticate GitHub user ID against the server-side numeric ID whitelist."""
    if github_user_id is None:
        raise AuthError("missing GitHub user ID")
    if allowed_user_id is None:
        raise AuthError("missing allowed user ID configuration")

    norm_user = str(github_user_id).strip()
    norm_allowed = str(allowed_user_id).strip()

    if not norm_user.isdigit() or not norm_allowed.isdigit():
        raise AuthError("GitHub user ID must be numeric")

    if norm_user != norm_allowed:
        raise AuthError("unauthorized GitHub user ID")

    return Actor(
        user_id=norm_user,
        role="owner",
        expires_at=time.monotonic() + AUTH_EXPIRY_SECONDS,
    )


def check_actor(actor: Actor | None) -> Actor:
    """Validate actor and raise AuthError if missing, expired, or revoked."""
    if actor is None:
        raise AuthError("missing authorization context")
    if not actor.is_valid:
        raise AuthError("authorization expired or revoked")
    return actor


def check_production_auth_config(
    client_id: str | None,
    client_secret: str | None,
    allowed_user_id: str | None,
) -> None:
    """Validate production OAuth environment variables; fail fast if missing or invalid."""
    if not client_id or not client_id.strip():
        raise AuthError("Production mode requires GITHUB_CLIENT_ID")
    if not client_secret or not client_secret.strip():
        raise AuthError("Production mode requires GITHUB_CLIENT_SECRET")
    if not allowed_user_id or not allowed_user_id.strip():
        raise AuthError("Production mode requires GITHUB_ALLOWED_USER_ID")
    if not allowed_user_id.strip().isdigit():
        raise AuthError("GITHUB_ALLOWED_USER_ID must be numeric")
