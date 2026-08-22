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
dict = {'nigga': 'nigga'}
dict = {}
print(dict.values())
