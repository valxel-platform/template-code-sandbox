# Security policy

## Reporting a vulnerability

Do not open a public issue. Use **Security > Report a vulnerability** on this repository
(GitHub private vulnerability reporting), or email the product's security team. Include what you
found, how to reproduce it, and the impact you expect. We acknowledge reports within two
business days.

## What this repository does by default

- Secret scanning with push protection; Dependabot alerts and security updates.
- CodeQL code scanning on every pull request; merges are blocked on new high or critical alerts.
- Dependency review on pull requests; vulnerable additions at the platform's threshold block.
- The default branch is protected: pull requests only, code-owner review, required checks.

## Handling sensitive data

This repository may belong to a product that handles PHI or payment data. Never commit real
patient or cardholder data, including in tests, fixtures, logs or screenshots. Use synthetic
data. Do not log request bodies, identifiers or tokens.

## Isolation in this sandbox

Code that runs here was written by a model, possibly steered by untrusted text. Assume it is hostile.

- **Who:** only the product's agents (`SANDBOX_ALLOWED_CLIENTS`), with the `sandbox:run` scope in a
  token whose audience is this server. People's own tokens are refused. Each run is audited with the
  agent, the person it acted for and the code's SHA-256; never the code, input or output.
- **Where:** each run claims a fresh sandbox from the platform's warm pool and deletes it afterwards;
  a sandbox runs one job. The platform's SandboxTemplate runs it under gVisor with no network (no
  egress, no DNS), no service account token, a read-only root, a size-limited work directory, CPU and
  memory limits and a deadline.
- **Inside:** the runner adds rlimits (CPU, memory, file size, open files, processes, no core
  dumps), an empty environment, Python's isolated mode, a wall-clock kill of the whole process group,
  capped output and only plain files from `out/` back.
- **Limits:** per-caller rate and concurrency, replica concurrency, code, input and output sizes.
- The `local` executor (development) has the rlimits but no network isolation, and refuses to start
  in staging or production.
