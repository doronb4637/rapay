"""
ConnectionManager: builds Connection / CompositeUnit instances from JSON
configuration and keeps a registry of them, so the application has one place
to call for a deterministic teardown of everything it owns.
"""
from __future__ import annotations

import logging
from typing import Any

from core.tools.file_functions import read_unit_config

from .base import Connection, Unit
from .composite import CompositeUnit
from .config import ConnectionConfig, TransportProtocol
from .handlers import UnitHandler, install_handler

logger = logging.getLogger("connmgr.manager")

#: A unit configuration name (resolved by `tools.file_functions.read_unit_config`)
#: or the JSON config dict itself.
UnitConfigSource = str | dict[str, Any]


class ConnectionManager:
    """Turns JSON configuration into connections and owns their teardown."""

    _registry: dict[TransportProtocol, type[Connection]] = {}

    def __init__(self) -> None:
        self._connections: dict[str, Unit] = {}

    @classmethod
    def register(cls, protocol: TransportProtocol, impl: type[Connection]) -> None:
        """Map `protocol` to the Connection subclass that implements it.
        `connections/__init__.py` registers the built-in ones at import."""
        cls._registry[protocol] = impl

    # -- construction ------------------------------------------------------
    @staticmethod
    def _load_config(config: UnitConfigSource) -> dict[str, Any]:
        """Normalize whatever the caller passed into a raw JSON config dict."""
        if isinstance(config, str):
            return read_unit_config(config)
        if isinstance(config, dict):
            return config
        raise TypeError(
            f"config must be a unit configuration name (str) or a JSON config "
            f"dict, got {type(config).__name__}"
        )

    def _check_name_free(self, name: str) -> None:
        if name in self._connections:
            raise ValueError(f"a connection named {name!r} is already registered")

    def _build(self, config: UnitConfigSource) -> Connection:
        connection_config = ConnectionConfig.from_json(self._load_config(config))
        connection_class = self._registry.get(connection_config.protocol)
        if connection_class is None:
            raise ValueError(f"No connection implementation registered for protocol {connection_config.protocol}")
        return connection_class(connection_config)

    def create(
        self, name: str, config: UnitConfigSource,
        handler_class: type[UnitHandler] | None = None,
    ) -> Connection:
        """Build a connection (not yet started) and register it under `name`.

        Args:
            name: Registry name; must not be taken.
            config: A unit configuration name, or the JSON config dict.
            handler_class: Installed via `handlers.install_handler` before
                registration, so a bad handler fails `create()` as a whole.

        Raises:
            ValueError: invalid config, unknown protocol, taken name, or a
                handler whose `unitCode` no configured unit carries.
        """
        self._check_name_free(name)
        connection = self._build(config)
        if handler_class is not None:
            install_handler(connection, handler_class)
        self._connections[name] = connection
        return connection

    def create_composite(
        self, name: str, members: dict[str, UnitConfigSource],
        handler_class: type[UnitHandler] | None = None,
    ) -> CompositeUnit:
        """Build one connection per entry of `members` (labels are descriptive
        only), combine them into a `CompositeUnit` and register it under
        `name`. The members belong to the composite and are not registered on
        their own; nothing is registered unless every step succeeds.
        """
        self._check_name_free(name)
        composite = CompositeUnit(name, [self._build(cfg) for cfg in members.values()])
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
        """Close every managed unit, newest first, tolerating individual failures."""
        for name, connection in reversed(list(self._connections.items())):
            try:
                connection.close(timeout=timeout)
            except Exception:
                logger.exception("error stopping connection %s", name)
        self._connections.clear()

    def get(self, name: str) -> Unit:
        return self._connections[name]

    def __enter__(self) -> ConnectionManager:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.shutdown_all()
