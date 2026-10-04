"""
DDS connection tests.

Split by what each one needs, because RTI has two very different requirements:

  * Loading a DDS Interface, parsing a QoS XML and building `@idl.struct` types
    needs only the `rti.connextdds` package. Almost all of this file runs there.
  * Creating a DomainParticipant needs an RTI LICENSE. The one test that puts a
    live participant on a domain is gated behind `requires_license`, so this
    suite stays green on a machine without one instead of reporting a code
    failure for an environment problem.

Routing is exercised by driving `_dispatch_incoming` directly, and sends by
handing a connection a recording writer -- no network at all. Negative-case
Interfaces are written to `tmp_path` and loaded by path, through the same loader
a deployment uses.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import logging
import sys
import textwrap
import threading
from pathlib import Path

import pytest

pytest.importorskip("rti.connextdds", reason="RTI Connext Python API not installed")

import rti.connextdds as dds  # noqa: E402

import core.connections.dds as dds_module  # noqa: E402
from core.connections.config import ConnectionConfig, TransportProtocol  # noqa: E402
from core.connections.dds import DdsConnection  # noqa: E402
from core.connections.dds_config import (DEFAULT_DOMAIN_ID, DEFAULT_QOS_FILE,  # noqa: E402
                                         TopicDirection, load_dds_interface, resolve_unit)
from core.connections.handlers import UnitHandler, route  # noqa: E402
from core.DDS import DdsUnit  # noqa: E402
from core.DDS.idl_types.Example.example_topics import SourceId, Status, Track  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
INTERFACE = str(REPO_ROOT / "core" / "DDS" / "Interfaces" / "Example" / "example_interface.py")
INTERFACE_DOTTED = "core.DDS.Interfaces.Example.example_interface"
#: The tests' own QoS file. DEFAULT_QOS_FILE is the deployment's, so its
#: contents are not something a test may pin.
QOS_FILE = str(Path(__file__).resolve().parent / "qos_fixture.xml")

SENSOR, CONTROL = "SensorUnit", "ControlUnit"
SENSOR_CODE, CONTROL_CODE = 0x01, 0x02
#: Well outside the default range so a stray participant on the machine cannot
#: join a test's domain.
TEST_DOMAIN = 77

#: The imports every stand-in Interface starts with -- absolute, as the rule is.
PREAMBLE = """\
from core.DDS import DdsUnit
from core.DDS.idl_types.Example.example_topics import Status, Track
INTERFACE_FORMAT = 1
"""

#: Three units, to have the cases two cannot express:
#:   * B hears Track from two publishers (A and C), but Status from A alone;
#:   * C is B's peer without publishing Status, and A's peer without
#:     subscribing Track.
THREE_UNITS = """\
A = DdsUnit(unitCode=0x0A, publish=(Track, Status))
B = DdsUnit(unitCode=0x0B, subscribe=(Track, Status))
C = DdsUnit(unitCode=0x0C, publish=(Track,), subscribe=(Status,))
"""

#: A topic type with no A_sourceID, for the cases where the sender cannot be
#: read off the sample.
NO_SOURCE_ID = """\
import rti.types as idl

@idl.struct
class Plain:
    x: idl.int32 = 0

"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def dds_config(unit: str = SENSOR, interface: str = INTERFACE, **overrides) -> dict:
    config = {"protocol": "dds", "unit": unit, "dds_interface": interface}
    config.update(overrides)
    return config


def build(unit: str = SENSOR, interface: str = INTERFACE, **overrides) -> DdsConnection:
    """A DdsConnection that was never started -- no participant, no license."""
    return DdsConnection(ConnectionConfig.from_json(dds_config(unit, interface, **overrides)))


def write_interface(tmp_path: Path, body: str, preamble: bool = True, types: str = "") -> str:
    """A stand-in for a generated Interface, written where a deployment's would
    be. `body` is dedented; the absolute-import preamble is prepended unless the
    test is about the preamble itself, then `types` (extra type definitions)."""
    path = tmp_path / "dds_interface.py"
    path.write_text((PREAMBLE if preamble else "") + types + textwrap.dedent(body), encoding="utf-8")
    return str(path)


def sample_from(cls, source_code: int, **fields):
    sample = cls(**fields)
    sample.A_sourceID.A_systemId = source_code
    return sample


