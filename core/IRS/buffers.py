import struct
from functools import lru_cache


@lru_cache(maxsize=None)
def get_packer(fmt: str) -> struct.Struct:
    return struct.Struct(fmt)


class BinaryReader:
    __slots__ = ('data', '_offset',)

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self.data = memoryview(data)
        self._offset = 0

    def offset(self, size: int) -> int:
        current = self._offset
        self._offset += size
        return current

    def remaining(self) -> int:
        """Bytes left to read. Lets a greedy array size itself in one step
        instead of probing `is_empty()` once per element."""
        return len(self.data) - self._offset

    def is_empty(self) -> bool:
        return self._offset >= len(self.data)


class BinaryWriter:
    __slots__ = ('buffer',)

    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    def get_bytes(self) -> bytes:
        return bytes(self.buffer)
