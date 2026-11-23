"""run_code end to end over MCP: who may call it, what it returns and what's recorded."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from sandbox.executors import LocalExecutor
from sandbox.server import build_server
from sandbox.settings import Settings

from .conftest import FakeVerifier, Live


def test_the_tool_is_declared_as_changing_nothing_outside_its_sandbox(live: Live) -> None:
    [tool] = live.tools("agent")
    assert tool.name == "run_code" and not tool.annotations.read_only_hint
    assert not tool.annotations.destructive_hint and not tool.annotations.open_world_hint
    assert {"code", "files", "timeout_s"} <= set(tool.input_schema["properties"])


def test_an_agent_runs_code_and_gets_its_output_and_files(live: Live) -> None:
    files = {"visits.csv": base64.b64encode(b"day,n\nmon,3\ntue,5\n").decode()}
    code = (
        "import csv\nrows = list(csv.DictReader(open('visits.csv')))\n"
        "total = sum(int(r['n']) for r in rows)\nprint(total)\nopen('out/total.txt','w').write(str(total))"
    )
    res = live.call("agent", "run_code", {"code": code, "files": files, "timeout_s": 5})
    assert not res.is_error, res.content
    out = res.structured_content
    assert out["exit_code"] == 0 and out["stdout"] == "8\n"
    assert out["files"] == [
        {"name": "total.txt", "content_base64": base64.b64encode(b"8").decode(), "bytes": 1}
    ]
    [rec] = live.audit.events
    data = rec["data"]
    assert data["action"] == "sandbox.run" and data["outcome"] == "success"
    assert data["actor"] == {"id": "intake-agent", "type": "agent", "client": "intake-agent"}
    assert data["on_behalf_of"] == "nurse-7"
    assert data["resource"] == {"type": "code", "ids": [hashlib.sha256(code.encode()).hexdigest()]}
    assert "visits" not in json.dumps(rec) and "total" not in json.dumps(rec)  # never the code or its data


def test_only_agents_run_code(live: Live) -> None:
    res = live.call("person", "run_code", {"code": "print(1)"})
    assert res.is_error and "Only the product's agents" in res.content[0].text
    assert live.audit.events[-1]["data"]["outcome"] == "denied"


@pytest.mark.parametrize("token", [None, "no-scope", "unknown"])
def test_no_token_or_no_scope_is_refused_before_the_tool(live: Live, token: str | None) -> None:
    with pytest.raises(Exception):  # noqa: B017 - the transport answers 401 or 403
        live.call(token, "run_code", {"code": "print(1)"})
    assert live.audit.events == []


def test_limits_are_refused_before_anything_runs(live: Live) -> None:
    for args, says in [
        ({"code": "print(1)", "timeout_s": 60}, "timeout_s"),
        ({"code": "print(1)", "files": {"../x": "eA=="}}, "plain names"),
        ({"code": "print(1)", "files": {"a": "not base64!"}}, "base64"),
        ({"code": "1", "language": "bash"}, "language"),
    ]:
        res = live.call("agent", "run_code", args)
        assert res.is_error and says in res.content[0].text, res.content
    assert {e["data"]["outcome"] for e in live.audit.events} == {"denied"}


def test_a_timeout_is_a_result_not_an_error(live: Live) -> None:
    res = live.call("agent", "run_code", {"code": "while True: pass", "timeout_s": 1})
    assert not res.is_error and res.structured_content["timed_out"]
    assert live.audit.events[-1]["data"]["details"]["ended"] == "timed_out"


def test_no_audit_record_means_no_result(live: Live) -> None:
    live.audit.fail = True
    res = live.call("agent", "run_code", {"code": "print('hidden')"})
    assert res.is_error and "withheld" in res.content[0].text and "hidden" not in res.content[0].text


def test_a_sandbox_with_no_allowed_clients_refuses_to_start() -> None:
    with pytest.raises(RuntimeError, match="ALLOWED_CLIENTS"):
        build_server(Settings(), verifier=FakeVerifier(), executor=LocalExecutor("dev"))
    with pytest.raises(RuntimeError, match="kubernetes"):
        LocalExecutor("production")


def test_the_example_feature_is_behind_its_flag(live: Live) -> None:
    """The pattern every feature follows: its new behaviour reads its flag, and off is today's."""
    from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider

    from sandbox import flags
    from sandbox.server import example_flag
    from sandbox.settings import Settings

    assert example_flag(Settings(product="emr")) == "emr.example.stdout-lines"
    args = {"code": "print(1)\nprint(2)", "timeout_s": 5}
    assert live.call("agent", "run_code", args).structured_content["stdout_lines"] is None
    on = InMemoryFlag("on", {"on": True, "off": False})
    flags.use_provider(InMemoryProvider({"example.example.stdout-lines": on}))
    try:
        assert live.call("agent", "run_code", args).structured_content["stdout_lines"] == 2
    finally:
        flags.use_provider(InMemoryProvider({}))
