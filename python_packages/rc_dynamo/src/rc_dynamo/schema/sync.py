"""Synchronize Python-declared DynamoDB table schemas.

BaseTable declarations are the source of truth. What the code declares is
applied; what the code does not declare is left alone.

Applied automatically, no permission needed:
  * create a declared table that does not exist, with its declared table
    options (TTL and PITR are enabled once the table is ACTIVE);
  * create a declared GSI that does not exist;
  * rebuild a declared GSI whose live key schema or projection differs, by
    deleting and recreating that one index;
  * change a declared table option in place: billing mode (to on-demand), table
    class, streams, TTL, and *enabling* PITR or deletion protection.

The third is the "declaration-first" case: the index is declared, so the
declaration wins. It cannot lose a row -- a GSI holds only derived data -- but
the index does not exist while it is rebuilt, so queries against it fail for the
duration of the backfill. Run rc-dynamo-report before deploying to see what will
be rebuilt.

Gated behind DYNAMO_PRUNE_UNDECLARED (default false):
  * delete a live GSI that is not declared in Python.

Gated behind DYNAMO_ALLOW_PROTECTION_DOWNGRADE (default false):
  * disable PITR (discards the restore window) or deletion protection when the
    declaration says False and the live table has it enabled.

Gated behind DYNAMO_ALLOW_TABLE_RECREATE (default false):
  * dump to S3, drop, recreate and restore a table whose primary key or key
    attribute types differ. The dump bucket must already exist; it is never
    created here. The dump is deleted only after a successful restore. The new
    table gets the declared options; unmanaged TTL and PITR are carried over
    from the live table.

Refused outright, whatever the permissions:
  * a recreate of a table with deletion protection enabled, or with anything a
    recreate cannot reproduce: an LSI, KMS encryption, replicas, on-demand
    limits, an undeclared stream / IA table class / provisioned billing, a
    Kinesis streaming destination or a resource policy;
  * creating a table, or switching one, to PROVISIONED billing: capacity is
    not modeled.

Reported but never changed:
  * a live sort key or projection on an index the declaration models only
    partially. `None` in a declaration means "not modeled": the live value is
    left alone and inherited on rebuild, not reset to a default. Adopt it into
    BaseItem.gsi_schemas to make it authoritative -- rc-dynamo-report
    --format python emits the declaration for you.

Ownership tags (ManagedBy, LifecycleOwner, Environment, plus any extras) are
written on create and added to existing declared tables on --apply, so
environment teardown can identify tables this tool owns. Missing tags are
reported on a dry run but do not count as pending schema changes.

See rc_dynamo.schema.diff for the classification and for the
elements this tooling does not model, and rc_dynamo.schema.options for the
UNMANAGED rule: an option the declaration does not set is never turned off.
"""

from __future__ import annotations

import gzip
import importlib
import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError

from rc_dynamo.base_table import BaseTable
from rc_dynamo.schema.diff import (
    Finding,
    Permission,
    Remedy,
    SchemaDiff,
    Severity,
    diff_schemas,
    resolve_projection,
)
from rc_dynamo.schema.options import (
    BILLING_PAY_PER_REQUEST,
    OPTION_NAMES,
    UNMANAGED,
)
from rc_dynamo.utils import aws, numeric
from rc_dynamo.utils.settings import Settings

MANAGED_BY = "rc-dynamo-sync"
RESERVED_TAG_KEYS = frozenset({"ManagedBy", "LifecycleOwner", "Environment"})

PERMISSION_ENV_VARS: Mapping[Permission, str] = {
    Permission.PRUNE_UNDECLARED: "DYNAMO_PRUNE_UNDECLARED",
    Permission.ALLOW_TABLE_RECREATE: "DYNAMO_ALLOW_TABLE_RECREATE",
    Permission.ALLOW_PROTECTION_DOWNGRADE: "DYNAMO_ALLOW_PROTECTION_DOWNGRADE",
}

# Unmanaged options a recreate copies from the table it replaces, the way an
# unmodeled GSI projection is inherited. The other options need no copying: a
# live value that differs from the create-time default (a stream, the IA class,
# provisioned billing, deletion protection on) makes the recreate refuse instead.
INHERITED_ON_RECREATE = ("point_in_time_recovery", "ttl_attribute")


class SchemaSyncError(RuntimeError):
    pass


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else Settings.from_env()


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# ─────────────────────────── schema module contract ───────────────────────────


