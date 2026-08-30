# logic/core.py
from typing import Any, Type
from enum import IntEnum
from beartype.door import is_bearable


from . import _compiler
from .buffers import BinaryReader, BinaryWriter
from .bitfields import BitField, baseType
from .containers import FixedList
from .fields import BaseField, Field, EnumField
from .constants import *

def OpCode(opCode: int):
    """ add _opCode variable to the message class"""
    def wrapper(msg: type[Message]) -> type[Message]:
        msg._opCode = opCode
        return msg
    return wrapper

class ArrayField(BaseField):
    __slots__ = ('baseType', 'length',)

    def __init__(self, base_type: Any, length: str | int | None, endian: str = little_endian) -> None:
        if isinstance(base_type, str):
            self.baseType = Field(base_type, endian)
        elif isinstance(base_type, type) and issubclass(base_type, IntEnum):
            self.baseType = EnumField(base_type)
        elif isinstance(base_type, type) and issubclass(base_type, (Structure, BitField)):
            self.baseType = base_type()
        else:
            self.baseType = base_type
        self.length = length

    def from_bytes(self, reader: BinaryReader, instance: Any = None) -> list[Any]:
        parse = self.baseType.from_bytes
        if isinstance(self.length, int):
            # Declared as exactly N items, so the LENGTH is part of the layout:
            # editing a value is fine, growing the array is a wire violation.
            return FixedList(parse(reader, instance) for _ in range(self.length))
        if self.length is not None:
            array_len = getattr(instance, self.length)
            return [parse(reader, instance) for _ in range(array_len)]
        array = []
        while reader.offset < reader._len:
            array.append(parse(reader, instance))
        return array

    def to_bytes(self, writer: BinaryWriter, value: list[Any]) -> None:
        if isinstance(self.length, int) and len(value) != self.length:
            raise ValueError(
                f"{getattr(self, '_name', '<array>')!r} holds {len(value)} items but is "
                f"declared as exactly {self.length}")
        write = self.baseType.to_bytes
        for item in value:
            write(writer, item)

    def from_dict(self, value: list[Any]) -> list[Any]:
        items = [self.baseType.from_dict(item) for item in value]
        return FixedList(items) if isinstance(self.length, int) else items

    def to_dict(self, value: list[Any]) -> list[Any]:
        return [self.baseType.to_dict(item) for item in value]

    def fill(self) -> list[Any]:
        if self.length is None or isinstance(self.length, str):
            return []
        if isinstance(self.baseType, Structure):
            return FixedList(type(self.baseType)().fill() for _ in range(self.length))
        return FixedList(self.baseType.fill() for _ in range(self.length))

class MessageMeta(type):
    def __new__(cls, name: str, bases: tuple, namespace: dict) -> 'MessageMeta': # TODO change to Self
        if name in ("Structure", "Message"):
             return type.__new__(cls, name, bases, namespace)

        fields_list = []
        annotations = namespace.get('__annotations__', {})
        endian = module_endian(namespace.get('__module__'))

        for field_name, field_type in annotations.items():
            field_value = namespace.get(field_name)

            if isinstance(field_value, list) and len(field_value) == 2:
                field_value = ArrayField(field_value[0], field_value[1], endian)
                field_value._name = field_name
                fields_list.append(field_value)
                namespace.pop(field_name, None)

            elif isinstance(field_value, str):
                field = Field(field_value, endian)
                field._name = field_name
                fields_list.append(field)
                namespace.pop(field_name, None)
            else:
                if isinstance(field_type, type) and issubclass(field_type, IntEnum):
                    field = EnumField(field_type, field_name)
                    fields_list.append(field)
                elif isinstance(field_type, type) and issubclass(field_type, (Structure, BitField)):
                    field = field_type()
                    field._name = field_name
                    fields_list.append(field)
                else:
                    raise TypeError(f"{name}.{field_name}: annotated {field_type!r} with no Type/Size")
        namespace['_fields_'] = tuple(fields_list)
        namespace['__slots__'] = tuple(annotations.keys())
        if _compiler.ENABLED:
            # The first parse of this layout compiles a function specific to it
            # and replaces these two; every later one goes straight there. See
            # `_compiler`'s module docstring for what that buys and why it is
            # lazy. A layout the generator cannot express drops back to the
            # `_fields_` loops below, which stay the reference semantics.
            namespace['from_bytes'] = classmethod(_compiler.lazy_from_bytes)
            namespace['to_bytes'] = _compiler.lazy_to_bytes
        built = type.__new__(cls, name, bases, namespace)
        _compiler.register(built)
        return built


