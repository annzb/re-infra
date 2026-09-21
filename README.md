# re-infra

The `main` branch of this repository is the source of truth for Retribalize deployment environments and shared deployment infrastructure. It owns:

- the environment configuration in [`envs.yaml`](envs.yaml);
- shared platform infrastructure;
- one core infrastructure stack per environment;
- the common Python 3.11 Lambda base image and reusable DynamoDB tooling.

Application code, application-specific table declarations, service images, the SAM template, and application releases remain in `retribalize-core`.

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
| `AWS_ROLE_GITHUB` | ARN of the role created manually by an administrator (section 8) | The only GitHub OIDC role. Every branch assumes it to publish its image; `main` additionally uses it for infrastructure applies. |

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
2. **Deploy images** ([`deploy-images.yml`](.github/workflows/deploy-images.yml)) starts after validation. In a single job it builds the image, runs the `rc_dynamo` test pipeline against it through [`compose-build-test.yaml`](python_packages/rc_dynamo/compose-build-test.yaml), and pushes it to `rc-lambda-base:<branch>`.

A branch run reaches AWS only to push its own image tag; it never deploys infrastructure, and a branch deletion is skipped rather than rebuilt. To see what a change would do to live infrastructure, run a local dry run (section 3).

### Pushes to main — `main.yml`

1. **Validate** — the same workflow, unchanged.
2. **Deploy infrastructure** ([`deploy-infra.yml`](.github/workflows/deploy-infra.yml)) starts after validation and applies `envs.yaml` together with `infra/*.yaml`.
   - It assumes `AWS_ROLE_GITHUB` through GitHub OIDC, and CloudFormation acts with that role's credentials.
   - It runs `rc-infra apply --yes`, which prints the plan, refuses it if anything is `BLOCKED`, and otherwise creates, imports, updates, or removes infrastructure until AWS matches `envs.yaml`. It does not repeat the validation stage's checks.
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

### Stack ownership

| Stack/resource | Owner | Contents |
|---|---|---|
| `rc-platform` | This repo | The shared ECR repositories, the four Cognito identity pools, and the shared `LambdaPower` Lambda execution role. |
| `rc-env-<name>` | This repo | The environment's durable buckets. |
| `rc-app-<name>` | `retribalize-core` | Application-specific SAM/CloudFormation resources. |
| DynamoDB application tables | `rc-dynamo-sync` from `retribalize-core` | Application table schemas and migrations; not owned by these CloudFormation templates. |

The GitHub OIDC role is deliberately absent from that table: it is provisioned manually (section 8) and owned by no stack here, because the role that deploys the infrastructure cannot also be deployed by it. The Cognito OIDC identity providers are absent for a different reason — they carry client secrets, so they stay outside every template; see [`infra/identity.md`](infra/identity.md).

This repository does not deploy application code. A successfully created `rc-env-<name>` stack only makes the environment available for a later `retribalize-core` deployment.

## 3. Deployment environments

[`envs.yaml`](envs.yaml) is the complete desired list of environments. Because `main` is authoritative, adding an entry creates infrastructure and removing an entry tears that environment down when the change reaches `main`.

> **Warning:** removing an environment deletes its `rc-app-<name>` stack, schema-sync-managed tables, buckets and bucket data, followed by its `rc-env-<name>` stack. `prod`, `staging`, and `dev` are protected from removal by validation, termination protection, and explicit IAM denies.

Never commit secrets to `envs.yaml`. Cognito pool/client IDs and hosted domains are identifiers; API keys, tokens, and client secrets belong in Secrets Manager.

### Configuration fields

| Field | Required | Description |
|---|---|---|
| `schema_version` | Yes | Configuration schema version; currently must be `1`. |
| `account_id` | Yes | The 12-digit AWS account. AWS-backed commands refuse credentials for another account. |
| `region` | Yes | Region used for all managed stacks, for example `us-east-1`. |
| `identity_profiles` | Yes | Named Cognito identifier sets containing `user_pool_id`, `client_id`, and `domain`. |
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

1. Delete its entry from `envs.yaml`.
2. Run the dry run above locally and read the `DELETE` action and its inventory carefully. Branch runs have no AWS access, so this is the only preview before the change reaches `main`.
3. Merge to `main` only when the listed application stack, tables, buckets, and data should be removed.

Teardown is idempotent and ordered: app stack, tagged schema-sync tables, buckets, then the core environment stack. Untagged tables are reported and preserved. Legacy app stacks named `rc-<name>` are also preserved.

### Derived names

For `preview3` in account `273268178059`:

| Resource | Derived name |
|---|---|
| Core stack | `rc-env-preview3` |
| Application stack | `rc-app-preview3` |
| DynamoDB table prefix | `rc-preview3-` |
| Buckets | `rc-preview3-{embeddings,user-corpus,avatars,recordings,schema-dumps}-273268178059` |

These names are derived, not published. An earlier version of the environment stack mirrored them into `/rc/env/<name>/*` SSM parameters as the contract with `retribalize-core`; those parameters have been removed and the contract is being expressed another way. Derive the names from the environment's name as above, or read them from the stack.

## 4. Infrastructure CLI

`uv sync` installs the `rc-infra` command from `src/rc_infra`. Both commands validate the selected config first.

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
2. checks whether `rc-platform` must be created, updated, or left unchanged;
3. checks every declared `rc-env-<name>` stack;
4. identifies existing buckets that should be imported and blocks imports whose live settings do not match the template;
5. creates and discards CloudFormation update change sets to show exact changes for existing stacks;
6. inventories managed environments that exist in AWS but are absent from `envs.yaml` and reports their teardown as `DELETE`;
7. prints each target as `BLOCKED`, `DELETE`, `IMPORT`, `CREATE`, `UPDATE`, or `NOOP`, preceded by a warning banner when anything would be deleted.

Without `--yes` it stops there: a dry run that modifies nothing. Building the plan is not literally API read-only — CloudFormation previews require temporary `CreateChangeSet` and `DeleteChangeSet` calls — but it never executes a change set.

With `--yes`, it refuses any plan containing `BLOCKED`, then applies creates/imports/updates before deletions. Failures in one environment are recorded without preventing independent environments from being attempted; any failure produces a nonzero exit code.

Routine applies belong in the protected main-branch workflow, not on developer machines.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Successful validation or apply. A dry-run apply also returns `0` when nothing is blocked. |
| `1` | Invalid config, account mismatch, blocked plan, or failed apply. |
| `2` | Invalid command-line usage reported by `argparse`. |

## 5. Reusable Python packages

`python_packages/` holds the reusable libraries this repository publishes. Each is a self-contained uv project with its own lockfile, Dockerfile and test pipeline, and its own README.

| Package | What it is |
|---|---|
| [`rc_dynamo`](python_packages/rc_dynamo/README.md) | A declarative DynamoDB layer - typed CRUD, index-aware queries, schema drift detection and migration - published as the Lambda parent image `rc-lambda-base`. |

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

Because tags move, the image a tag previously pointed at becomes untagged, and `rc-platform`'s lifecycle rule expires untagged images after **30 days**. That window is also the rollback window: refresh any digest pinned in `retribalize-core` within it, or the pin stops resolving.

## 6. Local setup and development

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

## 7. Integration contract with `retribalize-core`

`retribalize-core` should:

- refuse to deploy when the target `rc-env-<name>` stack does not exist;
- build service images from the immutable digest of `rc-lambda-base:main`, resolved from ECR rather than from a branch tag, and record that digest in its build manifest, refreshing it within 30 days of being superseded;
- export `TABLES: Mapping[str, BaseTable]` from its schema module and invoke `rc-dynamo-sync --schema-module <module> --environment <name> --apply`;
- own all `rc-app-<name>` application stacks and service releases.

## 8. One-time AWS and GitHub setup

1. With administrator credentials, create the GitHub OIDC role by hand. It is not declared in this repository: the role that deploys the infrastructure cannot be deployed by it, and keeping it out of the templates means a mistake here can never be applied automatically. Requirements:

   - **Trust policy:** `sts:AssumeRoleWithWebIdentity` federated through the account's `token.actions.githubusercontent.com` OIDC provider, with `aud` equal to `sts.amazonaws.com` and `sub` matching `repo:<org>/re-infra:ref:refs/heads/*`. Every branch may assume it, because `deploy-images.yml` publishes an image tag from every branch; only `main.yml` routes it into an infrastructure apply. Create the OIDC provider only if the account does not already have one — an account can hold just one.
   - **Permissions.** CloudFormation runs with this role's own credentials, so it needs everything a deploy or teardown touches: full management of the `rc-platform` and `rc-env-*` stacks, `cloudformation:DeleteStack` on `rc-app-*` and the underlying Lambda, API Gateway, SQS, CloudFront and log permissions those deletes exercise, `iam:PassRole` on `rc-app-*` roles, push access to the shared ECR repositories, and enough S3 and DynamoDB access to inspect, empty and delete `rc-*` buckets and `ManagedBy=rc-dynamo-sync` tables.
   - **Denies.** Add explicit denies on deleting the `rc-env-prod`, `rc-env-staging`, `rc-env-dev`, `rc-app-prod`, `rc-app-staging`, `rc-app-dev` and `rc-platform` stacks, the `rc-{prod,staging,dev}-*` buckets and the `rc-{prod,staging,dev}-*` tables. `rc-infra` refuses these itself, but IAM is what makes it impossible.