def load_schema_tables(module_name: str) -> Mapping[str, BaseTable[Any]]:
    """Import ``module_name`` and return its ``TABLES`` registry.

    The contract: the module exports ``TABLES``, a mapping of selection name to
    BaseTable instance. Only registered tables are ever touched -- other
    BaseTable instances in the module are ignored.
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SchemaSyncError(f"Cannot import schema module {module_name!r}: {exc}") from exc

    tables = getattr(module, "TABLES", None)
    if tables is None:
        raise SchemaSyncError(
            f"Schema module {module_name!r} does not export TABLES "
            "(a mapping of name -> BaseTable instance)"
        )
    if not isinstance(tables, Mapping):
        raise SchemaSyncError(
            f"{module_name}.TABLES must be a mapping of name -> BaseTable instance, "
            f"not {type(tables).__name__}"
        )
    invalid = sorted(
        str(name) for name, value in tables.items() if not isinstance(value, BaseTable)
    )
    if invalid:
        raise SchemaSyncError(
            f"{module_name}.TABLES entries are not BaseTable instances: {invalid}"
        )
    return tables


def select_tables(
    tables: Mapping[str, BaseTable[Any]],
    table_arg: str,
    *,
    module_name: str = "the schema module",
) -> list[BaseTable[Any]]:
    """Resolve ``--tables``: ``all`` or comma-separated ``TABLES`` keys."""
    if table_arg.strip() == "all":
        return list(tables.values())

    names = [part.strip() for part in table_arg.split(",") if part.strip()]
    if not names:
        raise SchemaSyncError("--tables must be 'all' or a comma-separated list of TABLES keys")

    unknown = [name for name in names if name not in tables]
    if unknown:
        raise SchemaSyncError(
            f"Unknown table(s) {unknown} in {module_name}.TABLES; known: {sorted(tables)}"
        )
    return [tables[name] for name in names]


# ─────────────────────────── tags ───────────────────────────


def managed_tags(environment: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The ownership tags written to every table this tool manages."""
    if not environment:
        raise SchemaSyncError("An environment name is required to tag managed tables")

    extra_tags = dict(extra or {})
    clashes = sorted(RESERVED_TAG_KEYS & set(extra_tags))
    if clashes:
        raise SchemaSyncError(
            f"Tag key(s) {clashes} are set by {MANAGED_BY} and cannot be overridden"
        )

    return {
        "ManagedBy": MANAGED_BY,
        "LifecycleOwner": MANAGED_BY,
        "Environment": environment,
        **extra_tags,
    }


def _boto_tags(tags: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in sorted(tags.items())]


def _table_arn_and_tags(client: Any, table_name: str) -> tuple[str, dict[str, str]]:
    arn = client.describe_table(TableName=table_name)["Table"]["TableArn"]
    live: dict[str, str] = {}
    kwargs: dict[str, Any] = {"ResourceArn": arn}
    while True:
        response = client.list_tags_of_resource(**kwargs)
        live.update({tag["Key"]: tag["Value"] for tag in response.get("Tags", [])})
        token = response.get("NextToken")
        if not token:
            return arn, live
        kwargs["NextToken"] = token


def ensure_table_tags(
    table: BaseTable[Any], tags: Mapping[str, str], *, apply: bool
) -> dict[str, str]:
    """Add missing or different ownership tags. Returns the tags that differed.

    Additive only: tags this tool does not set are left alone. A table that does
    not exist yet has nothing to tag.
    """
    client = table.table.meta.client
    table_name = table.table_name

    if not _table_exists(client, table_name):
        return {}

    arn, live = _table_arn_and_tags(client, table_name)
    changes = {key: value for key, value in tags.items() if live.get(key) != value}
    if not changes:
        return {}

    rendered = ", ".join(f"{key}={value}" for key, value in sorted(changes.items()))
    if apply:
        print(f"[schema-sync] Tagging {table_name}: {rendered}")
        client.tag_resource(ResourceArn=arn, Tags=_boto_tags(changes))
    else:
        print(f"[schema-sync] Would tag {table_name}: {rendered}")
    return changes


# ─────────────────────────── diff helpers ───────────────────────────


def _table_exists(client: Any, table_name: str) -> bool:
    try:
        client.describe_table(TableName=table_name)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise


def _table_diff(table: BaseTable[Any]) -> SchemaDiff:
    """Classified differences between this table's declaration and the live one."""
    client = table.table.meta.client
    expected = table.expected_schema()
    actual = table.actual_schema() if _table_exists(client, table.table_name) else None
    return diff_schemas(expected, actual)


def _granted_permissions(settings: Settings) -> dict[Permission, bool]:
    return {
        Permission.PRUNE_UNDECLARED: settings.prune_undeclared,
        Permission.ALLOW_TABLE_RECREATE: settings.allow_table_recreate,
        Permission.ALLOW_PROTECTION_DOWNGRADE: settings.allow_protection_downgrade,
    }


def _key_schema_to_boto(schema: Mapping[str, str | None]) -> list[dict[str, str]]:
    partition_key = schema.get("partition_key")
    if not partition_key:
        raise SchemaSyncError(f"Invalid key schema without partition_key: {schema}")

    key_schema = [{"AttributeName": partition_key, "KeyType": "HASH"}]
    sort_key = schema.get("sort_key")
    if sort_key:
        key_schema.append({"AttributeName": sort_key, "KeyType": "RANGE"})
    return key_schema