def spec_of(connection: DdsConnection, topic: str):
    spec = connection.config.dds.topic_named(topic)
    assert spec is not None, f"{connection.config.dds.unit} has no topic {topic}"
    return spec


def dispatch(connection: DdsConnection, unit_name: str, topic: str, sample) -> None:
    """Feed one sample through the framework's dispatch point, on the loop
    thread where a read loop would have called it."""
    async def fire() -> None:
        connection._dispatch_incoming(unit_name, topic, sample)
    connection._loop_thread.await_coroutine(fire())


class RecordingWriter:
    """Stands in for a DataWriter: keeps what `write` was given."""

    def __init__(self) -> None:
        self.written: list = []

    def write(self, sample) -> None:
        self.written.append(sample)


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


# --------------------------------------------------------------------------- #
# DdsUnit -- checked at the Interface's own import
# --------------------------------------------------------------------------- #
def test_a_lone_class_is_a_one_element_tuple():
    """`publish=(Track)` has no trailing comma, so it is not a tuple -- a
    generator's punctuation must not be able to break the contract."""
    unit = DdsUnit(unitCode=1, publish=(Track), subscribe=Status)
    assert unit.publish == (Track,) and unit.subscribe == (Status,)


def test_any_iterable_of_topic_classes_is_accepted():
    assert DdsUnit(unitCode=1, publish=[Track, Status]).publish == (Track, Status)


@pytest.mark.parametrize("not_a_topic", [int, object, "Track"])
def test_an_entry_that_is_not_a_topic_type_is_refused(not_a_topic):
    with pytest.raises(TypeError):
        DdsUnit(unitCode=1, publish=(not_a_topic,))


def test_a_class_without_idl_struct_is_refused_with_the_fix():
    class Plain:
        pass

    with pytest.raises(TypeError, match=r"@idl\.struct"):
        DdsUnit(unitCode=1, subscribe=(Plain,))


@pytest.mark.parametrize("code, error", [(-1, ValueError), (256, ValueError),
                                         ("1", TypeError), (True, TypeError)])
def test_unit_code_must_be_a_uint8_int(code, error):
    with pytest.raises(error):
        DdsUnit(unitCode=code)


def test_a_topic_listed_twice_is_refused():
    with pytest.raises(ValueError, match="more than once"):
        DdsUnit(unitCode=1, publish=(Track, Track))


def test_a_unit_is_immutable():
    unit = DdsUnit(unitCode=1, publish=(Track,))
    with pytest.raises(dataclasses.FrozenInstanceError):
        unit.unitCode = 2  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Loading and resolving an Interface
# --------------------------------------------------------------------------- #
def test_each_unit_gets_its_own_view_of_the_example_interface():
    sensor = resolve_unit(INTERFACE, SENSOR)
    assert (sensor.unit_code, sensor.system) == (SENSOR_CODE, "ExampleSystem")
    assert sensor.peers == ((CONTROL, CONTROL_CODE),)
    track, status = sensor.topics
    assert (track.name, track.sample_type, track.direction) == ("Track", Track, TopicDirection.PUBLISH)
    assert (track.publishers, track.subscribers) == ((), (CONTROL,))
    assert (status.name, status.direction, status.publishers) == ("Status", TopicDirection.SUBSCRIBE, (CONTROL,))

    control = resolve_unit(INTERFACE, CONTROL)
    assert control.peers == ((SENSOR, SENSOR_CODE),)
    assert {spec.name: spec.direction for spec in control.topics} == {
        "Status": TopicDirection.PUBLISH, "Track": TopicDirection.SUBSCRIBE}


def test_an_interface_loads_by_dotted_name_too():
    assert resolve_unit(INTERFACE_DOTTED, SENSOR).unit_code == SENSOR_CODE


def test_one_file_is_one_module_object():
    """The identity the topic classes depend on: loading the same file again --
    however its path is spelled -- must not mint a second module."""
    spelled_differently = str(Path(INTERFACE).parent / ".." / "Example" / Path(INTERFACE).name)
    assert load_dds_interface(INTERFACE) is load_dds_interface(spelled_differently)


def test_the_interface_classes_are_the_ones_application_code_imports():
    assert load_dds_interface(INTERFACE).Track is Track