class Structure(BaseField, metaclass=MessageMeta):
    __slots__ = ()

    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __setattr__(self, name: str, value: Any) -> None:
        """
        Intercepts assignments to enforce strict typing based on annotations.
        Only used in run time assignments *Not in to_bytes, to_dict or from_bytes, from_dict.
        """
        annotations = getattr(self.__class__, '__annotations__', {})
        expected_type = annotations.get(name, None)
        if expected_type is None:
            super().__setattr__(name, value)
            return
        if isinstance(expected_type, type) and issubclass(expected_type, IntEnum):
            if value is not None and not isinstance(value, expected_type):
                try:
                    expected_type(value) if isinstance(value, int) else expected_type[value]
                except (KeyError, ValueError, TypeError):
                    raise TypeError(f"{self.__class__.__name__}.{name} expects a "
                                    f"{expected_type.__name__} member, name, value, or None, "
                                    f"got {value!r}.") from None
        elif not is_bearable(value, expected_type):
            raise TypeError(f"{self.__class__.__name__}.{name} expects {expected_type}, got {type(value).__name__}.")
        super().__setattr__(name, value)

    @classmethod
    def from_bytes(cls, reader: BinaryReader, instance: Any = None) -> 'Structure': # TODO change to Self
        new_instance = cls.__new__(cls)
        for field in cls._fields_:
            object.__setattr__(new_instance, field._name, field.from_bytes(reader, new_instance))
        return new_instance

    def to_bytes(self, writer: BinaryWriter | None = None, value: Any = None) -> bytes | None:
        """Serialize into `writer`, or return the bytes when there is no writer.

        The no-writer form used to live on `Message` alone, which made
        `some_nested_struct.to_bytes()` a TypeError for no reason anyone chose --
        and left the compiled path (which has no such restriction) visibly
        different from this one. Both spellings now mean the same thing at every
        depth: a writer means append and return None, no writer means give me
        the bytes.
        """
        is_root = writer is None
        if is_root:
            writer = BinaryWriter()
        target = value if value is not None else self
        for field in target.__class__._fields_:
            field.to_bytes(writer, getattr(target, field._name))
        if is_root:
            return bytes(writer)

    @classmethod
    def from_dict(cls, data: dict) -> 'Structure': # TODO change to Self
        instance = cls.__new__(cls)
        for field in cls._fields_:
            if field._name in data:
                val = field.from_dict(data[field._name])
                object.__setattr__(instance, field._name, val)
        return instance

    def to_dict(self, value: Any = None) -> dict:
        target = value if value is not None else self
        result = {}
        for field in target.__class__._fields_:
            if hasattr(target, field._name):
                raw_val = getattr(target, field._name)
                result[field._name] = field.to_dict(raw_val)
        return result

    def fill(self) -> 'Structure':
        """Explicitly fills uninitialized fields with safe default values."""
        for field in self.__class__._fields_:
            # Does the class already have this field initialized?
            if not hasattr(self, field._name):
                value = type(field)().fill() if isinstance(field, Structure) else field.fill()
                object.__setattr__(self, field._name, value)
                continue
            value = getattr(self, field._name)
            if isinstance(value, Structure):
                value.fill()
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Structure):
                        item.fill()
        return self

    def _format_repr(self, indent_level: int = 0) -> str:
        inner = "    " * (indent_level + 1)
        lines = [f"<{self.__class__.__name__}>"]
        for field in self.__class__._fields_:
            if not hasattr(self, field._name): continue
            val = getattr(self, field._name)
            if isinstance(val, list):
                lines.append(f"{inner}{field._name}: [")
                for item in val:
                    if hasattr(item, '_format_repr'): lines.append(f"{inner}    {item._format_repr(indent_level + 2).lstrip()}")
                    else: lines.append(f"{inner}    {item!r}")
                lines.append(f"{inner}]")
            elif hasattr(val, '_format_repr'):
                lines.append(f"{inner}{field._name}: {val._format_repr(indent_level + 1).lstrip()}")
            else:
                lines.append(f"{inner}{field._name}: {val!r}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return self._format_repr()


class Message(Structure):
    """A layout that is a whole payload rather than a piece of one.

    Behaviourally identical to `Structure` -- `to_bytes()` with no writer
    returns bytes at every depth. The distinction `Message` carries is one of
    ROLE: only a `Message` is what `REGISTRY.register_message` binds to a
    (unitCode, opCode) route, and only a `Message` is what `irs_parser` hands
    back.
    """
    __slots__ = ()


