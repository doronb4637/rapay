import struct
from functools import lru_cache


@lru_cache(maxsize=None)
def get_packer(fmt: str) -> struct.Struct:
    return struct.Struct(fmt)


class BinaryReader:
    """A cursor over a payload. `data` is whatever supports the buffer protocol.

    `bytes`/`bytearray` are kept AS THEY ARE rather than wrapped in a
    `memoryview`. The wrap used to be unconditional, and it cost more than it
    saved at every operation the engine actually performs on `data` -- measured
    on CPython 3.11: `Struct.unpack_from` 197ns on bytes vs 205ns on a
    memoryview, `list(data[a:b])` 206ns vs 289ns, plus 118ns to build the view
    itself. That is ~0.24us of pure construction on a parse whose whole budget
    is ~1.6us. Anything else (an `array`, another memoryview, an mmap) is still
    wrapped, so slicing keeps working uniformly.
    """
    __slots__ = ('data', 'offset', '_len')

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self.data = data if type(data) in (bytes, bytearray, memoryview) else memoryview(data)
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