def _collect_attribute_definitions(schema: Mapping[str, Any]) -> list[dict[str, str]]:
    # TODO: BaseTable.expected_schema() reports key types in a top-level
    # "attribute_types" map and never sets partition_key_type/sort_key_type, so
    # every attribute below is created as "S". Harmless while every declared key
    # is a string, but a non-string GSI key would be created with the wrong type.
    attribute_types: dict[str, str] = {}

    def add_key(key_schema: Mapping[str, str | None]) -> None:
        pk = key_schema.get("partition_key")
        sk = key_schema.get("sort_key")
        pk_type = key_schema.get("partition_key_type") or "S"
        sk_type = key_schema.get("sort_key_type") or "S"

        if pk:
            attribute_types[pk] = pk_type
        if sk:
            attribute_types[sk] = sk_type

    add_key(schema["key_schema"])

    for gsi_schema in schema.get("gsis", {}).values():
        add_key(gsi_schema)

    return [
        {"AttributeName": attr_name, "AttributeType": attr_type}
        for attr_name, attr_type in sorted(attribute_types.items())
    ]


# ─────────────────────────── waiting ───────────────────────────


def _wait_table_deleted(client: Any, table_name: str, settings: Settings) -> None:
    print(f"[schema-sync] Waiting for table deletion: {table_name}")
    waiter = client.get_waiter("table_not_exists")
    waiter.wait(
        TableName=table_name,
        WaiterConfig={"Delay": max(1, int(settings.schema_poll_seconds)), "MaxAttempts": 60},
    )


def wait_table_active(client: Any, table_name: str, *, settings: Settings | None = None) -> None:
    settings = _settings(settings)
    poll_seconds = settings.schema_poll_seconds
    timeout_seconds = settings.schema_wait_timeout_seconds
    started_at = time.monotonic()

    while True:
        response = client.describe_table(TableName=table_name)
        table = response["Table"]
        table_status = table.get("TableStatus")
        gsi_progress = {
            gsi["IndexName"]: {
                "status": gsi.get("IndexStatus"),
                "backfilling": gsi.get("Backfilling"),
                "item_count": gsi.get("ItemCount"),
                "size_bytes": gsi.get("IndexSizeBytes"),
            }
            for gsi in table.get("GlobalSecondaryIndexes", [])
        }
        all_gsis_active = all(progress["status"] == "ACTIVE" for progress in gsi_progress.values())
        elapsed_seconds = time.monotonic() - started_at

        if table_status == "ACTIVE" and all_gsis_active:
            if elapsed_seconds >= poll_seconds:
                print(
                    f"[schema-sync] {table_name} is ACTIVE after "
                    f"{elapsed_seconds:.0f}s; gsis={json.dumps(gsi_progress, sort_keys=True)}"
                )
            return

        remaining_seconds = max(0.0, timeout_seconds - elapsed_seconds)
        print(
            f"[schema-sync] Waiting for {table_name}: "
            f"elapsed={elapsed_seconds:.0f}s, remaining={remaining_seconds:.0f}s, "
            f"table={table_status}, gsis={json.dumps(gsi_progress, sort_keys=True)}"
        )

        if elapsed_seconds >= timeout_seconds:
            raise SchemaSyncError(
                f"Timed out after {elapsed_seconds:.0f}s waiting for {table_name} "
                f"to become ACTIVE: table={table_status}, "
                f"gsis={json.dumps(gsi_progress, sort_keys=True)}. "
                "The DynamoDB operation may still be running in AWS; inspect it "
                "with describe-table before retrying. Increase "
                "DYNAMO_SCHEMA_WAIT_TIMEOUT_SECONDS if this duration is expected."
            )

        time.sleep(min(poll_seconds, remaining_seconds))


# ─────────────────────────── create ───────────────────────────


