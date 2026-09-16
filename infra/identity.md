# Identity (not managed yet)

The Cognito user pools, app clients, hosted UI domains, and identity providers for
`prod`, `staging`, `dev`, and `preview` are **managed outside this repository** for
now. Their non-secret identifiers are recorded under `identity_profiles` in
[`environments/catalog.yaml`](../../environments/catalog.yaml) and published to each
environment's SSM parameters.

A future `rc-identity` stack will own the shared Cognito pools, clients, domains,
and adjacent resources. When it is added:

- Existing pools must be **imported** into the stack with `DeletionPolicy: Retain`
  and `UpdateReplacePolicy: Retain`. They must never be recreated; recreating a pool
  loses every user account.
- Changes that force replacement (for example, pool schema attributes) must be
  caught in review before merge.
- Client secrets stay in Secrets Manager; nothing secret goes into a template or the
  catalog.

There is intentionally no template in this directory until that work starts.
