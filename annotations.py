from asyncio import Task, Future
from IRS.core import Message

""" IRS Types """
IrsMessage = type[Message]
UnitCode = int
OpCode = int

""" Connections Types """
Task = Task
Future = Future
