"""
CompositeUnit -- several direction-limited connections presented as one Unit.

Each member keeps its own sockets and lifecycle; the composite only picks, from
the members' `can_send` / `can_receive` flags, which one handles each direction,
and exposes the same public surface as a `Connection`.
"""
from __future__ import annotations

import time
from typing import Any, Callable, overload

from core.IRS.irs_parser import Interface

from .base import (ConnectCallback, ConnectedTarget, Connection, Message, MessageT,
                   OpCode, Outbound, TriggerFunction)


class CompositeUnit:
    """
    Combines direction-limited Connections into one logical Unit, e.g. a
    send-only and a receive-only UDP link that together form one peer.

    Build it through `ConnectionManager.create_composite`. Sends and periodic
    sends go to the one send-capable member, receives and callbacks to the one
    receive-capable member; each method has the contract of the same-named
    `Connection` method. Raises `ValueError` if more than one member can send,
    more than one can receive, or none can do either.
    """

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
        """The member owning the inbound direction -- where every on-receive and
        on-connect registration lands, and whose config `install_handler`
        resolves a handler's `unitCode` against."""
        return self._receiver

    def _require_sender(self) -> Connection:
        if self._sender is None:
            raise RuntimeError(f"CompositeUnit {self.name!r} has no send-capable member")
        return self._sender

    def _require_receiver(self) -> Connection:
        if self._receiver is None:
            raise RuntimeError(f"CompositeUnit {self.name!r} has no receive-capable member")
        return self._receiver

    # ------------------------------------------------------------------ #
    # Lifecycle -- fans out to every member.
    # ------------------------------------------------------------------ #
    def start(self, retry: bool = False) -> None:
        """Start every member; if one fails, close the ones already started."""
        started: list[Connection] = []
        try:
            for member in self._members:
                member.start(retry)
                started.append(member)
        except BaseException:
            for member in reversed(started):
                member.close()
            raise

    def close(self, timeout: float | int | None = 5.0) -> None:
        """Close every member even if one raises, then raise naming the failures."""
        errors: list[Exception] = []
        for member in self._members:
            try:
                member.close(timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - collect, keep closing the rest
                errors.append(exc)
        if errors:
            raise RuntimeError(
                f"CompositeUnit {self.name!r}: {len(errors)} member(s) failed to "
                f"stop cleanly: {errors}"
            )

    # ------------------------------------------------------------------ #
    # Public API -- same shape as Connection
    # ------------------------------------------------------------------ #
    def send_message(self, message: Outbound, unit_name: str | None = None) -> None:
        """`Connection.send_message` on the send-capable member."""
        self._require_sender().send_message(message, unit_name)

    def interface_for(self, unit_name: str | None = None) -> Interface:
        """The IRS interface of the first member that configures `unit_name`."""
        for member in self._members:
            if unit_name is None or unit_name in member.config.connections:
                return member.interface_for(unit_name)
        raise ValueError(f"CompositeUnit {self.name!r}: no member configures unit {unit_name!r}")

    @property
    def active_units(self) -> set[str]:
        """Units connected on ANY member."""
        return set().union(*(member.active_units for member in self._members))

    def wait_for_connected_units(
        self, target: ConnectedTarget, timeout: float | int | None = None
    ) -> bool:
        """Wait until `target` is connected on every member that configures it,
        so a composite counts as connected only once both directions are.
        `timeout` covers the whole call, not each member."""
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        for member, member_target in self._member_targets(target):
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not member.wait_for_connected_units(member_target, remaining):
                return False
        return True

    def _member_targets(self, target: ConnectedTarget) -> list[tuple[Connection, ConnectedTarget]]:
        """Split `target` per member: a count applies to each, names only to the
        members that configure them."""
        if isinstance(target, int):
            return [(member, target) for member in self._members]
        names = {target} if isinstance(target, str) else set(target)
        pairs: list[tuple[Connection, ConnectedTarget]] = []
        covered: set[str] = set()
        for member in self._members:
            mine = names & set(member.config.connections)
            if mine:
                pairs.append((member, sorted(mine)))
                covered |= mine
        unknown = names - covered
        if unknown:
            raise ValueError(f"CompositeUnit {self.name!r}: no member configures unit(s) {sorted(unknown)}")
        return pairs

    @overload
    def receive_message(
        self, opcode: type[MessageT], unit_name: str | None = None,
        timeout: float | int | None = None, trigger_function: TriggerFunction | None = None,
    ) -> MessageT: ...
    @overload
    def receive_message(
        self, opcode: int, unit_name: str | None = None,
        timeout: float | int | None = None, trigger_function: TriggerFunction | None = None,
    ) -> Message: ...
    def receive_message(
        self,
        opcode: OpCode,
        unit_name: str | None = None,
        timeout: float | int | None = None,
        trigger_function: TriggerFunction | None = None,
    ) -> Any:
        """`Connection.receive_message` on the receive-capable member."""
        return self._require_receiver().receive_message(opcode, unit_name, timeout, trigger_function)

    @overload
    def handle_on_receive(
        self, opcode: type[MessageT], callback_func: Callable[[MessageT], Any], unit_name: str | None = None,
    ) -> None: ...
    @overload
    def handle_on_receive(
        self, opcode: int, callback_func: Callable[[Message], Any], unit_name: str | None = None,
    ) -> None: ...
    def handle_on_receive(
        self,
        opcode: OpCode,
        callback_func: Callable[[Any], Any],
        unit_name: str | None = None,
    ) -> None:
        """`Connection.handle_on_receive` on the receive-capable member."""
        self._require_receiver().handle_on_receive(opcode, callback_func, unit_name)

    def stop_on_receive(self, opcode: OpCode, unit_name: str | None = None) -> bool:
        """`Connection.stop_on_receive` on the receive-capable member."""
        return self._require_receiver().stop_on_receive(opcode, unit_name)

    def handle_on_connect(self, callback_func: ConnectCallback, unit_name: str | None = None) -> None:
        """Registered on the receive-capable member -- the one `install_handler`
        resolves unit codes against."""
        self._require_receiver().handle_on_connect(callback_func, unit_name)

    def stop_on_connect(self, unit_name: str | None = None) -> bool:
        """`Connection.stop_on_connect` on the receive-capable member."""
        return self._require_receiver().stop_on_connect(unit_name)

    def periodic_sending(
        self,
        message: Outbound,
        interval: int | float,
        unit_name: str | None = None,
    ) -> None:
        """`Connection.periodic_sending` on the send-capable member."""
        self._require_sender().periodic_sending(message, interval, unit_name)

    def stop_periodic(self, opcode: OpCode, unit_name: str | None = None) -> bool:
        """`Connection.stop_periodic` on the send-capable member."""
        return self._require_sender().stop_periodic(opcode, unit_name)
