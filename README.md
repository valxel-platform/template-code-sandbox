# template-code-sandbox

The Valxel template for a **code-execution sandbox**: where a product's agents run code they
wrote, such as a calculation over a table, a chart or a file conversion. An agent never runs
model-written code in its own pod. It calls this server's one MCP tool, `run_code`, and each run
happens in a fresh, single-use sandbox with no network.

```
agent ──token exchange, sandbox:run──▶ run_code (this server) ──SandboxClaim──▶ warm pool
                                              ◀──── result ──── runner (gVisor, no network, one job)
```

## The tool

`run_code(code, files={name: base64}, timeout_s)` runs `code` as `main.py` with the files beside
it. It returns `exit_code`, `stdout`, `stderr` (capped), `timed_out`, `out_of_memory`, `truncated`,
`duration_ms` and the files the code wrote to `out/`. A timeout is a result, not an error.

## Isolation

See [SECURITY.md](SECURITY.md). In short:
- **Who:** only allowed agent clients may run code.
- **Where:** each run gets a fresh sandbox from the platform's SandboxTemplate (Agent Sandbox),
  under gVisor, with no network and no service-account token.
- **Inside:** the runner adds rlimits, an empty environment, a wall-clock kill, and caps on output
  and returned files.

## Ports

The API is on 8080, with `/healthz` and `/readyz` for Kubernetes' probes. `/metrics` is on the
management port, `MANAGEMENT_PORT` (8081), as Spring's actuator is in the Java templates. The
platform's metrics collector scrapes it there, and the product's mesh lets the collector reach that
port and no other. The API port serves no metrics.

## Running

```sh
uv sync
SANDBOX_OIDC_ISSUER=https://id.example.com/realms/dev SANDBOX_OIDC_JWKS_URL=... \
  SANDBOX_ALLOWED_CLIENTS='["my-agent"]' uv run code-sandbox      # local executor
uv run pytest
```

Deployed, set `SANDBOX_EXECUTOR=kubernetes`. Runs claim sandboxes from the warm pool
`SANDBOX_SANDBOX_TEMPLATE` (default `code-runner`) in the server's namespace, through Agent Sandbox
1.0 (`extensions.agents.x-k8s.io/v1beta1`): a `SandboxClaim` with `warmPoolRef`, reached at the
sandbox's pod IP, deleted after the run and ending itself shortly after the run's deadline if not.

The platform owns everything a sandbox is: the SDLC tool adds a `code-runner` resource for each
code-sandbox component, and the platform's `code-runner` type (valxel-platform/platform-resources)
creates the SandboxTemplate (gVisor, no network, no service account token, read-only root, limits),
the warm pool, a deny-all network policy, and a Role letting only this component's service account
create, read and delete `sandboxclaims` there. Each sandbox runs the platform's runner image
(`ghcr.io/valxel-platform/code-runner`, published from this repo by `runner-image.yml`), not this
component's image: change the server here, not the runner.

## After deploy

`post-deploy.yml` checks that the server is up, that it refuses calls without a token and that it
publishes its metadata. With `SMOKE_TOKEN` (an allowed agent client's token), it also runs code in
real sandboxes and checks four things:
- the code runs;
- it can't connect out or resolve a name;
- it has no service account token or secrets;
- a file one run writes is gone in the next.
