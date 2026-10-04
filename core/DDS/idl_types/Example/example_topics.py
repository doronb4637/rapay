"""
A worked example of a DDS type module -- copy this shape for a real ICD.

Nothing here registers anything with this project. `@idl.struct` builds the
TypeSupport Connext needs, `dds.Topic(...)` in `connections/dds.py` registers it
with the participant, and discovery does the rest. A struct becomes a TOPIC only
by appearing in a unit's publish/subscribe list in a DDS Interface
(`example_interface.py` imports `Track` and `Status` from here); the topic is
named after the type. `SourceId` appears in none, so it never gets an entity.

Two details matter for talking to a real unit, and both fail SILENTLY when they
are wrong -- discovery succeeds, the entities appear in Admin Console, and no
sample ever arrives:

  * The type NAME, which is also the TOPIC name. It defaults to the Python class
    name. If the peer's type came from real IDL it may be advertised as
    something else, in which case pin it on the type:
    `@idl.struct(type_annotations=[idl.type_name("MyModule::Track")])` -- and
    the topic becomes `MyModule::Track` too.

  * EXTENSIBILITY. `@idl.struct(type_annotations=[idl.final])` and friends must
    agree with the peer's IDL. `rtiddsspy -domainId <N>` shows what the peer
    actually advertises, which is the fastest way to check both.

One Python detail that is not a DDS detail: `@idl.struct` builds a dataclass, so
a NESTED struct member must use `field(default_factory=...)`. Writing
`A_sourceID: SourceId = SourceId()` raises "mutable default ... is not allowed"
at import time -- loudly, at least.
"""
from dataclasses import field

import rti.types as idl


@idl.struct
class SourceId:
    """
    The sender identity every topic carries, always as a member named
    `A_sourceID`.

    `connections/dds.py` reads `A_systemId` -- the unit code -- off inbound
    samples to work out which unit sent them (a DataReader serves every
    publisher on its topic at once, so the sample itself is the only thing that
    can say), and stamps it on outbound ones.
    """
    A_platformId: idl.int32 = 0
    A_systemId: idl.int16 = 0
    A_moduleId: idl.int16 = 0


@idl.struct
class Track:
    A_sourceID: SourceId = field(default_factory=SourceId)
    track_id: idl.uint32 = 0
    x: idl.float64 = 0.0
    y: idl.float64 = 0.0
    #: Payloads carry their own timestamps, so DDS SampleInfo metadata is not
    #: needed on the read path.
    timestamp_us: idl.uint64 = 0


@idl.struct
class Status:
    A_sourceID: SourceId = field(default_factory=SourceId)
    healthy: bool = True
    message: str = ""
