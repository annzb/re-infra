# rc_identity

> **Comment from LLM (plan2 rollback, 2026-10-05): not used.** Identity stays in
> `retribalize-core`; re-infra only references the existing Cognito pools by ID
> (`envs.yaml`). This package is kept in case identity moves here later. No workflow
> builds or tests it, nothing depends on it, and `infra/identity.yaml`, which would
> deploy its account directory table, is not deployed either.

An account directory (`(issuer, sub)` -> stable account ID, in DynamoDB), Cognito
PostConfirmation and PreTokenGeneration handlers that maintain it, a strict Cognito JWT
validator, and `rc-identity-backfill`.

```bash
cd python_packages/rc_identity
uv sync && uv run pytest && uv run ruff check src tests && uv run mypy
```
