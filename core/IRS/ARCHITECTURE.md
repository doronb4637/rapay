# IRS: Engine Architecture & Philosophy

Welcome to the internal documentation for the **IRS** binary parsing engine. 

If you are reading this, you are likely looking to understand *how* IRS parses binary data so quickly,
and *why* the codebase is structured the way it is. 
This document breaks down the core philosophy, the metaclass architecture, and the memory-management tricks that make IRS incredibly fast.

---

## 1. The Core Philosophy: "Zero Runtime Reflection"

Most Python binary parsers (like `construct`) are slow because they figure out the schema at **runtime**.
Every time a packet arrives, they loop through the packet definition, check the types,
allocate memory, and dynamically figure out how to unpack the bytes. This "Python Object Tax" destroys performance.

**The IRS Solution:** We move 100% of the reflection, type-checking, and schema resolution to **Import-Time**. 

When a developer writes a packet definition, a Python metaclass intercepts the class creation, pre-compiles the `struct.Struct` packers,
and builds a hardcoded roadmap (`_fields_`) for the parser. By the time a packet arrives over the network,
the engine does zero thinking—it just blindly loops through the pre-compiled roadmap and unpacks the memory.

---

## 2. The Engine Heart: `MessageMeta`

The entire architecture revolves around the `MessageMeta` metaclass. When you define a class like this:

```python
class Header(Structure):
    packet_id: UInt32
    flags: HardwareFlags
```
The metaclass intercepts this before the code even runs. It reads the __annotations__ dictionary,
processes the types, and generates a static tuple called _fields_.

**The Golden Rule:** Instances vs. Raw Classes
During this metaclass processing, IRS makes a highly specific optimization regarding what gets initialized.
We divide fields into two categories:

**Category A:** Primitive Fields (Initialized)
If a field is a base type (Field, EnumField, ArrayField), MessageMeta initializes an instance of it and appends it to _fields_.

Why? A primitive Field(UInt32) needs to create and hold a pre-compiled struct.Struct("<I") in memory.
An EnumField needs to store a reference to the specific Python IntEnum it resolves to.
They require state, so they must be instantiated.

**Category B:** Structures & BitFields (a single shared PROTOTYPE instance)
If a field is another Structure, Message, or BitField, MessageMeta stores one
instance of it -- `field = field_type()` -- created once, at class-creation
time, and reused for every packet. It is never the object a parsed packet holds;
it is a stateless handle that carries the field's `_name` and routes calls.

Why an instance and not the bare class? Two reasons, both load-bearing:

* A field entry has to carry its own `_name`, and a bare class cannot without
  being mutated globally.
* `_fields_` is a public introspection surface. `gsim/core_gateway/schema.py`
  and `payloads.py` dispatch on `isinstance(field, Structure)` /
  `isinstance(field, BitField)`, which only works on instances.

Nothing is wasted by it: `from_bytes` is a `@classmethod`, so calling it on the
prototype still routes to the class, and the prototype holds no per-packet
state.

## 3. How Arrays Bridge the Gap (ArrayField)
Because ArrayField can hold either Category A (primitives) or Category B (nested structures), it uses Duck Typing to serialize data without caring what it holds.

When ArrayField is initialized, it saves its inner type (e.g., UInt8 or Header) to self.baseType.

Deserialization: It simply calls self.baseType.from_bytes(reader, instance). If baseType is a primitive instance, it calls the instance method.
If it's a raw Structure class, Python routes it to the @classmethod. Both work flawlessly.

Serialization: It looks at the data it was handed and asks: "Do you know how to serialize yourself?"

```Python
def to_bytes(self, writer: BinaryWriter, value: list[Any]) -> None:
    if isinstance(self.length, int) and len(value) != self.length:
        raise ValueError(...)          # a fixed array's LENGTH is part of the layout
    write = self.baseType.to_bytes     # bound once, outside the loop
    for item in value:
        write(writer, item)            # primitive or Structure -- same call
```
The length check is why a fixed-length array is handed back as a `FixedList`
(see `containers.py`): the violation is caught at the `append` that caused it
rather than at the `to_bytes` that finally noticed.
## 4. The Reference Loop
Because the metaclass did all the hard work, the `_fields_` loop is brutally
simple. This is no longer the code that usually RUNS -- section 6 covers what
replaces it -- but it is still the definition of what a parse means, and the
compiler is tested against it:
```Python
@classmethod
def from_bytes(cls, reader: BinaryReader, instance: Any = None):
    new_instance = cls()
    for field in cls._fields_:
        # 1. Ask the field/class to parse the bytes
        parsed_value = field.from_bytes(reader, new_instance)
        # 2. Assign it DIRECTLY to memory, bypassing __setattr__ type checks
        object.__setattr__(new_instance, field._name, parsed_value)
    return new_instance
```
Notice the use of object.__setattr__. IRS features strict runtime type-checking (using beartype) to protect developers when they manually edit packets.
But during from_bytes, we know the incoming data is strictly matching the binary schema, so we bypass beartype completely to save microseconds.

