"""Inventory 와 JSON 문서 사이의 변환.

두 방향 모두 schema/inventory.schema.json 으로 검사한다. 읽을 때는 검사를
통과한 문서만 Inventory 로 만들고, 쓸 때는 스키마를 어기는 Inventory(예:
비밀 의심 값을 남긴 것)를 문서로 내보내지 않는다.

  document = inventory_json.inventory_to_json(inventory)
  inventory = inventory_json.inventory_from_json(document)
"""

import dataclasses
import datetime
import functools
import json
import pathlib
import types
import typing
from typing import Any

import jsonschema

from crosscloud import model

# 스키마는 패키지 밖 crosscloud/schema/ 에 있다(설계 2.2 배치). 저장소
# 체크아웃에서 설치(pip install -e)해 쓰는 것을 전제로 한다.
INVENTORY_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "schema"
    / "inventory.schema.json"
)


class InvalidInventoryError(ValueError):
    """Inventory 문서가 inventory.schema.json 을 어겼다.

    메시지에는 위반 위치와 규칙 이름만 담고, 어긴 값은 담지 않는다.
    """


def inventory_to_json(inventory: model.Inventory) -> dict[str, Any]:
    """Inventory 를 스키마를 지키는 JSON 문서(dict)로 바꾼다.

    Args:
        inventory: 내보낼 Inventory.

    Returns:
        json.dumps 로 바로 쓸 수 있는 dict.

    Raises:
        InvalidInventoryError: 결과 문서가 스키마를 어길 때. 예를 들어
            attestation.suspected_values_retained 가 0 이 아니거나
            collected_at 에 시간대가 없을 때.
    """
    document = _encode(inventory)
    _validate(document)
    return document


def inventory_from_json(document: object) -> model.Inventory:
    """JSON 문서(json.loads 결과)를 검사한 뒤 Inventory 로 바꾼다.

    Args:
        document: json.loads 로 읽은 값. 바꾸지 않는다.

    Returns:
        문서와 같은 내용의 Inventory.

    Raises:
        InvalidInventoryError: 문서가 스키마를 어길 때.
    """
    _validate(document)
    return _decode(model.Inventory, document)


@functools.cache
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(INVENTORY_SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _validate(document: object) -> None:
    error = jsonschema.exceptions.best_match(_validator().iter_errors(document))
    if error is None:
        return
    # error.message 는 어긴 값을 그대로 담는다. 그 값이 비밀일 수 있으므로
    # 위치와 어긴 규칙 이름만 남긴다.
    location = "/".join(str(part) for part in error.absolute_path) or "(root)"
    raise InvalidInventoryError(
        f"inventory 스키마 위반 — {location}: '{error.validator}' 규칙"
    )


def _encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _encode(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value


def _decode(hint: Any, value: Any) -> Any:
    # 스키마 검사를 통과한 문서만 들어온다. None 은 스키마가 허용한
    # 자리에만 있다.
    if value is None:
        return None
    if dataclasses.is_dataclass(hint):
        field_hints = typing.get_type_hints(hint)
        return hint(
            **{
                name: _decode(field_hint, value[name])
                for name, field_hint in field_hints.items()
            }
        )
    origin = typing.get_origin(hint)
    if origin is tuple:
        item_hint = typing.get_args(hint)[0]
        return tuple(_decode(item_hint, item) for item in value)
    if origin is types.UnionType:
        members = [a for a in typing.get_args(hint) if a is not type(None)]
        if len(members) == 1:
            return _decode(members[0], value)
        return value
    if hint is datetime.datetime:
        return datetime.datetime.fromisoformat(value)
    return value
