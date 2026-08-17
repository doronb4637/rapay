"""
Class-based message-handler sugar over `Connection.handle_on_receive`.

`route(opCode)` tags a plain method with the opcode it answers; every
`BaseUnitHandler` subclass collects its tagged methods into a class-level
opcode -> method-name map at class-definition time. Installing a handler on
a unit (`ConnectionManager.create(..., handler_class=...)`) does nothing more
than call `unit.handle_on_receive(opcode, bound_method, unit_name=...)` once
per route -- every existing dispatch behaviour (mutual exclusion with a live
`receive_message()` on the same route, executor-thread execution, eager
`validate_irs`, exception swallowing) applies unchanged, because this installs
an ORDINARY entry in `Connection._callbacks`. No new dispatch tier is added to
base.py, and none is needed.
"""
from __future__ import annotations

from typing import Callable, TypeVar

from core.annotations import *
from .base import Connection, ReceiveCallback
from .composite import CompositeUnit

_F = TypeVar("_F", bound=Callable)
_ROUTE_ATTR = "_route_opcode"


def route(opCode: int) -> Callable[[_F], _F]:
    """Tags a `BaseUnitHandler` method with '_ROUTE_ATTR'.
    than returns the function unchanged
    `BaseUnitHandler.__init_subclass__` is what turns it into a
    registration;"""
    def _tag(func: _F) -> _F:
        setattr(func, _ROUTE_ATTR, opCode)
        return func
    return _tag


class UnitHandler:
    """
    Base class for a class-based message handler bound to one configured
    unit.

    Subclass, set `unitCode` to the CONFIGURED PEER's code this handler
    answers for (NOT this unit own `unitCode`)

    Route methods are plain `def`, never `async def`,
    `unitCode` is REQUIRED on every concrete subclass
    """
    unitCode: int
    #: opcode -> method name, built once per subclass.
    _routes: dict[int, str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not hasattr(cls, "unitCode"):
            raise TypeError(f"{cls.__name__} must set attribute 'unitCode' to a UnitHandler Class")
        tagged_names: set[str] = set()
        for klass in cls.__mro__:
            if klass is object:
                continue
            for name, value in vars(klass).items():
                if hasattr(value, _ROUTE_ATTR):
                    tagged_names.add(name)

        routes: dict[int, str] = {}
        for name in tagged_names:
            opcode = getattr(getattr(cls, name), _ROUTE_ATTR, None)
            if opcode is None:
                continue
            if opcode in routes and routes[opcode] != name:
                raise TypeError(
                    f"In Class {cls.__name__}: both {routes[opcode]!r} and {name!r} "
                    f"are routed to opCode {opcode!r}; a handler may only "
                    f"have one method per opcode"
                )
            routes[opcode] = name
        cls._routes = routes

    def __init__(self, unit: Connection | CompositeUnit) -> None:
        self.unitConnection = unit


def _config_unit_codes(unit: Connection | CompositeUnit) -> dict[str, int]:
    """ Returns config.unit_codes
    used since CompositeUnit doesn't have unit_codes attribute
    and needs to be accessed
    """
    if isinstance(unit, CompositeUnit):
        if unit._receiver is None:
            raise ValueError(
                f"CompositeUnit {unit.name!r} has no receive-capable member; "
                f"a handler_class needs somewhere to receive on"
            )
        return unit._receiver.config.unit_codes
    return unit.config.unit_codes


def install_handler(unit: Connection | CompositeUnit, handler_class: type[UnitHandler]) -> UnitHandler:
    """
    Instantiate `handler_class` bound to `unit`, and register every one of
    its `@route`-tagged methods as a standing `handle_on_receive` callback on
    whichever configured unit's code matches `handler_class.unitCode`.

    * Raises: ValueError if no configured unit on `unit` carries that unitCode.
    * Raises: IRSNotFoundError or RuntimeErrorwhatever as part of what `handle_on_receive'
    itself raises
    """
    unit_codes = _config_unit_codes(unit)
    unit_name = next((n for n, c in unit_codes.items() if c == handler_class.unitCode), None)
    if unit_name is None:
        raise ValueError(
            f"No configured unit has unitCode={handler_class.unitCode!r}; "
            f"known unit codes: {unit_codes}"
        )
    handler = handler_class(unit)
    for opcode, method_name in handler_class._routes.items():
        handler_function: ReceiveCallback = getattr(handler, method_name)
        unit.handle_on_receive(opcode, handler_function, unit_name=unit_name)
    return handler
