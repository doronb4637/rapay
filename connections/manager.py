"""
ConnectionManager: builds Connection / CompositeUnit instances from JSON
configuration and keeps a registry of everything it created, so the
application has exactly one place to call for an absolute, deterministic
teardown of every connection it owns.
"""
from __future__ import annotations

import logging
from typing import Any

from .base import Connection
from .composite import CompositeUnit
from .config import ConnectionConfig, Protocol

logger = logging.getLogger("connmgr.manager")

ManagedUnit = Connection | CompositeUnit


class ConnectionManager:
    """
    The single, central place an application talks to for turning JSON
    configuration into live connections, and for guaranteeing every one of
    them is torn down absolutely and deterministically when the application
    is done with them.

    Concretely, `ConnectionManager` is two things bolted together:

    1. A **factory**: `create()` takes a JSON config dict, turns it into a
       typed `ConnectionConfig`, looks up which concrete `Connection`
       subclass implements that config's `protocol` (via the `_registry`
       described below), instantiates it, and hands it back.
       `create_composite()` does the same for several JSON configs at once
       and wraps the results in a `CompositeUnit` (see composite.py), so
       asymmetric, multi-protocol "Units" are built exactly the same way as
       everything else.

    2. A **lifecycle registry**: every `Connection` / `CompositeUnit` this
       manager creates is remembered in `self._connections`, keyed by the
       name the caller gave it. `start_all()` / `shutdown_all()` then act on
       every managed object at once, so an application never has to
       individually remember to close each socket it opened -- one call to
       `shutdown_all()` (or exiting a `with ConnectionManager() as mgr:`
       block) is an absolute guarantee that nothing is left open or running.

    Typical usage:

        mgr = ConnectionManager()
        radar = mgr.create("radar", {...tcp json...})
        beacon = mgr.create_composite("beacon", {
            "transport": {...multicast send-only json...},
            "receive":   {...udp receive-only json...},
        })
        mgr.start_all()
        ...
        mgr.shutdown_all()   # or: `with ConnectionManager() as mgr:`
    """

    # Maps each Protocol enum value (tcp/udp/multicast/dds) to the concrete
    # Connection subclass that implements it. This indirection is the heart
    # of the factory pattern here: `create()` never has a hardcoded
    # if/elif chain over protocol names -- it just looks the class up in
    # this dict. That means adding a brand new protocol implementation
    # anywhere in the codebase (or in a third-party plugin) is a single
    # `ConnectionManager.register(Protocol.X, MyConnectionClass)` call, with
    # zero changes required inside ConnectionManager itself. Without this
    # registry, every new protocol would require editing `create()` directly
    # -- exactly the kind of tight coupling the factory pattern exists to
    # avoid.
    _registry: dict[Protocol, type[Connection]] = {}

    def __init__(self) -> None:
        self._connections: dict[str, ManagedUnit] = {}

    # -- protocol plug-in registration -----------------------------------
    @classmethod
    def register(cls, protocol: Protocol, impl: type[Connection]) -> None:
        """
        Register `impl` as the concrete `Connection` subclass responsible
        for handling `protocol`.

        `impl` is a *class* (not an instance) -- a subclass of `Connection`
        such as `TcpConnection` or `UdpConnection` -- that `create()` will
        instantiate (as `impl(config)`) whenever it sees a JSON config whose
        `"protocol"` field matches `protocol`. Each protocol implementation
        module (tcp.py, udp.py, ...) calls this once, at import time, to
        plug itself into the factory.
        """
        cls._registry[protocol] = impl

    # -- construction ------------------------------------------------------
    def create(self, name: str, json_config: dict[str, Any]) -> Connection:
        config = ConnectionConfig.from_json(json_config)
        impl_cls = self._registry.get(config.protocol)
        if impl_cls is None:
            raise ValueError(f"No connection implementation registered for protocol {config.protocol}")
        connection = impl_cls(config)
        self._connections[name] = connection
        return connection

    def create_composite(self, name: str, members: dict[str, dict[str, Any]]) -> CompositeUnit:
        """
        `members` maps a short member label -> JSON config, e.g.:
            {"transport": multicast_send_only_json, "receive": udp_receive_only_json}
        Each member is built through the same `create()` factory (so it
        benefits from the same protocol registry) and then wrapped in a
        CompositeUnit registered under `name`.
        """
        built: list[Connection] = []
        for member_name, cfg in members.items():
            sub = self.create(f"{name}.{member_name}", cfg)
            built.append(sub)
        composite = CompositeUnit(name, built)
        self._connections[name] = composite
        return composite

    # -- lifecycle -----------------------------------------------------------
    def start_all(self) -> None:
        for name, connection in self._connections.items():
            logger.info("starting connection %s", name)
            connection.start()

    def shutdown_all(self, timeout: float | int | None = 5.0) -> None:
        """Absolute teardown of every managed connection/unit, tolerating
        individual failures so one bad actor can't leave the rest hanging."""
        for name, connection in reversed(list(self._connections.items())):
            try:
                connection.stop(timeout=timeout)
            except Exception:
                logger.exception("error stopping connection %s", name)
        self._connections.clear()

    def get(self, name: str) -> ManagedUnit:
        return self._connections[name]

    def __enter__(self) -> ConnectionManager:
        """
        Called when entering a `with ConnectionManager() as mgr:` block.
        Returns `self` unchanged -- all the real setup happens on-demand as
        the caller makes `create()` / `create_composite()` / `start_all()`
        calls inside the `with` body.
        """
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """
        Called automatically when the `with` block is exited -- whether it
        finished normally OR an exception propagated out of it. Either way,
        this calls `shutdown_all()`, guaranteeing every connection this
        manager created is stopped absolutely before control leaves the
        `with` block.
        """
        self.shutdown_all()
