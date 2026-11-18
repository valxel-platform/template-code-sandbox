"""The Kubernetes executor: one claim per run, deleted after, whatever happens."""

from __future__ import annotations

from datetime import UTC, datetime

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
        assert (group, version, plural, ns) == (
            "extensions.agents.x-k8s.io",
            "v1beta1",
            "sandboxclaims",
            "emr-dev",
        )
        self.created.append(body)

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        self.polls += 1
        ready = self.polls >= self.ready_after
        return {
            "status": {
                "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                "sandbox": {
                    "name": "sb-1",
                    "podIPs": ["10.1.2.3"],
                    "serviceFQDN": "sb-1.emr-dev.svc.cluster.local",
                },
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
        now=lambda: datetime(2026, 11, 18, 14, 0, tzinfo=UTC),
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
    assert claim["apiVersion"] == "extensions.agents.x-k8s.io/v1beta1" and claim["kind"] == "SandboxClaim"
    assert claim["spec"] == {  # Agent Sandbox 1.0: from the warm pool, ending itself after the run's deadline
        "warmPoolRef": {"name": "code-runner"},
        "lifecycle": {"shutdownPolicy": "Delete", "shutdownTime": "2026-11-18T14:05:05Z"},
    }
    assert api.deleted == [claim["metadata"]["name"]]
    assert seen == ["http://10.1.2.3:8888/run"]  # by the pod's IP


def test_without_a_pod_ip_yet_the_service_name_is_used():
    seen = []

    class NoIp(Api):
        def get_namespaced_custom_object(self, *a):
            out = super().get_namespaced_custom_object(*a)
            out["status"]["sandbox"].pop("podIPs")
            return out

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, json=Result(0, "", "").wire())

    executor(NoIp(), handler).run(JOB)
    assert seen == ["sb-1.emr-dev.svc.cluster.local"]


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
