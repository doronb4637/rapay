# logic/core.py
from typing import Any, Type
from enum import IntEnum
from beartype.door import is_bearable

from .buffers import BinaryReader, BinaryWriter
from .bitfields import BitField, baseType
from .fields import BaseField, Field, EnumField
from .constants import *


class ArrayField(BaseField):
    __slots__ = ('baseType', 'length',)

    def __init__(self, base_type: Any, length: str | int | None) -> None:
        if isinstance(base_type, str): self.baseType = Field(base_type)
        elif isinstance(base_type, type) and issubclass(base_type, IntEnum): self.baseType = EnumField(base_type)
        elif isinstance(base_type, type) and issubclass(base_type, (Structure, BitField)): self.baseType = base_type()
        else: self.baseType = base_type
        self.length = length

    def from_bytes(self, reader: BinaryReader, instance: Any = None) -> list[Any]:
        if self.length is not None:
            array_len = getattr(instance, self.length) if isinstance(self.length, str) else self.length
            return [self.baseType.from_bytes(reader, instance) for _ in range(array_len)]
        array = []
        while not reader.is_empty():
            array.append(self.baseType.from_bytes(reader, instance))
        return array

    def to_bytes(self, writer: BinaryWriter, value: list[Any]) -> None:
        for item in value:
            self.baseType.to_bytes(writer, item)

    def from_dict(self, value: list[Any]) -> list[Any]:
        return [self.baseType.from_dict(item) for item in value]

    def to_dict(self, value: list[Any]) -> list[Any]:
        return [self.baseType.to_dict(item) for item in value]

    def fill(self) -> list[Any]:
        if self.length is None or isinstance(self.length, str):
            return []
        return [self.baseType.fill()] * self.length

class MessageMeta(type):
    def __new__(cls, name: str, bases: tuple, namespace: dict) -> 'MessageMeta': # TODO change to Self
        if name in ("Structure", "Message"):
             return type.__new__(cls, name, bases, namespace)

        fields_list = []
        annotations = namespace.get('__annotations__', {})

        for field_name, field_type in annotations.items():
            field_value = namespace.get(field_name)

            if isinstance(field_value, list) and len(field_value) == 2:
                field_value = ArrayField(field_value[0], field_value[1])
                field_value._name = field_name
                fields_list.append(field_value)
                namespace.pop(field_name, None)

            elif isinstance(field_value, str):
                field = Field(field_value)
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

        namespace['_fields_'] = tuple(fields_list)
        namespace['__slots__'] = tuple(annotations.keys())
        return type.__new__(cls, name, bases, namespace)


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
        if isinstance(expected_type, type) and issubclass(expected_type, IntEnum):
                expected_type = expected_type | int | str
        elif not is_bearable(value, expected_type):
            raise TypeError(f"{self.__class__.__name__}.{name} expects {expected_type}, got {type(value).__name__}.")
        super().__setattr__(name, value)

    @classmethod
    def from_bytes(cls, reader: BinaryReader, instance: Any = None) -> 'Structure': # TODO change to Self
        new_instance = cls()
        for field in cls._fields_:
            object.__setattr__(new_instance, field._name, field.from_bytes(reader, new_instance))
        return new_instance

    def to_bytes(self, writer: BinaryWriter, value: Any = None) -> None:
        target = value if value is not None else self
        for field in target.__class__._fields_:
            field.to_bytes(writer, getattr(target, field._name))

    @classmethod
    def from_dict(cls, data: dict) -> 'Structure': # TODO change to Self
        instance = cls()
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
            if not hasattr(self, field._name):
                object.__setattr__(self, field._name, field.fill())
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
    __slots__ = ()

    def to_bytes(self, writer: BinaryWriter | None = None, value: Any = None) -> bytes | None:
        is_root = writer is None
        if is_root: writer = BinaryWriter()
        super().to_bytes(writer, value)
        if is_root: return writer.get_bytes()


