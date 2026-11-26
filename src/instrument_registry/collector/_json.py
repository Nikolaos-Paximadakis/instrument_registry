"""Typed, validating access to the JSON the collectors fetch.

`response.json()` returns `Any`, which a type checker accepts anywhere, so a
feed that changes shape (a field turning into a number, an object, or null)
would flow straight into a frozen dataclass declared `str` and on into the
cache. These helpers check each value's type at the point it is read and
raise `FeedShapeError` naming where, so a changed feed fails the refresh
loudly instead of storing a wrong-typed value.

A missing *required* key raises `FeedShapeError` too, which is a
`KeyError` subclass, so existing callers that expected a `KeyError` from
`row["X"]` still see one.
"""
from __future__ import annotations

from collections.abc import Mapping

JsonObject = Mapping[str, object]


class FeedShapeError(KeyError):
    """An external feed's JSON didn't have the shape this collector reads."""

    def __str__(self) -> str:
        # KeyError's own __str__ reprs its argument, which quotes the message.
        return str(self.args[0]) if self.args else ""


def as_object(value: object, where: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise FeedShapeError(f"{where}: expected a JSON object, got {type(value).__name__}")
    return value  # pyright: ignore[reportUnknownVariableType] - keys are str in decoded JSON


def as_objects(value: object, where: str) -> list[JsonObject]:
    if not isinstance(value, list):
        raise FeedShapeError(f"{where}: expected a JSON array, got {type(value).__name__}")
    items: list[object] = value  # pyright: ignore[reportUnknownVariableType] - each item is checked by as_object
    return [as_object(item, f"{where}[{i}]") for i, item in enumerate(items)]


def opt_objects(obj: JsonObject, key: str, where: str) -> list[JsonObject]:
    """An array field that may be absent or null, meaning empty. Any other
    non-array value (false, 0, a string) is a shape error, not "empty"."""
    value = obj.get(key)
    return [] if value is None else as_objects(value, f"{where}.{key}")


def opt_object(obj: JsonObject, key: str, where: str) -> JsonObject | None:
    """An object field that may be absent or null."""
    value = obj.get(key)
    return None if value is None else as_object(value, f"{where}.{key}")


def field(obj: JsonObject, key: str, where: str) -> object:
    if key not in obj:
        raise FeedShapeError(f"{where}: missing field {key!r}")
    return obj[key]


def req_str(obj: JsonObject, key: str, where: str) -> str:
    value = field(obj, key, where)
    if not isinstance(value, str):
        raise FeedShapeError(f"{where}.{key}: expected a string, got {type(value).__name__}")
    return value


def opt_str(obj: JsonObject, key: str, where: str) -> str | None:
    """A field that may be absent or null; anything else must be a string."""
    value = obj.get(key)
    if value is not None and not isinstance(value, str):
        raise FeedShapeError(f"{where}.{key}: expected a string or null, got {type(value).__name__}")
    return value


def req_int(obj: JsonObject, key: str, where: str) -> int:
    value = field(obj, key, where)
    # bool is an int subclass; a JSON true/false is not a count.
    if not isinstance(value, int) or isinstance(value, bool):
        raise FeedShapeError(f"{where}.{key}: expected an integer, got {type(value).__name__}")
    return value
