from dataclasses import field
from enum import IntEnum
from typing import Sequence

import rti.types as idl

@idl.struct
class SourceId:
    """
    The sender identity every topic carries, always as a member named
    `A_sourceID`.

    `connections/dds.py` reads `A_systemId` -- the unit code -- off inbound
    samples to work out which unit sent them (a DataReader serves every
    publisher on its topic at once, so the sample itself is the only thing that
    can say), and stamps it on outbound ones.
    """
    A_platformId: idl.int32 = 0
    A_systemId: idl.int16 = 0
    A_moduleId: idl.int16 = 0
