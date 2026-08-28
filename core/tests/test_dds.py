"""
DDS connection tests.

Split by what each one needs, because RTI has two very different requirements:

  * Parsing a QoS XML and building `@idl.struct` types needs only the
    `rti.connextdds` package. Most of this file runs there.
  * Creating a DomainParticipant needs an RTI LICENSE. Everything that puts a
    live participant on a domain is gated behind `requires_license`, so this
    suite stays green on a machine without one instead of reporting a code
    failure for an environment problem.

Routing is exercised by driving `_dispatch_incoming` directly -- the same
technique `test_echo.py` uses to observe `_do_send` -- which proves the header
extraction, the self-echo filter and the topic-to-opcode mapping without any
network at all.
"""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("rti.connextdds", reason="RTI Connext Python API not installed")

import rti.connextdds as dds  # noqa: E402
import rti.types as idl  # noqa: E402

from core.connections.config import ConnectionConfig, TopicDirection  # noqa: E402
from core.connections.dds import DdsConnection  # noqa: E402
from core.tools.general import topic_opcode  # noqa: E402

TYPE_MODULE = "core.DDS.Structures.Example.example_topics"
QOS_FILE = "core/configs/qos/UNIVERSAL_QOS.xml"
TOPIC = "TrackTopic"
STATUS_TOPIC = "StatusTopic"
OPCODE = topic_opcode(TOPIC)

OWN_CODE = 22
PEER_CODE = 7
PEER_NAME = "RadarUnit"
#: Well outside the default 0-4 range so a stray participant on the machine
#: cannot join a test's domain.
TEST_DOMAIN = 77


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def dds_config(**overrides):
    """A minimal, valid DDS config dict; override any key per test."""
    config = {
        "protocol": "dds",
        "side": "subscriber",
        "ip": "0.0.0.0",
        "unitCode": OWN_CODE,
        "connections": {PEER_NAME: {"port": TEST_DOMAIN, "unitCode": PEER_CODE}},
        "idl_modules": [TYPE_MODULE],
        # No explicit direction: it defaults from `side`, which several tests vary.
        "topics": [{"topic": TOPIC, "type": "Track"}],
    }
    config.update(overrides)
    return config


def build(**overrides) -> DdsConnection:
    """A DdsConnection that was never started -- no participant, no license."""
    return DdsConnection(ConnectionConfig.from_json(dds_config(**overrides)))


def dispatch(connection: DdsConnection, unit_name: str, opcode: int, sample) -> None:
    """Feed one sample through the framework's dispatch point, on the loop
    thread where a read loop would have called it."""
    async def fire() -> None:
        connection._dispatch_incoming(unit_name, opcode, sample)
    connection._loop_thread.await_coroutine(fire())


def _license_available() -> bool:
    try:
        participant = dds.DomainParticipant(TEST_DOMAIN)
    except Exception:
        return False
    participant.close()
    return True


requires_license = pytest.mark.skipif(
    not _license_available(),
    reason="no RTI license: a DomainParticipant cannot be created in this environment",
)


@pytest.fixture
def track_type():
    from core.DDS.Structures.Example.example_topics import Track
    return Track


# --------------------------------------------------------------------------- #
# The surrogate opcode
# --------------------------------------------------------------------------- #
def test_topic_opcode_is_deterministic_and_uint16():
    for name in ("TrackTopic", "StatusTopic", "a", "", "Some::Very::Long::Topic::Name"):
        value = topic_opcode(name)
        assert topic_opcode(name) == value, "must be stable across calls and processes"
        assert 0 <= value <= 0xFFFF, f"{name} -> {value:#x} does not fit framing's uint16 OpCode"


def test_distinct_topics_get_distinct_opcodes():
    assert topic_opcode(TOPIC) != topic_opcode(STATUS_TOPIC)


def test_opcode_collision_is_a_load_time_error():
    """
    Two topics on one route key would deliver one topic's samples to the other
    topic's subscriber, silently. It has to fail at load, naming both.
    """
    with pytest.raises(ValueError) as excinfo:
        build(topics=[
            {"topic": "Alpha", "type": "Track", "opcode": 0x1234},
            {"topic": "Beta", "type": "Track", "opcode": 0x1234},
        ])
    message = str(excinfo.value)
    assert "Alpha" in message and "Beta" in message
    assert "0x1234" in message


def test_explicit_opcode_resolves_a_collision():
    connection = build(topics=[
        {"topic": "Alpha", "type": "Track", "opcode": 0x1234},
        {"topic": "Beta", "type": "Track", "opcode": 0x1235},
    ])
    assert {t.topic: t.opcode for t in connection.config.topics} == {
        "Alpha": 0x1234, "Beta": 0x1235}


