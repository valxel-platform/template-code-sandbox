"""Who may run code, how often and how much at once, and the audit trail.

Only the product's agents run code: a caller needs the run scope (sandbox:run) in a token whose
audience is this server, and its OAuth client must be one of SANDBOX_ALLOWED_CLIENTS. A person's
own token is refused even with the scope: people don't call the sandbox, agents do, on their
behalf (token exchange keeps the person as the subject and the agent as the client).

Each caller has a rate limit and a cap on runs at once; the replica has its own cap. Every run
publishes one audit record (sandbox.run): the agent, the person it acted for, the code's SHA-256,
sizes, the exit code and the time it took. Never the code, its input or its output: any of them
may hold PHI. A run whose record can't be published returns nothing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError


class JwtVerifier:
    """Verifies the IdP's JWT access tokens: signature (JWKS), issuer, audience = this server."""

    def __init__(self, *, issuer: str, jwks_url: str, audience: str) -> None:
        import jwt

        if not (issuer and jwks_url):
            raise RuntimeError("Set SANDBOX_OIDC_ISSUER and SANDBOX_OIDC_JWKS_URL")
        self._jwt = jwt
        self.issuer, self.audience = issuer, audience
        self.jwks = jwt.PyJWKClient(jwks_url, cache_keys=True)

    def _verify(self, token: str) -> AccessToken | None:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = self._jwt.decode(
                token, key.key, algorithms=["RS256", "ES256"], audience=self.audience, issuer=self.issuer
            )
        except self._jwt.PyJWTError:
            return None
        scope = claims.get("scope") or claims.get("scp") or ""
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or ""),
            scopes=scope.split() if isinstance(scope, str) else list(scope),
            expires_at=claims.get("exp"),
            resource=self.audience,
            subject=claims.get("sub"),
            claims=claims,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await anyio.to_thread.run_sync(self._verify, token)


class RateLimiter:
    """A token bucket per caller, per replica (the gateway limits across replicas). Callers idle
    long enough to have a full bucket are forgotten, so memory stays bounded."""

    MAX_CALLERS = 10_000

    def __init__(self, per_minute: int, burst: int) -> None:
        self.rate, self.burst = per_minute / 60.0, float(burst)
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.pop(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            allowed = tokens >= 1
            self.buckets[key] = (tokens - 1 if allowed else tokens, now)
            if len(self.buckets) > self.MAX_CALLERS:
                self._forget(now)
            return allowed

    def _forget(self, now: float) -> None:
        full = self.burst / self.rate if self.rate else float("inf")  # seconds to refill
        for k, (_, last) in list(self.buckets.items()):
            if now - last >= full:
                del self.buckets[k]
        while len(self.buckets) > self.MAX_CALLERS:  # oldest first (insertion order)
            del self.buckets[next(iter(self.buckets))]


class Concurrency:
    """Runs at once: per caller and for the replica."""

    def __init__(self, per_caller: int, total: int) -> None:
        self.per_caller, self.total = per_caller, total
        self.running: dict[str, int] = {}
        self.lock = threading.Lock()

    @contextmanager
    def slot(self, who: str) -> Iterator[None]:
        with self.lock:
            if self.running.get(who, 0) >= self.per_caller:
                raise ToolError(f"You have {self.per_caller} runs going already; wait for one to finish.")
            if sum(self.running.values()) >= self.total:
                raise ToolError("The sandbox is busy; try again shortly.")
            self.running[who] = self.running.get(who, 0) + 1
        try:
            yield
        finally:
            with self.lock:
                self.running[who] -= 1
                if not self.running[who]:
                    del self.running[who]


def caller() -> AccessToken:
    token = get_access_token()
    if token is None:
        raise ToolError("Not authenticated.")
    return token


def authorize(token: AccessToken, scope: str, allowed_clients: list[str]) -> None:
    """The run scope, from an allowed agent client."""
    if scope not in token.scopes:
        raise ToolError(f"Running code needs the {scope} scope.")
    if token.client_id not in allowed_clients:
        raise ToolError("Only the product's agents may run code here.")


def on_behalf_of(token: AccessToken) -> str:
    """The person an agent acts for: the subject when it differs from the client (token exchange)."""
    sub = token.subject or ""
    return sub if sub and sub != token.client_id else ""
