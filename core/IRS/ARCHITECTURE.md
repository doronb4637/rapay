Markdown
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

**Category B:** Messages, Structures & BitFields (Uninitialized)
If a field is another Structure, Message, or BitField, MessageMeta does NOT initialize it. It appends the raw, uninitialized Class itself to _fields_.

Why? Nested structures and BitFields are already fully-compiled roadmaps (because they went through their own metaclass!).
They possess a @classmethod from_bytes(). If we initialized dummy instances of Header()
just to hold them in the parent class, we would waste CPU cycles and memory.
By storing the raw class, we simply call Header.from_bytes(reader) directly.

## 3. How Arrays Bridge the Gap (ArrayField)
Because ArrayField can hold either Category A (primitives) or Category B (nested structures), it uses Duck Typing to serialize data without caring what it holds.

When ArrayField is initialized, it saves its inner type (e.g., UInt8 or Header) to self.baseType.

Deserialization: It simply calls self.baseType.from_bytes(reader, instance). If baseType is a primitive instance, it calls the instance method.
If it's a raw Structure class, Python routes it to the @classmethod. Both work flawlessly.

Serialization: It looks at the data it was handed and asks: "Do you know how to serialize yourself?"

```Python
def to_bytes(self, writer: BinaryWriter, value: list[Any]) -> None:
    for item in value:
        if hasattr(item, 'to_bytes'):
            item.to_bytes(writer)      # The item is a Structure/BitField!
        else:
            self.baseType.to_bytes(writer, item) # It's a primitive integer!
```
## 4. The Runtime Loop (Maximum Speed)
Because the metaclass did all the hard work, the actual from_bytes loop executed at runtime is brutally simple and fast:
```Python
@classmethod
def from_bytes(cls, reader: BinaryReader, instance: Any = None):
    new_instance = cls()
    for field in cls._fields_:
        # 1. Ask the field/class to parse the bytes
        parsed_value = field.from_bytes(reader, new_instance)
        # 2. Assign it DIRECTLY to memory, bypassing __setattr__ type checks
        object.__setattr__(new_instance, field.name, parsed_value)
    return new_instance
```
Notice the use of object.__setattr__. IRS features strict runtime type-checking (using beartype) to protect developers when they manually edit packets.
But during from_bytes, we know the incoming data is strictly matching the binary schema, so we bypass beartype completely to save microseconds.

## 5. Safe Defaults & Memory Hygiene
To provide a flawless Developer Experience (DX), IRS ensures that no packet ever crashes because it was empty.

If a developer calls packet = UltimatePacket(), the Structure.__init__ loops through _fields_ and calls .get_default() on every item.

Primitive fields inject 0 or b"".

Array fields inject [].

Nested structures call self.__class__() to generate a fresh, empty sub-structure.

This guarantees two things:

Calling packet.to_bytes() on a brand new packet works immediately (serializing safe zeroes).

Memory Hygiene: Because nested structures generate fresh defaults, we completely avoid Python's dreaded "mutable default argument" bug where two separate packets accidentally share the same nested Header object in memory.
