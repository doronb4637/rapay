import struct
from functools import lru_cache


@lru_cache(maxsize=None)
def get_packer(fmt: str) -> struct.Struct:
    return struct.Struct(fmt)


class BinaryReader:
    __slots__ = ('data', 'offset', '_len')

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self.data = memoryview(data)
        self.offset = 0
        self._len = len(data)


class BinaryWriter:
    __slots__ = ('buffer',)

    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    def __bytes__(self) -> bytes:
        return bytes(self.buffer)
