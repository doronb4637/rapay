# logic/__init__.py
# FIRST, before anything else in this package runs: make `IRS.x` and
# `core.IRS.x` one module rather than two independent copies. No-op when IRS is
# imported standalone (as plain `IRS`). See _alias.py for why this is not
# optional -- the two-copies failure shows up as an empty STRUCTURE_REGISTRY and
# as every isinstance() check against IRS field types silently missing.
from . import _alias as _alias
_alias.install()

from .constants import *
from .buffers import BinaryReader, BinaryWriter
from .bitfields import baseType, BitField
from .fields import Field, EnumField
from .core import ArrayField, Structure, Message
from .annotations import IrsMessage, UnitCode, OpCode, Namespace, NamespaceScope
