"""PEP 561 stub-only package: makes bare `import IRS` / `from IRS import ...`
resolve statically in IDEs and type checkers.

This is NOT a runtime package -- there is no `__init__.py` here, only `.pyi`
stub files, so Python's real import system never sees it and it cannot
collide with `core/IRS/_alias.py`'s runtime aliasing (which is what actually
makes `import IRS` work when the code runs). See `core/IRS/_alias.py`'s
docstring and `core/IRS/CLAUDE.md` for why a *real* top-level `IRS/` package
would break that.

Keep this in sync with `core/IRS/__init__.py`'s re-exports.
"""
from core.IRS.constants import *
from core.IRS.buffers import BinaryReader as BinaryReader, BinaryWriter as BinaryWriter
from core.IRS.bitfields import baseType as baseType, BitField as BitField
from core.IRS.fields import Field as Field, EnumField as EnumField
from core.IRS.core import ArrayField as ArrayField, Structure as Structure, Message as Message
from core.IRS.annotations import (
    IrsMessage as IrsMessage,
    UnitCode as UnitCode,
    OpCode as OpCode,
    Namespace as Namespace,
    NamespaceScope as NamespaceScope,
)
