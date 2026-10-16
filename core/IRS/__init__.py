"""
FIRST, before anything else in this package runs: make `IRS.x` and
`core.IRS.x` one module rather than two independent copies.

*: USE REASON: using core.IRS.* creates a second copy of the IRS module, and
   every isinstance()/issubclass() check against IRS types silently misses.
*THIS FIXES THE PROBLEM.*
"""
from . import _alias as _alias
_alias.install()

from .constants import *
from .buffers import BinaryReader, BinaryWriter
from .bitfields import baseType, BitField
from .fields import Field, EnumField
from .core import ArrayField, Structure, Message, Definition, Pair, pair, is_definition
from .annotations import IrsMessage, UnitCode, OpCode, Namespace, NamespaceScope
