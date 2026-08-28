# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`core/DDS` holds hand-written DDS type definitions used by `core.connections.dds.DdsConnection`. It
is the topic-based counterpart to `core/IRS`, and is deliberately much smaller: no engine, no parser,
no registry -- just `Structures/`, a package of plain Python modules defining `@idl.struct` classes.

Standalone in the repo's dependency graph (see root `CLAUDE.md`): `core/DDS` imports nothing else in
this repo. `core/DDS/__init__.py` itself must never import `rti`, so `core.DDS` stays importable in a
process without Connext installed -- only the individual `Structures/` modules need it, and only once
someone actually imports one.

## Why there is no registry

If you've written an `IRS/Structures` file before, the instinct here will be to look for
`register_message` or something like it. There isn't one, and that's not an oversight.

IRS needs a registry because a binary payload carries no type information of its own: something has
to look up a layout by `(unitCode, opCode)` before the bytes can even be parsed, or parsing is simply
impossible. DDS is the opposite -- the type travels with the sample on the wire, and RTI Connext
matches publishers to subscribers on `(topic name, type name, QoS compatibility)` itself, during
discovery. Nothing in this repo needs to know that mapping in advance.

So a file under `Structures/` is exactly what an RTI Connext Python developer writes on any project,
with nothing of this repo's own layered on top:

```python
import rti.types as idl
from dataclasses import field

@idl.struct
class Header:
    source_unit: idl.uint8 = 0
    destination_unit: idl.uint8 = 0

@idl.struct
class Track:
    header: Header = field(default_factory=Header)
    x: float = 0.0
```

`@idl.struct` builds the TypeSupport Connext needs to serialize the class and publish its definition
during discovery. Importing the module is the entire job -- there is nothing else to call.

## How a module gets used

A `DdsConnection` config names the module in `config['idl_modules']` -- either a dotted path
(`core.DDS.Structures.Example.example_topics`) or an absolute file path for a type kept outside the
repo -- and `DdsConnection` imports it in an executor (`connections/dds.py`,
`_load_type_modules`/`_import_all`). Each entry in `config['topics']` then names one class from that
module by its `type` key. See `core/DDS/Structures/Example/example_topics.py` for a worked example to
copy from, and `core/connections/CLAUDE.md` §9 for the mechanics of how `DdsConnection` turns a topic
list into DataWriters/DataReaders, derives its local routing opcode, and resolves QoS -- this file is
about what lives in `core/DDS` and how to write a `Structures` module, not how the connection uses it.

## Two Python gotchas

- **Nested struct members need `field(default_factory=...)`, not a bare instance.** `@idl.struct`
  builds a dataclass under the hood, so `header: Header = Header()` raises `ValueError: mutable
  default ... is not allowed` at import time. Always `header: Header = field(default_factory=Header)`.
- **Include a `Header` struct with `source_unit`/`destination_unit` fields** (or whatever
  `config['header']` on the connection is set to name instead). A DDS DataReader serves every
  publisher on its topic at once and has no per-peer transport to infer a sender from, so
  `DdsConnection` reads `source_unit` off the sample itself to work out which configured unit sent
  it, and stamps both fields on the way out. A type with no such field only works when the connection
  has exactly one configured unit.

## Two ways this fails silently

Both produce the same symptom: discovery succeeds, the entities show up in RTI Admin Console, and no
sample ever arrives -- with no error anywhere.

- **Type name mismatch.** `@idl.struct` names the DDS type after the Python class by default. A peer
  whose type came from real IDL may be advertising a different name (`MyModule::Track`); set
  `type_name` on the topic's config entry when that's the case.
- **Extensibility mismatch.** `idl.final` / `idl.extensible` / `idl.mutable` (passed via
  `@idl.struct(type_annotations=[...])`) must agree with what the peer's IDL declares.

`rtiddsspy -domainId <N>` is the fastest way to check both: it shows the type name and extensibility
the peer is actually advertising on the wire.
