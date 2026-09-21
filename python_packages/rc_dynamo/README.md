# rc-dynamo

A declarative DynamoDB layer: describe a table as a Pydantic model plus a table
class, and get typed CRUD, index-aware queries, and schema drift detection and
migration from the same declaration.

Published as the Lambda parent image `rc-lambda-base` (see [`Dockerfile`](Dockerfile)).
Service images inherit from its immutable digest, so the package is already
installed — there is nothing to add to a service's requirements.

---

## 1. Usage

```python
from rc_dynamo import BaseItem, BaseTable

class User(BaseItem):
    partition_key = "user_id"
    sort_key = "created_at"          # optional
    gsis = {"email": "email-index"}  # attribute -> index name

    user_id: str
    created_at: str
    email: str
    age: int | None = None

class UserTable(BaseTable[User]):
    table_name = "rc-users"
    item_model = User

users = UserTable()                       # binds to boto3 lazily, on first use
user = users.create(user_id="u1", created_at=User.now_iso(), email="a@b.c")
users.update({"user_id": "u1", "created_at": user.created_at, "age": 31})
found = users.query_filter(email="a@b.c")  # picks email-index automatically
users.delete(("u1", user.created_at))
```

Declaring a table instance at module level is safe: no AWS call happens until a
method is called.

### `rc_dynamo` — the public surface

| Import | What it is |
|---|---|
| `BaseItem` | Pydantic model base; declares the keys and indexes |
| `BaseTable` | Generic table class, `BaseTable[YourItem]` |
| `QueryPlan` | Frozen result of query planning (index name, key/filter expressions) |
| `TableError` | Base class for the errors below |
| `ItemAlreadyExistsError` | `create()` hit an existing key |
| `ItemDoesNotExistError` | `update()` targeted a missing item |
| `ItemType`, `KeyType` | Type aliases: the item TypeVar, and `Hashable \| tuple[Hashable, Hashable]` |

### `BaseItem`

Declare these as class attributes:

| Attribute | Meaning |
|---|---|
| `partition_key: str` | **Required.** Must name a model field. |
| `sort_key: str \| None` | Optional sort key field. |
| `gsis: Mapping[str, str]` | Shorthand `attribute -> index name`. Declaration order is the priority order when planning a query. |
| `gsi_schemas: Mapping[str, Mapping]` | Full GSI specs for what the shorthand cannot express (composite keys, non-`ALL` projections). Not used for query planning. |
| `ignored_attributes: frozenset[str]` | Live attributes deliberately not modeled, so the drift report stops flagging them. |

| Method | Returns |
|---|---|
| `key_fields()` *(classmethod)* | `list[str]` — partition key, plus sort key if declared |
| `key_value` | The item's key: a scalar, or a `(pk, sk)` tuple |
| `to_dict(*, exclude_none=True)` | Plain dict, ready for DynamoDB |
| `now_iso()` *(staticmethod)* | Current UTC time as an ISO-8601 string |

Declarations are validated at class-creation time: a key or index naming a field
that does not exist raises `TypeError` on import, not at runtime.

### `BaseTable[ItemType]`

Class attributes: `table_name` (required), `item_model` (required),
`insert_unknown_columns_on_recreate` (default `True`),
`generated_pk_max_attempts` (default `1`).

| Method | Returns | Notes |
|---|---|---|
| `create(**fields)` | `ItemType` | Refuses to overwrite; raises `ItemAlreadyExistsError`. Generates the partition key via `_new_id()` when omitted. |
| `get_item(key_value, **kwargs)` | `ItemType \| None` | `key_value` is a scalar or a `(pk, sk)` tuple. |
| `update(item_or_fields, *, condition_expression=None, …, return_values="ALL_NEW")` | `ItemType \| None` | A model instance replaces the whole item; a mapping (with all key fields) patches only the fields present. Raises `ItemDoesNotExistError`. |
| `delete(key_value)` | `None` | |
| `query_filter(return_column=None, **field_values)` | `list[ItemType] \| list[Any]` | Equality conditions; picks the table key or the best declared GSI, falling back to a scan. Paginates to completion. |
| `query_page(return_column=None, **kwargs)` | `dict` | One raw boto3 `query` page with `Items` deserialized. |
| `query_raw(return_column=None, **kwargs)` | `list` | `query_page` paginated to completion. |
| `scan_page(return_column=None, **kwargs)` / `scan_raw(...)` | `dict` / `list` | Scan equivalents. |
| `expected_schema()` | `dict` | Schema as declared in Python. |
| `actual_schema()` | `dict` | Schema as it exists in DynamoDB. |
| `schema_diff()` | `SchemaDiff` | The two compared. |
| `exists()` | `bool` | |

