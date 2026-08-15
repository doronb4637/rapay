from asyncio import Task, Future
from typing import Iterable

from IRS.core import Message

""" IRS Types """
IrsMessage = type[Message]
UnitCode = int
OpCode = int
#: Defined by the module '__name__'
#: this is what distinguish when defining opcode for the same unit code from erasing each other.
Namespace = str
NamespaceScope = Namespace | Iterable[Namespace]

""" Connections Types """
Task = Task
Future = Future