def test_duplicate_topic_name_is_rejected():
    with pytest.raises(ValueError, match="already declared"):
        build(topics=[
            {"topic": TOPIC, "type": "Track"},
            {"topic": TOPIC, "type": "Status"},
        ])


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #
def test_topics_are_rejected_on_a_non_dds_protocol():
    config = dds_config(protocol="udp", side="receiver")
    with pytest.raises(ValueError, match="only meaningful on a 'dds' connection"):
        ConnectionConfig.from_json(config)


def test_a_dds_connection_needs_topics():
    with pytest.raises(ValueError, match=r"config\['topics'\]"):
        build(topics=[])


@pytest.mark.parametrize("echo_key", ["echo_opcode", "recv_echo_opcode", "EchoInterval"])
def test_echo_is_rejected_on_dds_at_connection_level(echo_key):
    """The echo lifecycle transmits raw bytes; a DataWriter cannot accept them,
    so an echo here would never heartbeat and its watchdog would then drop
    every unit. Better a load error than a connection that dies on a timer."""
    with pytest.raises(ValueError, match="not supported on a 'dds' connection"):
        ConnectionConfig.from_json(dds_config(**{echo_key: 5}))


def test_echo_is_rejected_on_dds_inside_a_unit_block():
    config = dds_config()
    config["connections"][PEER_NAME]["echo_opcode"] = 9
    with pytest.raises(ValueError, match="not supported on a 'dds' connection"):
        ConnectionConfig.from_json(config)


def test_all_units_must_share_a_domain_id():
    connection = build(connections={
        "A": {"port": 77, "unitCode": 7},
        "B": {"port": 78, "unitCode": 8},
    })
    with pytest.raises(ValueError, match="must share a domain id"):
        _ = connection.domain_id


def test_domain_id_comes_from_the_port():
    assert build().domain_id == TEST_DOMAIN


# --------------------------------------------------------------------------- #
# Direction and capabilities
# --------------------------------------------------------------------------- #
def test_direction_defaults_from_side():
    publisher = build(side="publisher")
    assert publisher.config.topics[0].direction is TopicDirection.PUBLISH
    subscriber = build(side="subscriber")
    assert subscriber.config.topics[0].direction is TopicDirection.SUBSCRIBE


def test_explicit_direction_overrides_side():
    connection = build(
        side="publisher",
        topics=[{"topic": TOPIC, "type": "Track", "direction": "both"}])
    topic = connection.config.topics[0]
    assert topic.publishes and topic.subscribes


def test_capabilities_are_the_union_of_topic_directions():
    """A subscribe-only DDS connection must not advertise itself as a sender --
    that is what stops CompositeUnit picking it as one."""
    subscriber = build(side="subscriber")
    assert (subscriber.can_send, subscriber.can_receive) == (False, True)

    publisher = build(side="publisher")
    assert (publisher.can_send, publisher.can_receive) == (True, False)

    duplex = build(topics=[
        {"topic": TOPIC, "type": "Track", "direction": "subscribe"},
        {"topic": STATUS_TOPIC, "type": "Status", "direction": "publish"},
    ])
    assert (duplex.can_send, duplex.can_receive) == (True, True)


def test_bad_direction_is_rejected():
    with pytest.raises(ValueError, match="is not one of"):
        build(topics=[{"topic": TOPIC, "type": "Track", "direction": "sideways"}])


# --------------------------------------------------------------------------- #
# QoS: the topic_filter fix
# --------------------------------------------------------------------------- #
def test_topic_filter_is_honored_and_the_profile_only_lookup_is_not():
    """
    The regression this pins: `datawriter_qos_from_profile(profile)` takes no
    topic name, so it cannot evaluate a `topic_filter` and hands every topic the
    profile's baseline. Only the topic-aware accessor sees the override.
    """
    provider = dds.QosProvider(QOS_FILE)
    baseline = provider.set_topic_datawriter_qos("MyLib::Reliable", TOPIC)
    filtered = provider.set_topic_datawriter_qos("MyLib::Reliable", STATUS_TOPIC)
    profile_only = provider.datawriter_qos_from_profile("MyLib::Reliable")

    assert filtered.history.depth == 37, "the topic_filter override should apply"
    assert baseline.history.depth == 10, "an unfiltered topic keeps the baseline"
    assert profile_only.history.depth == 10, "profile-only lookup is blind to the filter"


def test_qos_for_prefers_the_topic_aware_accessor():
    connection = build(qos_file=QOS_FILE, qos_profile="MyLib::Reliable")
    connection._qos_provider = connection._load_qos_provider()
    assert connection._qos_for("datawriter", STATUS_TOPIC).history.depth == 37
    assert connection._qos_for("datawriter", TOPIC).history.depth == 10


def test_qos_for_returns_none_without_a_qos_file():
    connection = build()
    assert connection._load_qos_provider() is None
    assert connection._qos_for("datawriter", TOPIC) is None


