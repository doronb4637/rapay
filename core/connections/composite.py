"""
CompositeUnit -- combines several direction-limited connections into one
cohesive logical Unit.

This is composition, not function-injection: `CompositeUnit` holds a list
of member `Connection` instances, inspects each member's `can_send` /
`can_receive` capability flags (set naturally by the protocol classes
themselves -- e.g. MulticastConnection flips these based on config.side)
to decide which member handles which direction, and exposes the exact same
public surface as a plain `Connection` (start/stop/send_message/
receive_message). Nothing is monkey-patched onto anything; each member
connection stays a fully independent, correctly-typed object with its own
sockets, its own read loop, and its own lifecycle.
"""
from __future__ import annotations

import time
from typing import Any

from .base import (ConnectCallback, ConnectedTarget, Connection, IrsMessage,
                   ReceiveCallback, TriggerFunction)


class CompositeUnit:
    """Combines multiple partial-capability Connections into one logical Unit."""

    def __init__(self, name: str, members: list[Connection]):
        senders = [m for m in members if m.can_send]
        receivers = [m for m in members if m.can_receive]
        if len(senders) > 1:
            raise ValueError(
                f"CompositeUnit {name!r}: more than one send-capable member "
                f"was given ({len(senders)}); ambiguous which one send_message "
                f"should use"
            )
        if len(receivers) > 1:
            raise ValueError(
                f"CompositeUnit {name!r}: more than one receive-capable member "
                f"was given ({len(receivers)}); ambiguous which one "
                f"receive_message should read from"
            )
        if not senders and not receivers:
            raise ValueError(f"CompositeUnit {name!r}: no member can send or receive")

        self.name = name
        self._members = members
        self._sender: Connection | None = senders[0] if senders else None
        self._receiver: Connection | None = receivers[0] if receivers else None

    @property
    def receiver(self) -> Connection | None:
        """The member owning this composite's INBOUND direction, or None if it
        has no receive-capable member.

        Public because it is canonical outside this class too:
        `handlers.install_handler` resolves a `UnitHandler`'s `unitCode` against
        this member's config, and it is the member every on-receive and
        on-connect registration lands on -- so the two cannot be allowed to
        disagree about which member that is.
        """
        return self._receiver

    # ------------------------------------------------------------------ #
    # Lifecycle -- fans out to every member, still an ABSOLUTE teardown.
    # ------------------------------------------------------------------ #
    def start(self, retry: bool = False) -> None:
        """Start every member. Each registers its own atexit teardown when it
        is constructed, so there is nothing to add here."""
        for member in self._members:
            member.start(retry)

    def close(self, timeout: float | int | None = 5.0) -> None:
        """Stops every member even if one of them raises, then re-raises so
        the caller still finds out something went wrong -- a partial
        teardown never passes silently."""
        errors: list[Exception] = []
        for member in self._members:
            try:
                member.close(timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - deliberately broad: collect & continue
                errors.append(exc)
        if errors:
            raise RuntimeError(
                f"CompositeUnit {self.name!r}: {len(errors)} member(s) failed to "
                f"stop cleanly: {errors}"
            )

    # ------------------------------------------------------------------ #
    # Public API -- identical shape to Connection.send_message/receive_message
    # ------------------------------------------------------------------ #
    def send_message(self, data: IrsMessage | dict, opcode: int | None = None,
                     unit_name: str | None = None) -> None:
        if self._sender is None:
            raise RuntimeError(f"CompositeUnit {self.name!r} has no send-capable member")
        self._sender.send_message(data, opcode, unit_name)

    @property
    def active_units(self) -> set[str]:
        """Units connected on ANY member: a composite's Unit is reachable if
        either half of it is."""
        return set().union(*(member.active_units for member in self._members))

    def wait_for_connected_units(
        self, target: ConnectedTarget, timeout: float | int | None = None
    ) -> bool:
        """Wait for every member in turn, so the composite is only "connected"
        once both directions are. `timeout` is the budget for the whole call,
        not per member."""
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        for member in self._members:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not member.wait_for_connected_units(target, remaining):
                return False
        return True

    def receive_message(
        self,
        opcode: int | str | IrsMessage,
        unit_name: str | None = None,
        timeout: float | int | None = None,
        trigger_function: TriggerFunction | None = None,
    ) -> IrsMessage:
        if self._receiver is None:
            raise RuntimeError(f"CompositeUnit {self.name!r} has no receive-capable member")
        return self._receiver.receive_message(opcode, unit_name, timeout, trigger_function)

    def handle_on_receive(
        self,
        opcode: int | str | IrsMessage,
        callback_func: ReceiveCallback,
        unit_name: str | None = None,
    ) -> None:
        """Standing on-receive handler, registered on the member that owns
        this composite's inbound direction."""
        if self._receiver is None:
            raise RuntimeError(f"CompositeUnit {self.name!r} has no receive-capable member")
        self._receiver.handle_on_receive(opcode, callback_func, unit_name)

    def stop_on_receive(self, opcode: int | str | IrsMessage, unit_name: str | None = None) -> bool:
        if self._receiver is None:
            raise RuntimeError(f"CompositeUnit {self.name!r} has no receive-capable member")
        return self._receiver.stop_on_receive(opcode, unit_name)

    def handle_on_connect(self, callback_func: ConnectCallback, unit_name: str | None = None) -> None:
        """Standing on-connect handler, registered on the same member
        `handle_on_receive` uses -- the receive-capable one. Not a coin
        flip: `install_handler` (`handlers.py`) already treats that member's
        `config.unit_codes` as canonical for resolving a `UnitHandler`'s
        `unitCode` on a composite, so a connect handler installed the same
        way fires on the same member it was resolved against."""
        if self._receiver is None:
            raise RuntimeError(f"CompositeUnit {self.name!r} has no receive-capable member")
        self._receiver.handle_on_connect(callback_func, unit_name)

    def stop_on_connect(self, unit_name: str | None = None) -> bool:
        if self._receiver is None:
            raise RuntimeError(f"CompositeUnit {self.name!r} has no receive-capable member")
        return self._receiver.stop_on_connect(unit_name)

    def periodic_sending(
        self,
        data: IrsMessage | dict[str, Any],
        opcode: int | None,
        interval: int | float,
        unit_name: str | None = None,
    ) -> None:
        """Repeating send, routed to the same member that handles
        send_message -- a composite's outbound direction is its send-capable
        member, periodic or not."""
        if self._sender is None:
            raise RuntimeError(f"CompositeUnit {self.name!r} has no send-capable member")
        self._sender.periodic_sending(data, opcode, interval, unit_name)

    def stop_periodic(self, opcode: int | str | IrsMessage, unit_name: str | None = None) -> bool:
        if self._sender is None:
            raise RuntimeError(f"CompositeUnit {self.name!r} has no send-capable member")
        return self._sender.stop_periodic(opcode, unit_name)
