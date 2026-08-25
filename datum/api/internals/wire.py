"""Walking protobuf tags without decoding them. Shared by the two wire formats."""

from __future__ import annotations

import zlib

import cramjam
from google.protobuf.internal.decoder import _DecodeVarint
from google.protobuf.message import DecodeError

CONTENT_TYPE = "application/x-protobuf"

# What a body being unreadable looks like, whichever format it claimed to be.
UNREADABLE = (
    DecodeError,
    cramjam.DecompressionError,
    zlib.error,
    ValueError,
    OSError,
    IndexError,
)

VARINT = 0
FIXED_64 = 1
LENGTH_DELIMITED = 2
FIXED_32 = 5


class UnknownWireType(ValueError):
    """A tag datum cannot step over, so the rest of the body is unreadable."""


class BodyTooLarge(ValueError):
    """Decompresses past what one request could hold. Answers 413."""


def read_varint(payload: bytes, position: int) -> tuple[int, int]:
    return _DecodeVarint(payload, position)


def skip(payload: bytes, position: int, wire_type: int) -> int:
    """Past one field's value, without decoding it."""
    if wire_type == LENGTH_DELIMITED:
        length, position = _DecodeVarint(payload, position)
        return position + length
    if wire_type == VARINT:
        return _DecodeVarint(payload, position)[1]
    if wire_type == FIXED_64:
        return position + 8
    if wire_type == FIXED_32:
        return position + 4
    raise UnknownWireType(f"unknown protobuf wire type {wire_type}")