2. Put the role's ARN in `AWS_ROLE_GITHUB` and add `AWS_REGION=us-east-1`.
3. Protect `main`: require CODEOWNERS review and the checks from `non-main.yml`, which is what runs on a pull request's source branch. A called workflow reports its checks as `<calling job> / <called job>`, so the validation check is named `validate / Lint, test, validate`. The image check is named `deploy-images / Build, test, and publish`. Jobs that exist only in `main.yml`, such as `deploy-infra`, can never be required checks. Block force pushes and branch deletion.
4. Create `rc-platform` by **importing**, never by creating. Fifteen of its sixteen resources already exist and hold live state: the four Cognito pools (the prod one had 7,039 user accounts), their app clients and hosted UI domains, the `rc-api` and `rc-matching` image repositories, and the `LambdaPower` role. Only `rc-lambda-base` is new.

   > **Do not run `rc-infra apply --yes` against a non-existent `rc-platform`.** It plans a `CREATE`, and a create would try to make resources that already exist: the hosted UI domain prefixes are globally unique and the role and repository names are taken, so the stack fails and rolls back, and because every resource is `Retain` the rollback can leave duplicate Cognito pools behind. Import first; after that the plan is an ordinary `UPDATE`.

   First, release `rc-api` and `rc-matching` from the `rc-ecr` stack in `retribalize-core` — a resource cannot belong to two stacks. Remove them from its `infra/ecr.yaml` while keeping `DeletionPolicy: Retain` so the repositories and their images survive, and deploy that stack. The images are deployed by digest, so nothing needs rebuilding.

   Then create `rc-platform` from the existing resources. A create-by-import change set may contain **only** the resources being imported, so `rc-lambda-base` is added by the ordinary apply that follows:

   ```bash
   # Identifiers: pool/client IDs and domains are in envs.yaml under identity_profiles.
   aws cloudformation create-change-set \
     --stack-name rc-platform \
     --change-set-name adopt \
     --change-set-type IMPORT \
     --capabilities CAPABILITY_NAMED_IAM \
     --template-body file://<template-without-rc-lambda-base> \
     --resources-to-import file://resources-to-import.json
   ```

   Each entry names a logical ID from `infra/platform.yaml` and its identifier, for example:

   ```json
   {"ResourceType":"AWS::Cognito::UserPool","LogicalResourceId":"ProdUserPool",
    "ResourceIdentifier":{"UserPoolId":"us-east-1_1GIFBpLKf"}}
   {"ResourceType":"AWS::ECR::Repository","LogicalResourceId":"ApiImageRepository",
    "ResourceIdentifier":{"RepositoryName":"rc-api"}}
   ```

   Client entries also carry `ClientId`, domain entries `Domain`, and the role entry `RoleName`.

   **Read the change set before executing it.** Every resource must report `Import`, and none may report a replacement. Then confirm nothing moved and that `rc-infra` agrees the template matches:

   ```bash
   aws cognito-idp describe-user-pool --user-pool-id us-east-1_1GIFBpLKf \
     --query 'UserPool.EstimatedNumberOfUsers'
   uv run rc-infra apply     # rc-platform: UPDATE adding only rc-lambda-base
   ```

   That apply must add `rc-lambda-base` and touch nothing else. If it proposes changing or replacing a pool, a client, a domain or the role, the template does not match what was imported — fix the template, and never apply it.

5. Before the first main apply, run `rc-infra apply` without `--yes` using authorized local credentials. Existing buckets should appear as `IMPORT`. If an import is `BLOCKED`, modify the template to match the live bucket before applying; do not modify production data merely to satisfy the template.

## 9. Troubleshooting

- **Find what ran:** GitHub Actions, then the **Main** or **Non-main** run summary for the branch.
- **Retry an interrupted deployment:** re-run the failed jobs or push again. Apply and teardown operations are designed to be idempotent.
- **Inspect an environment stack:** `aws cloudformation describe-stacks --stack-name rc-env-<name>`.
- **Resolve `BLOCKED`:** read the action details. Busy/broken CloudFormation stacks must stabilize or be repaired; import candidates require the template to match live bucket settings.
- **Roll back the base image:** pin a prior immutable ECR digest in `retribalize-core`. List what is still available with `aws ecr describe-images --repository-name rc-lambda-base`; tags name the branch they came from, and superseded images are untagged and expire 30 days later.
- **Avoid console drift:** do not repair stack-owned resources manually in the AWS console. Change `infra/*.yaml` or `envs.yaml` and apply through the workflow.