# from core.IRS.Structures.Tiful.tiful_to_dtu import *
# from core.connections.manager import ConnectionManager
# mng = ConnectionManager()
# tiful_config = {
#         "protocol": "tcp",
#         "side": "server",
#         "ip": "0.0.0.0",
#         "local_ip": "127.0.0.1",
#         "unitCode": 1,
#         "connections": {
#           "DTU": {
#             "port": 2000,
#             "unitCode": 2,
#             "echo_opcode": 6,
#             "EchoInterval": 1,
#             "EchoTimeout": 5
#           },
#         }
#       }
# DTU_config = {
#         "protocol": "tcp",
#         "side": "client",
#         "ip": "127.0.0.1",
#         "local_ip": "127.0.0.1",
#         "unitCode": 2,
#         "connections": {
#           "Tiful": {
#             "port": 2000,
#             "unitCode": 1,
#             "echo_opcode": 6,
#             "EchoInterval": 1,
#             "EchoTimeout": 5
#           }
#         }
#       }
#
#
# def send():
#     DTU.send_message(setg, 18)
#
#
# Tiful = mng.create('Tiful', tiful_config)
# Tiful.start()
# DTU = mng.create('DTU', DTU_config)
# DTU.start()
# setg = SetGeneralFlag().fill()
# DTU.send_message(setg, 18)
# sender, data = Tiful.receive_message(18, timeout=5, trigger_function=send)
# print(data)
from core.DDS.idl_types.Example.example_topics import Status, Track   # ABSOLUTE import

INTERFACE_FORMAT = 1           # loader rejects unknown versions
SYSTEM = "ExampleSystem"       # optional, used in logs


class DdsUnit:
    def __init__(self, unitCode: int, publish: tuple, subscribe: tuple) -> None:
        self.unitCode = unitCode
        self.publish = publish
        self.subscribe = subscribe

SensorUnit = DdsUnit(unitCode=0x01,
    publish=(Track),
    subscribe=(Status)
)
ControlUnit = DdsUnit(unitCode=0x02,
    publish=(Status),
    subscribe=(Track)
)
# TOPICS = (                     # optional per topic: "type_name": "Mod::Track" (wire type name)
#     {"name": "TrackTopic",   "type": Track},
#     {"name": "StatusTopic",  "type": Status},
#     {"name": "CommandTopic", "type": Command},
# )
# UNITS = (                      # "code" = the unit's uint8 wire identity (header source/destination)
#     {"name": "SensorUnit",  "code": 22,
#      "publish": ("TrackTopic", "StatusTopic"), "subscribe": ("CommandTopic",)},
#     {"name": "ControlUnit", "code": 7,
#      "publish": ("CommandTopic",), "subscribe": ("TrackTopic", "StatusTopic")},
# )