@pytest.mark.parametrize("format_line, expected", [
    ("", "the generated file must set it"),
    ("INTERFACE_FORMAT = 2", "reads format 1"),
])
def test_the_interface_format_is_checked(tmp_path, format_line, expected):
    path = write_interface(tmp_path, f"""\
        from core.DDS import DdsUnit
        from core.DDS.idl_types.Example.example_topics import Status, Track
        {format_line}
        A = DdsUnit(unitCode=1, publish=(Track,))
        B = DdsUnit(unitCode=2, subscribe=(Track,))
        """, preamble=False)
    with pytest.raises(ValueError, match=expected):
        resolve_unit(path, "A")


def test_an_interface_with_no_units_is_refused(tmp_path):
    path = write_interface(tmp_path, "")
    with pytest.raises(ValueError, match="defines no DdsUnit"):
        resolve_unit(path, "A")


def test_an_interface_defining_its_own_ddsunit_class_is_told_to_import_it(tmp_path):
    path = write_interface(tmp_path, """\
        from core.DDS.idl_types.Example.example_topics import Status, Track
        INTERFACE_FORMAT = 1

        class DdsUnit:
            def __init__(self, unitCode, publish, subscribe):
                self.unitCode, self.publish, self.subscribe = unitCode, publish, subscribe

        SensorUnit = DdsUnit(unitCode=1, publish=(Track,), subscribe=(Status,))
        """, preamble=False)
    with pytest.raises(ValueError, match="from core.DDS import DdsUnit"):
        resolve_unit(path, "SensorUnit")


def test_an_unknown_unit_lists_the_ones_that_exist():
    with pytest.raises(ValueError) as excinfo:
        resolve_unit(INTERFACE, "Nobody")
    assert SENSOR in str(excinfo.value) and CONTROL in str(excinfo.value)


def test_duplicate_unit_codes_are_refused(tmp_path):
    path = write_interface(tmp_path, """\
        A = DdsUnit(unitCode=7, publish=(Track,))
        B = DdsUnit(unitCode=7, subscribe=(Track,))
        """)
    with pytest.raises(ValueError, match="both use unitCode 0x07"):
        resolve_unit(path, "A")


def test_one_unit_under_two_names_is_refused(tmp_path):
    path = write_interface(tmp_path, """\
        A = DdsUnit(unitCode=1, publish=(Track,))
        B = DdsUnit(unitCode=2, subscribe=(Track,))
        Alias = A
        """)
    with pytest.raises(ValueError, match="same DdsUnit"):
        resolve_unit(path, "B")


def test_two_classes_with_one_name_are_refused(tmp_path):
    """A topic is named after its type, so these would be one topic on the
    wire carried by two types -- or one module imported two ways."""
    path = write_interface(tmp_path, """\
        import rti.types as idl
        from core.DDS.idl_types.Example import example_topics

        @idl.struct
        class Track:
            x: idl.int32 = 0

        A = DdsUnit(unitCode=1, publish=(example_topics.Track,))
        B = DdsUnit(unitCode=2, subscribe=(Track,))
        """)
    with pytest.raises(ValueError, match="two different classes"):
        resolve_unit(path, "A")


def test_a_pinned_type_name_is_the_topic_name(tmp_path):
    """IDL modules flatten into the class name (`P_Radar_PSM_Track`) but stay
    scoped in the pinned type name, which is what peers name the topic by."""
    path = write_interface(tmp_path, """\
        import rti.types as idl

        @idl.struct(type_annotations=[idl.type_name("P_Radar_PSM::Track")])
        class P_Radar_PSM_Track:
            x: idl.int32 = 0

        A = DdsUnit(unitCode=1, publish=(P_Radar_PSM_Track,))
        B = DdsUnit(unitCode=2, subscribe=(P_Radar_PSM_Track,))
        """)
    a = resolve_unit(path, "A")
    (spec,) = a.topics
    assert (spec.name, spec.type_name) == ("P_Radar_PSM::Track", "P_Radar_PSM::Track")
    assert a.topic_for(spec.sample_type) is spec and a.topic_for("P_Radar_PSM::Track") is spec


def test_a_unit_with_no_peers_is_refused(tmp_path):
    path = write_interface(tmp_path, """\
        A = DdsUnit(unitCode=1, publish=(Track,))
        B = DdsUnit(unitCode=2, publish=(Status,))
        """)
    with pytest.raises(ValueError, match="no peers"):
        resolve_unit(path, "A")


