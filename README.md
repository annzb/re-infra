# re-infra

The `main` branch of this repository is the authoritative list of Retribalize
deployment environments. It manages the shared platform resources, each
environment's core resources (buckets and published configuration), and the common
Python 3.11 Lambda base image with the reusable DynamoDB framework.

Application code, application table declarations, the application SAM template, and
releases stay in `retribalize-core`.

## Prerequisites

- Python 3.11 (pinned in `.python-version`; `uv` installs it if missing)
- [`uv`](https://docs.astral.sh/uv/)
- Docker with Buildx (for the base image and LocalStack tests)
- AWS CLI with an SSO profile for account `273268178059`, only for `rc-infra plan`

`pyproject.toml` and `uv.lock` are committed, so no `uv init` is needed.

## First-time local setup

```bash
uv sync --frozen
uv run rc-infra validate
uv run pytest
```

## Add or change an environment

1. Edit [`envs.yaml`](envs.yaml) (field reference:
   [Environment configuration](#environment-configuration-envsyaml) below). A new
   preview is one line: `preview12: {}`.
2. Check it locally:

   ```bash
   uv run rc-infra validate
   aws sso login   # plan reads AWS; it never changes anything
   uv run rc-infra plan
   ```

3. Push the branch. The **Infrastructure plan (read-only)** job summary shows every
   `CREATE`, `IMPORT`, `UPDATE`, `DELETE`, and `BLOCKED` action, and applies nothing.
4. Merge to `main`. The same run continues into `deploy-envs.yml`, which applies the
   plan; the new `rc-env-<name>` stack then publishes its SSM parameters.

This does not deploy the application. Deploy `retribalize-core` into the new
environment separately.

## Remove an environment

1. Delete its entry from `envs.yaml`.
2. Push the branch and read the plan: it lists the app stack, tables, and buckets
   that will be **deleted with their data**.
3. Merge to `main`. `deploy-envs.yml` deletes, in order: `rc-app-<name>`, tables tagged
   `ManagedBy=rc-dynamo-sync` and `Environment=<name>`, the environment's buckets
   (emptied first), and finally `rc-env-<name>`. If it fails midway, re-run the
   workflow; teardown continues where it stopped.

`prod`, `staging`, and `dev` cannot be removed. The validator rejects an `envs.yaml`
without them, their stacks have termination protection, and the deploy role has an
explicit IAM deny on deleting their stacks, buckets, and tables.

Untagged tables with the environment's prefix are listed as skipped and left alone.
Legacy app stacks named `rc-<name>` are not deleted.

## Environment configuration (`envs.yaml`)

[`envs.yaml`](envs.yaml) in the repository root is the complete list of Retribalize
deployment environments. Adding and removing entries is covered above; this section
describes the fields.

> **Removing an entry deletes that environment** when the change reaches `main`:
> its app stack, its schema-sync tables, its buckets and all their data.
> `prod`, `staging`, and `dev` can never be removed.

Never commit secret values here. Cognito pool and client IDs are identifiers, not
secrets; API keys, client secrets, and tokens belong in Secrets Manager.

### Top-level fields

| Field | Required | Description |
|---|---|---|
| `schema_version` | yes | Must be `1`. |
| `account_id` | yes | 12-digit AWS account. `rc-infra` refuses to run with credentials for any other account. |
| `region` | yes | Region for every stack, e.g. `us-east-1`. |
| `identity_profiles` | yes | Named Cognito identifier sets: `user_pool_id`, `client_id`, `domain`. |
| `environments` | yes | Map of environment name to its settings (usually `{}`). |

### Environment fields

| Field | Default | Description |
|---|---|---|
| `identity` | the profile with the same name as the environment, otherwise `preview` | Which `identity_profiles` entry the environment uses. |

Unknown fields are rejected, so a typo fails validation instead of being ignored.

### Names

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

### Published SSM parameters

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

### Lifecycle

```text
declared in envs.yaml ──merge──▶ rc-env-<name> created (or existing buckets imported)
removed from envs.yaml ──merge──▶ app stack, tables, buckets, core stack deleted
```

Protected environments (`prod`, `staging`, `dev`) have CloudFormation termination
protection, retained buckets, and an explicit IAM deny on deletion, on top of the
validator refusing an `envs.yaml` without them.

## Add or update a base-image dependency

```bash
cd images/lambda-base
uv add "<package><version-constraint>"   # e.g. uv add "requests>=2.32,<3"
uv lock --check
uv run pytest
```

Upgrade or remove a package:

```bash
uv lock --upgrade-package <package>
uv remove <package>
```

`pytest`, linters, and other test-only tools belong in the `dev` dependency group
(`uv add --dev <package>`). They are never installed into the image.

## Change the common Python / DynamoDB code

1. Edit `images/lambda-base/src/rc_lambda_base/`.
2. Add or update tests in `images/lambda-base/tests/`.
3. Run the unit tests and the LocalStack integration suite:

   ```bash
   cd images/lambda-base
   uv run pytest tests/unit

   docker compose up -d --wait          # LocalStack on :4566
   AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test \
     AWS_SECRET_ACCESS_KEY=test AWS_REGION=us-east-1 \
     uv run pytest tests/integration
   docker compose down
   ```

   The integration suite refuses to run unless it is pointed at LocalStack with
   those `test` credentials, so it can never touch a real table.

4. Push. `build-base-images.yml` builds the image and tests it on every branch; only
   on `main` does its `publish` job push to ECR. Existing application images are
   unchanged until `retribalize-core` rebuilds from the new digest.

## Build the base image locally

```bash
docker buildx build --platform linux/amd64 --load -t rc-lambda-base:local images/lambda-base

# Smoke test: package, Dynamo CLI, and Python version
docker run --rm --platform linux/amd64 --entrypoint python rc-lambda-base:local \
  -c "import sys, rc_lambda_base, rc_lambda_base.dynamo.cli; assert sys.version_info[:2] == (3, 11)"
docker run --rm --platform linux/amd64 --entrypoint rc-dynamo-sync rc-lambda-base:local --help
```

The image is a parent image with no `CMD`; service images add their handler.

## What runs what

A push is the only trigger. `validate.yml` is the entry point and calls the other two
workflows: nothing runs on a pull request, and nothing is started by hand.

**Every push, on any branch**

```text
.github/workflows/validate.yml
  1. validate -> ruff / mypy / pytest   -> src/rc_infra/**, tests/**
                 cfn-lint infra/*.yaml  -> roles.yaml, platform.yaml, environment.yaml
                 rc-infra validate      -> cli.py -> env_config.py -> envs.yaml
  2. plan     -> rc-infra plan, with the read-only role
                 -> planner.py -> cfn.py | buckets.py | tables.py,
                    reading infra/platform.yaml and infra/environment.yaml
                 -> plan in the run summary; fails the run on BLOCKED
  3. changes  -> did images/lambda-base/** change?
  4. build-base-images.yml
       a. build -> images/lambda-base/Dockerfile -> image artifact
       b. test  -> pytest tests/unit and tests/integration (LocalStack),
                   then smoke tests on the built image
                   (its publish job is skipped off main)
```

**Push to `main`, in the same run**

```text
  5. deploy-envs.yml -> rc-infra apply --yes -> apply.py
       a. infra/platform.yaml    -> stack rc-platform
       b. infra/environment.yaml -> stack rc-env-<name>, per envs.yaml entry
                                    (create, import existing buckets, or update)
       c. teardown.py            -> for environments removed from envs.yaml:
                                    rc-app-<name> -> tables -> buckets -> rc-env-<name>
  6. build-base-images.yml
       c. publish -> ECR build tag -> scan -> latest -> SSM /rc/lambda-base/image-uri
                     (only when images/lambda-base/** changed)
```

`deploy-envs.yml` deploys infrastructure only. Application stacks (`rc-app-<name>`)
are never touched here; `retribalize-core` owns them.

**Manual, once:** `infra/roles.yaml` -> stack `rc-bootstrap` (the three IAM roles).

**Elsewhere:** `retribalize-core` deploys `rc-app-<name>`, reading `/rc/env/<name>/*`
and building its images from the published base-image digest.

## Stacks and ownership

| Stack | Owner | Contents |
|---|---|---|
| `rc-bootstrap` | this repo, deployed manually once | GitHub OIDC roles: `rc-infra-plan`, `rc-infra-deploy`, `rc-infra-cfn-exec` |
| `rc-platform` | this repo | ECR repository `rc-lambda-base` |
| `rc-env-<name>` | this repo | Buckets and `/rc/env/<name>/*` SSM parameters |
| `rc-identity` | future | Shared Cognito resources (see [`infra/identity.md`](infra/identity.md)) |
| `rc-app-<name>` | `retribalize-core` | The application SAM stack |

DynamoDB tables are owned by `rc-dynamo-sync`, run from `retribalize-core`, never by
CloudFormation.

## For retribalize-core

- Read environment configuration from `/rc/env/<name>/*` in SSM, or from the
  `rc-env-<name>` stack outputs. A missing stack means the environment is not
  declared; refuse to deploy.
- Build service images `FROM` the digest in `/rc/lambda-base/image-uri`, recorded in
  the build manifest. Never build from `latest`.
- Export `TABLES: Mapping[str, BaseTable]` from the schema module and run
  `rc-dynamo-sync --schema-module <module> --environment <name> --apply`.

## One-time setup

1. With administrator credentials, deploy the bootstrap stack:

   ```bash
   aws cloudformation deploy \
     --stack-name rc-bootstrap \
     --template-file infra/roles.yaml \
     --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides GitHubOrg=annzb GitHubRepo=re-infra
   aws cloudformation update-termination-protection \
     --stack-name rc-bootstrap --enable-termination-protection
   ```

   Pass `CreateOidcProvider=true` only if the account has no GitHub OIDC provider yet.

2. Add repository **variables** (not secrets) from the stack outputs:
   `AWS_PLAN_ROLE_ARN`, `AWS_DEPLOY_ROLE_ARN`, `AWS_CFN_EXEC_ROLE_ARN`.
3. Protect `main`: require the `Lint, test, validate` and `Infrastructure plan
   (read-only)` checks (they report on every push, including a pull request's branch)
   plus CODEOWNERS review; block force pushes and deletion.
4. Before the first merge that applies, run `uv run rc-infra plan` locally. Existing
   `prod`/`staging`/`dev` buckets appear as `IMPORT`, or as `BLOCKED` with the exact
   differences between the live bucket and the template. Change the **template** to
   match the live bucket, not the other way around, until nothing is blocked.

## Troubleshooting

- **Workflow logs:** GitHub → Actions → *Validate*. Every job runs there, including
  the called *Deploy environments* and *Build base images* workflows, and the plan is
  in the run summary.
- **Retry a failed deploy:** open the run and press **Re-run jobs**, or push again.
  `rc-infra apply` is idempotent and re-plans from the current state.
- **See the plan without applying:** `uv run rc-infra plan` (or `apply` without
  `--yes`).
- **Inspect a stack:**
  `aws cloudformation describe-stacks --stack-name rc-env-<name>` and
  `aws ssm get-parameters-by-path --path /rc/env/<name>/ --recursive`.
- **BLOCKED stack status:** a stack is mid-operation or in a `*_FAILED` state. Wait,
  or fix it in CloudFormation, then re-run the workflow.
- **Roll back the base image:** pin the previous digest in `retribalize-core`. Build
  tags are immutable; list them with
  `aws ecr describe-images --repository-name rc-lambda-base`.
- Do not repair stack-owned resources by hand in the AWS console. Change the template
  or `envs.yaml` and let `deploy-envs.yml` apply it.
