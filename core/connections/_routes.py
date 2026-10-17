"""
`RouteTable` -- who owns a `(unit_code, key)` route, and the rule that
exactly one thing does. The key is the opcode on IRS-framed links and the topic
name on DDS; this module only ever hashes and compares it.

`Connection` hands each inbound message to that route's owner: a parked
`receive_message()` future, else a standing `handle_on_receive()` callback, else
nobody -- the message is dropped, never buffered. The two owners are mutually
exclusive, and every way of violating that is refused HERE.

Connect callbacks live here too, keyed by unit name (a connect event has no
opcode); `drop_unit()` must reach every table at once.

Everything runs ON the shared event-loop thread and is deliberately not
thread-safe: `Connection`'s public API marshals every mutation onto that thread,
and a lock here would only hide a caller that forgot to.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from core.annotations import IrsMessage, OpCode, UnitCode

UnitName = str
#: What names a message on a link: its opcode on IRS-framed links, its topic
#: name on DDS (which puts no opcode on the wire -- the topic IS the message).
MessageKey = OpCode | str
RouteKey = tuple[UnitCode, MessageKey]
ReceiveCallback = Callable[[IrsMessage], Any]
ConnectCallback = Callable[[UnitName], Any]


def describe_key(key: MessageKey) -> str:
    """A message key as logs and errors spell it."""
    return f"opcode={key:#06x}" if isinstance(key, int) else f"topic={key!r}"


def _describe(route: RouteKey) -> str:
    """How a route is spelled in every error below -- one place, so the four
    refusals cannot drift apart."""
    return f"(unit_code={route[0]}, {describe_key(route[1])})"


class RouteTable:
    """The subscription, on-receive and on-connect registries of one connection."""

    __slots__ = ("_subscriptions", "_callbacks", "_connect_callbacks")

    def __init__(self) -> None:
        self._subscriptions: dict[RouteKey, asyncio.Future[IrsMessage]] = {}
        self._callbacks: dict[RouteKey, ReceiveCallback] = {}
        self._connect_callbacks: dict[UnitName, ConnectCallback] = {}

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #
    def owner_of(self, route: RouteKey) -> asyncio.Future[IrsMessage] | ReceiveCallback | None:
        """Who this route currently belongs to, or None if nobody does.

        A parked `receive_message()` outranks a standing callback, though the
        two can never coexist -- the priority only matters for the instant
        between a future being satisfied and its slot being released.
        """
        future = self._subscriptions.get(route)
        if future is not None and not future.done():
            return future
        return self._callbacks.get(route)

    # ------------------------------------------------------------------ #
    # Subscriptions (one in-flight receive_message per route)
    # ------------------------------------------------------------------ #
    def claim(self, route: RouteKey, loop: asyncio.AbstractEventLoop) -> asyncio.Future[IrsMessage]:
        """
        Register the future that makes `route` subscribed, and return it without
        awaiting, so `receive_message` can run its trigger function in between
        and an immediate reply is captured rather than dropped as unsubscribed.

        The future is bound to `loop` because it is created by one coroutine and
        awaited by another.
        """
        if route in self._callbacks:
            raise RuntimeError(
                f"route {_describe(route)} already has an on-receive callback; "
                f"call stop_on_receive() before receive_message()"
            )
        existing = self._subscriptions.get(route)
        if existing is not None and not existing.done():
            # One subscriber per route, by design. Silently replacing the
            # earlier future would strand that caller forever, so say so.
            raise RuntimeError(
                f"already subscribed to {_describe(route)}: "
                f"only one receive_message() may be in flight per route"
            )
        future: asyncio.Future[IrsMessage] = loop.create_future()
        self._subscriptions[route] = future
        return future

    def settle(self, route: RouteKey, future: asyncio.Future[IrsMessage]) -> None:
        """Free `route` once `future` is done with it (delivered, timed out or
        cancelled). Removes only OUR future: a later subscriber may already own
        the slot, and evicting theirs would strand them."""
        if self._subscriptions.get(route) is future:
            del self._subscriptions[route]

    def release(self, route: RouteKey, future: asyncio.Future[IrsMessage]) -> None:
        """Release a subscription that will never be awaited (the caller's
        trigger function raised), so the route does not stay claimed."""
        self.settle(route, future)
        if not future.done():
            future.cancel()

    # ------------------------------------------------------------------ #
    # Standing on-receive callbacks
    # ------------------------------------------------------------------ #
    def register_callback(self, route: RouteKey, callback: ReceiveCallback) -> None:
        existing = self._subscriptions.get(route)
        if existing is not None and not existing.done():
            raise RuntimeError(
                f"route {_describe(route)} has a receive_message() in flight; "
                f"a route cannot be polled and handled at once"
            )
        if route in self._callbacks:
            raise RuntimeError(
                f"route {_describe(route)} already has an on-receive callback; "
                f"call stop_on_receive() first"
            )
        self._callbacks[route] = callback

    def unregister_callback(self, route: RouteKey) -> bool:
        """True if there was one to remove. A callback already running is left
        to finish -- only the registration goes."""
        return self._callbacks.pop(route, None) is not None

    # ------------------------------------------------------------------ #
    # Standing on-connect callbacks (keyed by unit, not by route)
    # ------------------------------------------------------------------ #
    def register_connect(self, unit_name: UnitName, callback: ConnectCallback) -> None:
        if unit_name in self._connect_callbacks:
            raise RuntimeError(
                f"unit {unit_name!r} already has an on-connect callback; "
                f"call stop_on_connect() first"
            )
        self._connect_callbacks[unit_name] = callback

    def unregister_connect(self, unit_name: UnitName) -> bool:
        return self._connect_callbacks.pop(unit_name, None) is not None

    def connect_callback(self, unit_name: UnitName) -> ConnectCallback | None:
        return self._connect_callbacks.get(unit_name)

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #
    def drop_unit(self, unit_code: UnitCode | None, reason: BaseException) -> None:
        """
        Retire every route belonging to `unit_code`: fail whoever is parked on
        one with `reason`, and forget its standing callbacks. The on-connect
        callback is kept, so a unit that comes back is greeted again.

        A `unit_code` of None (an unknown unit) matches nothing.
        """
        for route in [key for key in self._subscriptions if key[0] == unit_code]:
            future = self._subscriptions.pop(route)
            if not future.done():
                future.set_exception(reason)
        for route in [key for key in self._callbacks if key[0] == unit_code]:
            del self._callbacks[route]

    def fail_all_subscriptions(self, reason: BaseException) -> None:
        """Fail every parked `receive_message()` with `reason` (connection
        teardown). Called last, once sockets and tasks are gone, so the caller
        wakes to a connection that is fully down."""
        for future in self._subscriptions.values():
            if not future.done():
                future.set_exception(reason)
        self._subscriptions.clear()
