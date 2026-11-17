from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
import uvicorn
from mcp.server.auth.provider import AccessToken

from sandbox.app import create_app
from sandbox.audit import Auditor, MemorySink
from sandbox.executors import LocalExecutor
from sandbox.settings import Settings

RESOURCE = "http://127.0.0.1/mcp"
# token -> (client, subject, scopes): agents act for people through token exchange.
TOKENS: dict[str, tuple[str, str, list[str]]] = {
    "agent": ("intake-agent", "nurse-7", ["sandbox:run"]),
    "agent-alone": ("intake-agent", "intake-agent", ["sandbox:run"]),
    "other-agent": ("billing-agent", "clerk-2", ["sandbox:run"]),
    "no-scope": ("intake-agent", "nurse-7", []),
    "person": ("care-desk", "nurse-7", ["sandbox:run"]),
}


class FakeVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        if token not in TOKENS:
            return None
        client, sub, scopes = TOKENS[token]
        return AccessToken(
            token=token, client_id=client, scopes=scopes, subject=sub, resource=RESOURCE, claims={}
        )


class Live:
    def __init__(self, url: str, audit: MemorySink) -> None:
        self.url, self.audit = url, audit

    def call(self, token: str | None, tool: str, args: dict[str, Any]) -> Any:
        return anyio.run(self._call, token, tool, args)

    def tools(self, token: str) -> Any:
        async def go(c: Any) -> Any:
            return (await c.list_tools()).tools

        return anyio.run(self._session, token, go)

    async def _call(self, token: str | None, tool: str, args: dict[str, Any]) -> Any:
        async def go(c: Any) -> Any:
            return await c.call_tool(tool, args)

        return await self._session(token, go)

    async def _session(self, token: str | None, fn: Any) -> Any:
        from mcp.client.client import Client
        from mcp.client.streamable_http import (  # type: ignore[attr-defined]
            create_mcp_http_client,
            streamable_http_client,
        )

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with Client(
            streamable_http_client(self.url, http_client=create_mcp_http_client(headers=headers))
        ) as c:
            return await fn(c)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live() -> Iterator[Live]:
    port = free_port()
    settings = Settings(
        resource_url=RESOURCE,
        oidc_issuer="https://idp.test",
        rate_burst=20,
        rate_per_minute=600,
        allowed_clients=["intake-agent", "billing-agent"],
        max_seconds=5,
    )
    audit = MemorySink()
    auditor = Auditor(audit, service="code-sandbox", product="emr", environment="test", data_class="phi")
    app = create_app(settings, verifier=FakeVerifier(), executor=LocalExecutor("test"), auditor=auditor)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield Live(f"http://127.0.0.1:{port}/mcp", audit)
    server.should_exit = True
    thread.join(5)
