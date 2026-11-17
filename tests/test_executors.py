"""The Kubernetes executor: one claim per run, deleted after, whatever happens."""

from __future__ import annotations

import httpx
import pytest

from sandbox import executors
from sandbox.executors import KubernetesExecutor, Unavailable, make_executor
from sandbox.jobs import Job, Limits, Result
from sandbox.settings import Settings


class Api:
    def __init__(self, ready_after: int = 1) -> None:
        self.created, self.deleted, self.polls, self.ready_after = [], [], 0, ready_after

    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        assert (group, plural, ns) == ("extensions.agents.x-k8s.io", "sandboxclaims", "emr-dev")
        self.created.append(body)

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        self.polls += 1
        ready = self.polls >= self.ready_after
        return {
            "status": {
                "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                "sandbox": {"Name": "sb-1"},
            }
        }

    def delete_namespaced_custom_object(self, group, version, ns, plural, name):
        self.deleted.append(name)


JOB = Job("python", "print(1)", limits=Limits(seconds=5))


def executor(api, handler, ready_s=5.0):
    t = [0.0]

    def sleep(s):
        t[0] += s

    return KubernetesExecutor(
        namespace="emr-dev",
        template="code-runner",
        port=8888,
        ready_s=ready_s,
        api=api,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleep,
        clock=lambda: t[0],
    )


def test_each_run_claims_a_fresh_sandbox_and_deletes_it():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=Result(0, "1\n", "").wire())

    api = Api(ready_after=3)
    result = executor(api, handler).run(JOB)
    assert result.stdout == "1\n"
    [claim] = api.created
    assert claim["kind"] == "SandboxClaim" and claim["spec"] == {
        "sandboxTemplateRef": {"name": "code-runner"}
    }
    assert api.deleted == [claim["metadata"]["name"]]
    assert seen == ["http://sb-1.emr-dev.svc.cluster.local:8888/run"]


def test_no_sandbox_in_time_is_unavailable_and_the_claim_still_goes(caplog):
    api = Api(ready_after=10**9)
    with pytest.raises(Unavailable, match="no sandbox"):
        executor(api, lambda r: httpx.Response(500), ready_s=1).run(JOB)
    assert api.deleted and "sandbox.pool_exhausted" in caplog.text


def test_a_runner_failure_is_unavailable_and_the_claim_still_goes():
    api = Api()
    with pytest.raises(Unavailable, match="answered 409"):
        executor(api, lambda r: httpx.Response(409)).run(JOB)

    def broken(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(Unavailable, match="couldn't be reached"):
        executor(api, broken).run(JOB)
    assert len(api.deleted) == 2


def test_choosing_the_executor(monkeypatch):
    assert isinstance(make_executor(Settings(executor="local")), executors.LocalExecutor)
    with pytest.raises(RuntimeError, match="Unknown executor"):
        make_executor(Settings(executor="docker"))
    monkeypatch.setattr(executors, "_custom_objects", lambda: Api())
    k = make_executor(Settings(executor="kubernetes", namespace="emr-dev"))
    assert isinstance(k, KubernetesExecutor) and k.template == "code-runner"