def test_missing_qos_file_fails_loudly():
    connection = build(qos_file="core/configs/qos/does_not_exist.xml")
    with pytest.raises(FileNotFoundError, match="qos_file"):
        connection._load_qos_provider()


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #
def test_type_resolution_finds_the_class_in_the_named_module():
    connection = build()
    connection._type_modules = connection._import_all((TYPE_MODULE,))
    resolved = connection._resolve_type(connection.config.topics[0])
    idl.get_type_support(resolved)
    assert resolved.__name__ == "Track"


def test_unknown_type_names_the_topic_and_what_is_available():
    connection = build(topics=[{"topic": TOPIC, "type": "Nonexistent"}])
    connection._type_modules = connection._import_all((TYPE_MODULE,))
    with pytest.raises(ValueError) as excinfo:
        connection._resolve_type(connection.config.topics[0])
    message = str(excinfo.value)
    assert "Nonexistent" in message and TOPIC in message and "Track" in message


def test_a_class_without_idl_struct_is_rejected():
    connection = build(topics=[{"topic": TOPIC, "type": "NotAType"}])

    class NotAType:
        pass

    module = type("fake", (), {"NotAType": NotAType})
    connection._type_modules = [module]
    with pytest.raises(TypeError, match="@idl.struct"):
        connection._resolve_type(connection.config.topics[0])


def test_type_resolver_escape_hatch_wins(track_type):
    connection = build(type_resolver=lambda topic_name: track_type)
    assert connection._resolve_type(connection.config.topics[0]) is track_type


# --------------------------------------------------------------------------- #
# Header extraction, routing, self-echo
# --------------------------------------------------------------------------- #
def test_sending_unit_comes_from_the_header(track_type):
    connection = build()
    sample = track_type()
    sample.header.source_unit = PEER_CODE
    assert connection._sending_unit(sample, connection.config.topics[0]) == PEER_NAME


def test_unconfigured_source_unit_is_dropped(track_type):
    connection = build(connections={
        PEER_NAME: {"port": TEST_DOMAIN, "unitCode": PEER_CODE},
        "Other": {"port": TEST_DOMAIN, "unitCode": 9},
    })
    sample = track_type()
    sample.header.source_unit = 200  # nobody's
    assert connection._sending_unit(sample, connection.config.topics[0]) is None


def test_a_headerless_sample_falls_back_to_the_single_configured_unit(track_type):
    """Bring-up case: a type without our header still routes when there is only
    one unit it could possibly have come from."""
    connection = build(header={"field": "no_such_header"})
    assert connection._sending_unit(track_type(), connection.config.topics[0]) == PEER_NAME


def test_a_headerless_sample_with_several_units_is_dropped(track_type):
    connection = build(
        header={"field": "no_such_header"},
        connections={
            PEER_NAME: {"port": TEST_DOMAIN, "unitCode": PEER_CODE},
            "Other": {"port": TEST_DOMAIN, "unitCode": 9},
        })
    assert connection._sending_unit(track_type(), connection.config.topics[0]) is None


def test_dispatch_delivers_under_the_topics_opcode(track_type):
    """The whole point of the surrogate: a standing callback registered for a
    topic's opcode receives that topic's samples."""
    connection = build()
    received: list = []
    done = threading.Event()

    connection.handle_on_receive(OPCODE, lambda message: (received.append(message), done.set()),
                                 unit_name=PEER_NAME)
    sample = track_type(track_id=41)
    sample.header.source_unit = PEER_CODE
    dispatch(connection, PEER_NAME, OPCODE, sample)

    assert done.wait(5), "callback registered on the topic's opcode never fired"
    assert received[0].track_id == 41
    connection.close()


def test_a_sample_from_ourselves_is_filtered(track_type):
    """DDS delivers a participant's own writes back to its own readers, so a
    duplex connection hears itself. `source_unit` is what says so."""
    connection = build()
    sample = track_type()
    sample.header.source_unit = OWN_CODE
    assert sample.header.source_unit == connection._own_unit_code
    # `_read_loop` skips these before dispatch; assert the discriminator it uses.
    assert connection._sending_unit(sample, connection.config.topics[0]) != PEER_NAME


def test_custom_header_field_names(track_type):
    connection = build(header={"field": "header", "source_unit": "destination_unit"})
    sample = track_type()
    sample.header.destination_unit = PEER_CODE
    assert connection._sending_unit(sample, connection.config.topics[0]) == PEER_NAME


# --------------------------------------------------------------------------- #
# Send path
# --------------------------------------------------------------------------- #
def test_outgoing_header_is_stamped(track_type):
    connection = build()
    sample = track_type()
    connection._stamp_outgoing(sample, PEER_NAME)
    assert sample.header.source_unit == OWN_CODE
    assert sample.header.destination_unit == PEER_CODE


