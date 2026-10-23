"""
`core.tools.validate_interface` -- the offline checks a structures file or DDS
Interface no longer gets at load. Each case writes a file to `tmp_path` and
validates it by path, the way the script is run.
"""
import textwrap
from pathlib import Path

import pytest

from core.tools.validate_interface import main, validate

REPO_ROOT = Path(__file__).resolve().parents[2]


def write(tmp_path: Path, body: str) -> str:
    path = tmp_path / "structures.py"
    path.write_text("from IRS import *\n" + textwrap.dedent(body), encoding="utf-8")
    return str(path)


def problems_in(tmp_path: Path, body: str) -> str:
    return "\n".join(validate(write(tmp_path, body)))


@pytest.mark.parametrize("spec", [
    "core/IRS/Structures/Tiful/tiful_to_dtu.py",
    "core.IRS.Structures.Test.test_messages",
    "core.tests._messages",
])
def test_the_repos_own_structures_files_are_valid(spec):
    assert validate(str(REPO_ROOT / spec) if spec.endswith(".py") else spec) == []


def test_a_valid_file_has_no_problems(tmp_path):
    assert problems_in(tmp_path, """\
        PAIRS = {3: 2}

        @Definition(unitCode=2, opCode=0x10)
        class Reply(Message):
            count: int = Byte
            items: list[int] = [Byte, 'count']
        """) == ""


# --------------------------------------------------------------------------- #
# IRS structures files
# --------------------------------------------------------------------------- #
def test_a_file_that_cannot_import_reports_the_traceback(tmp_path):
    """A lost `= Byte` makes the class itself fail to build."""
    problems = problems_in(tmp_path, """\
        @Definition(unitCode=1, opCode=1)
        class Typo(Message):
            good: int = Byte
            oops: int
        """)
    assert problems.startswith("failed to import") and "class Typo" in problems


@pytest.mark.parametrize("declared, reported", [
    ("'!'", "ENDIAN must be bigEndian"), ("'='", "ENDIAN must be bigEndian"), ("''", "ENDIAN must be bigEndian"),
    # Not a struct prefix at all, so the file already fails to build its first field.
    ("'big'", "failed to import"), ("None", "failed to import"),
])
def test_an_invalid_endian_is_reported(tmp_path, declared, reported):
    problems = problems_in(tmp_path, f"""\
        ENDIAN = {declared}

        @Definition(unitCode=1, opCode=1)
        class Ping(Message):
            value: int = Byte
        """)
    assert reported in problems


def test_a_file_with_no_definition_is_reported(tmp_path):
    assert "defines no @Definition message" in problems_in(tmp_path, "Unit = 1\n")


def test_codes_that_do_not_fit_the_header_are_reported(tmp_path):
    problems = problems_in(tmp_path, """\
        @Definition(unitCode=0x100, opCode=0x10000)
        class Huge(Message):
            value: int = Byte
        """)
    assert "unitCode 256 does not fit the UInt8" in problems
    assert "opCode 65536 does not fit the UInt16" in problems


def test_two_classes_on_one_route_are_reported(tmp_path):
    problems = problems_in(tmp_path, """\
        @Definition(unitCode=1, opCode=0x10)
        class Alpha(Message):
            value: int = Byte

        @Definition(unitCode=1, opCode=0x10)
        class Rival(Message):
            other: int = Byte
        """)
    assert "(unitCode=1, opCode=16) is both Alpha and Rival" in problems


def test_a_count_naming_no_earlier_field_is_reported(tmp_path):
    problems = problems_in(tmp_path, """\
        @Definition(unitCode=1, opCode=1)
        class Counted(Message):
            items: list[int] = [Byte, 'count']
            count: int = Byte
        """)
    assert "Counted.items: length 'count' is not an earlier field" in problems


def test_a_bitfield_wider_than_its_base_type_is_reported(tmp_path):
    problems = problems_in(tmp_path, """\
        @baseType(1)
        class Flags(BitField):
            low: int = 5
            high: int = 4

        @Definition(unitCode=1, opCode=1)
        class Carrier(Message):
            flags: Flags
        """)
    assert "BitField Flags defines 9 bits, but its @baseType only allocates 8" in problems


def test_a_bitfield_without_base_type_is_reported(tmp_path):
    problems = problems_in(tmp_path, """\
        class Flags(BitField):
            low: int = 4

        @Definition(unitCode=1, opCode=1)
        class Ping(Message):
            value: int = Byte
        """)
    assert "BitField Flags has no @baseType" in problems


