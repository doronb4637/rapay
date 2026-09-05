from asyncio import Task, Future

from core.IRS.annotations import IrsMessage, UnitCode, OpCode, Namespace, NamespaceScope
from typing import TypeAlias

""" IRS Types -- re-exported so `core.annotations` stays the one-stop import
for connections/tools; the source of truth lives in IRS itself so IRS never
has to import outside its own package. """
IrsMessage: TypeAlias = IrsMessage
UnitCode: TypeAlias = UnitCode
OpCode: TypeAlias = OpCode
Namespace: TypeAlias = Namespace
NamespaceScope: TypeAlias = NamespaceScope

""" Connections Types """
Task: TypeAlias = Task
Future: TypeAlias = Future
