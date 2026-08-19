from asyncio import Task, Future

from core.IRS.annotations import IrsMessage, UnitCode, OpCode, Namespace, NamespaceScope

""" IRS Types -- re-exported so `core.annotations` stays the one-stop import
for connections/tools; the source of truth lives in IRS itself so IRS never
has to import outside its own package. """
IrsMessage = IrsMessage
UnitCode = UnitCode
OpCode = OpCode
Namespace = Namespace
NamespaceScope = NamespaceScope

""" Connections Types """
Task = Task
Future = Future
