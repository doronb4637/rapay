"""
ConnectionManager: builds Connection / CompositeUnit instances from JSON
configuration and keeps a registry of everything it created, so the
application has exactly one place to call for an absolute, deterministic
teardown of every connection it owns.
"""
from __future__ import annotations

import logging
from typing import Any

from core.tools.file_functions import read_unit_config
from core.tools.general import import_modules

from .base import Connection, Unit
from .composite import CompositeUnit
from .config import ConnectionConfig, Protocol
from .handlers import UnitHandler, install_handler

logger = logging.getLogger("connmgr.manager")

""" Annotations """
ManagedUnit = Unit
UnitConfigSource = str | dict[str, Any]


class ConnectionManager:
    """
    Used for turning JSON configuration into an actual connection object.
    and for guaranteeing teardown and management of connections.
    """

    _registry: dict[Protocol, type[Connection]] = {}

    def __init__(self) -> None:
        self._connections: dict[str, ManagedUnit] = {}

    @classmethod
    def register(cls, protocol: Protocol, impl: type[Connection]) -> None:
        """
        Register a subclass of Connection a *class* (not an instance)

        the module (via __init__.py) calls this once, at import time, to
        initialize the factory.
        """
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

    @staticmethod
    def _import_config_libs(name: str, config: ConnectionConfig) -> None:
        """ Import the message libraries(python files) declared in config under "Structures". """
        structures = config.all_structures_raw
        if not structures:
            return
        logger.info("connection %s: importing message libraries %s", name, list(structures))
        import_modules(list(structures))

    def create(
        self, name: str, config: UnitConfigSource,
        handler_class: type[UnitHandler] | None = None,
    ) -> Connection:
        """Build and register a connection under the given name.

        Args:
            name: The name under which the connection is registered.
            config: A configuration mapping(dict) or file path(str / Path) pointing
                to a valid connection configuration file.
            handler_class: Optional handler class to install via
                `handlers.install_handler` before registering the connection.
                Defaults to None.

        Returns:
            Connection: The initialized connection instance.

        Raises:
            FileNotFoundError: If `config` is a path that does not exist.
            ValueError: If `config` data or `name` is invalid.
        """
        config_json = self._load_config(config)
        connection_config = ConnectionConfig.from_json(config_json)
        self._import_config_libs(name, connection_config)
        connection_class = self._registry.get(connection_config.protocol)
        if connection_class is None:
            raise ValueError(f"No connection implementation registered for protocol {connection_config.protocol}")
        connection = connection_class(connection_config)
        if handler_class is not None:
            install_handler(connection, handler_class)
        self._connections[name] = connection
        return connection

    def create_composite(
        self, name: str, members: dict[str, UnitConfigSource],
        handler_class: type[UnitHandler] | None = None,
    ) -> CompositeUnit:
        """Assemble and register a composite unit under the given name.

        Each member specified in `members` is instantiated via the internal `create()`
        factory and bundled into a `CompositeUnit`. If provided, `handler_class` is
        installed once against the final assembled composite.

        Args:
            name: The name under which the composite connection is registered.
            members: A mapping of short member labels to their respective connection
                configurations (e.g., `{"sender": "MulticastSenderPath", "receiver": "UdpReceiverPath"}`).
            handler_class: Optional handler class to install once against the assembled
                composite before registration. Defaults to None.

        Returns:
            CompositeUnit: The assembled and registered composite instance.

        Raises:
            FileNotFoundError: If `config` is a path that does not exist.
            ValueError: If `config` data or `name` is invalid.
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
        """Absolute teardown of every managed connection/composite"""
        for name, connection in reversed(list(self._connections.items())):
            try:
                connection.close(timeout=timeout)
            except Exception:
                logger.exception("error stopping connection %s", name)
        self._connections.clear()

    def get(self, name: str) -> ManagedUnit:
        return self._connections[name]

    def __enter__(self) -> ConnectionManager:
        """Enter the context manager and return self."""
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """Exit the runtime context and shut down all managed connections."""
        self.shutdown_all()
