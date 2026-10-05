# re-infra

The `main` branch of this repository is the source of truth for Retribalize deployment environments and shared deployment infrastructure. It owns:

- the environment configuration in [`envs.yaml`](envs.yaml);
- shared platform infrastructure;
- one core infrastructure stack per environment;
- the common Python 3.11 Lambda base image and reusable DynamoDB tooling.

Application code, application-specific table declarations, service images, the SAM template, and application releases remain in `retribalize-core`. [`docs/OWNERSHIP.md`](docs/OWNERSHIP.md) classifies every resource in both repositories by owner and lifecycle.

## 1. Prerequisites

### Local tools

- Python 3.11, pinned by [`.python-version`](.python-version). `uv` can install it when missing.
- [`uv`](https://docs.astral.sh/uv/).
- Docker with Buildx, and **Docker Compose 2.37.3 or newer**, for local base-image builds
  and LocalStack tests. The floor is not cosmetic: the test image takes the base image
  through a `service:` build context, which Compose only resolves from 2.33.0, only
  handles correctly without Bake from 2.34.0, and only exports build caches under Bake
  from 2.37.3. An older Compose silently treats `service:base` as a *directory name*, and
  the build fails with `failed to get build context rc_dynamo_base: stat .../service:base:
  no such file or directory`. If you see that, update Docker Desktop.
- AWS CLI with credentials for account `273268178059` when running an AWS-backed dry run. The normal local option is an AWS SSO profile.

The repository already contains `pyproject.toml` and `uv.lock`; do not run `uv init` after cloning it.

### GitHub repository variables

Configure these under **Settings â†’ Secrets and variables â†’ Actions â†’ Variables**. They are identifiers, not secrets.

| Variable | Example/source | Used for |
|---|---|---|
| `AWS_REGION` | `us-east-1`; must match `envs.yaml` | Region used by the workflows that contact AWS. |
| `AWS_ROLE_GITHUB` | ARN of the role created manually by an administrator (section 9) | The only GitHub OIDC role. Every branch assumes it to publish its image; `main` additionally uses it for infrastructure applies. |

CloudFormation is not given a service role: it acts with the credentials of whoever calls it, which in the workflow is `AWS_ROLE_GITHUB`. That role therefore needs every permission a deploy or a teardown requires.

### Local environment variables

| Variable | Required? | Purpose |
|---|---|---|
| `AWS_PROFILE` | Optional | Selects a local AWS CLI/SDK profile, commonly an SSO profile. |
| `AWS_REGION` | Optional locally | Useful for direct AWS CLI commands and required by the LocalStack test command. `rc-infra` itself reads the target region from `envs.yaml`. |

`AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID=test`, and `AWS_SECRET_ACCESS_KEY=test` are used only for the guarded LocalStack integration suite described below.

## 2. Pipeline execution order

A push is the only trigger, and exactly one entry point runs: [`main.yml`](.github/workflows/main.yml) for `main`, [`non-main.yml`](.github/workflows/non-main.yml) for every other branch. Pull-request events, manual dispatch and tag pushes start nothing. The entry points only call reusable workflows; all the work lives in the three workflows they call.

### Every branch except main — `non-main.yml`

1. **Validate** ([`validate.yml`](.github/workflows/validate.yml)) checks the lockfile (`uv lock --check`), installs the root project from it, runs Ruff, mypy, and pytest, runs `cfn-lint` on `infra/*.yaml`, and runs `rc-infra validate` on `envs.yaml`.
2. **Deploy images** ([`deploy-images.yml`](.github/workflows/deploy-images.yml)) starts after validation. In a single job it builds the image, runs the `rc_dynamo` test pipeline against it through [`compose-build-test.yaml`](python_packages/rc_dynamo/compose-build-test.yaml), and pushes it to `rc-lambda-base:<branch>`. The repository URI is read from the `rc-platform` stack's `LambdaBaseImageRepositoryUri` output, so a branch fails at that step until `main` has deployed `rc-platform` once.

A branch run reaches AWS only to push its own image tag; it never deploys infrastructure, and a branch deletion is skipped rather than rebuilt. To see what a change would do to live infrastructure, run a local dry run (section 3).

### Pushes to main — `main.yml`

1. **Validate** — the same workflow, unchanged.
2. **Deploy infrastructure** ([`deploy-infra.yml`](.github/workflows/deploy-infra.yml)) starts after validation and applies `envs.yaml` together with `infra/*.yaml`.
   - It assumes `AWS_ROLE_GITHUB` through GitHub OIDC, and CloudFormation acts with that role's credentials.
   - It runs `rc-infra apply --yes`, which prints the plan, refuses it if anything is `BLOCKED`, and otherwise creates, updates, or removes infrastructure until AWS matches `envs.yaml`, then prints which durable resources are still empty. It does not repeat the validation stage's checks.
3. **Deploy images** starts only after the infrastructure deployment finishes successfully.
   - The same single job runs.
   - Because the branch is `main`, it pushes `rc-lambda-base:main`. This happens on every main push; Docker skips layers the registry already holds, so re-pushing an unchanged image costs almost nothing.

One `docker compose build` produces the base image and the test image as a single linked build graph, and the push comes after the suite passes, so an untested image can never reach ECR - the suite runs against exactly the bits that get pushed.

### Workflow responsibilities

| Workflow | Trigger | Responsibility | AWS access |
|---|---|---|---|
| `main.yml` | push to `main` | Entry point; orders validate, infrastructure, images | None of its own; grants OIDC to the jobs it calls. |
| `non-main.yml` | push to any other branch | Entry point; validation and image build/test only | None. |
| `validate.yml` | `workflow_call` | Lint, type-check, test, `cfn-lint`, `rc-infra validate` | None. |
| `deploy-infra.yml` | `workflow_call` | Applies `infra/*.yaml` for `envs.yaml`: reconciles `rc-platform` and `rc-env-*`, and tears down environments removed from `envs.yaml` | Main only, using the GitHub role. |
| `deploy-images.yml` | `workflow_call` | Builds, tests and publishes the Lambda base image in one job | Every branch pushes its own tag using the GitHub role. |

A called workflow must never declare the same concurrency group as its caller: GitHub reports that as a deadlock and cancels the run. Serialization therefore lives entirely in the entry points, which own `re-infra-<ref>`; `main.yml` sets `cancel-in-progress: false` so an apply already in flight is never cancelled, while `non-main.yml` replaces superseded branch runs. None of the three called workflows declares a group of its own.

Moving this orchestration into AWS (CodeBuild, CodePipeline, EventBridge or similar) is **deferred — plan TBD**. The GitHub workflows above remain the only deployment mechanism until a separate design replaces them.

### Stack ownership

| Stack/resource | Owner | Contents |
|---|---|---|
| `rc-platform` | This repo | The shared image repositories (`rc-lambda-base`, `rc-api-v2`, `rc-matching-v2`) and the shared Lambda execution role. |
| `rc-env-<name>` | This repo | The environment's six durable buckets, with CloudFormation-generated names. |
| `rc-app-<name>` | `retribalize-core` | Application-specific SAM/CloudFormation resources. |
| Cognito user pools, clients, domains, providers, triggers | `retribalize-core` / managed by hand | Not managed here. `envs.yaml` only references them by ID, and `rc-infra outputs` passes those IDs on. |
| DynamoDB application tables (`rc2-<name>-*`) | `rc-dynamo-sync` from `retribalize-core` | Application table schemas and migrations; not owned by these CloudFormation templates. |

The GitHub OIDC role is deliberately absent from that table: it is provisioned manually (section 9) and owned by no stack here, because the role that deploys the infrastructure cannot also be deployed by it. `apply` deploys `rc-platform` first, then the environments, and only ever updates a stack whose tags say re-infra owns it.

> **Comment from LLM:** `infra/identity.yaml` (Cognito), `infra/pipeline.yaml` (CodeBuild) and `python_packages/rc_identity` are kept in the repository in case they are wanted later, but are deliberately ignored for simplicity: `rc-infra apply` never deploys them and no workflow builds them. Identity stays with `retribalize-core`; images may all be built in GitHub Actions.

Every physical identifier AWS generates — bucket names, the role ARN, repository URIs — is a stack output, never configuration. `rc-infra outputs` prints them (section 4).

This repository does not deploy application code. A successfully created `rc-env-<name>` stack only makes the environment available for a later `retribalize-core` deployment.

## 3. Deployment environments

[`envs.yaml`](envs.yaml) is the complete desired list of environments. Because `main` is authoritative, adding an entry creates infrastructure and removing an entry tears that environment down when the change reaches `main`.

> **Warning:** removing an environment deletes its buckets and bucket data, followed by its `rc-env-<name>` stack. re-infra never deletes `retribalize-core`'s `rc-app-<name>` stack or tables: while either still exists, the deletion is `BLOCKED`. `prod`, `staging`, and `dev` are protected from removal by validation, termination protection, and explicit IAM denies.

Every entry is its own persistence lineage: its own `rc-env-<name>` stack and six buckets, and its own `rc2-<name>-*` tables once schema sync creates them. Environments share nothing durable; only an identity profile can be shared (all previews use `preview` by default). See [docs/OWNERSHIP.md](docs/OWNERSHIP.md#decisions).

Never commit secrets to `envs.yaml`, nor identifiers AWS generates: those are read back from stack outputs. API keys, tokens, and client secrets belong in Secrets Manager.

### Configuration fields

| Field | Required | Description |
|---|---|---|
| `schema_version` | Yes | Configuration schema version; currently must be `1`. |
| `account_id` | Yes | The 12-digit AWS account. AWS-backed commands refuse credentials for another account. |
| `region` | Yes | Region used for all managed stacks, for example `us-east-1`. |
| `identity_profiles.<profile>` | Yes | An existing Cognito pool referenced by its non-secret `user_pool_id`, `client_id` and hosted UI `domain`. Not managed by this repository. |
| `environments` | Yes | Map of environment names to environment settings. |
| `environments.<name>.identity` | No | Identity profile name. Defaults to a same-named profile when present, otherwise `preview`. |

Unknown fields are rejected. Environment names must match `^[a-z][a-z0-9]{1,19}$`: lowercase letters/digits, beginning with a letter, 2â€“20 characters, without hyphens.

### Add or change an environment

1. Edit `envs.yaml`. A preview using the default identity profile can be declared as one line:

   ```yaml
   preview12: {}
   ```

2. Validate and preview the result locally:

   ```bash
   uv run rc-infra validate
   aws sso login --profile <profile>
   AWS_PROFILE=<profile> uv run rc-infra apply
   ```

   Without `--yes`, `apply` is a dry run: it prints the plan and changes nothing.

3. Push the branch. Validation and the image build run; nothing touches AWS.
4. Merge to `main`. The main run applies `envs.yaml` and creates the environment's stack and buckets.
5. Deploy `retribalize-core` into the new environment separately.

### Remove an environment

1. Have `retribalize-core` delete the environment's `rc-app-<name>` stack and its tables.
2. Delete its entry from `envs.yaml`.
3. Run the dry run above locally and read the `DELETE` action and its inventory carefully. Branch runs have no AWS access, so this is the only preview before the change reaches `main`. It is `BLOCKED` while core's stack or tables still exist.
4. Merge to `main` only when the listed buckets and their data should be removed.

Teardown is idempotent and ordered: buckets, then the core environment stack. Only buckets the `rc-env-<name>` stack itself created are emptied and deleted.

### Names

For `preview3`:

| Resource | Name |
|---|---|
| Core stack | `rc-env-preview3` (derived) |
| Application stack | `rc-app-preview3` (derived) |
| DynamoDB table prefix | `rc2-preview3-` (derived; data generation 2, so it can never match the old `rc-preview3-*` tables; the tables belong to `rc-dynamo-sync`) |
| Buckets | Generated by CloudFormation as `rc-env-preview3-<logical id>-<suffix>`; read them with `rc-infra outputs --environment preview3` |

Only the first three are derived. Never reconstruct a bucket name: it carries a random suffix, which is what lets a fresh environment be created while an older generation of buckets still exists.

## 4. Infrastructure CLI

`uv sync` installs the `rc-infra` command from `src/rc_infra`. Every command validates the selected config first; every command except `validate` also checks that the AWS credentials belong to its `account_id`.

### `validate`

```bash
uv run rc-infra validate [--config PATH]
```

Validates config syntax, types, supported schema version, protected environments, naming rules, identity profiles, account/region formats, and derived environment definitions. It does not contact AWS or validate CloudFormation templates. The default path is `envs.yaml`.

### `apply`

```bash
uv run rc-infra apply [--config PATH] [--yes]
```

The command:

1. validates the config and verifies that the active AWS credentials belong to its `account_id`;
2. plans `rc-platform` and every declared `rc-env-<name>` stack: `CREATE` when the stack does not exist, otherwise a CloudFormation update change set, created and discarded, that shows the exact `UPDATE` (or `NOOP`);
3. inventories managed environments that exist in AWS but are absent from `envs.yaml` and reports their teardown as `DELETE`;
4. prints each target as `BLOCKED`, `DELETE`, `CREATE`, `UPDATE`, or `NOOP`, preceded by a warning banner when anything would be deleted.

Only the stacks themselves are inspected. The planner never looks for other resources that might already use a name: every name in the templates is either generated or new to this generation.

Without `--yes` it stops there: a dry run that modifies nothing. Building the plan is not literally API read-only — CloudFormation previews require temporary `CreateChangeSet` and `DeleteChangeSet` calls — but it never executes a change set.

With `--yes`, it applies in dependency order — `rc-platform`, then the environments, then deletions — and prints the `status` report below. If `rc-platform` or a protected environment fails, nothing after it is attempted; a failed preview is recorded and the others still apply. Any failure produces a nonzero exit code; an empty resource is never a failure. Persistent stacks end up with termination protection, including one whose protection was turned off while nothing else changed.

A `BLOCKED` action stops the whole apply only when it targets `rc-platform` or a protected environment (`prod`, `staging`, `dev`) — those are shared foundations, and applying anything on top of one nobody has looked at is not worth the speed. A blocked preview is skipped, counted as a failure, and everything else still applies. The plan output says which kind each one is. Besides a busy or broken stack, these are blocked:

- a stack of a managed name that exists but is not tagged as re-infra's: it is never taken over;
- a change that would **replace or remove** an IAM role, S3 bucket, ECR repository, DynamoDB table or Cognito resource. There is no override flag: a replacement would strand data behind its `Retain` policy and point consumers at an empty copy, and `main.yml` applies unattended, so this is not left to a reviewer to catch. If such a change is genuinely intended, make it by hand.

The replacement check runs on the preview and again on the change set apply actually executes, because the stack may have changed in between.

Routine applies belong in the protected main-branch workflow, not on developer machines.

### `status`

```bash
uv run rc-infra status [--config PATH] [--environment NAME] [--format text|json]
```

Read-only. For each environment's six buckets and its `ManagedBy=rc-dynamo-sync` tables, reports `EMPTY`, `NON-EMPTY`, or `NOT CREATED` (no stack yet, or schema sync has not created the tables). Emptiness is exact: a one-item listing or scan, never DynamoDB's approximate `ItemCount`, and a versioned bucket holding only old versions counts as non-empty.

```text
DATA STATUS
  dev / UserCorpusBucket        EMPTY
  dev / users                   EMPTY
Manual data transfer may be required for EMPTY durable resources.
```

The report never says where data should come from; that belongs to `rc-data-transfer` (section 5).

### `outputs`

```bash
uv run rc-infra outputs [--config PATH] [--environment NAME]
```

Read-only. Prints the deployment contract as JSON — every identifier `retribalize-core` needs, read from the `rc-platform` and `rc-env-<name>` stack outputs, plus the referenced Cognito identifiers from `envs.yaml`:

```json
{
  "schema_version": 2,
  "environment": "dev",
  "account_id": "273268178059",
  "region": "us-east-1",
  "data": {"generation": 2, "table_prefix": "rc2-dev-"},
  "buckets": {"user-corpus": {"name": "rc-env-dev-usercorpusbucket-…", "arn": "arn:aws:s3:::…"}, "...": {}},
  "identity": {"profile": "dev", "user_pool_id": "us-east-1_cbi939AdR", "issuer": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_cbi939AdR",
               "client_id": "…", "oauth_domain": "rc-dev-v2.auth.us-east-1.amazoncognito.com"},
  "platform": {"lambda_execution_role_arn": "…", "base_repository_uri": "…", "api_repository_uri": "…", "matching_repository_uri": "…"}
}
```

Without `--environment` it prints a list, one entry per environment. A missing or unhealthy stack, or a missing output, fails with exit code `1` rather than printing a guess. Adding a field keeps `schema_version`; removing or changing one bumps it.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Successful validation, apply, status or outputs. A dry-run apply also returns `0` when nothing is blocked. |
| `1` | Invalid config, account mismatch, blocked plan, failed apply, or outputs unavailable. |
| `2` | Invalid command-line usage reported by `argparse`. |

## 5. Manual data transfer

Fresh infrastructure starts empty. Copying data into it is a separate, manual step done with its own command, `rc-data-transfer`, which `rc-infra` never calls and no workflow may run (a test enforces both):

```bash
uv run rc-data-transfer table  --source <table ARN>  --target <table ARN>  --dry-run
uv run rc-data-transfer bucket --source <bucket ARN> --target <bucket ARN> --dry-run
uv run rc-data-transfer bucket --source <bucket ARN> --target <bucket ARN> --apply --manifest out/registry.jsonl --include-versions
uv run rc-data-transfer resolve-copy --source-layout legacy --source-env preview3 --source-resource UserCorpusBucket \
                                     --target-env preview3 --target-resource UserCorpusBucket --dry-run
```

It writes only into an empty target (or resumes into one holding exactly what its manifest recorded), refuses a table copy unless both configurations match exactly, and never modifies the source. A bucket copy replays every version and delete marker in order and records a manifest mapping each source version ID to the new one. See [`docs/DATA_TRANSFER.md`](docs/DATA_TRANSFER.md).

## 6. Reusable Python packages

`python_packages/` holds the reusable libraries this repository publishes. Each is a self-contained uv project with its own lockfile, Dockerfile and test pipeline, and its own README.

| Package | What it is |
|---|---|
| [`rc_dynamo`](python_packages/rc_dynamo/README.md) | A declarative DynamoDB layer - typed CRUD, index-aware queries, schema and table-settings (TTL, PITR, deletion protection, billing, class, streams) drift detection and migration - published as the Lambda parent image `rc-lambda-base`. |

### The `rc-lambda-base` Lambda parent image

`python_packages/rc_dynamo` also defines the parent image for Retribalize Python Lambda services. It centralizes slow, app-independent dependencies and reusable infrastructure code while leaving handlers and service-specific dependencies to child images.

The image contains:

- the AWS Lambda Python 3.11 base image, pinned by digest and built for `linux/amd64`;
- the Datadog Lambda Extension, disabled by default;
- locked production dependencies (`boto3` and `pydantic` currently);
- the `rc_dynamo` package, including the generic DynamoDB schema framework;
- the `rc-dynamo-sync` and `rc-dynamo-report` console commands.

It intentionally contains no Lambda handler/CMD, pytest, uv, source tests, or service-specific libraries - the test tooling is locked in a separate project under `tests/` and only ever enters the throwaway test image. Application Dockerfiles must inherit from an immutable digest, not from a branch tag: every tag moves. Resolve the current one from ECR - `aws ecr describe-images --repository-name rc-lambda-base --image-ids imageTag=main --query 'imageDetails[0].imageDigest' --output text` - and pin `<registry>/rc-lambda-base@<digest>`.

### Change reusable code

1. Edit `python_packages/rc_dynamo/src/rc_dynamo/`.
2. Update unit or integration tests under `python_packages/rc_dynamo/tests/`.
3. Run the checks - see [the package README](python_packages/rc_dynamo/README.md#2-local-development) for the dependency, venv and test-pipeline commands.
4. Push the branch. It is built, tested and published as `rc-lambda-base:<branch>`, so it can be pulled and tried before merging. Merging to `main` moves `rc-lambda-base:main`.

Every push republishes the tested image under a tag named after its branch, overwriting what that tag pointed at. There is no `latest`, and there are no per-build tags: a push to `main` moves `rc-lambda-base:main`, and the only immutable identity is the repository digest ECR assigns. ECR still scans on push and the findings are visible in the console, but no CI step fails on them.

Each image records where it came from. The OCI labels `org.opencontainers.image.revision` (the `re-infra` commit), `.version` (the `rc-dynamo` version) and `.created` are set by `deploy-images.yml`, and the same facts are in `/opt/rc-runtime.json` inside the image, so a child build can check what it inherits:

```bash
docker run --rm --entrypoint cat <registry>/rc-lambda-base@<digest> /opt/rc-runtime.json
```

The build fails if the declared version disagrees with the package actually installed.

Because tags move, the image a tag previously pointed at becomes untagged, and `rc-platform`'s lifecycle rule expires untagged images after **30 days**. That window is also the rollback window: refresh any digest pinned in `retribalize-core` within it, or the pin stops resolving.

## 7. Local setup and development

### First-time setup

```bash
uv sync --frozen
uv run rc-infra validate
uv run pytest
```

Run the same root checks used by validation:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest
uv run cfn-lint infra/*.yaml
uv run rc-infra validate
```

### Run an AWS-backed dry run locally

```bash
aws sso login --profile <profile>
export AWS_PROFILE=<profile>
uv run rc-infra apply        # no --yes: prints the plan, changes nothing
```

CloudFormation acts with your own credentials, so your local identity needs permission to inspect the resources involved and to create and delete preview change sets.

### Work on `rc_dynamo`

The package has its own venv, image and test pipeline. The whole gate - ruff, mypy, unit tests and the LocalStack integration suite - is one command, and it is exactly what CI runs:

```bash
cd python_packages/rc_dynamo
BUILD_CACHE_TO=type=inline docker compose -f compose-build-test.yaml build
docker compose -f compose-build-test.yaml run --rm tests
docker compose -f compose-build-test.yaml down -v
```

See [the package README](python_packages/rc_dynamo/README.md#2-local-development) for venv setup, dependency changes, and running individual checks without Docker.

### Local versus workflow-only behavior

| Operation | Local | GitHub workflow |
|---|---|---|
| Validate config/templates and run tests | Yes | Every push |
| Build the base image | Yes | Every push |
| Run LocalStack integration tests | Yes | Every push |
| Compare desired infrastructure with AWS | Yes, with suitable local AWS credentials | Main only, inside the infrastructure stage |
| Assume the GitHub OIDC role | No; its trust policy accepts only GitHub Actions tokens from this repository | Yes |
| Apply environment infrastructure | Technically possible with separately authorized local credentials, but not the normal path | Main only |
| Publish the tested image under its branch tag | Not reproduced by the documented local commands | Every push |

## 8. Integration contract with `retribalize-core`

`retribalize-core` should:

- resolve every identifier this repository owns from `rc-infra outputs --environment <name>` at deploy time — bucket names, the Cognito pool ID, client ID and OAuth domain, the shared Lambda execution role ARN, and the repository URIs — instead of hardcoding or reconstructing them, and refuse to deploy when that command fails;
- push API and matching images to `rc-api-v2` and `rc-matching-v2`, not the `rc-ecr` stack's `rc-api` and `rc-matching`;
- give every slot its own buckets: each `envs.yaml` environment, previews included, has its own `rc-env-<name>` stack and nothing is shared between slots;
- keep owning identity: the Cognito pools, clients, providers, trigger functions and their permissions. re-infra only passes the pool IDs from `envs.yaml` through `rc-infra outputs`;
- create its tables under `data.table_prefix` (`rc2-<name>-`) only, never under an earlier generation's names, and delete an environment's stack and tables before it is removed from `envs.yaml`;
- build service images from the immutable digest of `rc-lambda-base:main`, resolved from ECR rather than from a branch tag, and record that digest in its build manifest, refreshing it within 30 days of being superseded;
- export `TABLES: Mapping[str, BaseTable]` from its schema module and invoke `rc-dynamo-sync --schema-module <module> --environment <name> --apply`;
- own all `rc-app-<name>` application stacks and service releases.

## 9. One-time AWS and GitHub setup

1. With administrator credentials, create the GitHub OIDC role by hand. It is not declared in this repository: the role that deploys the infrastructure cannot be deployed by it, and keeping it out of the templates means a mistake here can never be applied automatically. Requirements:

   - **Trust policy:** `sts:AssumeRoleWithWebIdentity` federated through the account's `token.actions.githubusercontent.com` OIDC provider, with `aud` equal to `sts.amazonaws.com` and `sub` matching `repo:<org>/re-infra:ref:refs/heads/*`. Every branch may assume it, because `deploy-images.yml` publishes an image tag from every branch; only `main.yml` routes it into an infrastructure apply. Create the OIDC provider only if the account does not already have one — an account can hold just one.
   - **Permissions.** CloudFormation runs with this role's own credentials, so it needs everything a deploy or teardown touches: full management of the `rc-platform` and `rc-env-*` stacks, push access to the shared ECR repositories, enough S3 access to inspect, empty and delete `rc-env-*` buckets, and DynamoDB read access to report on `ManagedBy=rc-dynamo-sync` tables.
   - **Denies.** Add explicit denies on deleting the `rc-env-prod`, `rc-env-staging`, `rc-env-dev`, `rc-app-prod`, `rc-app-staging`, `rc-app-dev` and `rc-platform` stacks, the `rc-env-{prod,staging,dev}-*` buckets and the `rc-{prod,staging,dev}-*` / `rc2-{prod,staging,dev}-*` tables. `rc-infra` refuses these itself, but IAM is what makes it impossible.

2. Put the role's ARN in `AWS_ROLE_GITHUB` and add `AWS_REGION=us-east-1`.
3. Protect `main`: require CODEOWNERS review and the checks from `non-main.yml`, which is what runs on a pull request's source branch. A called workflow reports its checks as `<calling job> / <called job>`, so the validation check is named `validate / Lint, test, validate`. The image check is named `deploy-images / Build, test, and publish`. Jobs that exist only in `main.yml`, such as `deploy-infra`, can never be required checks. Block force pushes and branch deletion.
4. Run the first apply by hand and watch it rather than leaving it to `main`:

   ```bash
   AWS_PROFILE=<profile> uv run rc-infra apply          # read the plan
   AWS_PROFILE=<profile> uv run rc-infra apply --yes    # then carry it out
   ```

   The plan should say `CREATE` for `platform` and every environment, and nothing else. Every stack is created from scratch: the buckets and the role get generated names, so all of it coexists with the resources of earlier generations without touching them. Expect `prod`, `staging` and `dev` to come up **empty** — `rc-infra status` lists what holds no data yet, and moving data in is the manual step in section 5.

## 10. Troubleshooting

- **Find what ran:** GitHub Actions, then the **Main** or **Non-main** run summary for the branch.
- **Retry an interrupted deployment:** re-run the failed jobs or push again. Apply and teardown operations are designed to be idempotent.
- **Inspect an environment stack:** `aws cloudformation describe-stacks --stack-name rc-env-<name>`.
- **Resolve `BLOCKED`:** read the action details. Busy/broken CloudFormation stacks must stabilize or be repaired; a replacement of a protected resource must be done by hand if it is really intended.
- **Roll back the base image:** pin a prior immutable ECR digest in `retribalize-core`. List what is still available with `aws ecr describe-images --repository-name rc-lambda-base`; tags name the branch they came from, and superseded images are untagged and expire 30 days later.
- **Avoid console drift:** do not repair stack-owned resources manually in the AWS console. Change `infra/*.yaml` or `envs.yaml` and apply through the workflow.