def test_dangling_topics_are_warned_about(tmp_path, caplog):
    path = write_interface(tmp_path, """\
        A = DdsUnit(unitCode=1, publish=(Track, Status))
        B = DdsUnit(unitCode=2, subscribe=(Track,))
        """)
    with caplog.at_level(logging.WARNING, logger="connmgr.dds"):
        resolve_unit(path, "A")
    assert any("publishes 'Status', but no other unit subscribes it" in r.message for r in caplog.records)


def test_a_relative_import_is_refused_with_the_reason(tmp_path):
    (tmp_path / "topics.py").write_text("X = 1\n", encoding="utf-8")
    path = write_interface(tmp_path, """\
        from core.DDS import DdsUnit
        from .topics import X
        INTERFACE_FORMAT = 1
        """, preamble=False)
    with pytest.raises(ImportError, match="absolute module name"):
        load_dds_interface(path)


def test_a_missing_interface_file_is_named(tmp_path):
    with pytest.raises(FileNotFoundError, match="DDS Interface not found"):
        load_dds_interface(str(tmp_path / "nowhere.py"))


def test_publishing_and_subscribing_one_topic_is_both(tmp_path):
    path = write_interface(tmp_path, """\
        A = DdsUnit(unitCode=1, publish=(Track,), subscribe=(Track,))
        B = DdsUnit(unitCode=2, publish=(Track,), subscribe=(Track,))
        """)
    (spec,) = resolve_unit(path, "A").topics
    assert spec.direction is TopicDirection.BOTH
    assert (spec.publishers, spec.subscribers) == (("B",), ("B",))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_a_dds_config_is_one_unit_of_an_interface():
    config = ConnectionConfig.from_json(dds_config())
    assert config.protocol is TransportProtocol.DDS
    assert (config.side, config.ip, config.local_ip) == (None, None, None)
    assert config.unitCode == SENSOR_CODE
    assert list(config.connections) == [CONTROL]
    assert config.connections[CONTROL].unitCode == CONTROL_CODE
    assert config.dds.unit == SENSOR


def test_unit_id_is_accepted_for_unit():
    config = ConnectionConfig.from_json({"protocol": "dds", "unit_id": CONTROL, "dds_interface": INTERFACE})
    assert config.unitCode == CONTROL_CODE


@pytest.mark.parametrize("missing", ["unit", "dds_interface"])
def test_unit_and_interface_are_required(missing):
    config = dds_config()
    del config[missing]
    with pytest.raises(ValueError, match="protocol 'dds' needs"):
        ConnectionConfig.from_json(config)


@pytest.mark.parametrize("key, value", [
    ("side", "publisher"), ("ip", "0.0.0.0"), ("local_ip", "0.0.0.0"), ("unitCode", 22),
    ("connections", {"Peer": {"port": 0, "unitCode": 7}}), ("topics", []),
    ("idl_modules", ["x"]), ("idl_file", "x.py"), ("Structures", ["x"]),
    ("echo_opcode", 5), ("EchoInterval", 1.0),
])
def test_socket_and_hand_routed_keys_are_refused(key, value):
    """A DDS node has no socket to describe, and its routing is the Interface's
    to say: each of these is refused with the reason, never silently ignored."""
    with pytest.raises(ValueError, match="protocol 'dds' does not accept") as excinfo:
        ConnectionConfig.from_json(dds_config(**{key: value}))
    assert repr(key) in str(excinfo.value)


def test_topics_are_refused_on_socket_protocols_too():
    config = {"protocol": "tcp", "side": "server", "ip": "127.0.0.1", "unitCode": 1,
              "connections": {"Peer": {"port": 5000, "unitCode": 2}}, "topics": []}
    with pytest.raises(ValueError, match="only DDS has topics"):
        ConnectionConfig.from_json(config)


def test_domain_and_qos_file_default_to_the_constants():
    config = ConnectionConfig.from_json(dds_config())
    assert config.dds.domain_id == DEFAULT_DOMAIN_ID
    assert config.dds.qos_file == DEFAULT_QOS_FILE
    assert DEFAULT_QOS_FILE.is_absolute() and DEFAULT_QOS_FILE.is_file()
    assert config.dds.qos_profile is None


def test_domain_and_qos_file_can_still_be_overridden():
    config = ConnectionConfig.from_json(dds_config(domain_id=TEST_DOMAIN, qos_file=QOS_FILE,
                                                   qos_profile="MyLib::Reliable"))
    assert (config.dds.domain_id, config.dds.qos_profile) == (TEST_DOMAIN, "MyLib::Reliable")
    assert config.dds.qos_file == Path(QOS_FILE).resolve()
    assert {endpoint.port for endpoint in config.connections.values()} == {TEST_DOMAIN}, \
        "a DDS endpoint's port is its domain"


