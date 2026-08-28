"""
A worked example of a DDS type module -- copy this shape for a real ICD.

Nothing here registers anything with this project. `@idl.struct` builds the
TypeSupport Connext needs, `dds.Topic(...)` in `connections/dds.py` registers it
with the participant, and discovery does the rest. A connection reaches these
classes by naming this module in `config['idl_modules']` and each class in a
`config['topics']` entry's `type`.

Two details matter for talking to a real unit, and both fail SILENTLY when they
are wrong -- discovery succeeds, the entities appear in Admin Console, and no
sample ever arrives:

  * The type NAME. It defaults to the Python class name. If the peer's type came
    from real IDL it may be advertised as something else, in which case set
    `type_name` on the topic's config entry (e.g. "MyModule::Track").

  * EXTENSIBILITY. `@idl.struct(type_annotations=[idl.final])` and friends must
    agree with the peer's IDL. `rtiddsspy -domainId <N>` shows what the peer
    actually advertises, which is the fastest way to check both.

One Python detail that is not a DDS detail: `@idl.struct` builds a dataclass, so
a NESTED struct member must use `field(default_factory=...)`. Writing
`header: Header = Header()` raises "mutable default ... is not allowed" at
import time -- loudly, at least.
"""
from dataclasses import field

import rti.types as idl


@idl.struct
class Header:
    """
    The routing header every message on this link carries.

    `connections/dds.py` reads `source_unit` off inbound samples to work out
    which configured unit sent them -- a DataReader serves every publisher on
    its topic at once, so the sample itself is the only thing that can say --
    and stamps both fields on outbound ones. The field names are configurable
    per connection via config['header'] if an ICD spells them differently.
    """
    source_unit: idl.uint8 = 0
    destination_unit: idl.uint8 = 0
    #: Third header field, meaning still to be confirmed with the ICD owner.
    #: Carried and decoded, never routed on.
    reserved: idl.uint16 = 0


@idl.struct
class Track:
    header: Header = field(default_factory=Header)
    track_id: idl.uint32 = 0
    x: idl.float64 = 0.0
    y: idl.float64 = 0.0
    #: Payloads carry their own timestamps, so DDS SampleInfo metadata is not
    #: needed on the read path.
    timestamp_us: idl.uint64 = 0


@idl.struct
class Status:
    header: Header = field(default_factory=Header)
    healthy: bool = True
    message: str = ""
