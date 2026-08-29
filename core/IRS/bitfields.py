import logging
import struct
from enum import IntEnum, EnumMeta
from typing import Any, Callable

from .buffers import BinaryReader, BinaryWriter, get_packer
from .fields import BaseField
from .constants import *

logger = logging.getLogger("IRS.bitfields")

_warned_bits: set[tuple[str, str, int]] = set()


def _warn_once(enum_class: type[IntEnum], field_name: str, value: int) -> None:
    key = (enum_class.__qualname__, field_name, value)
    if key in _warned_bits:
        return
    _warned_bits.add(key)
    logger.warning(
        "%s.%s: %d is not a defined member of %s -- reading as None",
        enum_class.__qualname__, field_name, value, enum_class.__qualname__)


def baseType(byte_size: int, imposed_endian: str = None) -> Callable[[type], type]:
    """Give an IntEnum or BitField its wire size and byte order.

    `endian` defaults to whatever the declaring module declared (`ENDIAN = bigEndian`
    at the top of a structures file).
    """
    def wrapper(cls: EnumMeta | BitFieldMeta) -> EnumMeta | BitFieldMeta:
        if imposed_endian is None:
            endian = module_endian(cls.__module__)
        else:
            endian = imposed_endian
        fmt = {1: UInt8, 2: UInt16, 4: UInt32, 8: UInt64}.get(byte_size, UInt8) if isinstance(byte_size, int) else byte_size
        if isinstance(cls, BitFieldMeta):
            cls._packer_ = get_packer(endian + fmt)
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
        if enum_class is None:
            return value
        # value2member_map is identical to enum_class(value) but works much faster
        member = enum_class._value2member_map_.get(value)
        if member is None and value != 0:
            _warn_once(enum_class, name, value)
        return member

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
            # Shield against TypeError if field_type is not Int or Enum.
            is_enum = isinstance(field_type, type) and issubclass(field_type, IntEnum)
            enum_class = field_type if is_enum else None
            if is_enum:
                enum_map[field_name] = enum_class
            # Generate and attach the property directly to the class
            namespace[field_name] = _make_bit_property(field_name, current_shift, bits, enum_class)
            fields_list.append((field_name, current_shift, bits))
            current_shift += bits

        namespace['_fields_'] = tuple(fields_list)
        namespace['__enum_map__'] = enum_map
        # _packer_ is set by @baseType wrapper.
        namespace['_packer_'] = None

        return type.__new__(cls, name, bases, namespace)


class BitField(BaseField, metaclass=BitFieldMeta):
    __slots__ = ('_value',)

    def __init__(self, value: int = 0) -> None:
        self._value = value

    @classmethod
    def from_bytes(cls, reader: BinaryReader, instance: Any = None) -> 'BitField': # TODO change to Self
        packer = cls._packer_
        return cls(packer.unpack_from(reader.data, reader.offset(packer.size))[0])

    def to_bytes(self, writer: BinaryWriter, value: Any = None) -> None:
        target = value if value is not None else self
        writer.buffer += self.__class__._packer_.pack(target._value)

    @classmethod
    def from_dict(cls, data: dict) -> 'BitField': # TODO change to Self
        instance = cls(0)
        enum_map = getattr(cls, '__enum_map__', {})

        for name, _, _ in cls._fields_:
            val = data.get(name)
            if val is None:
                continue
            # TODO make sure the 2 line below are useless
            # if name in enum_map:
            #     val = enum_map[name][val].value if isinstance(val, str) else enum_map[name](val).value
            setattr(instance, name, val)
        return instance

    def to_dict(self, value: Any = None) -> dict: # TODO change anno : Any to : Self
        target = value if value is not None else self
        result = {}
        for name, _, _ in self.__class__._fields_:
            result[name] = getattr(target, name)
            if name in self.__class__.__enum_map__ and result[name] is not None:
                result[name] = result[name].name
        return result

    def _format_repr(self, indent_level: int = 0) -> str:
        inner = "    " * (indent_level + 1)
        lines = [f"<{self.__class__.__name__} (Raw: {self._value})>"]
        for field_name, _, _ in self._fields_:
            val = getattr(self, field_name)
            lines.append(f"{inner}{field_name}: {val!r}")
        return "\n".join(lines)

    def fill(self):
        return self.__class__(0)

    def __repr__(self) -> str:
        return self._format_repr()