def _creation_options(
    table_name: str,
    declared: Mapping[str, Any],
    live: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The options a new table gets: declared ones, plus inherited unmanaged ones."""
    resolved: dict[str, Any] = {}
    for name in OPTION_NAMES:
        value = declared.get(name, UNMANAGED)
        if value is not UNMANAGED:
            resolved[name] = value
            continue
        live_value = (live or {}).get(name, UNMANAGED)
        if name in INHERITED_ON_RECREATE and live_value is not UNMANAGED:
            print(
                f"[schema-sync] {table_name}: {name} not declared; inheriting live "
                f"{live_value!r}. Declare it in table_options to choose."
            )
            resolved[name] = live_value
    if resolved.get("billing_mode", BILLING_PAY_PER_REQUEST) != BILLING_PAY_PER_REQUEST:
        raise SchemaSyncError(
            f"{table_name}: table_options declares {resolved['billing_mode']} billing, but "
            f"provisioned capacity is not modeled, so {MANAGED_BY} cannot create the "
            f"table. Declare PAY_PER_REQUEST, or create it outside this tool."
        )
    return resolved


def create_table(
    table: BaseTable[Any],
    live_gsis: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    settings: Settings | None = None,
    tags: Mapping[str, str] | None = None,
    live_options: Mapping[str, Any] | None = None,
) -> None:
    """Create the table from its declaration.

    `live_gsis` carries the projections of the table being replaced, captured
    before it was dropped. Projection cannot be altered in place, so an index
    whose declaration does not model one must be rebuilt with the projection it
    already had -- otherwise a recreate silently widens KEYS_ONLY to ALL.

    `live_options` does the same for the table options: unmanaged TTL and PITR
    settings of the replaced table are carried over, never silently dropped.
    """
    settings = _settings(settings)
    client = table.table.meta.client
    expected = table.expected_schema()
    table_name = expected["table_name"]

    if _table_exists(client, table_name):
        print(f"[schema-sync] Table already exists: {table_name}")
        return

    options = _creation_options(table_name, expected.get("options", {}), live_options)

    create_gsis: dict[str, Mapping[str, Any]] = dict(expected.get("gsis", {}))

    # Carry over indexes the code does not declare. A recreate must not become a
    # back door for deleting undeclared work -- that is what DYNAMO_PRUNE_UNDECLARED
    # is for, and it runs as its own explicit step.
    for index_name, live_gsi in (live_gsis or {}).items():
        if index_name not in create_gsis:
            print(
                f"[schema-sync] Preserving undeclared GSI {index_name} on {table_name} "
                f"across the recreate"
            )
            create_gsis[index_name] = live_gsi

    gsis = [
        {
            "IndexName": index_name,
            "KeySchema": _key_schema_to_boto(gsi_schema),
            "Projection": _resolved_projection(
                table_name, index_name, gsi_schema, (live_gsis or {}).get(index_name)
            ),
        }
        for index_name, gsi_schema in create_gsis.items()
    ]

    # Attribute definitions must cover every preserved index's keys too.
    params: dict[str, Any] = {
        "TableName": table_name,
        "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": _collect_attribute_definitions(
            {
                "key_schema": expected["key_schema"],
                "gsis": create_gsis,
            }
        ),
        "KeySchema": _key_schema_to_boto(expected["key_schema"]),
    }

    if gsis:
        params["GlobalSecondaryIndexes"] = gsis
    if tags:
        params["Tags"] = _boto_tags(tags)
    # The options CreateTable accepts directly. TTL and PITR have no CreateTable
    # parameter and are applied below, once the table is ACTIVE.
    if "table_class" in options:
        params["TableClass"] = options["table_class"]
    if "deletion_protection" in options:
        params["DeletionProtectionEnabled"] = options["deletion_protection"]
    if options.get("stream_view_type") is not None:
        params["StreamSpecification"] = {
            "StreamEnabled": True,
            "StreamViewType": options["stream_view_type"],
        }

    print(f"[schema-sync] Creating table: {table_name}")
    client.create_table(**params)
    wait_table_active(client, table_name, settings=settings)

    if options.get("ttl_attribute") is not None:
        _set_ttl(client, table_name, enabled=True, attribute=options["ttl_attribute"])
    if options.get("point_in_time_recovery") is True:
        _set_point_in_time_recovery(client, table_name, True, settings)


def _resolved_projection(
    table_name: str,
    index_name: str,
    declared_gsi: Mapping[str, Any],
    live_gsi: Mapping[str, Any] | None,
) -> dict[str, Any]:
    projection = resolve_projection(declared_gsi, live_gsi)

    if declared_gsi.get("projection") is None:
        if live_gsi is None:
            # Creation is the only moment this choice is free: projection is
            # immutable, so ALL is locked in until someone rebuilds the index.
            print(
                f"[schema-sync] {table_name}.{index_name}: no projection declared; "
                f"creating with ALL. Declare one in gsi_schemas to choose."
            )
        else:
            print(
                f"[schema-sync] {table_name}.{index_name}: no projection declared; "
                f"inheriting live {projection['ProjectionType']}"
            )

    return projection


# ─────────────────────────── table options ───────────────────────────


def _set_ttl(client: Any, table_name: str, *, enabled: bool, attribute: str) -> None:
    state = "Enabling" if enabled else "Disabling"
    print(f"[schema-sync] {state} TTL on {table_name}.{attribute}")
    client.update_time_to_live(
        TableName=table_name,
        TimeToLiveSpecification={"Enabled": enabled, "AttributeName": attribute},
    )


def _set_point_in_time_recovery(
    client: Any, table_name: str, enabled: bool, settings: Settings
) -> None:
    """Enable or disable PITR, retrying while backups are still initialising.

    A freshly created table rejects UpdateContinuousBackups with
    ContinuousBackupsUnavailableException for a short while after it turns ACTIVE.
    """
    state = "Enabling" if enabled else "Disabling"
    print(f"[schema-sync] {state} point-in-time recovery on {table_name}")
    started_at = time.monotonic()
    while True:
        try:
            client.update_continuous_backups(
                TableName=table_name,
                PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": enabled},
            )
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            elapsed_seconds = time.monotonic() - started_at
            if (
                code != "ContinuousBackupsUnavailableException"
                or elapsed_seconds >= settings.schema_wait_timeout_seconds
            ):
                raise
            print(
                f"[schema-sync] Continuous backups not yet available on {table_name}; "
                f"retrying (elapsed={elapsed_seconds:.0f}s)"
            )
            time.sleep(settings.schema_poll_seconds)


def _update_table_option(client: Any, table_name: str, settings: Settings, **params: Any) -> None:
    # One UpdateTable per option: DynamoDB rejects several kinds of change in a
    # single call (streams together with billing, for one), and each leaves the
    # table UPDATING until it settles.
    client.update_table(TableName=table_name, **params)
    wait_table_active(client, table_name, settings=settings)


def _apply_option(table: BaseTable[Any], finding: Finding, settings: Settings) -> None:
    """Bring one declared option in line with the declaration, in place."""
    client = table.table.meta.client
    table_name = table.table_name
    name, declared, live = finding.attribute, finding.declared, finding.live
    print(f"[schema-sync] {finding.message}")

    if name == "billing_mode":
        if declared != BILLING_PAY_PER_REQUEST:
            # diff classifies this as Remedy.NONE; never reached through the plan.
            raise SchemaSyncError(f"{table_name}: cannot switch billing mode to {declared}")
        _update_table_option(client, table_name, settings, BillingMode=declared)
    elif name == "table_class":
        _update_table_option(client, table_name, settings, TableClass=declared)
    elif name == "deletion_protection":
        _update_table_option(client, table_name, settings, DeletionProtectionEnabled=declared)
    elif name == "stream_view_type":
        # A live stream's view type cannot be changed; it is disabled first. The
        # new stream gets a new ARN, so its consumers must be repointed.
        if live is not None:
            _update_table_option(
                client, table_name, settings, StreamSpecification={"StreamEnabled": False}
            )
        if declared is not None:
            _update_table_option(
                client,
                table_name,
                settings,
                StreamSpecification={"StreamEnabled": True, "StreamViewType": declared},
            )
    elif name == "point_in_time_recovery":
        _set_point_in_time_recovery(client, table_name, bool(declared), settings)
    elif name == "ttl_attribute":
        if live is not None:
            _set_ttl(client, table_name, enabled=False, attribute=live)
        if declared is not None:
            try:
                _set_ttl(client, table_name, enabled=True, attribute=declared)
            except ClientError as exc:
                if live is None:
                    raise
                raise SchemaSyncError(
                    f"{table_name}: TTL was disabled on {live!r} but could not be enabled "
                    f"on {declared!r} yet ({exc}). DynamoDB allows one TTL change per "
                    f"hour; deploy again once it has passed."
                ) from exc
    else:
        raise SchemaSyncError(f"{table_name}: no in-place update for option {name!r}")


# ─────────────────────────── dump / restore ───────────────────────────


def require_dump_bucket(bucket: str | None, *, settings: Settings | None = None) -> str:
    """Fail unless ``bucket`` names an existing S3 bucket. Never creates it."""
    settings = _settings(settings)
    if not bucket:
        raise SchemaSyncError(
            "--dump-bucket or DYNAMO_SCHEMA_DUMP_BUCKET is required when "
            "DYNAMO_ALLOW_TABLE_RECREATE=true"
        )

    try:
        aws.client("s3", settings).head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchBucket", "NotFound"}:
            raise SchemaSyncError(
                f"Dump bucket {bucket!r} does not exist. {MANAGED_BY} never creates buckets; "
                "point --dump-bucket at the environment's schema-dumps bucket "
                "(SSM /rc/env/<environment>/bucket/schema-dumps)."
            ) from exc
        raise
    return bucket


def _scan_all_raw(table: BaseTable[Any]) -> list[dict[str, Any]]:
    raw_items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {}

    while True:
        response = table.table.scan(**scan_kwargs)
        raw_items.extend(response.get("Items", []))

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return raw_items

        scan_kwargs["ExclusiveStartKey"] = last_key


def _dump_table_to_s3(
    table: BaseTable[Any], bucket_name: str, settings: Settings
) -> dict[str, str]:
    s3_client = aws.client("s3", settings)

    raw_items = _scan_all_raw(table)
    serializable_items = [numeric.decimal_to_float(item) for item in raw_items]
    body = gzip.compress(json.dumps(serializable_items, separators=(",", ":")).encode("utf-8"))
    key = f"dynamo-schema-sync/{table.table_name}/{_utc_stamp()}-{uuid4().hex}.json.gz"

    print(
        f"[schema-sync] Dumping {len(serializable_items)} rows from {table.table_name} to s3://{bucket_name}/{key}"
    )

    s3_client.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=body,
        ContentType="application/json",
        ContentEncoding="gzip",
    )

    return {
        "bucket": bucket_name,
        "key": key,
        "item_count": str(len(serializable_items)),
    }


def _load_dump_from_s3(dump_ref: Mapping[str, str], settings: Settings) -> list[dict[str, Any]]:
    s3_client = aws.client("s3", settings)
    response = s3_client.get_object(Bucket=dump_ref["bucket"], Key=dump_ref["key"])
    payload = gzip.decompress(response["Body"].read()).decode("utf-8")
    return json.loads(payload)


def _delete_dump(dump_ref: Mapping[str, str], settings: Settings) -> None:
    s3_client = aws.client("s3", settings)
    print(f"[schema-sync] Deleting table dump: s3://{dump_ref['bucket']}/{dump_ref['key']}")
    s3_client.delete_object(Bucket=dump_ref["bucket"], Key=dump_ref["key"])


def _required_key_fields(table: BaseTable[Any]) -> set[str]:
    fields = {table.pk_name}
    if table.sk_name:
        fields.add(table.sk_name)
    return fields


def _strict_restore_item(table: BaseTable[Any], raw_item: dict[str, Any]) -> dict[str, Any] | None:
    allowed_fields = set(table.item_model.model_fields)
    filtered = {key: value for key, value in raw_item.items() if key in allowed_fields}
    missing_keys = [key for key in _required_key_fields(table) if key not in filtered]

    if missing_keys:
        print(f"[schema-sync] Skipping item missing new key fields {missing_keys}: {raw_item}")
        return None

    try:
        model_item = table.item_model(**filtered)
    except Exception as exc:
        print(
            f"[schema-sync] Skipping item that does not validate against "
            f"{table.item_model.__name__}: {exc}; item={filtered}"
        )
        return None

    return table._to_item(model_item)


def _permissive_restore_item(
    table: BaseTable[Any], raw_item: dict[str, Any]
) -> dict[str, Any] | None:
    missing_keys = [key for key in _required_key_fields(table) if key not in raw_item]

    if missing_keys:
        print(f"[schema-sync] Skipping item missing new key fields {missing_keys}: {raw_item}")
        return None

    return numeric.float_to_decimal(raw_item)


def _restore_table_from_s3(
    table: BaseTable[Any], dump_ref: Mapping[str, str], settings: Settings
) -> None:
    raw_items = _load_dump_from_s3(dump_ref, settings)

    if not raw_items:
        print(f"[schema-sync] No rows to restore for {table.table_name}")
        return

    restored = 0
    skipped = 0
    batch: list[dict[str, Any]] = []

    for raw_item in raw_items:
        if table.insert_unknown_columns_on_recreate:
            item = _permissive_restore_item(table, raw_item)
        else:
            item = _strict_restore_item(table, raw_item)

        if item is None:
            skipped += 1
            continue

        batch.append(item)

        if len(batch) == 25:
            restored += _batch_write_items(table, batch, settings)
            batch = []

    if batch:
        restored += _batch_write_items(table, batch, settings)

    print(f"[schema-sync] Restored {restored} rows into {table.table_name}; skipped {skipped}")


def _batch_write_items(
    table: BaseTable[Any], items: Sequence[dict[str, Any]], settings: Settings
) -> int:
    client = table.table.meta.client
    request_items = {table.table_name: [{"PutRequest": {"Item": item}} for item in items]}

    written = len(items)

    while request_items:
        response = client.batch_write_item(RequestItems=request_items)
        unprocessed = response.get("UnprocessedItems", {})
        if not unprocessed:
            return written

        request_items = unprocessed
        print(
            f"[schema-sync] Retrying {sum(len(v) for v in unprocessed.values())} "
            "unprocessed write(s)"
        )
        time.sleep(settings.schema_poll_seconds)

    return written


# ─────────────────────────── destructive steps ───────────────────────────


def _delete_table(table: BaseTable[Any], settings: Settings) -> None:
    client = table.table.meta.client
    table_name = table.table_name

    if not _table_exists(client, table_name):
        print(f"[schema-sync] Table already absent: {table_name}")
        return

    print(f"[schema-sync] Deleting table: {table_name}")
    client.delete_table(TableName=table_name)
    _wait_table_deleted(client, table_name, settings)


def _unmodeled_attachments(client: Any, table_name: str) -> list[str]:
    """Things attached to the live table that DescribeTable does not show.

    Only checked before a recreate, the one operation that would destroy them.
    Anything that cannot be checked counts as present: failing closed costs a
    retry, failing open could cost a data feed or an access policy.
    """
    found: list[str] = []
    arn = client.describe_table(TableName=table_name)["Table"].get("TableArn")

    try:
        destinations = client.describe_kinesis_streaming_destination(TableName=table_name).get(
            "KinesisDataStreamDestinations", []
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        found.append(f"Kinesis streaming destinations (could not be checked: {code})")
    else:
        live = sorted(
            str(destination.get("StreamArn"))
            for destination in destinations
            if destination.get("DestinationStatus") not in ("DISABLED", "DISABLING")
        )
        if live:
            found.append(f"Kinesis streaming destination(s) {', '.join(live)}")

    try:
        client.get_resource_policy(ResourceArn=arn)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code != "PolicyNotFoundException":
            found.append(f"a resource policy (could not be checked: {code})")
    else:
        found.append("a resource policy")

    return found


def _refuse_unsafe_recreate(table: BaseTable[Any], actual: Mapping[str, Any]) -> None:
    """Last check before anything is dumped or dropped, against a fresh read.

    The plan was classified moments ago, but this is the irreversible step, so
    re-derive the refusal from the live table instead of trusting the plan.
    """
    client = table.table.meta.client
    expected = table.expected_schema()
    reasons = [f.message for f in diff_schemas(expected, actual).unfixable()]
    reasons += [
        f"{table.table_name}: the live table has {attachment}, which a recreate would destroy"
        for attachment in _unmodeled_attachments(client, table.table_name)
    ]
    if reasons:
        raise SchemaSyncError(
            f"Refusing to recreate {table.table_name}; nothing was dumped or deleted.\n"
            + "\n".join(f"  - {reason}" for reason in reasons)
        )


def _recreate_table(
    table: BaseTable[Any],
    dump_bucket: str,
    settings: Settings,
    tags: Mapping[str, str] | None,
) -> None:
    # Capture the live GSIs and options BEFORE dropping the table: projection is
    # immutable and unmodeled ones are inherited, so once the table is gone that
    # information is unrecoverable and every index comes back as ALL. Unmanaged
    # TTL/PITR settings are inherited the same way.
    actual = table.actual_schema()
    _refuse_unsafe_recreate(table, actual)
    live_gsis = actual.get("gsis", {})
    live_options = actual.get("options", {})
    if live_options.get("stream_view_type") is not None:
        print(
            f"[schema-sync] {table.table_name}: the recreated table gets a new stream ARN; "
            f"repoint its consumers"
        )

    dump_ref = _dump_table_to_s3(table, dump_bucket, settings)
    _delete_table(table, settings)
    create_table(
        table, live_gsis=live_gsis, settings=settings, tags=tags, live_options=live_options
    )
    _restore_table_from_s3(table, dump_ref, settings)
    wait_table_active(table.table.meta.client, table.table_name, settings=settings)
    _delete_dump(dump_ref, settings)


def _delete_gsi(table: BaseTable[Any], index_name: str, settings: Settings) -> None:
    client = table.table.meta.client

    print(f"[schema-sync] Deleting GSI {index_name} from {table.table_name}")

    client.update_table(
        TableName=table.table_name,
        GlobalSecondaryIndexUpdates=[
            {"Delete": {"IndexName": index_name}},
        ],
    )

    wait_table_active(client, table.table_name, settings=settings)


def _create_gsi(
    table: BaseTable[Any],
    index_name: str,
    gsi_schema: Mapping[str, Any],
    live_gsi: Mapping[str, Any] | None,
    settings: Settings,
) -> None:
    client = table.table.meta.client
    expected = table.expected_schema()
    # Only the table key and this one index: UpdateTable rejects
    # AttributeDefinitions for attributes no key schema in the request uses.
    create_schema = {
        "key_schema": expected["key_schema"],
        "gsis": {
            index_name: gsi_schema,
        },
    }

    print(f"[schema-sync] Creating GSI {index_name} on {table.table_name}")

    client.update_table(
        TableName=table.table_name,
        AttributeDefinitions=_collect_attribute_definitions(create_schema),
        GlobalSecondaryIndexUpdates=[
            {
                "Create": {
                    "IndexName": index_name,
                    "KeySchema": _key_schema_to_boto(gsi_schema),
                    "Projection": _resolved_projection(
                        table.table_name, index_name, gsi_schema, live_gsi
                    ),
                }
            }
        ],
    )

    wait_table_active(client, table.table_name, settings=settings)


def _apply_findings(
    table: BaseTable[Any],
    findings: Sequence[Finding],
    *,
    dump_bucket: str | None,
    settings: Settings,
    tags: Mapping[str, str] | None,
) -> None:
    """Execute the remedies for one table, cheapest blast radius first.

    A table recreate subsumes every index and option change -- the new table is
    built with the declared options -- so if one is queued nothing else needs
    doing. Otherwise: apply option changes first (switching a provisioned table
    to on-demand before an index is created on it), then drop undeclared indexes
    (freeing room under the 20-GSI limit), then rebuild conflicting ones
    pairwise, then create what is missing.
    """
    live_gsis = (
        table.actual_schema().get("gsis", {})
        if _table_exists(table.table.meta.client, table.table_name)
        else {}
    )
    expected_gsis = table.expected_schema().get("gsis", {})

    by_remedy: dict[Remedy, list[Finding]] = {}
    for finding in findings:
        by_remedy.setdefault(finding.remedy, []).append(finding)

    if by_remedy.get(Remedy.CREATE_TABLE):
        create_table(table, settings=settings, tags=tags)
        return

    if by_remedy.get(Remedy.RECREATE_TABLE):
        _recreate_table(table, require_dump_bucket(dump_bucket, settings=settings), settings, tags)
        return

    # Findings arrive in OPTION_NAMES order, which puts billing mode first.
    for finding in findings:
        if finding.remedy in (Remedy.UPDATE_OPTIONS, Remedy.DISABLE_PROTECTION):
            _apply_option(table, finding, settings)

    for finding in by_remedy.get(Remedy.DELETE_GSI, []):
        _delete_gsi(table, str(finding.index_name), settings)

    # Pairwise delete+create rather than batching all deletes: it keeps the
    # window in which an index does not exist as short as possible, and a
    # mid-loop failure leaves at most one index missing instead of all of them.
    for finding in by_remedy.get(Remedy.REBUILD_GSI, []):
        index_name = str(finding.index_name)
        print(
            f"[schema-sync] Rebuilding declared GSI {index_name} on {table.table_name} "
            f"to match the declaration. Queries against this index fail until the "
            f"backfill completes."
        )
        print(f"[schema-sync]   {finding.message}")
        _delete_gsi(table, index_name, settings)
        _create_gsi(
            table, index_name, expected_gsis[index_name], live_gsis.get(index_name), settings
        )

    for finding in by_remedy.get(Remedy.CREATE_GSI, []):
        index_name = str(finding.index_name)
        _create_gsi(
            table, index_name, expected_gsis[index_name], live_gsis.get(index_name), settings
        )


# ─────────────────────────── orchestration ───────────────────────────


def _report_findings(diff: SchemaDiff, granted: Mapping[Permission, bool]) -> None:
    """Print a table's findings, grouped so the ignored ones stay visible."""
    table_name = diff.table_name

    if diff.is_clean:
        print(f"[schema-sync] OK: {table_name}")
        return

    print(f"[schema-sync] Findings for {table_name}:")
    for finding in diff.findings:
        if finding.remedy is Remedy.ADOPT_DECLARATION:
            prefix = "undeclared"
        elif finding.severity is Severity.UNSUPPORTED:
            prefix = "unsupported"
        elif finding.severity is Severity.INFO:
            prefix = "info"
        elif finding in diff.unfixable():
            prefix = "REFUSED"
        elif finding in diff.blocked(granted):
            prefix = "BLOCKED"
        else:
            prefix = str(finding.remedy)
        print(f"[schema-sync]   [{prefix}] {finding.message}")


def _sync_table(
    table: BaseTable[Any],
    *,
    apply: bool,
    dump_bucket: str | None,
    granted: Mapping[Permission, bool],
    settings: Settings,
    tags: Mapping[str, str] | None,
) -> bool:
    """Reconcile one table. Returns whether anything differed from the declaration."""
    client = table.table.meta.client
    table_name = table.table_name

    if _table_exists(client, table_name):
        wait_table_active(client, table_name, settings=settings)

    diff = _table_diff(table)
    _report_findings(diff, granted)

    if not diff.requires_action(granted):
        return False

    # Checked before permissions: no permission can resolve these, so asking for
    # one would only send the operator down the wrong path.
    unfixable = diff.unfixable()
    if unfixable:
        detail = "\n".join(f"  - {f.message}" for f in unfixable)
        raise SchemaSyncError(
            f"Cannot reconcile {table_name}; nothing was changed.\n{detail}\n"
            f"These need a change in AWS or in the declaration; no permission flag "
            f"resolves them."
        )

    blocked = diff.blocked(granted)
    if blocked:
        needed = sorted(
            {
                PERMISSION_ENV_VARS[f.required_permission]
                for f in blocked
                if f.required_permission is not None
            }
        )
        detail = "\n".join(f"  - {f.message}" for f in blocked)
        raise SchemaSyncError(
            f"Refusing to change {table_name} without permission.\n"
            f"{detail}\n"
            f"Set {' and '.join(needed)} to allow this, after confirming the "
            f"declaration is what you want:\n"
            f"  rc-dynamo-report --schema-module <module> --tables <table> --format python"
        )

    actionable = diff.actionable(granted)
    if actionable and apply:
        _apply_findings(table, actionable, dump_bucket=dump_bucket, settings=settings, tags=tags)

    return True


def _unresolved_after_apply(
    table: BaseTable[Any],
    granted: Mapping[Permission, bool],
) -> tuple[Finding, ...]:
    """Findings that --apply should have fixed but did not.

    Re-reads the live schema. UNDECLARED findings are excluded: leaving those
    alone is the intended behaviour, not a failure.
    """
    return _table_diff(table).actionable(granted)


def sync_tables(
    tables: Sequence[BaseTable[Any]],
    *,
    apply: bool,
    settings: Settings | None = None,
    dump_bucket: str | None = None,
    tags: Mapping[str, str] | None = None,
) -> bool:
    """Reconcile ``tables``; return whether any schema differed from its declaration.

    Dry run unless ``apply``. Raises SchemaSyncError when a change is blocked by
    a missing permission, when a recreate is allowed without an existing dump
    bucket, or when --apply leaves a fixable difference behind.
    """
    settings = _settings(settings)
    granted = _granted_permissions(settings)
    dump_bucket = dump_bucket or None

    # Checked up front so a missing bucket fails before any table is touched.
    if apply and settings.allow_table_recreate:
        require_dump_bucket(dump_bucket, settings=settings)

    print(
        "[schema-sync] Settings: "
        f"apply={apply}, "
        f"prune_undeclared={settings.prune_undeclared}, "
        f"allow_table_recreate={settings.allow_table_recreate}, "
        f"allow_protection_downgrade={settings.allow_protection_downgrade}, "
        f"tables={','.join(table.table_name for table in tables)}"
    )

    any_diff = False

    for table in tables:
        any_diff = (
            _sync_table(
                table,
                apply=apply,
                dump_bucket=dump_bucket,
                granted=granted,
                settings=settings,
                tags=tags,
            )
            or any_diff
        )
        if tags:
            ensure_table_tags(table, tags, apply=apply)

    if not apply:
        return any_diff

    # Never report success while a fixable difference remains. Undeclared
    # elements are excluded -- leaving those alone is the intended behaviour.
    unresolved = {
        table.table_name: [f.to_json() for f in remaining]
        for table in tables
        if (remaining := _unresolved_after_apply(table, granted))
    }

    if unresolved:
        raise SchemaSyncError(
            "Schema differences remained after --apply:\n"
            + json.dumps(unresolved, indent=2, sort_keys=True)
        )

    return any_diff
