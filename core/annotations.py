from asyncio import Task, Future
from typing import Iterable

from IRS.core import Message

""" IRS Types """
IrsMessage = type[Message]
UnitCode = int
OpCode = int
#: The structures module a layout was registered from -- its `__name__`.
#: A structures file describes ONE link, so this is what keeps two files that
#: both define an opcode for the same unit code from erasing each other.
Namespace = str
#: One namespace or several. A unit's `Structures` is already a list, so every
#: scoped IRS lookup accepts both spellings; None/empty means "search all".
NamespaceScope = Namespace | Iterable[Namespace]

""" Connections Types """
Task = Task
Future = Future
