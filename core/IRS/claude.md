# IRS: High-Performance Binary Parsing Engine

## 📖 Project Overview
`IRS` is a blazing-fast, strictly-typed, and highly memory-efficient binary parsing and serialization engine for Python. It is designed to replace slow declarative parsers (like `construct` or `ctypes`) by moving all reflection and schema resolution to **import-time metaclasses**.

The engine is written in pure Python, maximizing the native C-level speed of the built-in `struct` module while maintaining flawless IDE type-hinting support and memory safety.

---

## 🏗️ Core Philosophy & AI Directives
When contributing to this codebase, you **MUST** adhere to the following rules:

1. **Zero Runtime Reflection:** Do not use `getattr`, `hasattr`, or `type()` inside the inner parsing loops (`from_bytes`, `to_bytes`). All schema resolution happens strictly inside the metaclasses (`MessageMeta`, `BitFieldMeta`) at import time. Since the compiler (`_compiler.py`) landed this is stronger than a convention: those loops are the FALLBACK path, and the code that actually runs is generated per message class. See "The Compiler" below before touching either.
2. **Memory Efficiency:** All classes must use `__slots__ = ()`. Do not allocate Python dictionaries (`__dict__`) for parsed packets.
3. **Targeted Strict Typing:** `Structure` overrides `__setattr__` to enforce strict type checking at runtime using `beartype.door.is_bearable` **only when a developer manually sets or modifies an attribute**. During binary parsing (`from_bytes`), this check is explicitly bypassed using `object.__setattr__` to guarantee maximum deserialization speed.
4. **Fail-Fast Safety:** Validate bounds and bit overflows immediately during class creation (e.g., verifying `@baseType` fits the defined bits so the engine never fails silently at runtime).

---

## 🗂️ Architecture Breakdown

### 1. `buffers` (`BinaryReader`, `BinaryWriter`)
Holds the cursor (`offset`) over a payload without ever slicing it. `bytes` and
`bytearray` are kept as-is rather than wrapped in a `memoryview`: measured on
3.11 the wrap costs more than it saves at every operation the engine performs
(`unpack_from` 197ns vs 205ns, `list(data[a:b])` 206ns vs 289ns, plus 118ns to
build the view). Anything else is still wrapped, so slicing stays uniform.

### 2. `fields` (`BaseField`, `Field`, `EnumField`)
The base serializers.
* Uses pre-compiled `struct.Struct` packers.
* Contains `fill()` to auto-initialize empty packets safely (e.g., floats -> `0.0`, ints -> `0`).
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
* A **fixed** array comes back as a `FixedList` (`containers.py`) -- a real `list`
  (so `isinstance`, `beartype`, and `to_dict` are all unaffected) whose LENGTH is
  frozen. `arr[0] = 5` is an edit and is allowed; `arr.append(...)` is a layout
  violation and raises `TypeError` at the mutation instead of silently at
  `to_bytes`. Counted and greedy arrays are meant to change length, so they stay
  plain lists.
* **Auto-Initialization:** `Structure.__init__(**kwargs)` applies `kwargs`; `.fill()` completes the rest with safe defaults, so `.to_bytes()` never crashes on a newly instantiated, empty object.
* `Structure.to_bytes()` and `Message.to_bytes()` mean the same thing at every depth: pass a writer to append to it, pass nothing to get the bytes back.

### 5. `_compiler` -- The Compiler
The metaclass resolves the SCHEMA at import time; the compiler resolves the
PARSE. On the first `from_bytes`/`to_bytes` of a class it generates Python
source specific to that layout, `exec`s it, and installs it over the
`_fields_` loops. Adjacent fixed fields -- including nested `Structure`s and
fixed arrays, which are FLATTENED into their parent -- collapse into a single
`struct.Struct` call with every offset a compile-time literal.

Measured on 3.11 (`--compare`): `from_bytes` 13.5us -> 1.8us (7.5x),
`to_bytes` 9.1us -> 0.9us (10.5x). A message dominated by arrays of structures
gains less (3.3x) because what remains is Python object allocation, not parsing.

**Rules for working on it:**
* The `_fields_` loops in `core.py`/`fields.py`/`bitfields.py` are the REFERENCE
  SEMANTICS and must keep working. `IRS_COMPILE=0` selects them process-wide;
  `core/tests/test_irs_compiler.py` runs both in subprocesses and demands the
  same answer for every layout in the repo over adversarial buffers. Change a
  parsing rule in one path and you must change it in the other.
* `_fields_` is a public introspection surface (`gsim/core_gateway/schema.py`,
  `payloads.py`). The compiler only reads it -- never reshape it for the
  compiler's convenience.
* Block coalescing must never merge across byte orders; a `Structure` defined in
  a `ENDIAN = bigEndian` file stays big-endian inside a little-endian parent.
* A layout the generator cannot express raises `Uncompilable` and silently keeps
  the interpreted path. That is a safety valve, not a place to hide bugs --
  `compile_all()` returns how many succeeded.
* `IRS.dump_source(MessageClass)` prints the generated code. Read it first when
  a layout misbehaves.

---

## 💻 Usage & Syntax Conventions

This is how packets are defined using `IRS`. All AI-generated code should match this exact syntax.

> **Always spell the import `IRS.x`, never `core.IRS.x`.** `IRS` physically lives at
> `core/IRS/`, so its real module name is `core.IRS` -- but `core/IRS/_alias.py` makes the two
> spellings resolve to *one* module object at every depth, and `IRS.x` is the only spelling that
> also survives copying `IRS/` out on its own. Both are enforced, not conventions:
>
> * A structures file spelling `core.IRS.x` would break the moment IRS is used standalone.
> * Without the alias, the two spellings are two independent module objects -- two
>   `STRUCTURE_REGISTRY` dicts and two sets of field classes. That fails *silently*: the file
>   prints a fully populated registry while `irs_parser` reads an empty one, and every
>   `isinstance(field, Field)` downstream misses. `_alias.py`'s module docstring has the full
>   account; read it before changing anything about how IRS is imported.

### Endianness
Byte order is a property of a **whole specification**, not of a field: one structures file describes one link, and that link does not mix byte orders. So it is declared once, at the top of the file, as a module-level constant:

```python
from IRS import *

ENDIAN = bigEndian   # `bigEndian`/`big_endian`/`littleEndian`/`little_endian` come from IRS.constants
```

* **A file that declares nothing is little endian.** Every structures file written before this existed keeps its exact byte layout.
* **`@baseType(n)` inherits the declaration** -- a big-endian file says it once, not on every enum and bitfield. `@baseType(n, littleEndian)` overrides it for one type.
* **A type carries the byte order of the file that DEFINED it.** A `Structure`, `BitField`, or `IntEnum` imported from a big-endian file stays big-endian inside a little-endian message. This is what makes "one endian per specification" coherent rather than ambiguous.

Resolution happens in `IRS.constants.module_endian()`, called once per class from `MessageMeta.__new__` and `@baseType` -- at class-creation time, never inside `from_bytes`/`to_bytes`. An `ENDIAN` that is neither `'<'` nor `'>'` raises at import.

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

@baseType(2, littleEndian) # 2 bytes, explicitly Little-Endian. Omit the second
                           # argument to inherit the file's `ENDIAN` declaration
                           # (which is itself Little-Endian when absent).
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
