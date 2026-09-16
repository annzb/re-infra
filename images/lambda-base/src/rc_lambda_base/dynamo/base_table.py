from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic
from uuid import uuid4

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from rc_lambda_base import numeric
from rc_lambda_base.aws import dynamodb_resource
from rc_lambda_base.dynamo.base_item import BaseItem, ItemType, KeyType
from rc_lambda_base.dynamo.schema_diff import SchemaDiff, diff_schemas


class TableError(Exception):
    pass


class ItemAlreadyExistsError(TableError):
    pass


class ItemDoesNotExistError(TableError):
    pass


@dataclass(frozen=True)
class QueryPlan:
    index_name: str | None
    key_condition: Any
    filter_expression: Any


class BaseTable(Generic[ItemType]):
    table_name: str = ""
    item_model: type[ItemType]
    insert_unknown_columns_on_recreate: bool = True
    generated_pk_max_attempts: int = 1

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        if cls is BaseTable:
            return

        table_name = getattr(cls, "table_name", None)
        if not isinstance(table_name, str):
            raise TypeError(f"{cls.__name__} must define table_name as a string")

        item_model = getattr(cls, "item_model", None)
        if item_model is None:
            raise TypeError(f"{cls.__name__} must define item_model")
        if not isinstance(item_model, type):
            raise TypeError(f"{cls.__name__}.item_model must be a BaseItem class, not an instance")
        if not issubclass(item_model, BaseItem):
            raise TypeError(f"{cls.__name__}.item_model must inherit from BaseItem")
        if item_model is BaseItem:
            raise TypeError(f"{cls.__name__}.item_model cannot be BaseItem itself")
        generated_pk_max_attempts = getattr(cls, "generated_pk_max_attempts", None)
        if not isinstance(generated_pk_max_attempts, int) or generated_pk_max_attempts < 1:
            raise TypeError(f"{cls.__name__}.generated_pk_max_attempts must be a positive integer")

    def __init__(self, table: Any = None, *, resource: Any = None):
        """Bind the table lazily.

        ``table`` injects a ready boto3 ``Table`` handle (tests, fakes);
        ``resource`` injects the DynamoDB resource to build it from (unusual
        endpoints). With neither, the handle is built on first use from the
        environment-configured resource, so declaring module-level table
        instances never touches boto3 at import time.
        """
        self._table = table
        self._resource = resource

    @property
    def table(self):
        if self._table is None:
            resource = self._resource if self._resource is not None else dynamodb_resource()
            self._table = resource.Table(self.table_name)
        return self._table

    @property
    def pk_name(self) -> str:
        return self.item_model.partition_key

    @property
    def sk_name(self) -> str | None:
        return self.item_model.sort_key

    @property
    def gsis(self) -> Mapping[str, str]:
        return self.item_model.gsis

    @property
    def key_fields(self) -> list[str]:
        return self.item_model.key_fields()

    def _to_item(self, item: ItemType, *, exclude_none: bool = True) -> dict[str, Any]:
        return numeric.float_to_decimal(item.model_dump(mode="python", exclude_none=exclude_none))

    def _from_item(self, raw: dict[str, Any]) -> ItemType:
        return self.item_model(**numeric.decimal_to_float(raw))

    def _key_to_dict(self, key_value: KeyType) -> dict[str, Any]:
        if self.sk_name is None:
            if isinstance(key_value, tuple):
                raise ValueError(f"{self.__class__.__name__} does not have a sort key.")
            return {self.pk_name: key_value}

        if not isinstance(key_value, tuple) or len(key_value) != 2:
            raise ValueError(
                f"{self.__class__.__name__} expected key tuple ({self.pk_name!r}, {self.sk_name!r})"
            )

        return numeric.float_to_decimal(
            {
                self.pk_name: key_value[0],
                self.sk_name: key_value[1],
            }
        )

    def _condition_failed(self, error: ClientError) -> bool:
        return error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"

    def _condition_exists(self, exists: bool) -> tuple[str, dict[str, str]]:
        fn = "attribute_exists" if exists else "attribute_not_exists"
        return f"{fn}(#pk)", {"#pk": self.pk_name}

    def _new_id(self, *args, **kwargs) -> str:
        return uuid4().hex

    def _validate_query_fields(self, field_values: Mapping[str, Any]) -> None:
        invalid_fields = [
            field_name
            for field_name in field_values
            if field_name not in self.item_model.model_fields
        ]
        if invalid_fields:
            raise ValueError(f"{self.item_model.__name__} has no fields: {invalid_fields}")

        none_fields = [field_name for field_name, value in field_values.items() if value is None]
        if none_fields:
            raise ValueError(f"query_filter() does not support None values: {none_fields}")

    def _validate_return_column(self, return_column: str | None) -> None:
        if return_column is None:
            return

        if not isinstance(return_column, str) or not return_column:
            raise ValueError("return_column must be a non-empty string")

        if return_column not in self.item_model.model_fields:
            raise ValueError(f"{self.item_model.__name__} has no field {return_column!r}")

    def _expression_and(self, expressions: list[Any]) -> Any:
        if not expressions:
            return None
        expression = expressions[0]
        for next_expression in expressions[1:]:
            expression = expression & next_expression
        return expression

    def _plan_equality_query(self, field_values: Mapping[str, Any]) -> QueryPlan:
        key_fields: set[str] = set()
        index_name: str | None = None
        key_condition = None

        if self.pk_name in field_values:
            key_fields.add(self.pk_name)
            key_condition = Key(self.pk_name).eq(field_values[self.pk_name])
            if self.sk_name and self.sk_name in field_values:
                key_fields.add(self.sk_name)
                key_condition = key_condition & Key(self.sk_name).eq(field_values[self.sk_name])
        else:
            for field_name, candidate_index_name in self.gsis.items():
                if field_name in field_values:
                    key_fields.add(field_name)
                    index_name = candidate_index_name
                    key_condition = Key(field_name).eq(field_values[field_name])
                    break

        filter_expression = None
        for field_name, value in field_values.items():
            if field_name in key_fields:
                continue
            condition = Attr(field_name).eq(value)
            filter_expression = (
                condition if filter_expression is None else filter_expression & condition
            )

        return QueryPlan(
            index_name=index_name,
            key_condition=key_condition,
            filter_expression=filter_expression,
        )

    def get_item(self, key_value: KeyType, **kwargs: Any) -> ItemType | None:
        kwargs = self._kwargs_with_key_projection(kwargs)
        item = (
            self.table.get_item(
                Key=numeric.float_to_decimal(self._key_to_dict(key_value)),
                **numeric.float_to_decimal(kwargs),
            ).get("Item")
            or {}
        )
        return self._from_item(item) if item else None

    def scan_page(self, return_column: str | None = None, **kwargs: Any) -> dict[str, Any]:
        self._validate_return_column(return_column)
        kwargs = self._kwargs_with_return_column(kwargs, return_column)

        if return_column is None:
            kwargs = self._kwargs_with_key_projection(kwargs)

        response = numeric.decimal_to_float(self.table.scan(**numeric.float_to_decimal(kwargs)))
        raw_items = response.get("Items", [])
        if return_column is not None:
            items = [item.get(return_column) for item in raw_items]
        else:
            items = [self._from_item(item) for item in raw_items]

        return {**response, "Items": items}

    def scan_raw(
        self, return_column: str | None = None, **kwargs: Any
    ) -> list[ItemType] | list[Any]:
        self._validate_return_column(return_column)
        items: list[Any] = []
        response = self.scan_page(
            return_column=return_column,
            **kwargs,
        )
        items.extend(response.get("Items", []))

        while "LastEvaluatedKey" in response:
            response = self.scan_page(
                return_column=return_column,
                ExclusiveStartKey=response["LastEvaluatedKey"],
                **kwargs,
            )
            items.extend(response.get("Items", []))

        return items

    def query_page(self, return_column: str | None = None, **kwargs: Any) -> dict[str, Any]:
        self._validate_return_column(return_column)
        kwargs = self._kwargs_with_return_column(kwargs, return_column)
        if return_column is None:
            kwargs = self._kwargs_with_key_projection(kwargs)

        response = numeric.decimal_to_float(self.table.query(**numeric.float_to_decimal(kwargs)))
        raw_items = response.get("Items", [])
        if return_column is not None:
            items = [item.get(return_column) for item in raw_items]
        else:
            items = [self._from_item(item) for item in raw_items]

        return {**response, "Items": items}

    def query_raw(
        self, return_column: str | None = None, **kwargs: Any
    ) -> list[ItemType] | list[Any]:
        self._validate_return_column(return_column)
        items: list[Any] = []
        response = self.query_page(return_column=return_column, **kwargs)
        items.extend(response.get("Items", []))

        while "LastEvaluatedKey" in response:
            response = self.query_page(
                return_column=return_column,
                ExclusiveStartKey=response["LastEvaluatedKey"],
                **kwargs,
            )
            items.extend(response.get("Items", []))

        return items

    def query_filter(
        self, return_column: str | None = None, **field_values: Any
    ) -> list[ItemType] | list[Any]:
        if not field_values:
            raise ValueError("query_filter() requires at least one field condition")

        self._validate_return_column(return_column)
        field_values = numeric.float_to_decimal(dict(field_values))
        self._validate_query_fields(field_values)
        plan = self._plan_equality_query(field_values)
        if plan.key_condition is None:
            kwargs: dict[str, Any] = {
                "FilterExpression": plan.filter_expression,
            }
            return self.scan_raw(
                return_column=return_column,
                **kwargs,
            )

        kwargs = {"KeyConditionExpression": plan.key_condition}
        if plan.index_name:
            kwargs["IndexName"] = plan.index_name
        if plan.filter_expression is not None:
            kwargs["FilterExpression"] = plan.filter_expression

        return self.query_raw(return_column=return_column, **kwargs)

    def create(self, **fields: Any) -> ItemType:
        generate_pk = self.pk_name not in fields or fields[self.pk_name] is None
        max_attempts = self.generated_pk_max_attempts if generate_pk else 1

        for attempt in range(1, max_attempts + 1):
            if generate_pk:
                fields[self.pk_name] = self._new_id(**fields)

            item = self.item_model(**fields)
            condition, expression_names = self._condition_exists(exists=False)
            try:
                self.table.put_item(
                    Item=self._to_item(item),
                    ConditionExpression=condition,
                    ExpressionAttributeNames=expression_names,
                )
                return item
            except ClientError as error:
                if not self._condition_failed(error):
                    raise

                if generate_pk and attempt < max_attempts:
                    continue

                raise ItemAlreadyExistsError(
                    f"{self.item_model.__name__} with key {item.key_value()!r} already exists"
                ) from error

        # Unreachable, but keeps static type checkers aware that create() cannot
        # implicitly return None.
        raise RuntimeError("create() exhausted attempts unexpectedly")

    def _used_expression_attribute_placeholders(
        self, *expressions: Any
    ) -> tuple[set[str], set[str]]:
        expression_text = " ".join(expr for expr in expressions if isinstance(expr, str))
        names = set()
        values = set()
        for token in expression_text.replace(",", " ").replace("(", " ").replace(")", " ").split():
            stripped = token.rstrip(",")
            if stripped.startswith("#"):
                names.add(stripped)
            elif stripped.startswith(":"):
                values.add(stripped)
        return names, values

    def _prune_unused_expression_attributes(self, kwargs: dict[str, Any]) -> None:
        """Drop expression placeholders DynamoDB would reject as unused."""
        used_names, used_values = self._used_expression_attribute_placeholders(
            kwargs.get("UpdateExpression"),
            kwargs.get("ConditionExpression"),
            kwargs.get("ProjectionExpression"),
            kwargs.get("FilterExpression"),
            kwargs.get("KeyConditionExpression"),
        )
        names = kwargs.get("ExpressionAttributeNames")
        if names and used_names:
            kwargs["ExpressionAttributeNames"] = {k: v for k, v in names.items() if k in used_names}
        elif names == {}:
            kwargs.pop("ExpressionAttributeNames", None)

        values = kwargs.get("ExpressionAttributeValues")
        if values and used_values:
            kwargs["ExpressionAttributeValues"] = {
                k: v for k, v in values.items() if k in used_values
            }
        elif values == {}:
            kwargs.pop("ExpressionAttributeValues", None)

    def _key_error_description(self, key: Mapping[str, Any]) -> str:
        return ", ".join(f"{field_name}={key.get(field_name)!r}" for field_name in self.key_fields)

    def _key_value_from_dict(self, key: Mapping[str, Any]) -> KeyType:
        if self.sk_name is None:
            return key[self.pk_name]
        return key[self.pk_name], key[self.sk_name]

    def _projected_attribute_names(
        self,
        projection_expression: str,
        expression_names: Mapping[str, str] | None,
    ) -> set[str]:
        names = expression_names or {}
        projected: set[str] = set()
        for part in projection_expression.split(","):
            token = part.strip()
            if not token:
                continue
            # Dynamo projections may use aliases (``#name``) and may also select
            # nested paths.  For model construction we only care whether the
            # top-level key attributes are included.
            top_level = token.split(".", 1)[0].split("[", 1)[0].strip()
            projected.add(names.get(top_level, top_level))
        return projected

    def _kwargs_with_key_projection(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        projection_expression = kwargs.get("ProjectionExpression")
        if not projection_expression or not isinstance(projection_expression, str):
            return dict(kwargs)

        projected = self._projected_attribute_names(
            projection_expression,
            kwargs.get("ExpressionAttributeNames"),
        )
        missing_key_fields = [
            field_name for field_name in self.key_fields if field_name not in projected
        ]
        if not missing_key_fields:
            return dict(kwargs)

        return {
            **dict(kwargs),
            "ProjectionExpression": ", ".join([projection_expression, *missing_key_fields]),
        }

    def _kwargs_with_return_column(
        self,
        kwargs: Mapping[str, Any],
        return_column: str | None,
    ) -> dict[str, Any]:
        kwargs = dict(kwargs)
        if return_column is None:
            return kwargs

        if "ProjectionExpression" in kwargs:
            raise ValueError("ProjectionExpression cannot be supplied when return_column is set")

        expression_names = dict(kwargs.get("ExpressionAttributeNames") or {})
        placeholder = "#return_column"
        while placeholder in expression_names and expression_names[placeholder] != return_column:
            placeholder += "_"

        expression_names[placeholder] = return_column
        kwargs["ProjectionExpression"] = placeholder
        kwargs["ExpressionAttributeNames"] = expression_names
        return kwargs

    def _existing_item_condition(self) -> tuple[str, dict[str, str]]:
        expression_names = {
            f"#k{index}": field_name for index, field_name in enumerate(self.key_fields)
        }
        condition = " AND ".join(
            f"attribute_exists(#k{index})" for index in range(len(self.key_fields))
        )
        return condition, expression_names

    def _validate_update_mapping(
        self, fields: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not fields:
            raise ValueError("update() requires a non-empty item or field mapping")

        missing_keys = [field_name for field_name in self.key_fields if field_name not in fields]
        if missing_keys:
            raise ValueError(
                f"update() requires key field(s) {missing_keys!r} for {self.item_model.__name__}"
            )

        empty_keys = [
            field_name
            for field_name in self.key_fields
            if fields.get(field_name) is None or fields.get(field_name) == ""
        ]
        if empty_keys:
            raise ValueError(
                f"update() key field(s) cannot be null or empty for "
                f"{self.item_model.__name__}: {empty_keys!r}"
            )

        key = {field_name: fields[field_name] for field_name in self.key_fields}
        updates = {
            field_name: value
            for field_name, value in fields.items()
            if field_name not in self.key_fields
        }
        if not updates:
            raise ValueError("update() requires at least one non-key field to update")
        return key, updates

    def _update_expression_from_fields(
        self, fields: Mapping[str, Any]
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        expression_names: dict[str, str] = {}
        expression_values: dict[str, Any] = {}
        assignments: list[str] = []
        for index, (field_name, value) in enumerate(fields.items()):
            name_key = f"#u{index}"
            value_key = f":u{index}"
            expression_names[name_key] = field_name
            expression_values[value_key] = value
            assignments.append(f"{name_key} = {value_key}")
        return f"SET {', '.join(assignments)}", expression_names, expression_values

    def _replace_existing(self, item: ItemType) -> ItemType:
        condition, expression_names = self._existing_item_condition()

        try:
            self.table.put_item(
                Item=self._to_item(item, exclude_none=False),
                ConditionExpression=condition,
                ExpressionAttributeNames=expression_names,
            )
            return item
        except ClientError as error:
            if self._condition_failed(error):
                key = self._key_to_dict(item.key_value())
                raise ItemDoesNotExistError(
                    f"{self.item_model.__name__} with key "
                    f"{self._key_error_description(key)} does not exist"
                ) from error
            raise

    def update(
        self,
        item_or_fields: ItemType | Mapping[str, Any],
        *,
        condition_expression: Any = None,
        condition_names: Mapping[str, str] | None = None,
        condition_values: Mapping[str, Any] | None = None,
        return_values: str = "ALL_NEW",
    ) -> ItemType | None:
        """Update an existing item.

        Pass a model instance to replace the full existing item, including null
        fields. Pass a mapping with all key fields to update only the non-key
        fields present in that mapping. Mapping updates leave all omitted
        database attributes unchanged and write explicit ``None`` values as
        DynamoDB ``NULL`` values.
        """
        if isinstance(item_or_fields, self.item_model):
            return self._replace_existing(item_or_fields)
        if not isinstance(item_or_fields, Mapping):
            raise TypeError(
                f"update() expects {self.item_model.__name__} or a mapping with "
                f"key fields {self.key_fields!r}"
            )

        key, updates = self._validate_update_mapping(item_or_fields)
        update_expression, update_names, update_values = self._update_expression_from_fields(
            updates
        )

        expression_names = {**dict(condition_names or {}), **update_names}
        expression_values = {**dict(condition_values or {}), **update_values}
        exists_condition, key_condition_names = self._existing_item_condition()
        default_condition_expression = condition_expression is None
        if default_condition_expression:
            condition_expression = exists_condition
        else:
            condition_expression = f"({exists_condition}) AND ({condition_expression})"
        expression_names = {**key_condition_names, **expression_names}

        kwargs: dict[str, Any] = {
            "Key": numeric.float_to_decimal(key),
            "UpdateExpression": update_expression,
            "ConditionExpression": condition_expression,
            "ExpressionAttributeNames": expression_names,
            "ExpressionAttributeValues": numeric.float_to_decimal(expression_values),
            "ReturnValues": return_values,
        }
        self._prune_unused_expression_attributes(kwargs)

        try:
            response = self.table.update_item(**kwargs)
        except ClientError as error:
            if self._condition_failed(error):
                item_missing = self.get_item(self._key_value_from_dict(key)) is None
                if default_condition_expression or item_missing:
                    raise ItemDoesNotExistError(
                        f"{self.item_model.__name__} with key "
                        f"{self._key_error_description(key)} does not exist"
                    ) from error
            raise
        attributes = response.get("Attributes") or {}
        return self._from_item(attributes) if attributes else None

    def delete(self, key_value: KeyType) -> None:
        self.table.delete_item(Key=numeric.float_to_decimal(self._key_to_dict(key_value)))

    def expected_schema(self) -> dict[str, Any]:
        item_model = self.item_model
        key_schema = {
            "partition_key": item_model.partition_key,
            "sort_key": getattr(item_model, "sort_key", None),
        }
        # The shorthand can only express a HASH key, so sort_key and projection
        # are None = "not modeled". schema_diff treats that as "leave the live
        # value alone", not as "the live index must have no sort key / must be ALL".
        gsis = {
            index_name: {
                "partition_key": attr_name,
                "sort_key": None,
                "projection": None,
                "non_key_attributes": None,
            }
            for attr_name, index_name in getattr(item_model, "gsis", {}).items()
        }
        # Explicit full specs win over the attr→index shorthand.
        for index_name, gsi_schema in getattr(item_model, "gsi_schemas", {}).items():
            non_key_attributes = gsi_schema.get("non_key_attributes")
            gsis[index_name] = {
                "partition_key": gsi_schema.get("partition_key"),
                "sort_key": gsi_schema.get("sort_key"),
                "projection": gsi_schema.get("projection"),
                "non_key_attributes": (
                    frozenset(non_key_attributes) if non_key_attributes else None
                ),
            }
        attribute_types = {
            attr_name: "S" for attr_name in self._schema_key_attribute_names(key_schema, gsis)
        }
        explicit_attribute_types = getattr(item_model, "attribute_types", {})
        attribute_types.update(explicit_attribute_types)
        return {
            "table_name": self.table_name,
            "billing_mode": "PAY_PER_REQUEST",
            "key_schema": key_schema,
            "gsis": gsis,
            "attribute_types": attribute_types,
        }

    def actual_schema(self) -> dict[str, Any]:
        response = self.table.meta.client.describe_table(TableName=self.table_name)
        table = response["Table"]
        key_schema = self._parse_key_schema(table.get("KeySchema", []))
        gsis = {
            gsi["IndexName"]: {
                **self._parse_key_schema(gsi.get("KeySchema", [])),
                **self._parse_projection(gsi.get("Projection", {})),
            }
            for gsi in table.get("GlobalSecondaryIndexes", [])
        }
        key_attrs = self._schema_key_attribute_names(key_schema, gsis)
        attribute_types = {
            attr["AttributeName"]: attr["AttributeType"]
            for attr in table.get("AttributeDefinitions", [])
            if attr["AttributeName"] in key_attrs
        }
        return {
            "table_name": table["TableName"],
            "billing_mode": table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED"),
            "key_schema": key_schema,
            "gsis": gsis,
            "attribute_types": attribute_types,
        }

    def schema_diff(self) -> SchemaDiff:
        """Classified differences between this table's declaration and the live one.

        Delegates to rc_lambda_base.dynamo.schema_diff, which is also what
        rc-dynamo-sync and rc-dynamo-report use -- so all three agree on what
        changed and on what should be done about it.
        """
        actual = self.actual_schema() if self.exists() else None
        return diff_schemas(self.expected_schema(), actual)

    def exists(self) -> bool:
        try:
            self.table.meta.client.describe_table(TableName=self.table_name)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return False
            raise

    def _parse_key_schema(self, key_schema: list[dict[str, str]]) -> dict[str, str | None]:
        parsed: dict[str, str | None] = {
            "partition_key": None,
            "sort_key": None,
        }
        for key in key_schema:
            if key["KeyType"] == "HASH":
                parsed["partition_key"] = key["AttributeName"]
            elif key["KeyType"] == "RANGE":
                parsed["sort_key"] = key["AttributeName"]
        return parsed

    def _parse_projection(self, projection: dict[str, Any]) -> dict[str, Any]:
        non_key_attributes = projection.get("NonKeyAttributes")
        return {
            "projection": projection.get("ProjectionType"),
            # A set, because describe_table returns NonKeyAttributes unordered.
            "non_key_attributes": frozenset(non_key_attributes) if non_key_attributes else None,
        }

    def _schema_key_attribute_names(
        self,
        key_schema: dict[str, str | None],
        gsis: dict[str, dict[str, Any]],
    ) -> set[str]:
        attrs: set[str] = set()
        for field in ("partition_key", "sort_key"):
            name = key_schema.get(field)
            if name:
                attrs.add(name)
        for gsi in gsis.values():
            for field in ("partition_key", "sort_key"):
                name = gsi.get(field)
                if name:
                    attrs.add(name)

        return attrs
