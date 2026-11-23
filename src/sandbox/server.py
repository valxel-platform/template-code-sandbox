"""run_code: an agent's generated code, run once in a fresh isolated sandbox.

The tool is not read-only (the code could do anything inside its sandbox) but it changes nothing
outside it: no network, no product data but the files the caller sends, and the sandbox is gone
after the run. Agents should still show a person what they're about to run when the code came
from untrusted text.
"""

from __future__ import annotations

import base64
import hashlib
import time

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import events, flags, usage
from .audit import Actor, Auditor, AuditUnavailable, make_sink
from .executors import Executor, Unavailable, make_executor
from .jobs import Job, Limits, Refused, check
from .security import Concurrency, RateLimiter, authorize, caller, on_behalf_of
from .settings import Settings

FILES: dict[str, str] = Field(
    default_factory=dict, description="Input files: plain name -> base64 content, put beside main.py"
)
RUN = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)


class OutFile(BaseModel):
    name: str
    content_base64: str
    bytes: int


class RunResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    out_of_memory: bool
    truncated: bool = Field(description="Output or files were cut at their limits")
    duration_ms: int
    files: list[OutFile]
    # The template's example feature, behind its flag (example_flag below): off, it stays empty.
    stdout_lines: int | None = Field(default=None, description="How many lines the code printed")


def example_flag(settings: Settings) -> str:
    """The example feature's flag. It shows the pattern every feature follows: the tool gives the
    feature a flag in the product's Flipt (on in dev and staging, off in production until it's
    released) and the new behaviour reads it. Replace it with your first feature's own."""
    return f"{settings.product or 'example'}.example.stdout-lines"


def make_auditor(settings: Settings) -> Auditor:
    sink = make_sink(
        settings.audit_sink,
        bootstrap=settings.audit_kafka_bootstrap,
        topic=settings.audit_topic,
        config=dict(settings.audit_kafka_config),
        data_class=settings.data_class,
    )
    return Auditor(
        sink,
        service=settings.name,
        product=settings.product,
        environment=settings.environment,
        data_class=settings.data_class,
        required=settings.audit_required,
    )


def build_server(
    settings: Settings,
    *,
    verifier: TokenVerifier,
    executor: Executor | None = None,
    auditor: Auditor | None = None,
) -> MCPServer:
    if not settings.allowed_clients:
        raise RuntimeError(
            "Set SANDBOX_ALLOWED_CLIENTS to the product's agent clients: nobody else runs code"
        )
    executor = executor or make_executor(settings)
    auditor = auditor or make_auditor(settings)
    limiter = RateLimiter(settings.rate_per_minute, settings.rate_burst)
    slots = Concurrency(settings.per_caller_concurrency, settings.max_concurrency)
    server = MCPServer(
        settings.name,
        instructions=(
            "Runs Python you write in a fresh sandbox with no network: send the code and any input "
            "files; write result files to out/. Each run starts clean."
        ),
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=settings.oidc_issuer or "https://issuer.invalid",  # type: ignore[arg-type]
            resource_server_url=settings.resource_url,  # type: ignore[arg-type]
            required_scopes=[settings.run_scope],
            validate_token_resource=True,
        ),
    )

    @server.tool(annotations=RUN)
    def run_code(
        code: str = Field(description="Python 3 source; it runs as main.py in the work directory"),
        files: dict[str, str] = FILES,
        timeout_s: int = Field(
            default=min(10, settings.max_seconds),
            ge=1,
            description=f"Wall-clock limit, at most {settings.max_seconds}",
        ),
        language: str = "python",
    ) -> RunResult:
        """Run code in an isolated sandbox: no network, no secrets, limited CPU, memory, time and output.
        Files written to out/ come back. Nothing persists between runs."""
        token = caller()
        allowed = token.client_id in settings.allowed_clients
        actor = Actor(id=token.client_id, type="agent" if allowed else "service", client=token.client_id)
        started, outcome, reason, result = time.monotonic(), "failure", "", None
        digest = hashlib.sha256(code.encode()).hexdigest()
        sizes = {"code_bytes": len(code.encode()), "files_in": len(files)}
        try:
            authorize(token, settings.run_scope, settings.allowed_clients)
            if not limiter.allow(token.client_id):
                raise ToolError("Rate limit reached; try again shortly.")
            try:
                decoded = {k: base64.b64decode(v, validate=True) for k, v in files.items()}
            except ValueError as exc:
                raise ToolError("files must be base64") from exc
            job = check(
                Job(
                    language=language,
                    code=code,
                    files=decoded,
                    limits=Limits(
                        seconds=timeout_s,
                        memory_mb=settings.memory_mb,
                        output_kb=settings.max_output_kb,
                        files_out=settings.max_files_out,
                        files_out_kb=settings.max_files_out_kb,
                    ),
                ),
                max_code_kb=settings.max_code_kb,
                max_input_kb=settings.max_input_kb,
                max_seconds=settings.max_seconds,
            )
            with slots.slot(token.client_id):
                result = executor.run(job)
            outcome = "success"
            lines = None
            if flags.enabled(example_flag(settings), user=token.client_id):  # off: today's behaviour
                lines = len(result.stdout.splitlines())
            return RunResult(
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                timed_out=result.timed_out,
                out_of_memory=result.out_of_memory,
                truncated=result.truncated,
                duration_ms=result.duration_ms,
                files=[
                    OutFile(name=n, content_base64=base64.b64encode(b).decode(), bytes=len(b))
                    for n, b in sorted(result.files.items())
                ],
                stdout_lines=lines,
            )
        except Refused as exc:
            outcome, reason = "denied", str(exc)
            raise ToolError(str(exc)) from exc
        except ToolError as exc:
            outcome, reason = "denied", str(exc)
            raise
        except Unavailable as exc:
            reason = "unavailable"
            raise ToolError(str(exc)) from exc
        finally:
            usage.record_call("run_code", token.client_id, outcome, time.monotonic() - started)
            ended = (
                (
                    "timed_out"
                    if result.timed_out
                    else "out_of_memory"
                    if result.out_of_memory
                    else "ok"
                    if result.exit_code == 0
                    else "error"
                )
                if result
                else outcome
            )
            events.record("sandbox.run", outcome=ended)
            try:
                auditor.record(
                    "sandbox.run",
                    actor=actor,
                    resource=("code", [digest]),
                    outcome=outcome,  # type: ignore[arg-type]
                    reason=reason,
                    on_behalf_of=on_behalf_of(token),
                    details={
                        **sizes,
                        "exit_code": result.exit_code if result else None,
                        "ended": ended,
                        "duration_ms": result.duration_ms if result else 0,
                    },
                )
            except AuditUnavailable as exc:
                raise ToolError(
                    "This run couldn't be recorded in the audit trail, so its result is withheld."
                ) from exc

    return server
