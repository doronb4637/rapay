"""
Class-based handlers: sugar over `handle_on_receive` / `handle_on_connect`.

`@route(opCode)` -- or `@route(Topic)` on DDS -- tags a method;
`UnitHandler.__init_subclass__` collects the tags at class-definition time.
`install_handler` then makes one ordinary `handle_on_receive` call per route
(and one `handle_on_connect` for an `@on_connect` method), so every existing
rule -- route exclusivity, executor-thread execution, eager validation,
exceptions logged -- applies unchanged. There is no separate dispatch tier.
"""
from __future__ import annotations

from typing import Callable, TypeVar

from .base import Connection, ConnectCallback, OpCode, ReceiveCallback, UnitName
from .composite import CompositeUnit

_F = TypeVar("_F", bound=Callable)
_ROUTE_ATTR = "_route_opcode"
_ON_CONNECT_ATTR = "_is_connect_handler"


def route(opCode: OpCode) -> Callable[[_F], _F]:
    """Tag a `UnitHandler` method with the opcode (or DDS topic) it handles;
    `UnitHandler.__init_subclass__` turns the tags into registrations."""
    def _tag(func: _F) -> _F:
        setattr(func, _ROUTE_ATTR, opCode)
        return func
    return _tag


def on_connect(func: _F) -> _F:
    """Tag the one `UnitHandler` method to run each time the handler's unit
    connects. It receives the unit's name:

        class TestHandler(UnitHandler):
            unitCode = 0x01

            @on_connect
            def greet(self, unit_name: str) -> None:
                self.unitConnection.send_message(hello, HELLO_OPCODE)
    """
    setattr(func, _ON_CONNECT_ATTR, True)
    return func


class UnitHandler:
    """
    A handler bound to one configured unit. Subclasses must set `unitCode` to
    the PEER's code (not this process's own) and tag plain, synchronous
    methods with `@route` / `@on_connect`.
    """
    unitCode: int
    #: selector (opcode, or DDS topic class) -> method name, built once per subclass.
    _routes: dict[OpCode, str]
    #: name of the `@on_connect`-tagged method, or None if the subclass has
    #: none. Built once per subclass, alongside `_routes`.
    _on_connect_name: str | None

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not hasattr(cls, "unitCode"):
            raise TypeError(f"{cls.__name__} must set attribute 'unitCode' to a UnitHandler Class")
        tagged_names: set[str] = set()
        connect_names: set[str] = set()
        for klass in cls.__mro__:
            if klass is object:
                continue
            for name, value in vars(klass).items():
                if hasattr(value, _ROUTE_ATTR):
                    tagged_names.add(name)
                if getattr(value, _ON_CONNECT_ATTR, False):
                    connect_names.add(name)

        routes: dict[OpCode, str] = {}
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

        if len(connect_names) > 1:
            raise TypeError(
                f"In class {cls.__name__}: {sorted(connect_names)} are all tagged "
                f"@on_connect; a handler answers for one unit, so only one "
                f"connect method is meaningful"
            )
        cls._on_connect_name = next(iter(connect_names), None)

    def __init__(self, unit: Connection | CompositeUnit) -> None:
        self.unitConnection = unit


def _config_unit_codes(unit: Connection | CompositeUnit) -> dict[UnitName, int]:
    """
    The unit-name -> unit-code mapping a handler's `unitCode` is resolved
    against.

    A `CompositeUnit` has no config of its own, so the answer comes from the
    member that owns its inbound direction -- the same member every route and
    connect callback is registered on, which is why it has to be that one and
    not just any member.
    """
    if isinstance(unit, CompositeUnit):
        if unit.receiver is None:
            raise ValueError(
                f"CompositeUnit {unit.name!r} has no receive-capable member; "
                f"a handler_class needs somewhere to receive on"
            )
        return unit.receiver.config.unit_codes
    return unit.config.unit_codes


def install_handler(unit: Connection | CompositeUnit, handler_class: type[UnitHandler]) -> UnitHandler:
    """
    Instantiate `handler_class` bound to `unit`, and register every one of
    its `@route`-tagged methods as a standing `handle_on_receive` callback,
    plus its `@on_connect`-tagged method (if any) as a standing
    `handle_on_connect` callback, on whichever configured unit's code matches
    `handler_class.unitCode`.

    Raises `ValueError` if no configured unit carries that unitCode, and
    whatever `handle_on_receive` raises (`IRSNotFoundError`, `RuntimeError`).
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
    if handler_class._on_connect_name is not None:
        connect_function: ConnectCallback = getattr(handler, handler_class._on_connect_name)
        unit.handle_on_connect(connect_function, unit_name=unit_name)
    return handler
