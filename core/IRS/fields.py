# logic/fields.py
import struct
from enum import IntEnum
from typing import Any
from .buffers import BinaryReader, BinaryWriter
from .constants import *

class BaseField:
    __slots__ = ('name',)
    def from_bytes(self, reader: BinaryReader, instance: Any = None) -> Any: raise NotImplementedError
    def to_bytes(self, writer: BinaryWriter, value: Any) -> None: raise NotImplementedError
    def from_dict(self, value: Any) -> Any: raise NotImplementedError
    def to_dict(self, value: Any) -> Any: raise NotImplementedError


class Field(BaseField):
    __slots__ = ('packer',)
    def __init__(self, fmt: str) -> None:
        self.packer = struct.Struct(f"<{fmt}")

    def from_bytes(self, reader: BinaryReader, instance: Any = None) -> Any:
        return self.packer.unpack_from(reader.data, reader.offset(self.packer.size))[0]

    def to_bytes(self, writer: BinaryWriter, value: Any) -> None:
        writer.write(self.packer.pack(value))

    def from_dict(self, value: Any) -> Any:
        return value

    def to_dict(self, value: Any) -> Any:
        return value

class EnumField(BaseField):
    __slots__ = ('enum_class', 'packer',)
    def __init__(self, enum_class: type[IntEnum], field_name: str = None) -> None:
        self.enum_class = enum_class
        self.name = field_name
        enum_fmt = getattr(enum_class, '_baseType_', Byte)
        self.packer = struct.Struct(enum_fmt)

    def from_bytes(self, reader: BinaryReader, instance: Any = None) -> IntEnum:
        value = self.packer.unpack_from(reader.data, reader.offset(self.packer.size))[0]
        try:
            # value2member_map is identical to self.enum_class(value) but works much faster
            return self.enum_class._value2member_map_[value]
        except (KeyError, ValueError):
            raise RuntimeError(f"Invalid Enum: {value}")
        except Exception as e:
            raise

    def to_bytes(self, writer: BinaryWriter, value: Any) -> None:
        raw_val = value.value if isinstance(value, self.enum_class) else value
        writer.write(self.packer.pack(raw_val))

    def from_dict(self, value: str | int) -> IntEnum:
        return self.enum_class[value] if isinstance(value, str) else self.enum_class(value)

    def to_dict(self, value: Any) -> str:
        return value.name if isinstance(value, self.enum_class) else str(value)

