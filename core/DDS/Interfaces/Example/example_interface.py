"""
DDS Interface for ExampleSystem.

GENERATED from the system XML -- do not edit by hand.

This file is the REFERENCE OUTPUT for the system-XML -> Python XSLT. Its shape is
the whole contract `core.connections.dds_config` reads:

  * `INTERFACE_FORMAT` -- the layout version; the loader refuses any it does
    not know.
  * `SYSTEM` -- optional, used in logs.
  * One module-level `DdsUnit` per unit. The variable name IS the unit name (a
    config's `"unit": "SensorUnit"`), and each topic is named after its class.
  * ABSOLUTE imports. The topic classes must be the very objects the
    application builds samples from, however this file itself gets loaded.
"""
from core.DDS import DdsUnit
from core.DDS.idl_types.Example.example_topics import Status, Track

INTERFACE_FORMAT = 1
SYSTEM = "ExampleSystem"

SensorUnit = DdsUnit(unitCode=0x01, publish=(Track,), subscribe=(Status,))
ControlUnit = DdsUnit(unitCode=0x02, publish=(Status,), subscribe=(Track,))
