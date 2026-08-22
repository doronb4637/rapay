"""IRS's own type aliases -- kept inside IRS so the package never has to
reach out to `core.annotations` for its own vocabulary. `core.annotations`
imports these back in (`from core.IRS.annotations import *`) so every other
package keeps seeing the same names under `core.annotations`; IRS itself
never imports outside its own directory.
"""
from typing import Iterable

from .core import Message


IrsMessage = type[Message]
UnitCode = OpCode = int
Namespace = str
NamespaceScope = Namespace | Iterable[Namespace]
