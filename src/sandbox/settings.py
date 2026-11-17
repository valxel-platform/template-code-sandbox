"""Configuration from the environment (prefix SANDBOX_). Auth has no defaults: a sandbox without an
issuer refuses to start rather than run code for anyone."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SANDBOX_")

    name: str = "code-sandbox"
    host: str = "0.0.0.0"  # noqa: S104  (inside the container)
    port: int = 8080
    resource_url: str = "http://localhost:8080/mcp"  # the token audience (RFC 8707)
    oidc_issuer: str = ""
    oidc_jwks_url: str = ""
    run_scope: str = "sandbox:run"
    # The OAuth clients allowed to run code: the product's agents. Empty refuses everyone.
    allowed_clients: list[str] = []
    # Where runs happen: "kubernetes" (a fresh Agent Sandbox per run; required in staging and
    # production) or "local" (a subprocess with resource limits, for development and tests only).
    executor: str = "local"
    namespace: str = ""  # the Kubernetes namespace the sandbox claims go in (the pod's own if empty)
    sandbox_template: str = "code-runner"  # the SandboxTemplate / warm pool to claim from
    claim_ready_s: float = 20.0  # how long to wait for a sandbox before giving up
    runner_port: int = 8888
    # Limits for every run; a caller may ask for less time, never more.
    max_code_kb: int = 64
    max_input_kb: int = 5120
    max_output_kb: int = 64  # each of stdout and stderr
    max_files_out: int = 20
    max_files_out_kb: int = 5120
    max_seconds: int = 30
    memory_mb: int = 512
    # Per caller: runs per minute and runs at once; and for the whole replica.
    rate_per_minute: int = 30
    rate_burst: int = 10
    per_caller_concurrency: int = 2
    max_concurrency: int = 8
    # Audit trail (audit.py): "kafka" in deployed environments, "log" locally.
    product: str = ""
    environment: str = ""
    data_class: str = "general"
    audit_sink: str = "log"
    audit_kafka_bootstrap: str = ""
    audit_topic: str = "audit.v1"
    audit_kafka_config: dict[str, str] = {}
    audit_required: bool = True
    flags_url: str = ""
    flags_environment: str = ""
    flags_namespace: str = ""
    flags_token: str = ""
    flags_timeout_s: float = 0.25
    flags_cache_s: float = 30.0
    allowed_hosts: list[str] = []
