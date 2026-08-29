# logic/fields.py
import struct
from enum import IntEnum
from typing import Any
from .buffers import BinaryReader, BinaryWriter, get_packer
from .constants import *

class BaseField:
    __slots__ = ('_name',)
    def from_bytes(self, reader: BinaryReader, instance: Any = None) -> Any: raise NotImplementedError
    def to_bytes(self, writer: BinaryWriter, value: Any) -> None: raise NotImplementedError
    def from_dict(self, value: Any) -> Any: raise NotImplementedError
    def to_dict(self, value: Any) -> Any: raise NotImplementedError
    def fill(self) -> Any: raise NotImplementedError

class Field(BaseField):
    __slots__ = ('packer',)
    def __init__(self, fmt: str, endian: str = little_endian) -> None:
        self.packer = get_packer(endian + fmt)

    def from_bytes(self, reader: BinaryReader, instance: Any = None) -> int:
        packer = self.packer
        return packer.unpack_from(reader.data, reader.offset(packer.size))[0]

    def to_bytes(self, writer: BinaryWriter, value: int) -> None:
        writer.buffer += self.packer.pack(value)

    def from_dict(self, value: Any) -> Any:
        return value

    def to_dict(self, value: Any) -> Any:
        return value

    def fill(self) -> int | float:
        if self.packer.format[-1] in Floats:
            return 0.0
        return 0

class EnumField(BaseField):
    __slots__ = ('enum_class', 'packer',)
    def __init__(self, enum_class: type[IntEnum], field_name: str = None) -> None:
        self.enum_class = enum_class
        self._name = field_name
        enum_fmt = getattr(enum_class, '_baseType_', Byte)
        self.packer = get_packer(enum_fmt)

    def from_bytes(self, reader: BinaryReader, instance: Any = None) -> IntEnum | None:
        packer = self.packer
        value = packer.unpack_from(reader.data, reader.offset(packer.size))[0]
        # value2member_map is identical to self.enum_class(value) but works much faster
        member = self.enum_class._value2member_map_.get(value)
        if member is not None:
            return member
        if value == 0:
            return None
        raise ValueError(
            f"{self.enum_class.__qualname__}: {value} is not a defined member")

    def to_bytes(self, writer: BinaryWriter, value: Any) -> None:
        if value is None:
            raw_val = 0
        elif isinstance(value, self.enum_class):
            raw_val = value.value
        else:
            raw_val = value
        writer.buffer += self.packer.pack(raw_val)

    def from_dict(self, value: str | int | None) -> IntEnum | None:
        if value is None:
            return None
        return self.enum_class[value] if isinstance(value, str) else self.enum_class(value)

    def to_dict(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value.name
        return self.enum_class(value).name

    def fill(self) -> IntEnum | None:
        """None is the deliberate to_bytes writes it as 0"""
        try:
            return self.enum_class(0)
        except (KeyError, ValueError):
            return None
