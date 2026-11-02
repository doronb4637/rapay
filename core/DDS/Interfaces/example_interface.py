"""
DDS Interface for ExampleSystem.

GENERATED from the system XML -- do not edit by hand.

This file is the REFERENCE OUTPUT for the system-XML -> Python XSLT. Its shape is
the whole contract `core.connections.dds_config` reads:

  * `SYSTEM` -- optional, used in logs.
  * One module-level `DdsUnit` per unit. The variable name IS the unit name (a
    config's `"unit": "SensorUnit"`), and each topic is named after its type.
  * Its generated topics imported RELATIVELY (`..pyIdl`): the generator always
    puts `pyIdl/` beside the Interface's folder, so a regenerated pair drops in
    unchanged wherever it lives. See the root CLAUDE.md for how to name it in a
    config so the application's topic classes are the same objects.
  * `register_idl_folder` called with that same relative name BEFORE the topic
    imports, so rtiddsgen's bare imports between its files resolve
    (`core/DDS/_idl_alias.py`).
"""
from core.DDS import DdsUnit, register_idl_folder
register_idl_folder("..pyIdl", __package__)
from ..pyIdl.example_topics import SensorReport, Status, Track  # noqa: E402

SYSTEM = "ExampleSystem"

SensorUnit = DdsUnit(unitCode=0x01, publish=(Track, SensorReport), subscribe=(Status,))
ControlUnit = DdsUnit(unitCode=0x02, publish=(Status,), subscribe=(Track, SensorReport))
