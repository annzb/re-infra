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

1. Edit [`environments/catalog.yaml`](environments/catalog.yaml) (field reference:
   [`environments/README.md`](environments/README.md)). A new preview is one line:
   `preview12: {}`.
2. Check it locally:

   ```bash
   uv run rc-infra validate
   aws sso login   # plan reads AWS; it never changes anything
   uv run rc-infra plan
   ```

3. Open a pull request. The **Infrastructure plan** job summary shows every
   `CREATE`, `IMPORT`, `UPDATE`, `DELETE`, and `BLOCKED` action.
4. Merge. `deploy.yml` applies the plan and the new `rc-env-<name>` stack publishes
   its SSM parameters.

This does not deploy the application. Deploy `retribalize-core` into the new
environment separately.

## Remove an environment

1. Delete its entry from `environments/catalog.yaml`.
2. Open a pull request and read the plan: it lists the app stack, tables, and
   buckets that will be **deleted with their data**.
3. Merge. `deploy.yml` deletes, in order: `rc-app-<name>`, DynamoDB tables tagged
   `ManagedBy=rc-dynamo-sync` and `Environment=<name>`, the environment's buckets
   (emptied first), and finally `rc-env-<name>`. If it fails midway, re-run the
   workflow; teardown continues where it stopped.

`prod`, `staging`, and `dev` cannot be removed. The validator rejects a catalog
without them, their stacks have termination protection, and the deploy role has an
explicit IAM deny on deleting their stacks, buckets, and tables.

Untagged tables with the environment's prefix are listed as skipped and left alone.
Legacy app stacks named `rc-<name>` are not deleted.

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

4. Merge. `build.yml` builds the image, `test.yml` tests it, and only then is it
   published. Existing
   application images are unchanged until `retribalize-core` rebuilds from the new
   digest.

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

Every deployment starts with a workflow, which runs the repository's files in order.

**On a pull request**

```text
1. .github/workflows/validate.yml
   a. ruff / mypy / pytest      -> src/rc_infra/**, tests/**
   b. cfn-lint infra/*.yaml     -> roles.yaml, platform.yaml, environment.yaml
   c. rc-infra validate         -> cli.py -> catalog.py -> environments/catalog.yaml
   d. rc-infra plan             -> planner.py -> cfn.py | buckets.py | tables.py,
                                   reading infra/platform.yaml and infra/environment.yaml
                                   -> plan in the run summary (fails on BLOCKED)
2. .github/workflows/build.yml       (only when images/lambda-base/** changed)
   a. build  -> images/lambda-base/Dockerfile -> image uploaded as an artifact
   b. test   -> .github/workflows/test.yml -> pytest + smoke tests on that image
              (nothing is published from a pull request)
```

**On merge to main**

```text
1. .github/workflows/deploy.yml -> rc-infra apply --yes -> apply.py
   a. infra/platform.yaml    -> stack rc-platform
   b. infra/environment.yaml -> stack rc-env-<name>, per catalog entry
                                (create, import existing buckets, or update)
   c. teardown.py            -> for environments removed from the catalog:
                                rc-app-<name> -> tables -> buckets -> rc-env-<name>
2. .github/workflows/build.yml
   a. build   -> image artifact
   b. test    -> .github/workflows/test.yml
   c. publish -> ECR build tag -> scan -> latest -> SSM /rc/lambda-base/image-uri
```

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
3. Protect `main`: require pull requests, the *Validate infrastructure* checks, and
   CODEOWNERS review; block force pushes and deletion.
4. Before the first merge that applies, run `uv run rc-infra plan` locally. Existing
   `prod`/`staging`/`dev` buckets appear as `IMPORT`, or as `BLOCKED` with the exact
   differences between the live bucket and the template. Change the **template** to
   match the live bucket, not the other way around, until nothing is blocked.

## Troubleshooting

- **Workflow logs:** GitHub → Actions → *Validate infrastructure*, *Deploy
  infrastructure*, *Build base image*, or *Test base image*. The plan is in the
  validate and deploy run summaries.
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
  or catalog and let `deploy.yml` apply it.
