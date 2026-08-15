# IRS: High-Performance Binary Parsing Engine

## 📖 Project Overview
`IRS` is a blazing-fast, strictly-typed, and highly memory-efficient binary parsing and serialization engine for Python. It is designed to replace slow declarative parsers (like `construct` or `ctypes`) by moving all reflection and schema resolution to **import-time metaclasses**.

The engine is written in pure Python, maximizing the native C-level speed of the built-in `struct` module while maintaining flawless IDE type-hinting support and memory safety.

---

## 🏗️ Core Philosophy & AI Directives
When contributing to this codebase, you **MUST** adhere to the following rules:

1. **Zero Runtime Reflection:** Do not use `getattr`, `hasattr`, or `type()` inside the inner parsing loops (`from_bytes`, `to_bytes`). All schema resolution happens strictly inside the metaclasses (`MessageMeta`, `BitFieldMeta`) at import time.
2. **Memory Efficiency:** All classes must use `__slots__ = ()`. Do not allocate Python dictionaries (`__dict__`) for parsed packets.
3. **Targeted Strict Typing:** `Structure` overrides `__setattr__` to enforce strict type checking at runtime using `beartype.door.is_bearable` **only when a developer manually sets or modifies an attribute**. During binary parsing (`from_bytes`), this check is explicitly bypassed using `object.__setattr__` to guarantee maximum deserialization speed.
4. **Fail-Fast Safety:** Validate bounds and bit overflows immediately during class creation (e.g., verifying `@baseType` fits the defined bits so the engine never fails silently at runtime).

---

## 🗂️ Architecture Breakdown

### 1. `buffers` (`BinaryReader`, `BinaryWriter`)
Wraps `memoryview` and `bytearray` to maintain the memory cursor state (`_offset`) efficiently without slicing byte arrays.

### 2. `fields` (`BaseField`, `Field`, `EnumField`)
The base serializers. 
* Uses pre-compiled `struct.Struct` packers.
* Contains `get_default()` to auto-initialize empty packets safely (e.g., floats -> `0.0`, ints -> `0`).
* `EnumField` automatically unpacks bytes into Python `IntEnum` objects.

### 3. `bitfields` (`BitField`, `BitFieldMeta`)
Handles sub-byte bitwise logic.
* Driven by the `@baseType(size, endian="<")` decorator.
* `BitFieldMeta` reads type annotations (e.g., `is_active: int = 1`) and auto-generates bitwise getter/setter `property` objects.
* Backed by a single internal `_value` integer. No C-struct bitfields are used; we rely purely on fast Python bitwise math (`<<`, `>>`, `&`).

### 4. `core` (`Structure`, `Message`, `ArrayField`)
The heavy lifters.
* `MessageMeta` converts standard Python class annotations into a tuple of initialized `BaseField` objects (`_fields_`).
* `ArrayField` handles fixed arrays (`length=5`), dynamic arrays (`length="count_field"`), and greedy arrays (`length=None`).
* **Auto-Initialization:** `Structure.__init__(**kwargs)` intercepts initialization to apply `kwargs` or fill the class with `get_default()` values, ensuring `.to_bytes()` never crashes on a newly instantiated, empty object.

---

## 💻 Usage & Syntax Conventions

This is how packets are defined using `IRS`. All AI-generated code should match this exact syntax.

### Defining BitFields
```python
from IRS.bitfields import BitField, baseType

@baseType(1) # Defines exactly 1 byte (8 bits). Strict validation will throw if bits != 8.
class HardwareFlags(BitField):
    power_mode: int = 2
    has_fault: int = 1
    reserved: int = 5
```
### Defining Enums
```python
from enum import IntEnum
from IRS.bitfields import baseType

@baseType(2, '<') # 2 bytes, Little-Endian, will default to Big-Endian
class PacketType(IntEnum):
    HANDSHAKE = 0x01
    DATA = 0x02
```
### Defining Messages
```python
from IRS.core import Message
from IRS.constants import UInt32, UInt16, UInt8

class UltimatePacket(Message):
    packet_id: UInt32
    ptype: PacketType
    flags: HardwareFlags
    
    # Fixed Array: 4 UInt16s
    sensors: [UInt16, 4] 
    
    # Dynamic Array: Length bound to 'packet_id'
    dynamic_data: [UInt8, "packet_id"] 
    
    # Greedy Array: Reads until EOF
    payload: [UInt8, None]
```
### Interacting with Messages
```python
# 1. Parse from bytes (Bypasses beartype for maximum speed)
packet = UltimatePacket.from_bytes(BinaryReader(raw_data))

# 2. Strict Type Safety (Triggers beartype in __setattr__, raises TypeError if assigned a string)
packet.packet_id = 42 

# 3. Serialize back to bytes
raw_out = packet.to_bytes()

# 4. Safe Empty Initialization (Auto-fills with defaults)
empty_packet = UltimatePacket(packet_id=10)
```