## 5. Safe Defaults & Memory Hygiene
To provide a flawless Developer Experience (DX), IRS ensures that no packet ever crashes because it was empty.

`UltimatePacket(**kwargs)` applies just those kwargs; `.fill()` then walks
`_fields_` and completes every field left unset -- it never overwrites one that
is already there.

Primitive fields inject 0 or 0.0.

Array fields inject [].

Nested structures call self.__class__() to generate a fresh, empty sub-structure.

This guarantees two things:

Calling packet.to_bytes() on a brand new packet works immediately (serializing safe zeroes).

Memory Hygiene: Because nested structures generate fresh defaults, we completely avoid Python's dreaded "mutable default argument" bug where two separate packets accidentally share the same nested Header object in memory.

---

## 6. The Compiler: resolving the PARSE at first use

Sections 1-5 describe a schema resolved at import time and then *interpreted*
per packet. Profiling said what that costs: parsing a 92-byte message made 62
calls to `Field.from_bytes`, and the actual `Struct.unpack_from` work was ~10%
of the runtime. The other 90% was Python call frames and `reader.offset`
attribute traffic -- overhead that exists only because the loop does not know,
at the moment it runs, what it is about to parse.

`_compiler.py` removes it. On the first `from_bytes`/`to_bytes` of a class it
generates source specific to that ONE layout, `exec`s it, and installs it over
the loop above. Runs of adjacent fixed fields collapse into a single
`struct.Struct` call -- and a nested `Structure` or a fixed array is FLATTENED
into its parent's run rather than getting a call of its own:

```
class BenchMessage(Message):        ONE unpack_from covering all of these:
    id: int = UInt32                  <IQBBfffHHHHHHHHH>
    timestamp: int = UInt64           16 values, 44 bytes, every offset a
    kind: E_Kind                      compile-time literal
    status: StatusFlags
    pos: Position                   <- nested struct, flattened in
    samples: list[int] = [UInt16, 8]<- fixed array, flattened in
    count: int = UInt16
    payload: [Byte, "count"]          list(data[a:b]) -- count is a local
    tail: [Byte, None]                list(data[b:end])
```

Measured on CPython 3.11 (`bench_message_parsing.py --compare`):

| | interpreted | compiled | |
|---|---|---|---|
| `BenchMessage.from_bytes` | 13.5us | 1.8us | 7.5x |
| `BenchMessage.to_bytes` | 9.1us | 0.9us | 10.5x |
| `NestedMessage.from_bytes` (arrays of structs) | 26.3us | 8.1us | 3.3x |
| `NestedMessage.to_bytes` | 16.6us | 2.1us | 7.8x |

The nested case gains least, and that is the honest floor rather than a missing
optimization: what remains there is allocating one Python object per element
(18 allocations alone account for 1.7us of it). Three different loop shapes --
one big unpack with indexing, `iter_unpack` with tuple unpacking, and a
list-comprehension -- measured within 3% of each other.

**Why it stays lazy.** Generating a class costs ~100us. A structures file with
hundreds of layouts would pay all of it at import, mostly for messages never
exchanged; instead each class pays on its first packet. `IRS.compile_all()`
does it up front when the first packet must not be the slow one.

**Why the interpreted path is still here.** It is the reference semantics.
`IRS_COMPILE=0` selects it process-wide, and `core/tests/test_irs_compiler.py`
runs both in separate processes and demands identical results -- same parsed
values, byte-identical output, agreeing rejections -- across every layout in the
repo. A layout the generator cannot express raises `Uncompilable` and quietly
keeps the loop. `IRS.dump_source(cls)` prints what was generated.
