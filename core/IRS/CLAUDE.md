# CLAUDE.md -- core/IRS

High-performance binary parsing/serialization engine. Moves all reflection and schema resolution to import-time metaclasses and leans on the built-in `struct` module. Import-boundary and two-spelling rules are in the root `CLAUDE.md`.

## Core Commands

- Tests: `.venv/Scripts/python.exe -m pytest core/tests/test_irs_fill.py core/tests/test_endianness.py core/tests/test_irs_scope.py core/tests/test_interface.py`, or the full suite from the repo root.
- Before committing an import change, run the IRS boundary greps from the root `CLAUDE.md`.

## Architecture & Tech Stack

- `buffers`: `BinaryReader`/`BinaryWriter` wrap `memoryview`/`bytearray` and keep the cursor in `_offset`, with no slicing.
- `fields`: `BaseField`, `Field`, `EnumField` use pre-compiled `struct.Struct` packers. `get_default()` and `fill()` supply safe defaults at any depth; `EnumField` unpacks to `IntEnum`.
- `bitfields`: `BitField`/`BitFieldMeta`, driven by `@baseType(size, endian)`. Annotations become bitwise properties over one internal `_value`; no C-struct bitfields.
- `core`: `Structure`, `Message`, `ArrayField`. `MessageMeta` turns annotations into the `_fields_` tuple. Arrays are fixed (int length), counted (length names a sibling field) or greedy (`None`).
- `Structure.__init__(**kwargs)` applies kwargs and fills the rest with defaults, so `to_bytes()` never fails on a fresh object.
- No registry. `@Definition(unitCode=0x02, opCode=0x12)` binds a message to its route; the unit code is the number itself, never a named constant (unit names live only in the connection config). `irs_parser.Interface` reads one structures module's layouts and pairs. Its owner (a connection) holds it per link; see `core/connections/CLAUDE.md`.
- A structures file's module-level `PAIRS = {0x03: 0x02}` makes 0x03 a pair that speaks 0x02's IRS and has no layouts of its own in that file. `Interface.from_module` turns it into plain routes `(0x03, op)` for every 0x02 layout in the *same file*, so a pair never reaches another file merged onto the link (that file may give 0x03 layouts of its own).
- Byte order is a property of a whole specification, declared once per structures file as a module-level `ENDIAN` constant. No declaration means little endian.
- `@baseType(n)` inherits the file's `ENDIAN`; a second argument overrides it for one type. A type keeps the byte order of the file that *defined* it.
- Endianness resolves in `IRS.constants.module_endian()` at class-creation time.

## Code Style & Constraints

- Zero runtime reflection: no `getattr`, `hasattr` or `type()` inside `from_bytes`/`to_bytes`. Resolve schemas in the metaclasses (`MessageMeta`, `BitFieldMeta`) at import time.
- All classes use `__slots__ = ()`; never allocate `__dict__` for parsed packets.
- `Structure.__setattr__` enforces types with `beartype.door.is_bearable` only on manual assignment. `from_bytes` bypasses it via `object.__setattr__` for speed.
- Never validate structures-file content in IRS (bit overflows, `ENDIAN`, `PAIRS`, duplicate routes); those checks live in `core/tools/validate_interface.py`, run offline.
- Spell imports `IRS.x` in structures files; do not use `core.IRS.x` there.
- Read `core/IRS/_alias.py`'s module docstring before changing how IRS is imported.

## Reference Codebases

- Message, enum and bitfield definition syntax: `core/tests/_messages.py` and `core/IRS/Structures/Test/test_messages.py`.
- Big-endian file declaration: `core/tests/_messages_big_endian.py`.
- Parse/serialize/assign usage: `core/tests/test_irs_fill.py`.
- Design background: `core/IRS/ARCHITECTURE.md`.
