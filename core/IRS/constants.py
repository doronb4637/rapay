# logic/constants.py
""" Types Formats"""
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
