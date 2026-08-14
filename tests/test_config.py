"""
connections/config.py -- ConnectionConfig.from_json validation/coercion and
EchoSettings resolution. Pure logic, no I/O, no event loop.
"""
import pytest

from connections.config import ConnectionConfig, EchoSettings, Protocol, Side


def _base(**overrides):
    cfg = {
        "protocol": "tcp",
        "side": "server",
        "ip": "127.0.0.1",
        "unitCode": 1,
        "connections": {"Peer": {"port": 5000, "unitCode": 2}},
    }
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------- #
# Required top-level fields
# --------------------------------------------------------------------------- #
def test_own_unit_code_is_required():
    cfg = _base()
    del cfg["unitCode"]
    with pytest.raises(ValueError, match="unitCode"):
        ConnectionConfig.from_json(cfg)


def test_connections_is_required():
    cfg = _base()
    del cfg["connections"]
    with pytest.raises(ValueError, match="connections"):
        ConnectionConfig.from_json(cfg)


def test_connections_empty_dict_is_rejected():
    with pytest.raises(ValueError, match="connections"):
        ConnectionConfig.from_json(_base(connections={}))


@pytest.mark.parametrize("bad_code", [256, -1, 0x100])
def test_own_unit_code_out_of_uint8_range_rejected(bad_code):
    with pytest.raises(ValueError):
        ConnectionConfig.from_json(_base(unitCode=bad_code))


def test_own_unit_code_non_numeric_rejected():
    with pytest.raises(ValueError):
        ConnectionConfig.from_json(_base(unitCode="not-a-number"))


@pytest.mark.parametrize("alias", ["UnitCode", "unitCode", "unit_code"])
def test_own_unit_code_accepts_every_key_spelling(alias):
    cfg = _base()
    del cfg["unitCode"]
    cfg[alias] = 5
    config = ConnectionConfig.from_json(cfg)
    assert config.unitCode == 5


def test_protocol_and_side_are_lowercased_and_coerced_to_enums():
    config = ConnectionConfig.from_json(_base(protocol="TCP", side="SERVER"))
    assert config.protocol is Protocol.TCP
    assert config.side is Side.SERVER


def test_unknown_protocol_rejected():
    with pytest.raises(ValueError):
        ConnectionConfig.from_json(_base(protocol="carrier-pigeon"))


def test_local_ip_defaults_to_all_interfaces():
    config = ConnectionConfig.from_json(_base())
    assert config.local_ip == "0.0.0.0"


@pytest.mark.parametrize("key", ["local_ip", "localIp"])
def test_local_ip_accepts_both_key_spellings(key):
    config = ConnectionConfig.from_json(_base(**{key: "10.0.0.9"}))
    assert config.local_ip == "10.0.0.9"


# --------------------------------------------------------------------------- #
# Per-connection unit spec: port + unitCode
# --------------------------------------------------------------------------- #
def test_connection_missing_port_is_rejected():
    with pytest.raises(ValueError, match="port"):
        ConnectionConfig.from_json(_base(connections={"Peer": {"unitCode": 2}}))


def test_connection_spec_must_be_a_dict():
    with pytest.raises(ValueError):
        ConnectionConfig.from_json(_base(connections={"Peer": "not-a-dict"}))


def test_connection_port_non_numeric_rejected():
    with pytest.raises(ValueError, match="port"):
        ConnectionConfig.from_json(
            _base(connections={"Peer": {"port": "nope", "unitCode": 2}})
        )


@pytest.mark.parametrize("bad_port", [-1, 0x10000])
def test_connection_port_out_of_range_rejected(bad_port):
    with pytest.raises(ValueError, match="port"):
        ConnectionConfig.from_json(
            _base(connections={"Peer": {"port": bad_port, "unitCode": 2}})
        )


def test_connection_unit_code_is_required_per_unit():
    """No default-from-port fallback -- every connection must name its own
    unitCode explicitly."""
    with pytest.raises(ValueError):
        ConnectionConfig.from_json(_base(connections={"Peer": {"port": 5000}}))


@pytest.mark.parametrize("bad_code", [256, -1])
def test_connection_unit_code_out_of_uint8_range_rejected(bad_code):
    with pytest.raises(ValueError):
        ConnectionConfig.from_json(
            _base(connections={"Peer": {"port": 5000, "unitCode": bad_code}})
        )


def test_duplicate_unit_codes_across_connections_rejected():
    with pytest.raises(ValueError, match="unitCode"):
        ConnectionConfig.from_json(_base(connections={
            "A": {"port": 5000, "unitCode": 9},
            "B": {"port": 5001, "unitCode": 9},
        }))


def test_multiple_connections_with_distinct_codes_accepted():
    config = ConnectionConfig.from_json(_base(connections={
        "A": {"port": 5000, "unitCode": 9},
        "B": {"port": 5001, "unitCode": 10},
    }))
    assert config.unit_codes == {"A": 9, "B": 10}
    assert config.ports == [5000, 5001]
    assert sorted(config.connected_units) == ["A", "B"]


# --------------------------------------------------------------------------- #
# extra / lookups
# --------------------------------------------------------------------------- #
def test_unrecognized_keys_land_in_extra():
    config = ConnectionConfig.from_json(_base(idl_file="x.py", ttl=3))
    assert config.extra["idl_file"] == "x.py"
    assert config.extra["ttl"] == 3


def test_endpoint_for_unknown_unit_raises():
    config = ConnectionConfig.from_json(_base())
    with pytest.raises(ValueError, match="Peer"):
        config.endpoint_for("Ghost")


