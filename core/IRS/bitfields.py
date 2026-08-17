# logic/bitfields.py
import struct
from enum import IntEnum, EnumType
from typing import Any, Callable
from .buffers import BinaryReader, BinaryWriter
from .fields import BaseField
from .constants import *


def baseType(byte_size: int, endian ="<") -> Callable[[type], type]:
    def wrapper(cls: EnumType | BitFieldMeta) -> EnumType | BitFieldMeta:
        fmt = {1: UInt8, 2: UInt16, 4: UInt32, 8: UInt64}.get(byte_size, UInt8) if isinstance(byte_size, int) else byte_size
        if isinstance(cls, BitFieldMeta):
            cls._packer_ = struct.Struct(endian + fmt)
            # Safety Enum Configurations
            allocated_bits = cls._packer_.size * 8
            actual_bits = sum(bits for _, _, bits in cls._fields_)
            if actual_bits > allocated_bits: raise ValueError(f"BitField '{cls.__name__}' defines {actual_bits} bits, "
                    f"but its @baseType only allocates {allocated_bits} bits.")
        else:
            cls._baseType_ = endian + fmt
        return cls
    return wrapper


def _make_bit_property(name: str, shift: int, bits: int, enum_class: type[IntEnum] | None = None):
    mask = (1 << bits) - 1

    def getter(self):
        value = (self._value >> shift) & mask
        if enum_class:
            # value2member_map is identical to self.enum_class(value) but works much faster
            try:
                return enum_class._value2member_map_[value]
            except (KeyError, ValueError):
                raise RuntimeError(f"Invalid Enum: {value}")
            except Exception as e:
                raise
        return value

    def setter(self, value):
        if enum_class:
            if isinstance(value, str):
                value = enum_class[value].value
            elif isinstance(value, enum_class):
                value = value.value

        value = int(value) & mask
        self._value = (self._value & ~(mask << shift)) | (value << shift)

    return property(getter, setter)


class BitFieldMeta(type):
    def __new__(cls, name: str, bases: tuple, namespace: dict) -> 'BitFieldMeta': # TODO change to Self
        if name == "BitField":
            return type.__new__(cls, name, bases, namespace)

        annotations = namespace.get('__annotations__', {})
        enum_map = {}
        fields_list = []
        current_shift = 0

        for field_name, field_type in annotations.items():
            bits = namespace.pop(field_name)  # Strict: Bits must be defined

            # Shield against TypeError if field_type is a string/generic
            is_enum = isinstance(field_type, type) and issubclass(field_type, IntEnum)
            enum_class = field_type if is_enum else None

            if is_enum:
                enum_map[field_name] = enum_class

            # Generate and attach the property directly to the class
            namespace[field_name] = _make_bit_property(field_name, current_shift, bits, enum_class)
            fields_list.append((field_name, current_shift, bits))

            current_shift += bits

        # Strict Format Resolution: Respect @baseType padding over auto-calculation
        namespace['_packer_'] = None
        namespace['_fields_'] = tuple(fields_list)
        namespace['__enum_map__'] = enum_map

        # Memory efficiency: lock it down to a single backing integer
        namespace['__slots__'] = ('_value', 'name')

        return type.__new__(cls, name, bases, namespace)


class BitField(BaseField, metaclass=BitFieldMeta):
    __slots__ = ()

    def __init__(self, value: int = 0) -> None:
        self._value = value

    @classmethod
    def from_bytes(cls, reader: BinaryReader, instance: Any = None) -> 'BitField': # TODO change to Self
        value = cls._packer_.unpack_from(reader.data, reader.offset(cls._packer_.size))[0]
        new_instance = cls(value)
        return new_instance

    def to_bytes(self, writer: BinaryWriter, value: Any = None) -> None:
        target = value if value is not None else self
        writer.write(self.__class__._packer_.pack(target._value))

    @classmethod
    def from_dict(cls, data: dict) -> 'BitField': # TODO change to Self
        instance = cls()
        instance._value = 0
        enum_map = getattr(cls, '__enum_map__', {})

        for name, _, _ in cls._fields_:
            val = data[name]  # Strict: Raises KeyError if missing
            if name in enum_map:
                val = enum_map[name][val].value if isinstance(val, str) else enum_map[name](val).value
            setattr(instance, name, val)
        return instance

    def to_dict(self, value: Any = None) -> dict:
        target = value if value is not None else self
        result = {}
        for name, _, _ in self.__class__._fields_:
            result[name] = getattr(target, name)
            if name in self.__class__.__enum_map__:
                result[name] = result[name].name
        return result

    def _format_repr(self, indent_level: int = 0) -> str:
        inner = "    " * (indent_level + 1)
        lines = [f"<{self.__class__.__name__} (Raw: {self._value})>"]
        for field_name, _, _ in self._fields_:
            val = getattr(self, field_name)
            lines.append(f"{inner}{field_name}: {val!r}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return self._format_repr()
