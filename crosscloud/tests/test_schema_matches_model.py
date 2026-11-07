"""model.py 와 inventory.schema.json 이 서로 어긋나지 않게 지킨다.

스키마와 dataclass 는 둘 다 손으로 쓴다. 한쪽에만 필드나 Literal 값을 더하면
이 시험이 실패한다.
"""

import dataclasses
import json
import typing

import jsonschema
import pytest

from crosscloud import inventory_json, model


def _load_schema() -> dict:
    return json.loads(inventory_json.INVENTORY_SCHEMA_PATH.read_text())


def _dataclasses_reachable_from(root: type) -> list[type]:
    found: list[type] = []
    pending = [root]
    while pending:
        cls = pending.pop()
        if cls in found:
            continue
        found.append(cls)
        for hint in typing.get_type_hints(cls).values():
            pending.extend(_nested_dataclasses(hint))
    return found


def _nested_dataclasses(hint: object) -> list[type]:
    if dataclasses.is_dataclass(hint):
        return [hint]
    nested: list[type] = []
    for arg in typing.get_args(hint):
        nested.extend(_nested_dataclasses(arg))
    return nested


def _literal_values(hint: object) -> tuple | None:
    if typing.get_origin(hint) is typing.Literal:
        return typing.get_args(hint)
    return None


_MODEL_CLASSES = _dataclasses_reachable_from(model.Inventory)


def _definition(schema: dict, cls: type) -> dict:
    if cls is model.Inventory:
        return schema
    return schema["$defs"][cls.__name__]


def _resolve(schema: dict, node: dict) -> dict:
    ref = node.get("$ref")
    if ref is None:
        return node
    return schema["$defs"][ref.removeprefix("#/$defs/")]


def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(_load_schema())


def test_every_model_class_has_a_schema_definition():
    schema = _load_schema()
    nested = {cls.__name__ for cls in _MODEL_CLASSES} - {"Inventory"}
    # 대문자로 시작하는 정의는 dataclass 하나에 대응한다.
    # 소문자 정의는 여러 곳이 같이 쓰는 조각이다.
    class_definitions = {name for name in schema["$defs"] if name[0].isupper()}

    assert nested == class_definitions


@pytest.mark.parametrize("cls", _MODEL_CLASSES, ids=lambda c: c.__name__)
def test_schema_fields_match_dataclass_fields(cls):
    definition = _definition(_load_schema(), cls)
    field_names = {field.name for field in dataclasses.fields(cls)}

    assert set(definition["properties"]) == field_names
    assert set(definition["required"]) == field_names
    assert definition["additionalProperties"] is False


@pytest.mark.parametrize("cls", _MODEL_CLASSES, ids=lambda c: c.__name__)
def test_schema_enums_match_literal_values(cls):
    schema = _load_schema()
    definition = _definition(schema, cls)

    for name, hint in typing.get_type_hints(cls).items():
        values = _literal_values(hint)
        if values is None:
            continue
        prop = _resolve(schema, definition["properties"][name])
        assert set(prop["enum"]) == set(values), name


def test_model_classes_are_frozen():
    for cls in _MODEL_CLASSES:
        assert cls.__dataclass_params__.frozen, cls.__name__


def test_model_collections_are_tuples():
    for cls in _MODEL_CLASSES:
        for name, hint in typing.get_type_hints(cls).items():
            origin = typing.get_origin(hint)
            assert origin not in (list, dict, set), f"{cls.__name__}.{name}"
