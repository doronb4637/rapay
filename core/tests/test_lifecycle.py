"""
Lifecycle edges: failed starts, close() releasing waiters, restarts, and units
that come back after being dropped.
"""
import socket
import threading

import pytest

from core.connections.config import ConnectionConfig
from core.connections.framing import pack_message
from core.connections.multicast import MulticastConnection
from core.tests._messages import TEXT_UNIT_CODE


def _tcp(side, *ports):
    return {
        "protocol": "tcp", "unitCode": TEXT_UNIT_CODE, "side": side,
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {f"Peer{i}": {"port": port, "unitCode": TEXT_UNIT_CODE + i}
                        for i, port in enumerate(ports)},
    }


def _udp_server(port):
    return {
        "protocol": "udp", "unitCode": 100, "side": "server",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": port, "unitCode": TEXT_UNIT_CODE}},
    }


def _in_thread(func):
    result: dict = {}

    def _run():
        try:
            result["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            result["exc"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, result


def test_a_failed_start_closes_what_it_had_opened(manager, free_ports):
    free, taken = free_ports(2)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", taken))
    blocker.listen()
    try:
        server = manager.create("server", _tcp("server", free, taken))
        with pytest.raises(OSError):
            server.start()
    finally:
        blocker.close()
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", free))  # the first listener was closed again
    finally:
        probe.close()


def test_close_releases_a_waiter_with_no_timeout(manager, free_port):
    server = manager.create("server", _udp_server(free_port))
    server.start()
    thread, result = _in_thread(lambda: server.wait_for_connected_units("Peer"))
    thread.join(0.2)
    server.close()
    thread.join(3)
    assert result == {"value": False}


def test_close_fails_a_parked_receive_with_connection_error(manager, free_port):
    server = manager.create("server", _udp_server(free_port))
    server.start()
    thread, result = _in_thread(lambda: server.receive_message(1))
    thread.join(0.2)
    server.close()
    thread.join(3)
    assert isinstance(result.get("exc"), ConnectionError), result


def test_a_restarted_connection_keeps_its_handlers(manager, free_port):
    server = manager.create("server", _udp_server(free_port))
    received = threading.Event()
    server.handle_on_receive(1, lambda message: received.set())
    server.start()
    server.close()
    server.start()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(pack_message(TEXT_UNIT_CODE, 1, b"hello"), ("127.0.0.1", free_port))
    assert received.wait(3)


def test_a_udp_unit_dropped_by_the_watchdog_comes_back(manager, free_port):
    server = manager.create("server", _udp_server(free_port))
    server.start()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(pack_message(TEXT_UNIT_CODE, 1, b"a"), ("127.0.0.1", free_port))
        assert server.wait_for_connected_units("Peer", timeout=3)
        server._loop_thread.await_coroutine(server._disconnect_unit("Peer"))
        assert "Peer" not in server.active_units
        sock.sendto(pack_message(TEXT_UNIT_CODE, 1, b"b"), ("127.0.0.1", free_port))
        assert server.wait_for_connected_units("Peer", timeout=3)


def test_a_replaced_tcp_peer_is_closed(manager, free_port):
    server = manager.create("server", _tcp("server", free_port))
    server.start()
    first = socket.create_connection(("127.0.0.1", free_port))
    second = None
    try:
        assert server.wait_for_connected_units("Peer0", timeout=3)
        second = socket.create_connection(("127.0.0.1", free_port))
        first.settimeout(3)
        assert first.recv(1) == b"", "the replaced peer's socket must be closed"
    finally:
        first.close()
        if second is not None:
            second.close()


def test_a_retrying_tcp_client_reconnects_after_its_peer_drops(manager, free_port):
    server = manager.create("server", _tcp("server", free_port))
    client = manager.create("client", _tcp("client", free_port))
    server.start()
    client.start(retry=True)
    assert client.wait_for_connected_units("Peer0", timeout=3)

    server.close()
    gone = threading.Event()
    for _ in range(100):
        if "Peer0" not in client.active_units:
            gone.set()
            break
        gone.wait(0.02)
    assert gone.is_set(), "the client never noticed its peer drop"
    server.start()
    assert client.wait_for_connected_units("Peer0", timeout=5)


def test_a_multicast_receiver_joins_its_group():
    config = ConnectionConfig.from_json({
        "protocol": "multicast", "unitCode": 1, "side": "receiver",
        "ip": "239.1.2.3", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": 0, "unitCode": 2}},
    })
    connection = MulticastConnection(config)
    try:
        sock = connection._open_socket("Peer", 0)
    except OSError as exc:  # no multicast-capable interface here
        pytest.skip(f"multicast join unavailable: {exc}")
    sock.close()
