"""
connections/base.py -- `_send_key`: on a send, a `@Definition` message names
its own opcode and unit, so the caller's `opcode` is optional for it -- and its
unit must be this connection's own. Bytes carry nothing and need an opcode;
anything else is refused.
"""
import pytest

from core.IRS import Definition, Message
from core.tests._messages import MESSAGES, TEXT_UNIT_CODE

OWN_CODE = 101


@Definition(unitCode=OWN_CODE, opCode=0x21)
class Tagged(Message):
    pass


@Definition(unitCode=OWN_CODE, opCode=0)
class TaggedZero(Message):
    pass


@Definition(unitCode=TEXT_UNIT_CODE, opCode=0x21)
class Theirs(Message):
    pass


class Untagged(Message):
    pass


@pytest.fixture
def client(manager, free_port):
    return manager.create("client", {
        "protocol": "udp", "unitCode": OWN_CODE, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE, "Structures": [MESSAGES]}},
    })


def test_message_supplies_its_own_opcode(client):
    assert client._send_key(Tagged(), None) == 0x21


def test_opcode_zero_is_a_real_opcode(client):
    assert client._send_key(TaggedZero(), None) == 0


def test_matching_explicit_opcode_is_accepted(client):
    assert client._send_key(Tagged(), 0x21) == 0x21
    assert client._send_key(Tagged(), "0x21") == 0x21
    assert client._send_key(Tagged(), Tagged) == 0x21


def test_conflicting_explicit_opcode_raises(client):
    with pytest.raises(ValueError, match="opcode"):
        client._send_key(Tagged(), 0x22)


def test_a_message_another_unit_sends_is_refused(client):
    with pytest.raises(ValueError, match=f"sent by unit {TEXT_UNIT_CODE}, but this connection is unit {OWN_CODE}"):
        client._send_key(Theirs(), None)


def test_a_message_without_definition_is_refused(client):
    with pytest.raises(TypeError, match="no @Definition"):
        client._send_key(Untagged(), 5)


def test_a_dict_is_refused(client):
    with pytest.raises(TypeError, match="message instance or bytes"):
        client._send_key({"a": 1}, 5)


@pytest.mark.parametrize("data", [b"raw", None])
def test_bytes_require_an_opcode(client, data):
    with pytest.raises(TypeError, match="opcode is required"):
        client._send_key(data, None)


def test_bytes_use_the_explicit_opcode(client):
    assert client._send_key(b"raw", 5) == 5
    assert client._send_key(b"raw", "0x5") == 5
