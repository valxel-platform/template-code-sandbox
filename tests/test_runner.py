"""The runner's own limits, which hold even if the sandbox's were loosened."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from sandbox import runner
from sandbox.jobs import Job, Limits, Refused, Result, check


def run(code: str, **limits) -> Result:
    return runner.execute(Job("python", code, limits=Limits(**{"seconds": 5, **limits})))


def test_code_runs_with_its_files_and_returns_out_files():
    job = Job(
        "python",
        "import json\nrows = json.load(open('rows.json'))\nprint(sum(rows))\n"
        "open('out/total.txt', 'w').write(str(sum(rows)))",
        files={"rows.json": b"[1, 2, 3]"},
        limits=Limits(seconds=5),
    )
    r = runner.execute(job)
    assert (r.exit_code, r.stdout.strip(), r.files) == (0, "6", {"total.txt": b"6"})
    assert not (r.timed_out or r.truncated)


def test_the_environment_is_empty_and_isolated(monkeypatch):
    monkeypatch.setenv("SANDBOX_SECRET", "s3cret")
    r = run("import os, sys\nprint(sorted(os.environ))\nprint(sys.flags.isolated)")
    assert "SANDBOX_SECRET" not in r.stdout and r.stdout.splitlines()[-1] == "1"


def test_time_is_enforced_and_children_die_with_the_run():
    started = time.monotonic()
    r = run("import subprocess, time\nsubprocess.Popen(['sleep', '30'])\ntime.sleep(30)", seconds=1)
    assert r.timed_out and time.monotonic() - started < 5


def test_memory_and_output_are_capped():
    r = run("x = bytearray(400 * 1024 * 1024)", memory_mb=128)
    assert r.exit_code != 0 and r.out_of_memory
    r = run("print('x' * 200_000)", output_kb=1)
    assert r.truncated and len(r.stdout) == 1024


def test_out_files_are_capped_and_links_never_come_back(tmp_path):
    (tmp_path / "secret").write_text("host file")
    r = run(
        f"import os\nos.symlink('{tmp_path}/secret', 'out/link')\n"
        "for i in range(5): open(f'out/f{i}.txt', 'w').write('y' * 100)",
        files_out=2,
    )
    assert set(r.files) == {"f0.txt", "f1.txt"} and r.truncated


def test_requests_are_checked_before_they_run():
    ok = Job("python", "print(1)", limits=Limits(seconds=5))
    assert check(ok, max_code_kb=1, max_input_kb=1, max_seconds=10) is ok
    for job, says in [
        (Job("bash", "ls"), "language"),
        (Job("python", "  "), "no code"),
        (Job("python", "#" * 2048), "code is limited"),
        (Job("python", "1", files={"../etc/passwd": b""}), "plain names"),
        (Job("python", "1", files={"a": b"x" * 2048}), "input files"),
        (Job("python", "1", limits=Limits(seconds=60)), "timeout_s"),
    ]:
        with pytest.raises(Refused, match=says):
            check(job, max_code_kb=1, max_input_kb=1, max_seconds=10)


def test_the_wire_format_round_trips():
    job = Job("python", "print(1)", files={"a.bin": b"\x00\x01"}, limits=Limits(seconds=3))
    assert Job.from_wire(json.loads(json.dumps(job.wire()))) == job
    res = Result(0, "o", "e", files={"x": b"\xff"})
    assert Result.from_wire(json.loads(json.dumps(res.wire()))) == res


def test_a_runner_serves_one_job_then_stops(monkeypatch):
    port = 18000 + int(time.time()) % 1000
    monkeypatch.setenv("SANDBOX_RUNNER_PORT", str(port))
    monkeypatch.setattr(runner._OneJob, "done", threading.Event())
    monkeypatch.setattr(runner._OneJob, "finished", threading.Event())
    t = threading.Thread(target=runner.main, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            assert httpx.get(f"{base}/healthz").status_code == 200
            break
        except httpx.ConnectError:
            time.sleep(0.05)
    first = httpx.post(
        f"{base}/run", json=Job("python", "print(2)", limits=Limits(seconds=5)).wire(), timeout=20
    )
    assert first.status_code == 200 and Result.from_wire(first.json()).stdout == "2\n"
    t.join(5)
    assert not t.is_alive()
