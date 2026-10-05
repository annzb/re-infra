# Secrets

Secret **values** never enter this repository, a template parameter, or a stack output. They
are supplied by an operator or a provider console into Secrets Manager, and templates read
them through dynamic references (`{{resolve:secretsmanager:<name>:SecretString:<key>}}`) or
CodeBuild `SECRETS_MANAGER` variables. This file is the inventory of names, from the
read-only audit of 2026-10-05 (`scripts/aws-audit.sh`, metadata only).

## Naming contract

- New secrets are named `rc-<owner>/<purpose>`, where `<owner>` is the stack or package that
  reads them (e.g. `rc-platform/<purpose>`), and hold a JSON object whose keys are documented
  next to the reader.
- A secret's container may be created by an operator or a re-infra command, never with a value
  in source control. A template never creates a secret with a placeholder value that something
  could start using.
- Per-environment secrets carry the environment as a suffix (`…-prod`, `…-staging`); shared
  ones carry none.
- Renaming an application secret is `retribalize-core`'s change: until then the existing names
  below stay as they are, and nothing in re-infra reads them.

## Read by re-infra

None. The deployed stacks (`rc-platform`, `rc-env-*`) reference no secret. The unused
templates would: `infra/identity.yaml` reads the OAuth provider secrets from
`rc-identity/oauth-providers` (which does not exist), and `infra/pipeline.yaml` reads
`rc-vercel-token`.

## Read by retribalize-core (not managed here)

Referenced by core's `backend/template-v2.yaml` or its Lambda environment. Grouped by provider;
`-prod`/`-staging`/`-preview…` variants exist where the provider has separate projects.

| Provider | Secrets |
|---|---|
| Anthropic, OpenAI, Voyage | `tribalize-anthropic-key`, `tribalize-openai-key`, `tribalize-voyage-key` |
| Datadog | `DdApiKeySecret-…` (stack-generated), `tribalize-datadog-api-key` |
| Daily.co | `tribalize-daily-key` |
| LiveKit | `tribalize-livekit-{key,secret}[-prod,-staging,-preview89]`, `tribalize-livekit-egress-{key,secret}` |
| Stripe | `tribalize-stripe-key-{live,test}`, `tribalize-stripe-webhook-secret-{prod,staging,dev,preview,preview67,preview89}` |
| Email / marketing | `tribalize-resend-api-key`, `tribalize-ghl-signup-webhook-url[-staging]`, `tribalize-kit-api-key-test`, `tribalize-kit-webhook-secret-test` |
| Supabase (user migration only) | `tribalize-prod-supabase-key`, `tribalize-dev-supabase-key` |
| Expo | `tribalize-expo-access-token` (only in a commented-out line of core's template; not in the account) |

No recorded access in the audit: `anthropic-key`, `supabase-key`, `matching-app-dev-*`,
`tribalize-dev-anthropic-key`, `tribalize-prod-email-key`. Older names that now have
per-environment variants, possibly superseded: `tribalize-stripe-key`,
`tribalize-stripe-test-key`, `tribalize-stripe-webhook-secret`, `tribalize-livekit-{key,secret}`,
`tribalize-supabase-key`. `tribalize-{prod,dev}-oauth` hold OAuth client IDs and secrets for
Google, Discord and LinkedIn (no Apple key). The pools hold their own copies; these
containers are not read by anything in re-infra. Delete none of these from here: confirm with core first.

## Exposure

The Google, Discord and LinkedIn client secrets were committed to `retribalize-core`'s
history (`backend/scripts/create-cognito-pools.sh`, see `docs/OWNERSHIP.md`). Rotate them in
each provider console and update the identity providers on the live pools.
