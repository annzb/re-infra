# Data transfer

`rc-data-transfer` copies data into fresh, empty infrastructure: every item of one DynamoDB
table into another, or every object of one S3 bucket into another. It is the only way data
moves between resources in this repository, and it only ever runs by hand.

- `rc-infra apply` never calls it, and no workflow may run it. `tests/test_architecture.py`
  fails the build if either changes.
- It never modifies or deletes anything in the source.
- It never writes into a target that already holds data, except to resume a bucket copy into
  exactly what that copy's own manifest recorded.
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
  --dry-run                                         # then: --apply --manifest out/<name>.jsonl
```

It copies every current object with its bytes, `Content-Type`, `Content-Encoding`,
`Cache-Control`, `Content-Disposition`, `Content-Language`, user metadata and tags, through
boto3's managed copy, so objects too large for a single copy use multipart. Afterwards it
checks that exactly the same keys are current in both buckets with the same sizes, and every
object's headers, metadata and tags. ETags are not compared: a multipart copy legitimately
produces different ones. With `--verify-content` it also streams both copies of every object
and compares their SHA-256 digests, which reads every byte twice but proves the bytes, not
just the sizes. Objects are copied one at a time, so a large bucket takes a while.

`--apply` requires `--manifest <file>`: a JSON-lines record, flushed to disk after every
object, of each copy (`key`, `source_version`, `target_version`). It is both the version-ID
map (section 9) and the checkpoint (section 11). Keep it with the migration log.

Bucket configuration is never copied. Storage class, ACLs, Object Lock retention and original
creation times are not preserved either: copies land in the target's default class, owned by
the target account, and lifecycle ages restart from the copy. The audit found no Object Lock,
ACL grants or non-default storage classes on the old buckets.

## 6. Resolve by environment and resource name

The ARN commands above are the primitive. `resolve-copy` works out the two bucket ARNs from an
environment and a logical resource name, prints them together with the equivalent primitive
command, then runs it:

```bash
uv run rc-data-transfer resolve-copy \
  --source-layout legacy  --source-env preview3 --source-resource UserCorpusBucket \
  --target-env preview3   --target-resource UserCorpusBucket \
  --dry-run
```

- `--source-layout current` reads the source from the `rc-env-<env>` stack outputs, like the
  target.
- `--source-layout legacy` resolves the bucket the generation before `re-infra` used for that
  environment and purpose, from an explicit table in `src/rc_infra/transfer/legacy.py` (the
  only place that layout is known). It is not one formula:

  | Environment | Legacy bucket |
  |---|---|
  | `prod`, `staging`, `dev` | `rc-<env>-<purpose>-<account>` |
  | any preview slot | the shared `rc-preview-<purpose>-<account>` |
  | `preview89` property registry | its own `rc-preview89-property-registry-<account>` |
  | `dev` recordings, `preview67` property registry | none: core declared them, but they were never created |
  | schema dumps | none: they were a prefix of the embeddings bucket |

  Several previews share one source. Which slot should receive the shared preview data is a
  decision for the migration, not for the resolver.
- Resources are the bucket logical IDs (`UserCorpusBucket`) or purposes (`user-corpus`).

Tables cannot be resolved by name: their names belong to `retribalize-core`'s schema code.
Pass table ARNs to `table`, or use core's schema-aware migration (section 13).

## 7. When a table copy is refused

```text
REFUSED: table transfer (dry run)
  source: arn:aws:dynamodb:us-east-1:273268178059:table/rc-prod-users
  target: arn:aws:dynamodb:us-east-1:273268178059:table/rc2-prod-users
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
  target bucket is not empty (counting old versions and delete markers); transfers only write into an empty bucket
