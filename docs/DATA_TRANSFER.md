# Data transfer

`rc-data-transfer` copies data into fresh, empty infrastructure: every item of one DynamoDB
table into another, or every object of one S3 bucket into another. It is the only way data
moves between resources in this repository, and it only ever runs by hand.

- `rc-infra apply` never calls it, and no workflow may run it. `tests/test_architecture.py`
  fails the build if either changes.
- It never modifies or deletes anything in the source.
- It never writes into a target that already holds data.
- It never changes a target's configuration. Buckets and their settings come from
  CloudFormation; tables and their schema come from `retribalize-core`'s schema sync.

## 1. Prerequisites

- `uv sync` in this repository; the command is installed next to `rc-infra`.
- The target already exists and is empty:
  - **buckets** — `rc-infra apply` has created the target environment (`rc-env-<name>`);
  - **tables** — `retribalize-core` has run `rc-dynamo-sync --apply` for the target environment,
    so the target table exists with the current schema.
- `rc-infra status --environment <name>` lists the target's durable resources as `EMPTY`.

## 2. AWS credentials

Use credentials for account `273268178059` that can read the source and write the target:

```bash
aws sso login --profile <profile>
export AWS_PROFILE=<profile>
```

Both resources must belong to the account those credentials are for. Cross-account
transfers are refused; there is no role-assumption mode.

## 3. Always dry-run first

Exactly one of `--dry-run` or `--apply` is required; there is no default. A dry run performs
every check an apply performs and reports what it found, including the source's exact size,
and writes nothing. Run it, read it, and only then repeat the same command with `--apply`.

`--format json` prints the same report as JSON for a migration log.

## 4. Table to table

```bash
uv run rc-data-transfer table \
  --source arn:aws:dynamodb:us-east-1:273268178059:table/<source-table> \
  --target arn:aws:dynamodb:us-east-1:273268178059:table/<target-table> \
  --dry-run
```

Before writing a single item it:

1. compares the two tables' configurations and **refuses on any difference** in: key schema,
   attribute definitions, every LSI and GSI (keys, projection, non-key attributes, provisioned
   throughput), billing mode, provisioned throughput, stream settings, encryption, table class,
   and TTL (enabled and attribute). Volatile fields — ARN, ID, creation time, status, item
   count, size — are ignored. There is no override flag;
2. reports point-in-time recovery and tags side by side, without blocking on them;
3. refuses unless the target is empty (an unfiltered one-item scan, never the approximate
   `ItemCount`);
4. counts the source exactly, with a consistent scan.

With `--apply` it then scans the source and writes in batches of 25, retrying unprocessed
items with bounded exponential backoff. Items travel as raw DynamoDB values, so numbers,
sets, binary and nested maps arrive unchanged. Afterwards it counts the target, and fails if
the count differs from what was scanned, or if the source changed during the copy.

Item counts are what is verified. They are evidence of a complete copy, not proof that every
byte is identical.

## 5. Bucket to bucket

```bash
uv run rc-data-transfer bucket \
  --source arn:aws:s3:::<source-bucket> \
  --target arn:aws:s3:::<target-bucket> \
  --dry-run
```

It copies every current object with its bytes, `Content-Type`, `Content-Encoding`,
`Cache-Control`, `Content-Disposition`, `Content-Language`, user metadata and tags, through
boto3's managed copy, so objects too large for a single copy use multipart. Afterwards it
checks every object's size, headers, metadata and tags against the source, and the total
object count and bytes. ETags are not compared: a multipart copy legitimately produces
different ones. Objects are copied one at a time, so a large bucket takes a while.

Bucket configuration is never copied. Storage class is not preserved either: copies land in
the target's default class.

## 6. Resolve by environment and resource name

The ARN commands above are the primitive. `resolve-copy` works out the two bucket ARNs from an
environment and a logical resource name, prints them together with the equivalent primitive
command, then runs it:

```bash
uv run rc-data-transfer resolve-copy \
  --source-layout legacy  --source-env preview67 --source-resource UserCorpusBucket \
  --target-env dev        --target-resource UserCorpusBucket \
  --dry-run
```

- `--source-layout current` reads the source from the `rc-env-<env>` stack outputs, like the
  target.
- `--source-layout legacy` names the bucket the way the generation before `re-infra` did,
  `rc-<env>-<purpose>-<account>`. Legacy environment names are not checked against
  `envs.yaml`, because some, such as the shared `preview` tier, are not deployment slots
  today. This rule lives only in `src/rc_infra/transfer/legacy.py`.
- Resources are the bucket logical IDs (`UserCorpusBucket`) or purposes (`user-corpus`).

Tables cannot be resolved by name yet: their names belong to `retribalize-core`'s schema
code, which does not publish them as a contract. Pass table ARNs to `table`.

## 7. When a table copy is refused

```text
REFUSED: table transfer (dry run)
  source: arn:aws:dynamodb:us-east-1:273268178059:table/rc-prod-users
  target: arn:aws:dynamodb:us-east-1:273268178059:table/rc-prod-users-new
  DynamoDB configurations differ.

Configurations differ:

global_secondary_indexes.GSI1-Email.projection:
  source: "ALL"
  target: "INCLUDE"

ttl.attribute:
  source: "expiresAt"
  target: null

No data was written.
```

Fix the target, which usually means changing the schema code in `retribalize-core` and
running schema sync again, then dry-run again. Never edit the source to match.

## 8. When the target is not empty

```text
REFUSED: bucket transfer (dry run)
  target bucket is not empty; transfers only write into an empty bucket
No data was written.
```

There is no merge mode. If an earlier attempt left partial data behind, empty the target, or
delete and recreate it, before retrying (see section 11).

## 9. Versioned buckets

By default only current object versions are copied, which is enough for a working target.
To carry history as well, for example for `PropertyRegistryBucket`:

```bash
uv run rc-data-transfer bucket --source <arn> --target <arn> --include-versions --dry-run
```

- The target must have versioning enabled, or the command refuses.
- Every version is copied oldest first, so each key's newest version ends up current.
- Target version IDs are new; they cannot equal the source's.
- **Delete markers are not reproduced.** The report says how many the source holds. A key
  whose newest source entry is a delete marker ends up with a current version in the target.
  This is not an exact replica of the version history.
- The target must also hold no old versions or delete markers, not only no current objects.

## 10. Exit codes

| Code | Meaning |
|---|---|
| `0` | Dry run passed every check, or the copy completed and verified. |
| `1` | Refused before writing anything, or a copy or verification failed. |
| `2` | Invalid usage: missing mode, malformed ARN, unknown resource. |
| `130` | Interrupted with Ctrl-C. Anything already written stays in the target. |

## 11. Rollback

The source is never touched, so there is nothing to roll back there. A failed or
interrupted copy leaves a partial target. Before cutover, nothing reads the target, so the
recovery is to empty it, or delete and recreate it (`rc-infra apply` for a bucket's
environment stack, schema sync for a table), and run the transfer again. Do not try to
resume into a partly filled target: the command refuses, by design.

## 12. Migration checklist

1. `rc-infra apply` has created the target environment, and schema sync has created its tables.
2. `rc-infra status --environment <target>` shows the resources to fill as `EMPTY`.
3. Writes to the source are stopped, or the copy will fail its source-changed check.
4. Every table: `rc-data-transfer table … --dry-run`, then `--apply`, saving both reports
   (`--format json`).
5. Every bucket: `rc-data-transfer bucket …` (or `resolve-copy …`) `--dry-run`, then `--apply`,
   with `--include-versions` where history matters.
6. `rc-infra status --environment <target>` shows them `NON-EMPTY`.
7. Verify the application against the target before cutover.
8. Cut over. Keep the source untouched until the target has been trusted for long enough to
   retire it.