def test_unit_from_port_and_port_for_unit_round_trip():
    config = ConnectionConfig.from_json(_base())
    assert config.unit_from_port(5000) == "Peer"
    assert config.unit_from_port(9999) is None
    assert config.port_for_unit("Peer") == 5000
    assert config.port_for_unit("Ghost") is None


def test_config_is_frozen():
    config = ConnectionConfig.from_json(_base())
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError, a subclass of AttributeError
        config.ip = "changed"


# --------------------------------------------------------------------------- #
# EchoSettings.from_extra -- the shared/recv/send opcode resolution
# --------------------------------------------------------------------------- #
def test_echo_disabled_by_default():
    settings = EchoSettings.from_extra({})
    assert settings.enabled is False


def test_echo_shared_opcode_drives_both_directions():
    settings = EchoSettings.from_extra({"echo_opcode": 42})
    assert (settings.recv_opcode, settings.send_opcode) == (42, 42)
    assert settings.enabled is True


def test_echo_shared_opcode_overridden_in_one_direction():
    settings = EchoSettings.from_extra({"echo_opcode": 42, "send_echo_opcode": 43})
    assert (settings.recv_opcode, settings.send_opcode) == (42, 43)


@pytest.mark.parametrize("only_key,other_attr", [
    ("recv_echo_opcode", "send_opcode"),
    ("send_echo_opcode", "recv_opcode"),
])
def test_echo_one_sided_opcode_leaves_the_other_direction_unset(only_key, other_attr):
    """Regression: ECHO_OPCODE_KEYS must never include recv/send spellings,
    or a lone one-directional key gets misread as the symmetric `echo_opcode`
    and silently fills in the other direction too."""
    settings = EchoSettings.from_extra({only_key: 99})
    assert getattr(settings, other_attr) is None
    assert settings.enabled is False


def test_echo_timeout_must_exceed_interval():
    with pytest.raises(ValueError, match="EchoTimeout"):
        EchoSettings.from_extra({"echo_opcode": 1, "EchoInterval": 5.0, "EchoTimeout": 5.0})
    with pytest.raises(ValueError):
        EchoSettings.from_extra({"echo_opcode": 1, "EchoInterval": 5.0, "EchoTimeout": 1.0})


def test_echo_interval_and_timeout_defaults():
    settings = EchoSettings.from_extra({})
    assert settings.interval == 1.0
    assert settings.timeout == 5.0


@pytest.mark.parametrize("bad", [0, -1])
def test_echo_interval_must_be_positive(bad):
    with pytest.raises(ValueError):
        EchoSettings.from_extra({"EchoInterval": bad, "EchoTimeout": 5.0})


# --------------------------------------------------------------------------- #
# EchoSettings.resolve -- per-unit override vs connection-wide default
# --------------------------------------------------------------------------- #
def test_resolve_unit_with_no_echo_keys_inherits_global():
    global_extra = {"echo_opcode": 99, "EchoInterval": 1.0, "EchoTimeout": 5.0}
    settings = EchoSettings.resolve({}, global_extra)
    assert (settings.recv_opcode, settings.send_opcode) == (99, 99)


def test_resolve_unit_opcode_override_replaces_global_as_a_group():
    """Overriding with only send_echo_opcode must drop the inherited shared
    echo_opcode entirely, not merge it with the global recv side."""
    global_extra = {"echo_opcode": 99, "EchoInterval": 1.0, "EchoTimeout": 5.0}
    settings = EchoSettings.resolve({"send_echo_opcode": 55}, global_extra)
    assert settings.recv_opcode is None
    assert settings.send_opcode == 55


def test_resolve_unit_timing_overrides_individually():
    """EchoInterval/EchoTimeout resolve per-key, unlike the opcode group."""
    global_extra = {"echo_opcode": 99, "EchoInterval": 1.0, "EchoTimeout": 5.0}
    settings = EchoSettings.resolve({"EchoTimeout": 10.0}, global_extra)
    assert settings.interval == 1.0  # inherited
    assert settings.timeout == 10.0  # overridden
    assert (settings.recv_opcode, settings.send_opcode) == (99, 99)  # untouched


def test_resolve_null_opcode_opts_a_unit_out():
    global_extra = {"echo_opcode": 99, "EchoInterval": 1.0, "EchoTimeout": 5.0}
    settings = EchoSettings.resolve({"echo_opcode": None}, global_extra)
    assert settings.enabled is False


def test_resolve_bad_merge_names_error_from_unit_perspective():
    global_extra = {"EchoInterval": 1.0, "EchoTimeout": 5.0}
    with pytest.raises(ValueError, match="EchoTimeout"):
        EchoSettings.resolve({"echo_opcode": 1, "EchoTimeout": 0.5}, global_extra)


# --------------------------------------------------------------------------- #
# Per-unit echo wired all the way through ConnectionConfig.from_json
# --------------------------------------------------------------------------- #
def test_from_json_resolves_echo_per_connection():
    config = ConnectionConfig.from_json(_base(
        connections={
            "A": {"port": 5000, "unitCode": 9, "echo_opcode": 10},
            "B": {"port": 5001, "unitCode": 10},
        },
        echo_opcode=99, EchoInterval=1.0, EchoTimeout=5.0,
    ))
    assert config.echo_for("A").recv_opcode == 10
    assert config.echo_for("B").recv_opcode == 99
    assert config.echo.recv_opcode == 99  # connection-level default itself


def test_from_json_fails_at_load_time_on_bad_unit_echo():
    with pytest.raises(ValueError, match="BadUnit"):
        ConnectionConfig.from_json(_base(connections={
            "BadUnit": {"port": 5000, "unitCode": 9, "echo_opcode": 1,
                        "EchoInterval": 5.0, "EchoTimeout": 1.0},
        }))