def test_caller_set_header_values_are_not_overwritten(track_type):
    connection = build()
    sample = track_type()
    sample.header.source_unit = 111
    sample.header.destination_unit = 112
    connection._stamp_outgoing(sample, PEER_NAME)
    assert (sample.header.source_unit, sample.header.destination_unit) == (111, 112)


def test_stamping_can_be_disabled(track_type):
    connection = build(header={"stamp": False})
    sample = track_type()
    connection._stamp_outgoing(sample, PEER_NAME)
    assert (sample.header.source_unit, sample.header.destination_unit) == (0, 0)


def test_send_on_an_unknown_opcode_names_the_configured_topics(track_type):
    connection = build(side="publisher")
    with pytest.raises(ValueError) as excinfo:
        connection._loop_thread.await_coroutine(
            connection._do_send(PEER_NAME, track_type(), 0xABCD))
    assert TOPIC in str(excinfo.value)


def test_send_on_a_subscribe_only_topic_is_refused(track_type):
    connection = build(side="subscriber")
    with pytest.raises(ConnectionError, match="no DataWriter"):
        connection._loop_thread.await_coroutine(
            connection._do_send(PEER_NAME, track_type(), OPCODE))


def test_send_refuses_raw_bytes():
    connection = build(side="publisher")
    connection._writers[TOPIC] = object()  # a writer exists; the payload is wrong
    with pytest.raises(TypeError, match="typed samples, not bytes"):
        connection._loop_thread.await_coroutine(
            connection._do_send(PEER_NAME, b"raw", OPCODE))


# --------------------------------------------------------------------------- #
# Route validation (the IRS blocker)
# --------------------------------------------------------------------------- #
def test_subscribing_to_a_configured_topic_is_allowed():
    """Regression: this used to raise IRSNotFoundError for every DDS route,
    because DDS registers no IRS layouts and the check was unconditional."""
    connection = build()
    connection._validate_route(PEER_NAME, (PEER_CODE, OPCODE))


def test_subscribing_to_an_unconfigured_opcode_still_fails_loudly():
    connection = build()
    with pytest.raises(ValueError) as excinfo:
        connection._validate_route(PEER_NAME, (PEER_CODE, 0x4242))
    message = str(excinfo.value)
    assert TOPIC in message, "the error should name the topics that do exist"
    assert "topic_opcode" in message


def test_receive_message_accepts_a_dds_route(track_type, receive_in_background):
    connection = build()
    background = receive_in_background(connection, OPCODE, PEER_NAME, 5.0)
    sample = track_type(track_id=7)
    sample.header.source_unit = PEER_CODE
    # Give the background subscription time to arm -- subscribe-or-drop.
    threading.Event().wait(0.3)
    dispatch(connection, PEER_NAME, OPCODE, sample)
    background.join()
    unit, message = background.result
    assert unit == PEER_NAME and message.track_id == 7
    connection.close()


# --------------------------------------------------------------------------- #
# Live domain (needs an RTI license)
# --------------------------------------------------------------------------- #
@requires_license
def test_publish_and_subscribe_over_a_real_domain(manager, track_type):
    """
    The end-to-end proof: two participants, one topic, a real sample.

    This is what fails if `rti.asyncio` stops being imported (no
    `take_data_async`) or if route validation regresses -- neither of which any
    of the offline tests above can catch.
    """
    def config(own, peer_name, peer_code, direction):
        return {
            "protocol": "dds",
            "side": "publisher" if direction == "publish" else "subscriber",
            "ip": "0.0.0.0",
            "unitCode": own,
            "connections": {peer_name: {"port": TEST_DOMAIN, "unitCode": peer_code}},
            "idl_modules": [TYPE_MODULE],
            "qos_file": QOS_FILE,
            "qos_profile": "MyLib::Reliable",
            "topics": [{"topic": TOPIC, "type": "Track", "direction": direction}],
        }

    subscriber = manager.create("sub", config(PEER_CODE, "Pub", OWN_CODE, "subscribe"))
    publisher = manager.create("pub", config(OWN_CODE, "Sub", PEER_CODE, "publish"))
    subscriber.start()
    publisher.start()
    # TRANSIENT_LOCAL durability in the profile means a sample published before
    # discovery completes is still delivered, so no sleep is load-bearing here.
    unit, message = subscriber.receive_message(
        OPCODE, timeout=15,
        trigger_function=lambda: publisher.send_message(
            track_type(track_id=99, x=1.5), opcode=OPCODE, unit_name="Sub"))

    assert unit == "Pub", "the sender should be identified from header.source_unit"
    assert message.track_id == 99 and message.x == 1.5
    assert message.header.source_unit == OWN_CODE
    assert message.header.destination_unit == PEER_CODE
