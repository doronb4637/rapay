import time

from core.connections.manager import ConnectionManager
mng = ConnectionManager()
config = {
    "protocol": "dds",
    "unit": "SensorUnit",
    "dds_interface": "core.DDS.Interfaces.example_interface",
}
sensor = mng.create("sensor", config)

from core.DDS.Interfaces.example_interface import *

print(Track.type_support.type_name)
