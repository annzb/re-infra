# Identity

The Cognito user pools, app clients and hosted UI domains for `prod`, `staging`,
`dev` and `preview` are declared in [`platform.yaml`](platform.yaml) and owned by the
`rc-platform` stack.

They live in the platform stack rather than the per-environment stack because they are
**shared**: all ten preview environments (`preview1`–`preview8`, `preview67`, `preview89`)
sign in against the single `preview` pool. There are four pools for thirteen environments,
so no environment owns one. Which pool an environment uses is recorded as its
`identity` profile in [`envs.yaml`](../envs.yaml).

## These pools were adopted, not created

They hold real user accounts — prod had 7,039 at the time of adoption. They were brought
under CloudFormation with a one-time import (see README section 8), and their properties in
`platform.yaml` were generated from the live pools with
`aws cloudformation create-generated-template`, not written by hand.

Two rules follow, and neither has changed since this file first recorded them:

- **A pool must never be recreated.** Recreating one loses every account in it. Both
  `DeletionPolicy` and `UpdateReplacePolicy` are `Retain`, but those only stop the
  *deletion*; they do not stop CloudFormation from building a replacement and pointing
  everything at an empty pool.
- **Changes that force replacement must be caught in review.** `Schema`,
  `UsernameAttributes` and `AliasAttributes` are the replacement-forcing properties.
  `rc-infra apply` prints `(replacement: True)` against any resource a change set would
  replace — never apply a plan that says that about a pool. To add a custom attribute,
  add it to the live pools first and then record it here.

## What is deliberately not declared

The OIDC identity providers — Discord, Google, LinkedIn and Sign in with Apple — are
configured on all four pools but are **not** in the template.
`AWS::Cognito::UserPoolIdentityProvider` carries the provider's client secret in
`ProviderDetails`, and nothing secret goes into a template or into `envs.yaml`. They stay
managed outside this repository. The app clients' `SupportedIdentityProviders` lists name
them, which is safe: those are names, not credentials.

The app clients themselves generate no client secret (they are public mobile and web
clients), so they are declared in full.

## The trigger Lambdas belong to `retribalize-core`

Each pool's `LambdaConfig` names a `PostConfirmation` and a `UserMigration` trigger — for
example `rc-prod-post-signup:live` and `rc-prod-user-migration`. Those functions are
application code: they live in `retribalize-core` under
`backend/src/functions/user/{post-signup,user-migration}/`, depend on its shared Lambda
layer, write its `rc-<env>-users` table, and read its Supabase secrets. They stay there.

This template references them by literal ARN, so the dependency is one-way and textual:
nothing here builds or deploys them. If a function is renamed in `retribalize-core`, the
ARN here must be updated to match, and that is the only coupling.

Note that the `preview` pool's triggers point at `rc-preview1-*`. All ten preview
environments share that pool, so they all run preview1's trigger functions.

## Who owns the trigger Lambdas

Each pool's `LambdaConfig` is declared here, in `platform.yaml`, naming the functions by ARN:

| Owned by re-infra | Owned by `retribalize-core` |
|---|---|
| The user pools, clients and domains | The trigger Lambda functions and their `live` aliases |
| Each pool's complete `LambdaConfig` | The `AWS::Lambda::Permission` letting Cognito invoke them |

**The contract between the repositories is the function and alias names** —
`rc-<slot>-post-signup:live` and `rc-<slot>-user-migration`. Renaming either in core is a breaking
change that must be mirrored here in the same change.

`AWS::Lambda::Permission` belongs in core because it edits the *function's* resource policy, not the
pool: it is a property of the thing being invoked, so it lives with the thing being invoked.

`LambdaConfig` is deliberately kept here rather than being set by core after deployment. Cognito's
`UpdateUserPool` rewrites the whole pool, so a template that omitted `LambdaConfig` would silently
clear the triggers the next time any other pool property changed. Keeping it in the template means
the template is the source of truth and there is nothing to clear.

One ordering consequence: adding a **new** identity profile requires its trigger functions to exist
before the pool is written, because Cognito validates the ARNs. Existing profiles are unaffected —
their functions are already deployed. Note that the `preview` pool's triggers point at
`rc-preview1-*`, so every preview slot runs preview1's trigger functions.