@pytest.mark.parametrize("bad", [-1, "7", True, 1.5])
def test_a_bad_domain_id_is_refused(bad):
    with pytest.raises(ValueError, match="domain_id"):
        ConnectionConfig.from_json(dds_config(domain_id=bad))


def test_a_missing_qos_file_fails_at_load():
    with pytest.raises(FileNotFoundError, match="qos_file"):
        ConnectionConfig.from_json(dds_config(qos_file="core/DDS/Configuration/does_not_exist.xml"))


def test_an_unknown_qos_profile_fails_at_create_naming_the_files_profiles():
    with pytest.raises(ValueError) as excinfo:
        build(qos_file=QOS_FILE, qos_profile="MyLib::Nope")
    message = str(excinfo.value)
    assert "MyLib::Nope" in message and "MyLib::Reliable" in message
    assert "BuiltinQosLib::" not in message, "RTI's built-in profiles would bury the file's own"


def test_a_builtin_qos_profile_is_accepted():
    build(qos_file=QOS_FILE, qos_profile="BuiltinQosLib::Generic.StrictReliable")


# --------------------------------------------------------------------------- #
# Capabilities: the union of this unit's topic directions
# --------------------------------------------------------------------------- #
def test_capabilities_follow_the_interface(tmp_path):
    """A subscribe-only unit must not advertise itself as a sender -- that is
    what stops CompositeUnit picking it as one."""
    assert (build(SENSOR).can_send, build(SENSOR).can_receive) == (True, True)
    path = write_interface(tmp_path, THREE_UNITS)
    assert (build("A", path).can_send, build("A", path).can_receive) == (True, False)
    assert (build("B", path).can_send, build("B", path).can_receive) == (False, True)


# --------------------------------------------------------------------------- #
# QoS: topic filters, with and without a named profile
# --------------------------------------------------------------------------- #
def test_topic_filter_is_honored_and_the_profile_only_lookup_is_not():
    """
    The regression this pins: `datawriter_qos_from_profile(profile)` takes no
    topic name, so it cannot evaluate a `topic_filter` and hands every topic the
    profile's baseline. Only the topic-aware accessor sees the override.
    """
    provider = dds.QosProvider(QOS_FILE)
    assert provider.set_topic_datawriter_qos("MyLib::Reliable", "Status").history.depth == 37
    assert provider.set_topic_datawriter_qos("MyLib::Reliable", "Track").history.depth == 10
    assert provider.datawriter_qos_from_profile("MyLib::Reliable").history.depth == 10


@pytest.mark.parametrize("profile", [None, "MyLib::Reliable"])
def test_entity_qos_honors_topic_filters_with_or_without_a_profile(profile):
    """With no profile named, the file's default profile is used -- through the
    topic-aware `get_topic_*_qos`, not the filter-blind `.datawriter_qos`."""
    overrides = {} if profile is None else {"qos_profile": profile}
    connection = build(SENSOR, qos_file=QOS_FILE, **overrides)
    assert connection._qos_for("datawriter", "Status").history.depth == 37
    assert connection._qos_for("datawriter", "Track").history.depth == 10


# --------------------------------------------------------------------------- #
# Selectors: topics, not opcodes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("selector", [Track, Track(), "Track"])
def test_a_topic_is_selected_by_class_sample_or_name(selector):
    assert build()._message_key(selector) == "Track"


def test_an_opcode_is_refused_with_the_reason():
    with pytest.raises(TypeError, match="DDS has no opcodes"):
        build()._message_key(0x12)


@pytest.mark.parametrize("selector", ["Nope", SourceId])
def test_an_unknown_topic_lists_the_units_topics(selector):
    with pytest.raises(ValueError) as excinfo:
        build()._message_key(selector)
    assert "Track" in str(excinfo.value) and "Status" in str(excinfo.value)


