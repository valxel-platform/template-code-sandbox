"""Checks a deployed code sandbox and writes JUnit for the post-deploy report.

Always: it is up, it refuses calls without a token, and it publishes its OAuth protected-resource
metadata. With SMOKE_TOKEN (a client-credentials token for an allowed agent client, with
sandbox:run), it proves the isolation from inside real sandboxes: code runs; it can't open a
network connection or resolve a name; it has no Kubernetes service account token and no secrets in
its environment; and a file one run writes is gone in the next. Changes no product data, so it is
safe in production.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import anyio
import httpx

PROBES = {
    "code runs": ("print(6 * 7)", lambda out: out["exit_code"] == 0 and out["stdout"].strip() == "42"),
    "no network": (
        "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "    print('open')\nexcept OSError:\n    print('blocked')\ntry:\n"
        "    socket.getaddrinfo('example.com', 443)\n    print('dns')\nexcept OSError:\n    print('no dns')",
        lambda out: out["stdout"].split() == ["blocked", "no", "dns"],
    ),
    "no service account token or secrets": (
        "import os\nprint(os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'))\n"
        "print(sorted(k for k in os.environ if k not in ('PATH', 'HOME', 'LANG', 'LC_CTYPE')))",
        lambda out: out["stdout"].split("\n")[:2] == ["False", "[]"],
    ),
    "each run starts clean": (
        "import os, pathlib\nmark = pathlib.Path('/tmp/smoke-mark')\nseen = mark.exists()\n"
        "mark.write_text('x')\nprint(seen)",
        lambda out: out["stdout"].strip() == "False",
    ),
}


def run_code(url: str, token: str, code: str) -> dict[str, Any]:
    from mcp.client.client import Client
    from mcp.client.streamable_http import (  # type: ignore[attr-defined]
        create_mcp_http_client,
        streamable_http_client,
    )

    async def go() -> dict[str, Any]:
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
        async with Client(streamable_http_client(url, http_client=http)) as c:
            res = await c.call_tool("run_code", {"code": code, "timeout_s": 10})
            if res.is_error:
                raise RuntimeError("run_code failed")
            return dict(res.structured_content or {})

    return anyio.run(go)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="e.g. https://care-mcp.dev.example.com")
    ap.add_argument("--junit", default="reports/deployed-smoke.xml")
    args = ap.parse_args(argv)
    base = args.base_url.rstrip("/")
    mcp_url = f"{base}/mcp"
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    accept = {"Accept": "application/json, text/event-stream"}
    checks: dict[str, Callable[[], bool]] = {
        "healthz": lambda: httpx.get(f"{base}/healthz", timeout=10).status_code == 200,
        "calls need a token": lambda: (
            httpx.post(mcp_url, json=rpc, headers=accept, timeout=10).status_code == 401
        ),
        "protected-resource metadata": lambda: (
            httpx.get(f"{base}/.well-known/oauth-protected-resource/mcp", timeout=10).status_code == 200
        ),
    }
    token = os.environ.get("SMOKE_TOKEN", "")
    if token:
        for name, (code, ok) in PROBES.items():
            checks[name] = lambda code=code, ok=ok: ok(run_code(mcp_url, token, code))  # type: ignore[misc]
        # Twice: the second run must not see the first one's file.
        checks["each run starts clean (again)"] = lambda: PROBES["each run starts clean"][1](
            run_code(mcp_url, token, PROBES["each run starts clean"][0])
        )
    suite = ET.Element("testsuite", name="smoke")
    failed = 0
    for name, check in checks.items():
        t0 = time.monotonic()
        try:
            ok = check()
        except Exception:  # noqa: BLE001  (an unreachable server is a failed check)
            ok = False
        tc = ET.SubElement(
            suite, "testcase", classname="smoke", name=name, time=f"{time.monotonic() - t0:.3f}"
        )
        if not ok:
            failed += 1
            ET.SubElement(tc, "failure", message=f"{name} failed at {base}")
        print(f"{'pass' if ok else 'FAIL'}  {name}")
    suite.set("tests", str(len(checks)))
    suite.set("failures", str(failed))
    Path(args.junit).parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(args.junit, encoding="utf-8", xml_declaration=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