Properties: `table` (the boto3 handle), `pk_name`, `sk_name`, `gsis`, `key_fields`.

Passing `return_column="email"` to any read returns a list of that attribute's
values instead of items, and projects only that attribute server-side.

`BaseTable(table=...)` injects a ready boto3 `Table` (used by the test fakes);
`BaseTable(resource=...)` injects the resource to build it from.

### Schema tooling

| Import | Highlights |
|---|---|
| `rc_dynamo.schema` | Re-exports the diff vocabulary: `SchemaDiff`, `Finding`, `Severity`, `FindingKind`, `Remedy`, `Permission`, `diff_schemas`, `resolve_projection` |
| `rc_dynamo.schema.sync` | `sync_tables`, `load_schema_tables`, `select_tables`, `create_table`, `wait_table_active`, `managed_tags`, `ensure_table_tags`, `SchemaSyncError` |
| `rc_dynamo.schema.report` | `build_report`, `render_text`, `python_suggestions`, `SchemaReportError` |

`sync` and `report` are imported from their own modules, not from
`rc_dynamo.schema`, because they depend on `BaseTable`.

A `SchemaDiff` is a tuple of `Finding`s plus `is_clean`, `of_severity(...)`,
`with_remedy(...)`, `actionable(granted)`, `blocked(granted)`,
`requires_action(granted)` and `to_json()`.

### Helpers

| Import | Contents |
|---|---|
| `rc_dynamo.utils.settings` | `Settings` (frozen dataclass), `Settings.from_env()`, `SettingsError` |
| `rc_dynamo.utils.aws` | `resource(service, settings=None)`, `client(...)`, `dynamodb_resource(...)`, `clear_cache()` — cached boto3 factories |
| `rc_dynamo.utils.numeric` | `decimal_to_float(obj)`, `float_to_decimal(obj)` — recursive, for DynamoDB's `Decimal` |

`Settings.from_env()` reads `AWS_REGION` / `AWS_DEFAULT_REGION`,
`AWS_ENDPOINT_URL`, `DYNAMO_SCHEMA_POLL_SECONDS` (10),
`DYNAMO_SCHEMA_WAIT_TIMEOUT_SECONDS` (3600), `DYNAMO_PRUNE_UNDECLARED` (false)
and `DYNAMO_ALLOW_TABLE_RECREATE` (false). Both destructive permissions are off
unless explicitly enabled.

### Console scripts

The image installs `rc-dynamo-sync` and `rc-dynamo-report`. Both load an
application module that exports `TABLES: Mapping[str, BaseTable]`:

```bash
rc-dynamo-report --schema-module app.schema                     # what drifted?
rc-dynamo-sync   --schema-module app.schema                     # dry run
rc-dynamo-sync   --schema-module app.schema --environment prod --apply
```

| Exit code | Meaning |
|---|---|
| `0` | Clean / success |
| `1` | Blocked change, difference left after `--apply`, bad schema module, missing dump bucket, invalid settings |
| `2` | Usage error |
| `3` | Differences found (sync dry run: pending changes; report: findings) |

Run either with `--help` for the full flag list.

---

## 2. Local development

Two uv projects live here. The root one is the package that ships; `tests/` is a
separate project holding pytest, mypy and ruff, so **no test tooling can ever
reach the published image**. Run everything from this directory —
`--project tests` selects the test project without changing the working
directory.

Never hand-edit `pyproject.toml` or `uv.lock`; use the `uv` commands below.

### Set up after a fresh clone

```bash
cd python_packages/rc_dynamo
uv sync --project tests            # creates tests/.venv with rc-dynamo installed editable
```

That single environment covers everything: the package, pytest, mypy and ruff.

### After a `git pull`

```bash
uv sync --project tests --frozen   # --frozen: install exactly the lockfile, never re-resolve
```

If it complains that the lockfile is out of date, someone changed a dependency —
drop `--frozen` once.

### Change dependencies

