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

## Running

```sh
uv sync
SANDBOX_OIDC_ISSUER=https://id.example.com/realms/dev SANDBOX_OIDC_JWKS_URL=... \
  SANDBOX_ALLOWED_CLIENTS='["my-agent"]' uv run code-sandbox      # local executor
uv run pytest
```

Deployed, set `SANDBOX_EXECUTOR=kubernetes` and `SANDBOX_SANDBOX_TEMPLATE` (default `code-runner`).
The server's service account may only create, read and delete `sandboxclaims` in its namespace.
The same image runs as `sandbox-runner` in each sandbox.

## After deploy

`post-deploy.yml` checks that the server is up, that it refuses calls without a token and that it
publishes its metadata. With `SMOKE_TOKEN` (an allowed agent client's token), it also runs code in
real sandboxes and checks four things:
- the code runs;
- it can't connect out or resolve a name;
- it has no service account token or secrets;
- a file one run writes is gone in the next.
