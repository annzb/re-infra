from __future__ import annotations

from collections.abc import Hashable
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, ClassVar, Dict, List, Mapping, Optional, TypeVar, Union

from pydantic import BaseModel

KeyType = Union[Hashable, tuple[Hashable, Hashable]]

_GSI_SCHEMA_KEYS = frozenset({'partition_key', 'sort_key', 'projection', 'non_key_attributes'})
_PROJECTION_TYPES = frozenset({'ALL', 'KEYS_ONLY', 'INCLUDE'})


class BaseItem(BaseModel):
    partition_key: ClassVar[str]
    sort_key: ClassVar[Optional[str]] = None
    # the declared order of gsis is the priority order when selecting an index
    gsis: ClassVar[Mapping[str, str]] = MappingProxyType({})
    # Full specs for GSIs the attr→index `gsis` mapping cannot express: composite
    # HASH+RANGE indexes, a second index sharing a HASH attribute, or a
    # projection other than ALL. Keyed by index name; overlays any `gsis`-derived
    # entry of the same name in expected_schema(). Not consulted for query
    # planning — indexes declared only here must be queried explicitly.
    #
    # Supported keys:
    #   partition_key      required; must name a model field
    #   sort_key           optional; must name a model field
    #   projection         optional; ALL | KEYS_ONLY | INCLUDE
    #   non_key_attributes required iff projection == INCLUDE
    #
    # Omitting sort_key or projection means "not modeled" — the live value is
    # left alone and inherited on rebuild, NOT reset to a default. See
    # rc_lambda_base.dynamo.schema_diff for the full rule.
    gsi_schemas: ClassVar[Mapping[str, Mapping[str, Any]]] = MappingProxyType({})

    # Attributes known to exist in the table but deliberately not modeled as
    # fields. Purely an acknowledgement channel for the drift report, so a
    # reviewed-and-intentional attribute stops being reported every run.
    ignored_attributes: ClassVar[frozenset[str]] = frozenset()

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)

        if cls is BaseItem:
            return

        if not getattr(cls, 'partition_key', None):
            raise TypeError(f'{cls.__name__} must define partition_key')

        if cls.partition_key not in cls.model_fields:
            raise TypeError(f'{cls.__name__}.partition_key={cls.partition_key!r} does not match any model field')

        if cls.sort_key is not None and cls.sort_key not in cls.model_fields:
            raise TypeError(f'{cls.__name__}.sort_key={cls.sort_key!r} does not match any model field')

        invalid_gsi_fields = [field_name for field_name in cls.gsis if field_name not in cls.model_fields]
        if invalid_gsi_fields:
            raise TypeError(f'{cls.__name__}.gsis contains fields that do not exist on the model: {invalid_gsi_fields}')
        cls.gsis = MappingProxyType(dict(cls.gsis))

        for index_name, gsi_schema in cls.gsi_schemas.items():
            # Reject typos outright. An unrecognised key used to be accepted here
            # and then silently dropped by expected_schema(), so a misspelled
            # 'projection' looked like it worked.
            unknown_keys = sorted(set(gsi_schema) - _GSI_SCHEMA_KEYS)
            if unknown_keys:
                raise TypeError(
                    f'{cls.__name__}.gsi_schemas[{index_name!r}] has unknown key(s) {unknown_keys}; '
                    f'supported keys are {sorted(_GSI_SCHEMA_KEYS)}'
                )

            if not gsi_schema.get('partition_key'):
                raise TypeError(f'{cls.__name__}.gsi_schemas[{index_name!r}] must declare a partition_key')
            invalid_schema_fields = [
                field_name
                for field_name in (gsi_schema.get('partition_key'), gsi_schema.get('sort_key'))
                if field_name is not None and field_name not in cls.model_fields
            ]
            if invalid_schema_fields:
                raise TypeError(
                    f'{cls.__name__}.gsi_schemas[{index_name!r}] references fields that do not exist '
                    f'on the model: {invalid_schema_fields}'
                )

            projection = gsi_schema.get('projection')
            if projection is not None and projection not in _PROJECTION_TYPES:
                raise TypeError(
                    f'{cls.__name__}.gsi_schemas[{index_name!r}].projection={projection!r} '
                    f'must be one of {sorted(_PROJECTION_TYPES)}'
                )

            non_key_attributes = gsi_schema.get('non_key_attributes')
            if projection == 'INCLUDE' and not non_key_attributes:
                raise TypeError(
                    f'{cls.__name__}.gsi_schemas[{index_name!r}] declares projection=INCLUDE '
                    f'but no non_key_attributes'
                )
            if non_key_attributes and projection != 'INCLUDE':
                raise TypeError(
                    f'{cls.__name__}.gsi_schemas[{index_name!r}] declares non_key_attributes '
                    f'but projection is {projection!r}, not INCLUDE'
                )
            # Deliberately NOT requiring non_key_attributes to be model fields:
            # permissive models carry real attributes that are not declared, and
            # that rule would make adopting a live INCLUDE index impossible.

        cls.gsi_schemas = MappingProxyType({
            index_name: MappingProxyType(dict(gsi_schema))
            for index_name, gsi_schema in cls.gsi_schemas.items()
        })

    @classmethod
    def key_fields(cls) -> List[str]:
        fields = [cls.partition_key]
        if cls.sort_key:
            fields.append(cls.sort_key)
        return fields

    def model_post_init(self, __context: Any) -> None:
        for field_name in self.key_fields():
            value = getattr(self, field_name, None)
            if value is None:
                raise ValueError(f"Missing key field {field_name!r}")
            if isinstance(value, str) and not value:
                raise ValueError(f"Key field {field_name!r} cannot be empty")

    def key_value(self) -> KeyType:
        pk_value = getattr(self, self.partition_key)
        if self.sort_key is None:
            return pk_value
        sk_value = getattr(self, self.sort_key)
        return (pk_value, sk_value)

    def to_dict(self, *, exclude_none: bool = True) -> Dict[str, Any]:
        return self.model_dump(mode='python', exclude_none=exclude_none)

    # def to_json(self, *, exclude_none: bool = True) -> Dict[str, Any]:
    #     return self.model_dump(mode='json', exclude_none=exclude_none)


ItemType = TypeVar("ItemType", bound=BaseItem)
