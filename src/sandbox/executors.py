"""Where a job runs.

kubernetes  each run claims a fresh sandbox from the warm pool (Agent Sandbox: a SandboxClaim
            on the SandboxTemplate `sandbox_template`), sends the job to its runner, reads the
            result and deletes the claim. A sandbox runs one job, then it's gone: nothing from
            one run (files, processes, memory) is ever seen by another. The template, owned by
            the platform, sets the isolation: gVisor, no egress, no service account token,
            read-only root, CPU and memory limits and a deadline.
local       the runner's execute() in this process's container, for development and tests: the
            rlimits hold, but there's no network isolation. Refused in staging and production.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from .jobs import Job, Result
from .runner import execute

log = logging.getLogger("sandbox.executors")

GROUP, VERSION, PLURAL = "extensions.agents.x-k8s.io", "v1alpha1", "sandboxclaims"


class Unavailable(RuntimeError):
    """No sandbox could be had in time (the pool is exhausted or the cluster is unhealthy)."""


class Executor(Protocol):
    def run(self, job: Job) -> Result: ...


class LocalExecutor:
    def __init__(self, environment: str) -> None:
        if environment in ("staging", "production"):
            raise RuntimeError(
                "the local executor has no network isolation: staging and production must use "
                "SANDBOX_EXECUTOR=kubernetes"
            )

    def run(self, job: Job) -> Result:
        return execute(job)


class KubernetesExecutor:
    def __init__(
        self,
        *,
        namespace: str,
        template: str,
        port: int,
        ready_s: float,
        api: Any = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.namespace, self.template, self.port, self.ready_s = namespace, template, port, ready_s
        self.api = api or _custom_objects()
        self.http = http or httpx.Client(timeout=60, follow_redirects=False, trust_env=False)
        self.sleep, self.clock = sleep, clock

    def run(self, job: Job) -> Result:
        name = f"run-{uuid.uuid4().hex[:16]}"
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "SandboxClaim",
            "metadata": {"name": name, "labels": {"valxel.io/sandbox-run": "true"}},
            "spec": {"sandboxTemplateRef": {"name": self.template}},
        }
        self.api.create_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL, body)
        try:
            host = self._ready(name)
            resp = self.http.post(
                f"http://{host}:{self.port}/run", json=job.wire(), timeout=job.limits.seconds + 30
            )
            if resp.status_code != 200:
                raise Unavailable(f"the sandbox answered {resp.status_code}")
            return Result.from_wire(resp.json())
        except httpx.HTTPError as exc:
            raise Unavailable(f"the sandbox couldn't be reached ({type(exc).__name__})") from exc
        finally:
            try:
                self.api.delete_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL, name)
            except Exception:  # noqa: BLE001 - the claim's TTL removes it anyway
                log.warning("sandbox.claim_not_deleted claim=%s", name)

    def _ready(self, name: str) -> str:
        deadline = self.clock() + self.ready_s
        while self.clock() < deadline:
            claim = self.api.get_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL, name)
            status = claim.get("status") or {}
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or []
            )
            sandbox = (status.get("sandbox") or {}).get("Name") or (status.get("sandbox") or {}).get("name")
            if ready and sandbox:
                return f"{sandbox}.{self.namespace}.svc.cluster.local"
            self.sleep(0.2)
        log.error("sandbox.pool_exhausted template=%s waited_s=%s", self.template, self.ready_s)
        raise Unavailable("no sandbox was ready in time; try again shortly")


def _custom_objects() -> Any:
    from kubernetes import client, config

    config.load_incluster_config()
    return client.CustomObjectsApi()


def make_executor(settings: Any) -> Executor:
    if settings.executor == "kubernetes":
        namespace = settings.namespace or _own_namespace()
        return KubernetesExecutor(
            namespace=namespace,
            template=settings.sandbox_template,
            port=settings.runner_port,
            ready_s=settings.claim_ready_s,
        )
    if settings.executor == "local":
        return LocalExecutor(settings.environment)
    raise RuntimeError(f"Unknown executor {settings.executor!r}: use kubernetes or local")


def _own_namespace() -> str:
    from pathlib import Path

    return Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip()