@pytest.mark.parametrize("pairs", ["[(3, 2)]", "{'MFD': 2}", "{3: 'DTU'}", "{3: True}"])
def test_malformed_pairs_are_reported(tmp_path, pairs):
    problems = problems_in(tmp_path, f"""\
        PAIRS = {pairs}

        @Definition(unitCode=2, opCode=1)
        class Reply(Message):
            value: int = Byte
        """)
    assert "PAIRS must be a dict" in problems


def test_a_pair_that_defines_its_own_messages_is_reported(tmp_path):
    problems = problems_in(tmp_path, """\
        PAIRS = {1: 2}

        @Definition(unitCode=1, opCode=1)
        class Own(Message):
            value: int = Byte

        @Definition(unitCode=2, opCode=1)
        class Reply(Message):
            value: int = Byte
        """)
    assert "unit 1 is paired to 2 but also defines messages of its own" in problems


def test_a_pair_to_a_unit_the_file_does_not_send_is_reported(tmp_path):
    problems = problems_in(tmp_path, """\
        PAIRS = {3: 2}

        @Definition(unitCode=1, opCode=1)
        class Ping(Message):
            value: int = Byte
        """)
    assert "unit 3 is paired to 2, which sends no message" in problems


# --------------------------------------------------------------------------- #
# DDS Interfaces
# --------------------------------------------------------------------------- #
DDS_PREAMBLE = """\
import rti.types as idl
from core.DDS import DdsUnit
from core.DDS.pyIdl.example_topics import Status, Track

@idl.struct
class Plain:
    x: idl.int32 = 0

"""


def dds_problems(tmp_path: Path, body: str) -> str:
    pytest.importorskip("rti.connextdds", reason="RTI Connext Python API not installed")
    path = tmp_path / "dds_interface.py"
    path.write_text(DDS_PREAMBLE + textwrap.dedent(body), encoding="utf-8")
    return "\n".join(validate(str(path)))


def test_the_example_dds_interface_is_valid():
    pytest.importorskip("rti.connextdds", reason="RTI Connext Python API not installed")
    assert validate("core.DDS.Interfaces.example_interface") == []


def test_several_publishers_and_no_source_id_are_reported(tmp_path):
    """Every sample would be unattributable and silently dropped."""
    problems = dds_problems(tmp_path, """\
        A = DdsUnit(unitCode=0x0A, publish=(Plain,))
        B = DdsUnit(unitCode=0x0B, subscribe=(Plain,))
        C = DdsUnit(unitCode=0x0C, publish=(Plain,))
        """)
    assert "unit B subscribes Plain and it has 2 publishers ['A', 'C']" in problems


def test_a_topic_the_unit_also_publishes_needs_a_source_id(tmp_path):
    problems = dds_problems(tmp_path, """\
        A = DdsUnit(unitCode=1, publish=(Plain,), subscribe=(Plain,))
        B = DdsUnit(unitCode=2, publish=(Plain,), subscribe=(Plain,))
        """)
    assert "hears its own writes" in problems


def test_one_publisher_needs_no_source_id(tmp_path):
    assert dds_problems(tmp_path, """\
        A = DdsUnit(unitCode=1, publish=(Plain,))
        B = DdsUnit(unitCode=2, subscribe=(Plain,))
        """) == ""


def test_a_shared_unit_code_is_reported(tmp_path):
    problems = dds_problems(tmp_path, """\
        A = DdsUnit(unitCode=1, publish=(Track,))
        B = DdsUnit(unitCode=1, subscribe=(Track,))
        """)
    assert "units A and B both use unitCode 0x01" in problems


def test_a_shared_wire_type_name_is_reported(tmp_path):
    problems = dds_problems(tmp_path, """\
        @idl.struct(type_annotations=[idl.type_name("Plain")])
        class Other:
            y: idl.int32 = 0

        A = DdsUnit(unitCode=1, publish=(Plain, Other))
        B = DdsUnit(unitCode=2, subscribe=(Plain, Other))
        """)
    assert "share the wire type name 'Plain'" in problems


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def test_main_exits_1_and_names_the_failing_file(tmp_path, capsys):
    bad = write(tmp_path, "Unit = 1\n")
    assert main(["core.tests._messages", bad]) == 1
    out = capsys.readouterr().out
    assert "OK" in out and f"FAIL {bad}" in out


def test_main_exits_0_when_everything_is_valid():
    assert main(["core.tests._messages"]) == 0
