# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`core/DDS` holds what a DDS system is *described* in, as opposed to how a connection runs it:

- `idl_types/` -- plain Python modules of `@idl.struct` topic types (`Example/example_topics.py`).
- `interface.py` -- `DdsUnit`, the vocabulary a **DDS Interface** is written in, plus
  `INTERFACE_FORMAT`. Re-exported by `__init__.py`.
- `Interfaces/Example/example_interface.py` -- the reference DDS Interface: the exact shape the
  system-XML -> Python XSLT (written and owned by the user, not in this repo) must produce.
- `Configuration/` -- the deployment's QoS: `UNIVERSAL_QOS.xml` (`dds_config.DEFAULT_QOS_FILE`) and
  the security files it references. Data, not code. The RTI license sits beside the package, at
  `core/rti_license.dat` (`dds_config.DEFAULT_LICENSE_FILE`, exported as `RTI_LICENSE_FILE` by
  `connections/dds.py` before RTI is imported, unless the environment already sets it).

Standalone in the repo's dependency graph (see root `CLAUDE.md`): nothing here imports
`core.connections`, `core.tools`, `core.IRS` or `core.annotations`. `__init__.py` and
`interface.py` must never import `rti`, so `core.DDS` stays importable in a process without Connext
-- only the `idl_types` modules (and the Interfaces that import them) need it.
`core.connections.dds_config` is what loads and validates an Interface; the arrow points from
`connections` into `DDS`, never back.

## The DDS Interface

A generated module, one per system, naming every unit, its wire unit code, and the topic classes
it publishes and subscribes:

```python
from core.DDS import DdsUnit
from my_icd.topics import Status, Track      # ABSOLUTE imports

INTERFACE_FORMAT = 1
SYSTEM = "ExampleSystem"                     # optional, used in logs

SensorUnit = DdsUnit(unitCode=0x01, publish=(Track,), subscribe=(Status,))
ControlUnit = DdsUnit(unitCode=0x02, publish=(Status,), subscribe=(Track,))
```

A connection then names only which unit it is: `{"protocol": "dds", "unit": "SensorUnit",
"dds_interface": "<path to this file>"}`. Its own code, its peers, its topics and therefore its
DataWriters/DataReaders all follow from the Interface.

The rules, each enforced -- by `DdsUnit.__post_init__` at the Interface's own import, or by
`dds_config.resolve_unit` when a config loads it:

- **A unit's name is its variable.** `DdsUnit` deliberately has no `name` field. One object bound
  to two names is an error, and so are two units sharing a `unitCode` (it identifies senders).
- **A topic's name is its class's wire type name** (`type_support.type_name`): the class name,
  unless the type pins one with `idl.type_name`. Types generated from IDL with modules do -- class
  `P_Radar_PSM_Track`, type and topic `P_Radar_PSM::Track` -- and peers name the topic after the
  type, so the class name would never match. There is no topic table: the classes ARE the topics.
  Two different classes sharing a type name are an error -- they would be one topic on the wire
  carried by two types.
- **Import `DdsUnit` from `core.DDS`, never define it.** The loader finds units by `isinstance`
  against this one class; an Interface with its own `class DdsUnit` gets an error saying so.
- **Imports are absolute.** The Interface's topic classes must be the very objects the application
  builds samples from. An Interface is loaded by *path*, so it has no package to be relative to,
  and giving it one would load a private second copy of every class -- the same two-module-objects
  failure `core/IRS/_alias.py` exists to prevent for IRS. The loader refuses relative imports and
  says why.
- **`(Track)` is not a tuple.** `DdsUnit` accepts a lone class or any iterable for
  `publish`/`subscribe`, so a missing trailing comma in generated code cannot change the contract.
- **`INTERFACE_FORMAT`** must equal `interface.INTERFACE_FORMAT`. Bump both, and the XSLT, if the
  shape ever changes; an Interface written for another format is refused rather than half-read.

## There is no "topic" marker on a struct

RTI's `@idl.struct` takes only extensibility, `type_name`, data-representation and XTypes
annotations -- nothing says "this struct is a topic" versus "this struct is nested inside one". So
the generated type modules cannot tell the two apart, and do not need to: a struct becomes a topic
only by appearing in some `DdsUnit`'s `publish`/`subscribe`. `SourceId` in the example appears in
none, so no unit ever gets an entity for it.

## Still no TYPE registry

IRS needs a registry because a binary payload carries no type information of its own: something
has to look up a layout by `(unitCode, opCode)` before the bytes can be parsed at all. DDS is the
opposite -- the type travels with the sample, and RTI matches publishers to subscribers on (topic
name, type name, QoS compatibility) during discovery. The DDS Interface is a *routing* contract
(who publishes and subscribes what), not a type registry; a type module stays exactly what an RTI
Connext Python developer writes on any project:

```python
import rti.types as idl
from dataclasses import field

@idl.struct
class SourceId:
    A_platformId: idl.int32 = 0
    A_systemId: idl.int16 = 0
    A_moduleId: idl.int16 = 0

@idl.struct
class Track:
    A_sourceID: SourceId = field(default_factory=SourceId)
    x: float = 0.0
```

## The sender is always `A_sourceID`

Every topic type carries its sender as a member named `A_sourceID`: a struct of
`A_platformId: int32`, `A_systemId: int16`, `A_moduleId: int16`. This is fixed by the ICD, so
nothing configures it -- a connection config with a `header` key is refused.

- **`A_systemId` is the unit code** -- the `unitCode` of the sender's `DdsUnit`. It alone decides
  who sent a sample; `A_platformId` (always 1) and `A_moduleId` (0 unless changed) are carried, not
  routed on.
- **On send, only `A_systemId` is stamped** (with our own unit code, and only while it is still 0).
  The platform and module ids are the caller's to set. There is no destination field: naming a
  destination on a send is checked against the Interface, nothing more.
- A DataReader serves every publisher of its topic at once, so the sample is the only thing that
  says who sent it. A topic with a single publisher still routes without `A_sourceID`, but one with
  several publishers -- or one a unit both publishes and subscribes -- is refused at load if its
  type lacks it.

## A Python gotcha

**Nested struct members need `field(default_factory=...)`, not a bare instance.** `@idl.struct`
builds a dataclass under the hood, so `A_sourceID: SourceId = SourceId()` raises `ValueError:
mutable default ... is not allowed` at import time. Always
`A_sourceID: SourceId = field(default_factory=SourceId)`.

## Two ways this fails silently

Both produce the same symptom: discovery succeeds, the entities show up in RTI Admin Console, and
no sample ever arrives -- with no error anywhere.

- **Type name mismatch.** `@idl.struct` names the DDS type after the Python class. A peer whose
  type came from real IDL may be advertising a different name (`MyModule::Track`); pin it on the
  type with `@idl.struct(type_annotations=[idl.type_name("MyModule::Track")])`. The TOPIC name
  follows it, so the pin fixes both -- and QoS `topic_filter`s must then match `MyModule::Track`
  (or a wildcard such as `*Track`), or that topic silently gets the profile's baseline.
- **Extensibility mismatch.** `idl.final` / `idl.extensible` / `idl.mutable` (passed via
  `@idl.struct(type_annotations=[...])`) must agree with what the peer's IDL declares.

`rtiddsspy -domainId <N>` is the fastest way to check both: it shows the type name and
extensibility the peer is actually advertising on the wire.