def test_a_second_copy_of_a_topic_class_is_named_as_such():
    """The trap a path-loaded module sets: same class name, different object.
    It must say so, not report an unknown topic."""
    source = REPO_ROOT / "core" / "DDS" / "idl_types" / "Example" / "example_topics.py"
    spec = importlib.util.spec_from_file_location("_second_copy_of_example_topics", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        with pytest.raises(TypeError, match="two copies of one generated module"):
            build()._message_key(module.Track)
        with pytest.raises(TypeError, match="two copies of one generated module"):
            build().send_message(module.Track())
    finally:
        sys.modules.pop("_second_copy_of_example_topics", None)


# --------------------------------------------------------------------------- #
# Inbound: who sent it
# --------------------------------------------------------------------------- #
def test_the_sender_comes_from_a_source_id_system_id():
    control = build(CONTROL)
    assert control._sending_unit(sample_from(Track, SENSOR_CODE), spec_of(control, "Track")) == SENSOR


def test_platform_and_module_ids_do_not_pick_the_sender():
    control = build(CONTROL)
    sample = sample_from(Track, SENSOR_CODE)
    sample.A_sourceID.A_platformId, sample.A_sourceID.A_moduleId = 1, CONTROL_CODE
    assert control._sending_unit(sample, spec_of(control, "Track")) == SENSOR


def test_a_sample_without_source_id_falls_back_to_the_sole_publisher(tmp_path):
    b = build("B", write_interface(tmp_path, """\
        A = DdsUnit(unitCode=1, publish=(Plain,))
        B = DdsUnit(unitCode=2, subscribe=(Plain,))
        """, types=NO_SOURCE_ID))
    spec = spec_of(b, "Plain")
    assert b._sending_unit(spec.sample_type(), spec) == "A"


def test_an_unstamped_source_id_falls_back_to_the_sole_publisher():
    control = build(CONTROL)
    assert control._sending_unit(Track(), spec_of(control, "Track")) == SENSOR


def test_with_several_publishers_the_source_id_decides(tmp_path):
    b = build("B", write_interface(tmp_path, THREE_UNITS))
    assert b._sending_unit(sample_from(Track, 0x0C), spec_of(b, "Track")) == "C"
    assert b._sending_unit(sample_from(Track, 0x0A), spec_of(b, "Track")) == "A"


def test_several_publishers_and_no_source_id_fails_at_load(tmp_path):
    """Every sample would be unattributable -- a load error, not silent drops."""
    path = write_interface(tmp_path, """\
        A = DdsUnit(unitCode=0x0A, publish=(Plain,))
        B = DdsUnit(unitCode=0x0B, subscribe=(Plain,))
        C = DdsUnit(unitCode=0x0C, publish=(Plain,))
        """, types=NO_SOURCE_ID)
    with pytest.raises(ValueError, match=r"2 publishers \['A', 'C'\]"):
        build("B", path)


def test_a_topic_the_unit_also_publishes_needs_a_source_id(tmp_path):
    path = write_interface(tmp_path, """\
        A = DdsUnit(unitCode=1, publish=(Plain,), subscribe=(Plain,))
        B = DdsUnit(unitCode=2, publish=(Plain,), subscribe=(Plain,))
        """, types=NO_SOURCE_ID)
    with pytest.raises(ValueError, match="hears its own writes"):
        build("A", path)


def test_a_units_own_samples_are_filtered(tmp_path):
    path = write_interface(tmp_path, """\
        A = DdsUnit(unitCode=1, publish=(Track,), subscribe=(Track,))
        B = DdsUnit(unitCode=2, publish=(Track,), subscribe=(Track,))
        """)
    a = build("A", path)
    assert a._sending_unit(sample_from(Track, 1), spec_of(a, "Track")) is None
    assert a._sending_unit(sample_from(Track, 2), spec_of(a, "Track")) == "B"


def test_a_peer_the_interface_does_not_list_as_publisher_is_dropped_and_warned_once(tmp_path, caplog):
    b = build("B", write_interface(tmp_path, THREE_UNITS))
    with caplog.at_level(logging.WARNING, logger="connmgr.dds"):
        for _ in range(3):
            assert b._sending_unit(sample_from(Status, 0x0C), spec_of(b, "Status")) is None
    warnings = [r for r in caplog.records if "does not list C as publishing it" in r.message]
    assert len(warnings) == 1, "a cause is warned about once, not once per sample"


def test_a_sample_from_an_unknown_unit_code_is_dropped():
    control = build(CONTROL)
    assert control._sending_unit(sample_from(Track, 0x99), spec_of(control, "Track")) is None


# --------------------------------------------------------------------------- #
# Receiving through the public API
# --------------------------------------------------------------------------- #
def test_a_callback_registered_by_class_gets_the_topics_samples():
    control = build(CONTROL)
    received: list = []
    done = threading.Event()
    control.handle_on_receive(Track, lambda message: (received.append(message), done.set()))
    dispatch(control, SENSOR, "Track", sample_from(Track, SENSOR_CODE, track_id=41))
    assert done.wait(5), "the callback registered for Track never fired"
    assert received[0].track_id == 41


def test_receive_message_by_class_returns_the_sample(receive_in_background):
    control = build(CONTROL)
    background = receive_in_background(control, Track, None, 5.0)
    threading.Event().wait(0.3)  # subscribe-or-drop: let the subscription arm
    dispatch(control, SENSOR, "Track", sample_from(Track, SENSOR_CODE, track_id=7))
    background.join()
    assert background.result.track_id == 7


def test_route_decorator_takes_a_topic_class(manager):
    received: list = []
    done = threading.Event()

    class SensorHandler(UnitHandler):
        unitCode = SENSOR_CODE

        @route(Track)
        def on_track(self, message):
            received.append(message)
            done.set()

    control = manager.create("control", dds_config(CONTROL), handler_class=SensorHandler)
    dispatch(control, SENSOR, "Track", sample_from(Track, SENSOR_CODE, track_id=5))
    assert done.wait(5) and received[0].track_id == 5


def test_subscribing_to_a_topic_the_unit_only_publishes_fails_loudly():
    with pytest.raises(ValueError, match="does not subscribe 'Status'"):
        build(CONTROL).handle_on_receive(Status, lambda message: None)


def test_subscribing_for_a_unit_that_does_not_publish_the_topic_fails_loudly(tmp_path):
    b = build("B", write_interface(tmp_path, THREE_UNITS))
    with pytest.raises(ValueError, match="'C' does not publish 'Status'"):
        b.handle_on_receive(Status, lambda message: None, unit_name="C")


def test_unit_name_comes_from_the_interface_only_when_unambiguous(tmp_path):
    b = build("B", write_interface(tmp_path, THREE_UNITS))
    b.handle_on_receive(Status, lambda message: None)  # only A publishes Status
    with pytest.raises(ValueError, match="unit_name is required"):
        b.handle_on_receive(Track, lambda message: None)  # A and C both publish Track
    b.handle_on_receive(Track, lambda message: None, unit_name="C")


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def test_a_sample_is_sent_on_its_classs_topic_stamped_with_its_source():
    sensor = build(SENSOR)
    sensor._writers["Track"] = writer = RecordingWriter()
    sample = Track(track_id=3)
    sensor.send_message(sample)
    assert writer.written == [sample]
    source_id = sample.A_sourceID
    assert (source_id.A_platformId, source_id.A_systemId, source_id.A_moduleId) == (0, SENSOR_CODE, 0), \
        "only the unit code is ours to stamp"


def test_a_caller_set_system_id_is_not_overwritten():
    sensor = build(SENSOR)
    sensor._writers["Track"] = RecordingWriter()
    sample = Track()
    sample.A_sourceID.A_systemId = 111
    sensor.send_message(sample)
    assert sample.A_sourceID.A_systemId == 111


def test_send_before_start_says_so():
    with pytest.raises(ConnectionError, match="start"):
        build(SENSOR).send_message(Track())


def test_send_refuses_raw_bytes():
    with pytest.raises(TypeError, match="typed samples, not bytes"):
        build(SENSOR).send_message(b"raw")


def test_send_on_a_topic_the_unit_only_subscribes_is_refused():
    with pytest.raises(ValueError, match="does not publish 'Status'"):
        build(SENSOR).send_message(Status())


def test_periodic_sending_is_keyed_by_topic():
    sensor = build(SENSOR)
    sensor._writers["Track"] = writer = RecordingWriter()
    sensor.periodic_sending(Track(track_id=3), 0.02)
    deadline = threading.Event()
    for _ in range(100):
        if len(writer.written) >= 2:
            break
        deadline.wait(0.02)
    assert sensor.stop_periodic(Track) is True, "stop_periodic(Track) must find the schedule"
    assert len(writer.written) >= 2


def test_a_named_destination_is_checked():
    sensor = build(SENSOR)
    sensor._writers["Track"] = writer = RecordingWriter()
    sensor.send_message(Track(), unit_name=CONTROL)
    assert len(writer.written) == 1
    with pytest.raises(ValueError, match="does not subscribe"):
        sensor.send_message(Track(), unit_name="NoSuchUnit")


def test_a_selector_naming_another_topic_is_refused():
    sensor = build(SENSOR)
    sensor._writers["Track"] = RecordingWriter()
    with pytest.raises(ValueError, match="names topic 'Status'"):
        sensor.send_message(Track(), Status)


def test_an_int_opcode_is_refused_on_dds():
    with pytest.raises(TypeError, match="no opcodes"):
        build(SENSOR).send_message(Track(), 5)


def test_unknown_dds_config_keys_are_refused():
    with pytest.raises(ValueError, match="no setting"):
        ConnectionConfig.from_json(dds_config(domainID=3))

def test_a_header_block_is_refused_with_the_reason():
    """The sender's location is fixed (A_sourceID.A_systemId); a config that
    still tries to set it must not believe it is in force."""
    with pytest.raises(ValueError, match="A_sourceID"):
        ConnectionConfig.from_json(dds_config(header={"field": "header"}))


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def test_read_loops_stop_before_the_participant_closes():
    """The one teardown ordering RTI cannot do for us: a read loop's
    ReadCondition sits on rti.asyncio's process-wide WaitSet, and cancelling the
    loop is what detaches it -- so loops first, participant second."""
    connection = build(CONTROL)
    order: list = []

    class FakeParticipant:
        def close(self) -> None:
            order.append(("participant.close", all(task.done() for task in tasks)))

    async def read_loop() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            order.append("read loop stopped")

    async def scenario() -> None:
        tasks.append(connection._track(read_loop()))
        connection._read_tasks.extend(tasks)
        connection._participant = FakeParticipant()
        await asyncio.sleep(0)  # let the loop start waiting
        await connection._close_entities()

    tasks: list = []
    connection._loop_thread.await_coroutine(scenario())
    assert order == ["read loop stopped", ("participant.close", True)]
    assert connection._participant is None and connection._read_tasks == []


def test_a_failed_start_closes_what_it_created(monkeypatch):
    closed: list = []

    class FakeParticipant:
        implicit_publisher = implicit_subscriber = None

        def __init__(self, *args) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

    def failing_topic(*args, **kwargs):
        raise RuntimeError("topic creation failed")

    monkeypatch.setattr(dds_module.dds, "DomainParticipant", FakeParticipant)
    monkeypatch.setattr(dds_module.dds, "Topic", failing_topic)
    live_before = dds_module._live_connections
    connection = build(CONTROL)
    with pytest.raises(RuntimeError, match="topic creation failed"):
        connection.start()
    assert closed == [True], "the participant created before the failure must be closed"
    assert connection._participant is None and not connection._counted_live
    assert dds_module._live_connections == live_before


# --------------------------------------------------------------------------- #
# Live domain (needs an RTI license)
# --------------------------------------------------------------------------- #
@requires_license
def test_two_units_talk_over_a_real_domain(manager):
    """
    The end-to-end proof: each unit gets exactly the entities the Interface
    gives it, and samples flow both ways, attributed from their A_sourceID.

    This is what fails if `rti.asyncio` stops being imported (no
    `take_data_async`) -- which none of the offline tests above can catch.
    """
    sensor = manager.create("sensor", dds_config(SENSOR, domain_id=TEST_DOMAIN, qos_file=QOS_FILE))
    control = manager.create("control", dds_config(CONTROL, domain_id=TEST_DOMAIN, qos_file=QOS_FILE))
    control.start()
    sensor.start()
    assert (set(sensor._writers), set(sensor._readers)) == ({"Track"}, {"Status"})
    assert (set(control._writers), set(control._readers)) == ({"Status"}, {"Track"})

    # TRANSIENT_LOCAL durability in the profile means a sample published before
    # discovery completes is still delivered, so no sleep is load-bearing here.
    track = control.receive_message(
        Track, timeout=15,
        trigger_function=lambda: sensor.send_message(Track(track_id=99, x=1.5)))
    assert track.track_id == 99 and track.x == 1.5
    assert track.A_sourceID.A_systemId == SENSOR_CODE

    status = sensor.receive_message(
        Status, timeout=15,
        trigger_function=lambda: control.send_message(Status(healthy=False, message="degraded")))
    assert status.message == "degraded" and status.A_sourceID.A_systemId == CONTROL_CODE
