"""API-key authentication for the middleware.

This surface actuates physical hardware. Anything that can reach `/rpc` can move a robotic
arm, so the server refuses to start without a configured token rather than defaulting to
open access — a middleware that silently comes up unauthenticated is worse than one that
does not come up at all.

Two header forms are accepted, because devices and agents rarely share an HTTP client:

    Authorization: Bearer <token>
    x-api-key: <token>

Tokens are compared with `secrets.compare_digest`, so a wrong token takes the same time to
reject regardless of how many leading characters it got right.

**Limitations, stated plainly (v0.1):**

- One shared secret per deployment. There is no per-device identity, so any holder of the
  token can command any registered device. Multiple tokens are supported for rotation, not
  for authorisation — they all grant the same access.
- No transport security here. Over anything but localhost, terminate TLS in front of this
  server; a bearer token on plain HTTP is a token in everyone's packet capture.
- Registration is authenticated but not attested: a caller with the token can register a
  capability tag declaring any limits it likes. Signed tags are the fix, and are not built.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Iterable, Sequence

from fastapi import HTTPException, Request, status

ENV_VAR = "OPEN_MHS_AUTH_TOKEN"
MIN_TOKEN_LENGTH = 16

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail=(
        "Missing or invalid credentials. Supply the middleware token as "
        "'Authorization: Bearer <token>' or 'x-api-key: <token>'."
    ),
    headers={"WWW-Authenticate": 'Bearer realm="open-mhs"'},
)


class AuthNotConfigured(RuntimeError):
    """No token was configured. Raised at app construction, never at request time."""


@dataclass(frozen=True)
class AuthPolicy:
    """The set of tokens this server accepts."""

    tokens: tuple[str, ...]

    def verify(self, presented: str | None) -> bool:
        """Constant-time membership test. An empty or absent token is never valid."""
        if not presented:
            return False
        # Compare against every token even after a match, so the number of comparisons
        # does not depend on which token was presented.
        matched = False
        for token in self.tokens:
            if secrets.compare_digest(presented, token):
                matched = True
        return matched


def load_tokens(configured: str | Sequence[str] | None = None) -> tuple[str, ...]:
    """Resolve accepted tokens from an explicit value or the environment.

    Accepts a comma-separated list so a token can be rotated without downtime: add the new
    one, migrate callers, drop the old one. All listed tokens grant identical access.

    Raises:
        AuthNotConfigured: nothing was configured, or a token is too short to be a secret.
    """
    if configured is None:
        configured = os.getenv(ENV_VAR)

    raw: Iterable[str]
    if configured is None:
        raw = ()
    elif isinstance(configured, str):
        raw = configured.split(",")
    else:
        raw = configured

    tokens = tuple(t.strip() for t in raw if t and t.strip())

    if not tokens:
        raise AuthNotConfigured(
            f"{ENV_VAR} is not set. This server can actuate physical hardware, so it will "
            "not start without authentication.\n"
            "Generate one and export it:\n"
            '  export OPEN_MHS_AUTH_TOKEN="$(python -c '
            "\"import secrets; print(secrets.token_urlsafe(32))\")\"\n"
            "On Windows PowerShell:\n"
            '  $env:OPEN_MHS_AUTH_TOKEN = python -c '
            '"import secrets; print(secrets.token_urlsafe(32))"'
        )

    short = [t for t in tokens if len(t) < MIN_TOKEN_LENGTH]
    if short:
        raise AuthNotConfigured(
            f"{ENV_VAR} contains a token shorter than {MIN_TOKEN_LENGTH} characters. A token "
            "that guards a robotic arm should be generated, not chosen: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return tokens


def extract_token(request: Request) -> str | None:
    """Pull the presented token out of either accepted header form."""
    header = request.headers.get("authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        return None
    api_key = request.headers.get("x-api-key")
    return api_key.strip() if api_key and api_key.strip() else None


async def require_auth(request: Request) -> None:
    """FastAPI dependency guarding every endpoint that can see or touch hardware.

    Missing and incorrect credentials are answered identically, so the response cannot be
    used to probe which tokens exist. The presented value is never echoed or logged.
    """
    policy: AuthPolicy = request.app.state.auth
    if not policy.verify(extract_token(request)):
        raise _UNAUTHORIZED
