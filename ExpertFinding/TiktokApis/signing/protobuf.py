"""Small, strict protobuf wire helpers used by TikTok's IM HTTP API.

The web IM SDK ships its own protobufjs bundle.  Pulling in the generated
bundle would make the Python client depend on browser JavaScript, so this
module implements only the primitives needed by the captured request.  It is
deliberately strict: callers must provide every field that the browser object
actually owns; no padding, truncation, or guessed defaults are performed.
"""

from __future__ import annotations


class ProtobufWireError(ValueError):
    """The supplied value cannot be represented by the captured wire shape."""


def _int(value, name: str) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ProtobufWireError(f"{name} 必须是整数") from exc


def varint(value: int, *, name: str = "value") -> bytes:
    """Encode an unsigned protobuf varint."""
    number = _int(value, name)
    if number < 0:
        # int64/int32 are not used with negative values in this request.  A
        # negative varint would be a ten-byte two's-complement value and is
        # therefore rejected rather than silently changing the wire shape.
        raise ProtobufWireError(f"{name} 不允许为负数")
    out = bytearray()
    while number > 0x7F:
        out.append((number & 0x7F) | 0x80)
        number >>= 7
    out.append(number)
    return bytes(out)


def field_varint(field_number: int, value: int, *, name: str = "value") -> bytes:
    field = _int(field_number, "field_number")
    if field <= 0:
        raise ProtobufWireError("field_number 必须为正数")
    return varint(field << 3, name="field tag") + varint(value, name=name)


def field_bytes(field_number: int, value: bytes, *, name: str = "value") -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ProtobufWireError(f"{name} 必须是 bytes")
    raw = bytes(value)
    return (varint(_int(field_number, "field_number") << 3 | 2,
                   name="field tag") + varint(len(raw), name="length") + raw)


def field_string(field_number: int, value: str, *, name: str = "value") -> bytes:
    if value is None:
        raise ProtobufWireError(f"{name} 不允许为 None")
    if not isinstance(value, str):
        value = str(value)
    return field_bytes(field_number, value.encode("utf-8"), name=name)


def field_message(field_number: int, payload: bytes, *, name: str = "message") -> bytes:
    return field_bytes(field_number, payload, name=name)

