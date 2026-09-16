# Environment catalog reference

`catalog.yaml` is the complete list of Retribalize deployment environments. How to
add or remove one is in the [root README](../README.md); this file only describes
the fields.

> **Removing an entry deletes that environment** when the change reaches `main`:
> its app stack, its schema-sync tables, its buckets and all their data.
> `prod`, `staging`, and `dev` can never be removed.

Never commit secret values here. Cognito pool and client IDs are identifiers, not
secrets; API keys, client secrets, and tokens belong in Secrets Manager.

## Top-level fields

| Field | Required | Description |
|---|---|---|
| `schema_version` | yes | Must be `1`. |
| `account_id` | yes | 12-digit AWS account. `rc-infra` refuses to run with credentials for any other account. |
| `region` | yes | Region for every stack, e.g. `us-east-1`. |
| `identity_profiles` | yes | Named Cognito identifier sets: `user_pool_id`, `client_id`, `domain`. |
| `environments` | yes | Map of environment name to its settings (usually `{}`). |

## Environment fields

| Field | Default | Description |
|---|---|---|
| `identity` | the profile with the same name as the environment, otherwise `preview` | Which `identity_profiles` entry the environment uses. |

Unknown fields are rejected, so a typo fails validation instead of being ignored.

## Names

Environment names are lowercase letters and digits, start with a letter, and are at
most 20 characters (`^[a-z][a-z0-9]{1,19}$`). No hyphens: `rc-<name>-` must be an
unambiguous prefix.

Everything else is derived from the name. For `preview3` in account `273268178059`:

| Resource | Name |
|---|---|
| Core stack (this repo) | `rc-env-preview3` |
| App stack (retribalize-core) | `rc-app-preview3` |
| DynamoDB table prefix (rc-dynamo-sync) | `rc-preview3-` |
| Buckets | `rc-preview3-{embeddings,user-corpus,avatars,recordings,schema-dumps}-273268178059` |
| SSM parameters | `/rc/env/preview3/...` |

## Published SSM parameters

Each `rc-env-<name>` stack writes these `String` parameters (and the same values as
stack outputs). retribalize-core reads them instead of keeping its own mapping.

| Parameter | Value |
|---|---|
| `/rc/env/<name>/region` | AWS region |
| `/rc/env/<name>/table-prefix` | `rc-<name>-` |
| `/rc/env/<name>/app-stack-name` | `rc-app-<name>` |
| `/rc/env/<name>/bucket/<purpose>` | bucket name for `embeddings`, `user-corpus`, `avatars`, `recordings`, `schema-dumps` |
| `/rc/env/<name>/cognito/profile` | identity profile name |
| `/rc/env/<name>/cognito/user-pool-id` | Cognito user pool ID |
| `/rc/env/<name>/cognito/client-id` | Cognito app client ID |
| `/rc/env/<name>/cognito/domain` | Cognito hosted UI domain |

## Lifecycle

```text
declared in catalog.yaml ──merge──▶ rc-env-<name> created (or existing buckets imported)
removed from catalog.yaml ──merge──▶ app stack, tables, buckets, core stack deleted
```

Protected environments (`prod`, `staging`, `dev`) have CloudFormation termination
protection, retained buckets, and an explicit IAM deny on deletion, in addition to
the validator refusing a catalog without them.
