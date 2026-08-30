"""
FIRST, before anything else in this package runs: make `IRS.x` and
`core.IRS.x` one module rather than two independent copies.

*: USE REASON: using core.IRS.* creates a second copy of the IRS module.
   STRUCTURE_REGISTRY will always stay empty.
   and every isinstance() check against IRS field types silently missing.
*THIS FIXES THE PROBLEM.*
"""
from . import _alias as _alias
_alias.install()

from .constants import *
from .buffers import BinaryReader, BinaryWriter
from .bitfields import baseType, BitField
from .containers import FixedList
from .fields import Field, EnumField
from .core import ArrayField, Structure, Message
from ._compiler import compile_all, dump_source
from .annotations import IrsMessage, UnitCode, OpCode, Namespace, NamespaceScope