| Goal | Command |
|---|---|
| Add a runtime dependency (ships in the image) | `uv add --no-sync "<pkg><constraint>"` then `uv lock --project tests && uv sync --project tests` |
| Add a test/lint dependency | `uv add --project tests "<pkg><constraint>"` |
| Upgrade one package | `uv lock --upgrade-package <pkg>` (add `--project tests` for test deps) |
| Remove a package | `uv remove <pkg>` (add `--project tests` for test deps) |

A runtime dependency needs the second command because `tests/uv.lock` pins the
package's own dependency closure too.

### Run checks directly

```bash
uv run --project tests pytest tests/unit
uv run --project tests ruff check src tests
uv run --project tests ruff format src tests
uv run --project tests mypy
```

Unit tests need no AWS and no Docker.

### Build the image

[`compose-build-test.yaml`](compose-build-test.yaml) builds it, under the one name the
image has everywhere - `<registry>/rc-lambda-base:<tag>`. The defaults are the real ECR registry
and `latest`; export `ECR_REGISTRY` or `IMAGE_TAG` to build under a different name.
`BUILD_CACHE_TO=type=inline` is needed because the default buildx driver cannot export the
GitHub Actions cache the file asks for on CI; the file header explains it.

```bash
BUILD_CACHE_TO=type=inline docker compose -f compose-build-test.yaml build base

IMAGE=273268178059.dkr.ecr.us-east-1.amazonaws.com/rc-lambda-base:latest
docker run --rm --platform linux/amd64 --entrypoint python "$IMAGE" \
  -c "import sys, rc_dynamo, rc_dynamo.cli; assert sys.version_info[:2] == (3, 11)"
docker run --rm --platform linux/amd64 --entrypoint rc-dynamo-sync "$IMAGE" --help
```

### Run the full test pipeline

[`compose-build-test.yaml`](compose-build-test.yaml) is the whole gate — lockfile
checks, ruff, mypy, unit tests, and the LocalStack integration suite — and it is
exactly what CI runs. [`tests.Dockerfile`](tests.Dockerfile) layers the test tooling
and the suite onto the base image, which compose hands it directly as the
`rc_dynamo_base` build context - so the suite exercises the exact image that gets
pushed, and `build` with no service name builds both.

```bash
BUILD_CACHE_TO=type=inline docker compose -f compose-build-test.yaml build
docker compose -f compose-build-test.yaml run --rm tests
docker compose -f compose-build-test.yaml down -v
```

The steps themselves live in [`tests/run-checks.sh`](tests/run-checks.sh); add
checks there and both CI and every laptop pick them up.

Two things to know:

- Both images are built as one linked graph, so any buildx driver works and the test
  image never resolves the base image by name. This needs Docker Compose 2.37.3 or
  newer - see the root README's prerequisites for why.
- The integration suite refuses to run against anything but LocalStack: it
  asserts the endpoint is local, the credentials are `test`, and the table prefix
  contains `schema-sync-test`. It cannot touch a real table.

To iterate on the integration tests without rebuilding the image, start
LocalStack alone and run pytest on the host:

```bash
docker compose -f compose-build-test.yaml up -d --wait localstack
AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test \
AWS_SECRET_ACCESS_KEY=test AWS_REGION=us-east-1 \
DYNAMO_SCHEMA_TEST_PREFIX=rc-local-schema-sync-test \
  uv run --project tests pytest tests/integration
docker compose -f compose-build-test.yaml down -v
```

### Layout

```
pyproject.toml uv.lock   the published package; also the shared ruff/mypy config
src/rc_dynamo/           base_item, base_table, cli
  schema/                diff (pure), sync (apply engine), report (drift report)
  utils/                 settings, aws, numeric
tests/                   its own pyproject.toml + uv.lock (pytest, mypy, ruff)
  unit/                  mirrors src/; no AWS, no Docker
  integration/           LocalStack-backed schema-sync suite
Dockerfile               the published image
tests.Dockerfile         test image, FROM the published image
compose-build-test.yaml  builds both images; LocalStack + the test image = the gate
```

### What CI does that you cannot

Pushing the image to ECR under a branch tag is the one thing that happens only in
[`.github/workflows/deploy-images.yml`](../../.github/workflows/deploy-images.yml).
Everything it checks, you can run locally with the commands above.
