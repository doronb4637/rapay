# logic/constants.py
""" Types Formats"""
import sys as _sys

# Endianness
bigEndian = big_endian = '>'
littleEndian = little_endian = '<'
# Bytes
Int8 = SByte = 'b'
UInt8 = Byte = 'B'
# Shorts
Int16 = Short = 'h'
UInt16 = UShort ='H'
# Ints
Int32 = Int = 'i'
UInt32 = UInt = 'I'
# Longs
Int64 = Long = 'q'
UInt64 = ULong = 'Q'
# Floating Point Types
Float32 = Float = Single = 'f'
Float64 = Double = 'd'

Floats = [Single, Double]
Ints = [Int8, Int16, Int32, Int64, Int64]
UInts = [UInt8, UInt16, UInt32, UInt64, UInt64]

""" Endianness Resolution """
ENDIAN_ATTRIBUTE = 'ENDIAN'


def module_endian(module_name: str | None) -> str:
    """Return the byte order declared by `module_name`, little endian if it declares none.

    Resolved once per class, at class-creation/decoration time
    Args:
        module_name (str | None): `__module__` of the class being built.

    Returns:
        str: `littleEndian` ('<') or `bigEndian` ('>'), ready to prefix a struct format.

    Raises:
        ValueError: If the module declares an `ENDIAN` that is neither.
    """
    module = _sys.modules.get(module_name) if module_name else None
    endian = getattr(module, ENDIAN_ATTRIBUTE, little_endian)
    if endian not in (little_endian, big_endian):
        raise ValueError(f"{module_name}.{ENDIAN_ATTRIBUTE} must be bigEndian ('>') or "
                         f"littleEndian ('<'), got {endian!r}.")
    return endian
