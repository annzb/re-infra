# Identity

The Cognito user pools, app clients and hosted UI domains for the identity profiles in
[`envs.yaml`](../envs.yaml) — `prod`, `staging`, `dev` and `preview` — are declared in
[`identity.yaml`](identity.yaml) and owned by the `rc-identity` stack.

They live in their own stack rather than the per-environment stack because they are
**shared**: every preview environment signs in against the single `preview` pool, so no
environment owns one. Which pool an environment uses is its `identity` profile in `envs.yaml`
(by default the profile of the same name, otherwise `preview`).

## A fresh generation

These pools are created by this repository, not adopted. Their names
(`rc-<profile>-user-pool-v3`) and hosted UI domain prefixes (`rc-<profile>-v3`) are new, so
`rc-identity` can be created while the pools of earlier generations still exist and still
serve users. Domain prefixes are globally unique, which is why they are chosen here rather
than generated.

Pool IDs, client IDs and the full OAuth domain are **outputs**, never configuration:

```bash
uv run rc-infra outputs --environment dev   # .identity.user_pool_id, .client_id, .oauth_domain
```

Moving users from an earlier generation is not something a deployment does. Cognito does not
expose passwords, `sub` values change in a new pool, and federated identities relink on first
sign-in, so how existing users keep access is an open decision (bulk copy with a forced reset,
or a temporary migrate-on-first-login trigger) and is handled outside `rc-infra`.

## Rules

- **A pool must never be replaced.** Replacing one creates an empty pool and strands every
  account in the old one. `DeletionPolicy` and `UpdateReplacePolicy` are `Retain`, but those
  only stop the *deletion*. `rc-infra apply` refuses any plan that would replace or remove a
  pool, client or domain, with no override.
- **`Schema`, `UsernameAttributes` and `AliasAttributes` force replacement.** To add a custom
  attribute to a deployed pool, add it to the live pool first, then record it here.

## What is deliberately not declared yet

- **Triggers.** The pools have no `LambdaConfig`. The post-confirmation and user-migration
  functions are application code in `retribalize-core`, which deploys them, with the
  `AWS::Lambda::Permission` that lets Cognito invoke them, against these pools' outputs. Only
  then can a pool point at them, because Cognito validates the ARNs. Wiring them is a later,
  deliberate update of this stack (for example, function ARNs passed as stack parameters).
  Cognito's `UpdateUserPool` rewrites the whole pool, so once wired, `LambdaConfig` must stay
  in this template or the next unrelated change would clear it. For the shared `preview` pool,
  one slot must be chosen to supply the canonical trigger functions.
- **Identity providers.** `AWS::Cognito::UserPoolIdentityProvider` carries the provider's
  client secret, and nothing secret goes into a template. Until the providers can take their
  secrets from Secrets Manager, the app clients support `COGNITO` only, since listing a
  provider that does not exist on the pool would fail the create.

The app clients generate no client secret (they are public mobile and web clients), so they
are declared in full.
