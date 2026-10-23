# CLAUDE.md -- core/DDS

What a DDS system is *described* in, as opposed to how a connection runs it. Dependency rules are in the root `CLAUDE.md`.

## Core Commands

- Tests: `.venv/Scripts/python.exe -m pytest core/tests/test_dds.py`. Most tests need only `rti.connextdds`; the one that creates real participants is gated by `requires_license`.
- Peer inspection: `rtiddsspy -domainId <N>` shows the type name and extensibility a peer actually advertises.

## Architecture & Tech Stack

- `pyIdl/`: plain modules of `@idl.struct` topic types (RTI Connext Python API).
- `interface.py`: `DdsUnit`, the vocabulary a generated **DDS Interface** is written in; re-exported by `__init__.py`.
- `Interfaces/`: reference Interface -- the exact shape the user's system-XML -> Python XSLT must produce.
- `Configuration/`: `UNIVERSAL_QOS.xml` and the security files it references. Data, not code. The RTI license is `core/rti_license.dat`.
- An Interface names every unit, its wire unit code, and the topic classes it publishes and subscribes. A connection config names only which unit it is.
- A unit's name is its variable; `DdsUnit` has no `name` field. Every unit has its own `unitCode`.
- A topic's name is its class's wire type name (`type_support.type_name`). Pin it with `idl.type_name` for IDL-module types; peers name topics after the type.
- No topic table: the classes are the topics. Two classes must not share a type name. A struct becomes a topic only by appearing in some unit's `publish`/`subscribe`.
- `DdsUnit` accepts a lone class or any iterable, so a missing trailing comma cannot change the contract. The loader picks up every module-level object whose class is *named* `DdsUnit`.
- The Interface is generated and trusted; `dds_config.resolve_unit` does not validate it.
- No type registry: the type travels with the sample and RTI matches on topic name, type name and QoS compatibility.
- Sender identity is fixed by the ICD: every topic type carries `A_sourceID` (`A_platformId`, `A_systemId`, `A_moduleId`). `A_systemId` is the sender's unit code and alone decides who sent a sample.
- On send only `A_systemId` is stamped, and only while it is 0. There is no destination field; a named destination is only checked against the Interface.
- A topic with several publishers, or one the unit both publishes and subscribes, needs `A_sourceID` in its type; nothing checks this at load, `core/tools/validate_interface.py` does offline.
- Two silent failures (discovery succeeds, no samples arrive): a type-name mismatch (QoS `topic_filter`s must match the pinned name too) and an extensibility mismatch (`idl.final`/`extensible`/`mutable`).

## Code Style & Constraints

- Never import `rti` in `__init__.py` or `interface.py`.
- An Interface imports its topics relatively (`from ..pyIdl.x import T`), as the generator emits it. How to name it in a config: root `CLAUDE.md`, "Generated files".
- Import `DdsUnit` from `core.DDS`.
- Nested struct members need a default factory, not a bare instance (`@idl.struct` is a dataclass; a bare instance raises at import).

## Reference Codebases

- Topic types: `core/DDS/pyIdl/example_topics.py`.
- Reference Interface: `core/DDS/Interfaces/example_interface.py`.
- Runtime behaviour (loader, routing, QoS, lifecycle): `core/connections/dds_config.py`, `core/connections/dds.py`, and `core/connections/CLAUDE.md`.
