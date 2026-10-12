"""
connections/base.py -- `_send_key`: on a send, an IRS message names its own
opcode (`@OperationCode`), so the caller's `opcode` is optional for it and
required for a dict or bytes.
"""
import pytest

from core.IRS.core import Message, OperationCode
from core.tests._messages import TEXT_UNIT_CODE


@OperationCode(0x21)
class Tagged(Message):
    pass


@OperationCode(0)
class TaggedZero(Message):
    pass


class Untagged(Message):
    pass


@pytest.fixture
def client(manager, free_port):
    return manager.create("client", {
        "protocol": "udp", "unitCode": 101, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE}},
    })


def test_message_supplies_its_own_opcode(client):
    assert client._send_key(Tagged(), None) == 0x21


def test_opcode_zero_is_a_real_opcode(client):
    assert client._send_key(TaggedZero(), None) == 0


def test_matching_explicit_opcode_is_accepted(client):
    assert client._send_key(Tagged(), 0x21) == 0x21
    assert client._send_key(Tagged(), "0x21") == 0x21


def test_conflicting_explicit_opcode_raises(client):
    with pytest.raises(ValueError, match="opcode"):
        client._send_key(Tagged(), 0x22)


@pytest.mark.parametrize("data", [{"a": 1}, b"raw", Untagged(), None])
def test_data_without_an_opcode_requires_one(client, data):
    with pytest.raises(TypeError, match="opcode is required"):
        client._send_key(data, None)


def test_data_without_an_opcode_uses_the_explicit_one(client):
    assert client._send_key({"a": 1}, 5) == 5
    assert client._send_key(Untagged(), "0x5") == 5
