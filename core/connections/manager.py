"""
ConnectionManager: builds Connection / CompositeUnit instances from JSON
configuration and keeps a registry of everything it created, so the
application has exactly one place to call for an absolute, deterministic
teardown of every connection it owns.
"""
from __future__ import annotations

import logging
from typing import Any

import IRS
from tools.file_functions import read_unit_config
from tools.general import import_modules

from .base import Connection
from .composite import CompositeUnit
from .config import ConnectionConfig, Protocol
from .handlers import UnitHandler, install_handler

logger = logging.getLogger("connmgr.manager")

ManagedUnit = Connection | CompositeUnit
#: What `create()` accepts in place of a config: either the JSON dict itself,
#: or the name of a unit configuration for `tools.file_functions.read_unit_config`
#: to load from config/Units.
UnitConfigSource = str | dict[str, Any]


class ConnectionManager:
    """
    The single, central place an application talks to for turning JSON
    configuration into live connections, and for guaranteeing every one of
    them is torn down absolutely and deterministically when the application
    is done with them.

    Concretely, `ConnectionManager` is two things bolted together:

    1. A **factory**: `create()` takes a unit configuration name (loaded from
       config/Units by `tools.file_functions.read_unit_config`) or a JSON config
       dict, turns it into a typed `ConnectionConfig`, imports any message
       libraries the config declares, looks up which concrete `Connection`
       subclass implements that config's `protocol` (via the `_registry`
       described below), instantiates it, and hands it back.
       `create_composite()` does the same for several configs at once
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
        radar = mgr.create("radar", "TcpServer")   # config/Units/TcpServer.json
        beacon = mgr.create_composite("beacon", {
            "transport": "MulticastSender",
            "receive":   "UdpReciver",
        })
        mgr.start_all()
        ...
        mgr.shutdown_all()   # or: `with ConnectionManager() as mgr:`
    """

    # Maps each Protocol enum value (tcp/udp/multicast/dds) to the concrete
    # Connection subclass that implements it. This indirection is the heart
    # of the factory pattern here
    _registry: dict[Protocol, type[Connection]] = {}

    def __init__(self) -> None:
        self._connections: dict[str, ManagedUnit] = {}

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
    @staticmethod
    def _load_config(config: UnitConfigSource) -> dict[str, Any]:
        """
        Normalise whatever the caller passed into a raw JSON config dict.

        A `str` is a unit configuration NAME, resolved through
        `tools.file_functions.read_unit_config` -- that function owns the
        config/Units directory layout and the `.json` suffix, so this module
        never builds a config path itself and a change in where unit configs
        live stays a change in one place. A `dict` is taken as an
        already-loaded config and used as-is, which is what callers that
        assemble configs programmatically (and the test suite) rely on.
        """
        if isinstance(config, str):
            return read_unit_config(config)
        if isinstance(config, dict):
            return config
        raise TypeError(
            f"config must be a unit configuration name (str) or a JSON config "
            f"dict, got {type(config).__name__}"
        )

    def create(
        self,
        name: str,
        config: UnitConfigSource,
        handler_class: type[UnitHandler] | None = None,
    ) -> Connection:
        """
        Build one connection, registered under `name`.

        `config` is either a unit configuration name (loaded by
        `read_unit_config`) or the JSON dict itself:

            mgr.create("radar", "TcpServer")     # config/Units/TcpServer.json
            mgr.create("radar", {...json...})    # already-loaded config

        `handler_class`, if given, is installed via `handlers.install_handler`
        before this connection is registered.
        """
        config_json = self._load_config(config)
        connection_config = ConnectionConfig.from_json(config_json)
        self._import_config_libs(name, connection_config)
        impl_cls = self._registry.get(connection_config.protocol)
        if impl_cls is None:
            raise ValueError(
                f"No connection implementation registered for protocol {connection_config.protocol}"
            )
        connection = impl_cls(connection_config)
        if handler_class is not None:
            install_handler(connection, handler_class)
        self._connections[name] = connection
        return connection

    @staticmethod
    def _import_config_libs(name: str, config: ConnectionConfig) -> None:
        """
        Import the message libraries a config declares under `structures`, via
        `tools.general.import_modules`.

        This is the step that populates `IRS.REGISTRY`: a message module
        registers its types by *being imported*

            "Structures": ["messages.radar", "messages.tracker"]
        """
        structures: list[str] = config.extra.get("Structures")
        if structures is None:
            return
        logger.info("connection %s: importing message libraries %s", name, structures)
        import_modules(structures)

    def create_composite(
        self,
        name: str,
        members: dict[str, UnitConfigSource],
        handler_class: type[UnitHandler] | None = None,
    ) -> CompositeUnit:
        """
        `members` maps a short member label -> config, e.g.:
            {"transport": "MulticastSender", "receive": "UdpReciver"}
        Each member is built through the same `create()` factory
        and then wrapped in a CompositeUnit registered under `name`.

        `handler_class`, if given, installs once against the assembled
        composite.
        """
        built: list[Connection] = []
        for member_name, cfg in members.items():
            sub = self.create(f"{name}.{member_name}", cfg)
            built.append(sub)
        composite = CompositeUnit(name, built)
        if handler_class is not None:
            install_handler(composite, handler_class)
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
                connection.close(timeout=timeout)
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
