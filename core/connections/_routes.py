"""
`RouteTable` -- who owns a `(unit_code, opcode)` route, and the rule that
exactly one thing does.

`Connection` hands each inbound message to that route's owner: a parked
`receive_message()` future if one is in flight, else a standing
`handle_on_receive()` callback, else nobody -- in which case the message is
dropped, never buffered. Those two owners are mutually exclusive by design, and
the four ways that can be violated (subscribing over a callback, subscribing
over a live subscription, registering a callback over a subscription,
registering one over a callback) are all refused HERE rather than in four
separate methods on `Connection`, each with its own nearly-identical message.

Connect callbacks live here too. They are keyed by unit name alone -- a connect
event has no opcode -- but they share this object because they share its
lifecycle: `drop_unit()` and the teardown helpers have to reach every table at
once, or a retired unit keeps a live registration.

Everything here runs ON the shared event-loop thread and is deliberately not
thread-safe: `Connection`'s public API already marshals every mutation onto that
thread, and a second lock here would only hide a caller that forgot to.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from core.annotations import IrsMessage, OpCode, UnitCode

UnitName = str
RouteKey = tuple[UnitCode, OpCode]
ReceiveCallback = Callable[[IrsMessage], Any]
ConnectCallback = Callable[[UnitName], Any]


def _describe(route: RouteKey) -> str:
    """How a route is spelled in every error below -- one place, so the four
    refusals cannot drift apart."""
    return f"(unit_code={route[0]}, opcode={route[1]})"


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
        Register the future that makes `route` subscribed, and return it
        without awaiting.

        Returning before the await is what lets `receive_message` run a trigger
        function in between: by the time the trigger fires, the future is
        already here, so a reply that arrives immediately -- even before the
        caller gets as far as awaiting -- is captured rather than dropped as
        unsubscribed.

        The future is bound to `loop` rather than to whichever loop happens to
        be running, because it outlives the coroutine that creates it and is
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
        """Free `route` now that `future` is done with it -- delivered, timed
        out or cancelled alike.

        Removes only OUR future: by the time an abandoned call gets here a later
        subscriber may already own the slot, and evicting theirs would strand
        them exactly as this whole rule exists to prevent.
        """
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
        one with `reason`, and forget its standing callbacks.

        The unit's ON-CONNECT callback is deliberately kept. A unit that drops
        may well come back, and it should be greeted again when it does.

        `unit_code` may be None (a unit this connection never had a code for),
        in which case nothing matches and nothing is dropped.
        """
        for route in [key for key in self._subscriptions if key[0] == unit_code]:
            future = self._subscriptions.pop(route)
            if not future.done():
                future.set_exception(reason)
        for route in [key for key in self._callbacks if key[0] == unit_code]:
            del self._callbacks[route]

    def drop_all_callbacks(self) -> None:
        """Forget every standing registration, on-receive and on-connect.

        Called BEFORE a connection's background tasks are cancelled, so no
        in-flight dispatch can find a callback to invoke on the way down.
        """
        self._callbacks.clear()
        self._connect_callbacks.clear()

    def cancel_all_subscriptions(self) -> None:
        """Release every parked `receive_message()` by cancelling its future.

        Called LAST during teardown, once the sockets and tasks are already
        gone, so a caller is woken to a connection that is fully down rather
        than one mid-collapse.
        """
        for future in self._subscriptions.values():
            if not future.done():
                future.cancel()
        self._subscriptions.clear()
