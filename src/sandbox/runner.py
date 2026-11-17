"""Runs one job and exits: the program inside each sandbox.

The sandbox (a fresh Agent Sandbox pod from the warm pool: gVisor, no network, no service account
token, read-only root, a size-limited work directory) gives the isolation. This adds limits that
hold even if the pod's were loosened:

  - the code runs as `python3 -I -B` (isolated: no environment variables or user site packages) in a
    new session, in the work directory, with an empty environment but PATH, HOME and LANG;
  - CPU time, address space, file size, open files, processes and core dumps are capped (rlimits);
  - the wall clock is enforced: the whole process group is killed at the time limit;
  - stdout and stderr are read up to their cap and the rest discarded;
  - files the code writes to out/ come back, up to the count and size caps; nothing else does.

`sandbox-runner` serves exactly one POST /run on SANDBOX_RUNNER_PORT, then exits: a sandbox never
runs a second job, so nothing one run leaves behind reaches another.
"""

from __future__ import annotations

import contextlib
import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import IO, Any

from .jobs import Job, Limits, Result, check_name

ENTRY = "main.py"
OUT = "out"


def _limits(limits: Limits, *, processes: bool) -> Any:
    def apply() -> None:  # runs in the child, before exec
        os.setsid()
        cpu = limits.seconds + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        memory = limits.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        size = max(limits.files_out_kb, limits.output_kb) * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if processes:  # inside the sandbox the runner's user owns nothing else
            resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))

    return apply


class _Capped:
    def __init__(self, stream: IO[bytes], cap: int) -> None:
        self.stream, self.cap, self.data, self.over = stream, cap, bytearray(), False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        while chunk := self.stream.read(65536):
            room = self.cap - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > max(room, 0):
                self.over = True

    def text(self) -> str:
        self.thread.join(5)
        return self.data.decode("utf-8", errors="replace")


def _collect(out: Path, limits: Limits) -> tuple[dict[str, bytes], bool]:
    files: dict[str, bytes] = {}
    total, truncated = 0, False
    if not out.is_dir():
        return files, False
    for p in sorted(out.iterdir()):
        if p.is_symlink() or not p.is_file():
            truncated = True  # links and folders never come back
            continue
        try:
            name = check_name(p.name)
        except ValueError:
            truncated = True
            continue
        size = p.stat().st_size
        if len(files) >= limits.files_out or total + size > limits.files_out_kb * 1024:
            truncated = True
            continue
        files[name] = p.read_bytes()
        total += size
    return files, truncated


def execute(job: Job, *, workdir: str | None = None, sandboxed: bool = False) -> Result:
    """Run one job in a fresh work directory and return what came back."""
    with tempfile.TemporaryDirectory(prefix="run-", dir=workdir) as tmp:
        work = Path(tmp)
        for name, content in job.files.items():
            (work / check_name(name)).write_bytes(content)
        (work / ENTRY).write_text(job.code, encoding="utf-8")
        (work / OUT).mkdir()
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(work), "LANG": "C.UTF-8"}
        started = time.monotonic()
        proc = subprocess.Popen(  # noqa: S603 - a fixed interpreter and a file we wrote
            [sys.executable, "-I", "-B", ENTRY],
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_limits(job.limits, processes=sandboxed),
            close_fds=True,
        )
        assert proc.stdout is not None and proc.stderr is not None
        cap = job.limits.output_kb * 1024
        out, err = _Capped(proc.stdout, cap), _Capped(proc.stderr, cap)
        timed_out = False
        try:
            code = proc.wait(timeout=job.limits.seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            code = proc.wait()
        else:
            with contextlib.suppress(ProcessLookupError):  # anything it left running in its session goes too
                os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = out.text(), err.text()
        files, files_cut = _collect(work / OUT, job.limits)
        cpu_killed = code in (-signal.SIGXCPU, -signal.SIGKILL) and not timed_out
        return Result(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out or code == -signal.SIGXCPU,
            out_of_memory="MemoryError" in stderr[-2000:] and not cpu_killed,
            truncated=out.over or err.over or files_cut,
            duration_ms=int((time.monotonic() - started) * 1000),
            files=files,
        )


class _OneJob(BaseHTTPRequestHandler):
    done = threading.Event()  # a job has been accepted: no other will be
    finished = threading.Event()  # its answer has been sent
    max_body = 16 * 1024 * 1024

    def log_message(self, fmt: str, *args: Any) -> None:  # no request lines: nothing about the job
        return

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send(200 if self.path == "/healthz" else 404, {"status": "ready"})

    def do_POST(self) -> None:
        if self.path != "/run" or self.done.is_set():
            self._send(409 if self.path == "/run" else 404, {"error": "this sandbox has run its job"})
            return
        self.done.set()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= self.max_body:
                self._send(413, {"error": "too large"})
                return
            job = Job.from_wire(json.loads(self.rfile.read(length)))
            result = execute(job, workdir=os.environ.get("SANDBOX_WORKDIR") or None, sandboxed=True)
            self._send(200, result.wire())
        except (ValueError, KeyError, TypeError) as exc:
            self._send(400, {"error": type(exc).__name__})
        finally:
            self.finished.set()


def main() -> None:
    port = int(os.environ.get("SANDBOX_RUNNER_PORT", "8888"))
    server = HTTPServer(("0.0.0.0", port), _OneJob)  # noqa: S104 - reachable only from the sandbox service
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _OneJob.finished.wait()
    server.shutdown()
