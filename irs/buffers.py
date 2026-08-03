# logic/streams.py

class BinaryReader:
    __slots__ = ('_data', '_offset',)

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self._data = memoryview(data)
        self._offset = 0

    @property
    def data(self) -> memoryview:
        return self._data

    def offset(self, size: int) -> int:
        current = self._offset
        self._offset += size
        return current

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
