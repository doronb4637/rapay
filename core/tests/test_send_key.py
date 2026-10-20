"""
connections/base.py -- `_send_key`: on a send, a `@Definition` message names
its own opcode. The message and its Interface are trusted, so nothing in them is
re-checked; anything that is not a message (bytes, a dict) is refused.
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


@pytest.fixture
def client(manager, free_port):
    return manager.create("client", {
        "protocol": "udp", "unitCode": OWN_CODE, "side": "client",
        "ip": "127.0.0.1", "local_ip": "127.0.0.1",
        "connections": {"Peer": {"port": free_port, "unitCode": TEXT_UNIT_CODE, "Structures": [MESSAGES]}},
    })


def test_message_supplies_its_own_opcode(client):
    assert client._send_key(Tagged()) == 0x21


def test_opcode_zero_is_a_real_opcode(client):
    assert client._send_key(TaggedZero()) == 0


@pytest.mark.parametrize("data", [{"a": 1}, b"raw", None])
def test_anything_but_a_message_is_refused(client, data):
    with pytest.raises(TypeError, match="message instance"):
        client._send_key(data)