No data was written.
```

There is no merge mode. A target counts as empty only with no current objects, no old
versions and no delete markers. To continue an interrupted copy, rerun it with the same
`--manifest` (section 11); otherwise empty the target, or delete and recreate it.

## 9. Versioned buckets

By default only current object versions are copied, which is enough for a working target.
To carry history as well, for example for `PropertyRegistryBucket`:

```bash
uv run rc-data-transfer bucket --source <arn> --target <arn> --include-versions --dry-run
```

- The target must have versioning enabled, or the command refuses.
- Each key's versions **and delete markers** are replayed in the order they happened. A key
  deleted in the source is deleted in the target too (its history is kept, it is not
  current), and every other key's newest version is current.
- Target version IDs are new; they cannot equal the source's. The manifest maps every source
  version ID to the target version it became, and verification checks each recorded version
  against the exact target version, not only the current one.
- **Records that pin a version must be rewritten with that map.** The property registry
  stores `s3VersionId`; copying the bucket alone leaves those pointing at version IDs that do
  not exist in the target. This is the schema-aware migration's job (section 13).

## 10. Exit codes

| Code | Meaning |
|---|---|
| `0` | Dry run passed every check, or the copy completed and verified. |
| `1` | Refused before writing anything, or a copy or verification failed. |
| `2` | Invalid usage: missing mode, malformed ARN, unknown resource. |
| `130` | Interrupted with Ctrl-C. Anything already written stays in the target, recorded in the manifest. |

An AWS error at any point (listing, copying, verifying) fails the transfer with the phase it
happened in; it never surfaces as a traceback or passes for a completed copy.

## 11. Resume and rollback

The source is never touched, so there is nothing to roll back there.

An interrupted or partly failed bucket copy resumes: run the same command with the same
`--manifest`. The target may then hold exactly what the manifest records — no other object,
version or delete marker — or the command refuses. Every recorded entry is skipped, and
verification covers the whole bucket again.

A table copy cannot resume. Before cutover nothing reads the target, so the recovery is to
empty it, or delete and recreate it (schema sync for a table, `rc-infra apply` for a bucket's
environment stack), and run the transfer again.

## 12. Migration checklist

1. `rc-infra apply` has created the target environment, and schema sync has created its tables.
2. `rc-infra status --environment <target>` shows the resources to fill as `EMPTY`.
3. Writes to the source are stopped, or the copy will fail its source-changed check.
4. Every table: `rc-data-transfer table … --dry-run`, then `--apply`, saving both reports
   (`--format json`).
5. Every bucket: `rc-data-transfer bucket …` (or `resolve-copy …`) `--dry-run`, then `--apply
   --manifest …`, with `--include-versions` where history matters and `--verify-content` where
   the bytes must be proven.
6. `rc-infra status --environment <target>` shows them `NON-EMPTY`.
7. Verify the application against the target before cutover.
8. Cut over. Keep the source untouched until the target has been trusted for long enough to
   retire it.

## 13. Application data migration (`retribalize-core`)

Copying a bucket or a table byte for byte is not a migration of application data: rows refer
to buckets by URL and to object revisions by version ID, and the target tables are a new
generation (`rc2-<env>-*`) that core creates itself. That migration belongs in
`retribalize-core`, built on these primitives, and is not implemented here. Its contract:

**Inputs**

- the deployment contract for the target environment (`rc-infra outputs --environment <env>`):
  `data.table_prefix`, every `buckets.<purpose>.name`;
- the legacy layout for the source (`rc_infra.transfer.legacy.bucket`, section 6), and the
  legacy table names, which core already knows;
- one bucket manifest per copied bucket (section 5), as the version-ID map.

**Steps, per environment**

1. Freeze writers to the source (a maintenance window): count checks cannot detect an
   update, a delete or a TTL expiry that happens during the copy.
2. Copy every bucket with `rc-data-transfer bucket --apply --manifest …`
   (`--include-versions` for the property registry).
3. Create every target table from core's schema declarations with `rc-dynamo-sync --apply`,
   under `data.table_prefix`, refusing when a target table's ARN equals its source's.
4. Copy rows as raw DynamoDB attribute values (never through the pydantic models, which
   drop unknown fields and turn arbitrary-precision numbers into floats), applying explicit
   per-table transforms only:
   - rewrite URLs of this repository's buckets (`https://<old-bucket>.s3…/<key>`) to the
     target bucket; leave every external URL alone;
   - rewrite `s3VersionId` (and any other pinned version) through the manifest:
     `(source bucket, key, source_version) -> target_version`; a version missing from the
     manifest fails the row, never silently keeps the old ID;
   - drop transient rows: deployment and matching leases, expired TTL rows, in-flight queue
     state.
5. Verify per table: item counts, plus a canonical per-key hash of every row after the
   transform; read back a sample of pinned historical revisions through their new version IDs.
6. Keep the source untouched until the application has been verified against the target.

Identity needs no migration: the existing Cognito pools stay where they are (re-infra only
references them by ID), so every account, password and `custom:user_id` is unchanged.
