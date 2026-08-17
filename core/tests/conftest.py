"""
Shared fixtures for the `connections` package test suite.

Test structure note (same constraint `connections/test_framework.py`
documents): subscribe-or-drop delivery means a receiver must already be
*subscribed* -- its `receive_message()` call already in flight -- before the
sender fires, or the message is dropped by design. Tests that need to prove
delivery either use `trigger_function` (arms the subscription, then fires the
trigger) or `receive_in_background` below (arms it on a thread, then the main
thread sends).
"""
from __future__ import annotations

import socket
import threading
from typing import Any, Callable

import pytest

# Importing this is what registers this suite's IRS message layouts --
# module-level `register_message()` calls, same convention the framework
# itself documents for `IRS.Structures.Test.test_messages`.
import core.tests._messages as messages  # noqa: F401
from core.connections.manager import ConnectionManager


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #
@pytest.fixture
def free_port() -> int:
    """One OS-assigned free TCP/UDP port on 127.0.0.1, released before return
    so a connection under test can bind it. Small TOCTOU race in principle
    (something else could grab it between release and bind); acceptable for
    a local test suite, same trade-off `pytest`'s own `unused_tcp_port`
    recipe makes."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def free_ports() -> Callable[[int], list[int]]:
    """Factory for N free ports at once, each from its own transient socket
    so the OS doesn't hand back the same ephemeral port twice in a row."""
    def _make(count: int) -> list[int]:
        socks = [socket.socket(socket.AF_INET, socket.SOCK_STREAM) for _ in range(count)]
        try:
            for sock in socks:
                sock.bind(("127.0.0.1", 0))
            return [sock.getsockname()[1] for sock in socks]
        finally:
            for sock in socks:
                sock.close()
    return _make


# --------------------------------------------------------------------------- #
# ConnectionManager: guarantees absolute teardown even if a test fails
# mid-assertion, so one failing test can never leak a live socket/echo task
# into the next one.
# --------------------------------------------------------------------------- #
@pytest.fixture
def manager():
    mgr = ConnectionManager()
    try:
        yield mgr
    finally:
        mgr.shutdown_all()


# --------------------------------------------------------------------------- #
# Background receive_message() -- for tests proving delivery without a
# trigger_function (e.g. dispatch priority tests that must not themselves
# trigger the send).
# --------------------------------------------------------------------------- #
class Background:
    """Runs `connection.receive_message(...)` on a daemon thread and stores
    the (unit, message) result -- or the exception -- for the caller to
    collect after giving the subscription time to arm."""

    def __init__(self, connection: Any, opcode: int, unit_name: str | None, timeout: float):
        self.result: Any = None
        self.exception: BaseException | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(connection, opcode, unit_name, timeout), daemon=True
        )
        self._thread.start()

    def _run(self, connection, opcode, unit_name, timeout) -> None:
        try:
            self.result = connection.receive_message(opcode, unit_name, timeout=timeout)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test thread, not swallowed
            self.exception = exc
        finally:
            self._done.set()

    def join(self, timeout: float = 5.0) -> None:
        if not self._done.wait(timeout):
            raise TimeoutError("background receive_message() did not finish in time")
        if self.exception is not None:
            raise self.exception


@pytest.fixture
def receive_in_background():
    """Factory: `receive_in_background(connection, opcode, unit_name, timeout)`
    -> Background. Caller is responsible for giving it a brief head start
    (the subscription only exists once `receive_message` has actually run on
    the background thread) before sending."""
    return Background


def as_bytes(message: Any) -> bytes:
    """Text messages carry `data: list[int]`; tests compare against plain
    `bytes`."""
    return bytes(message.data)
