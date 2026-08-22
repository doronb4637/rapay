"""
Standalone IRS parsing/serialization speed benchmark.

Deliberately NOT a pytest test and NOT under `core/tests` (`pytest.ini`'s
`testpaths` only collects `core/tests` anyway) -- this is a repeatable timing
script you run by hand before/after touching `core/IRS`'s hot paths (e.g. the
`struct.Struct` packer caching in `core/IRS/buffers.py`) to see whether things
actually got faster, not a pass/fail correctness check.

`BenchMessage` below is built to exercise every field kind IRS knows about in
one message, so a single parse/serialize round covers the whole engine:

  * plain primitive fields (`Field`)              -- id, timestamp
  * an enum field (`EnumField`)                    -- kind
  * a bitfield (`BitField` via `@baseType`)         -- status
  * a static/fixed-length array (`ArrayField`)      -- samples  (length=8)
  * a dynamic array, length bound to another field  -- payload  (length="count")
  * a greedy array, consumes the rest of the buffer -- tail     (length=None)

Note: a nested `Structure`-typed field is deliberately NOT included. It hits a
pre-existing bug -- `Structure.__setattr__` (core/IRS/core.py) falls through
to `is_bearable(value, None)` and always raises when an attribute has no
annotation (which is exactly what `MessageMeta` does when it sets `_name` on
a nested-structure field instance at class-creation time), so nested
`Structure` fields are currently unusable. Flagged separately; add `pos:
Position` back here once that's fixed.

Usage:
    python -m core.IRS.benchmark.bench_message_parsing [--iterations N]
"""
import argparse
import time
from enum import IntEnum

from core.IRS import (
    Byte,
    Message,
    BitField,
    UInt16,
    UInt32,
    UInt64,
    baseType,
)
from core.IRS.buffers import BinaryReader


@baseType(1)
class E_Kind(IntEnum):
    UNKNOWN = 0x00
    ALPHA = 0x01
    BETA = 0x02


@baseType(1)
class StatusFlags(BitField):
    active: int = 1
    error: int = 1
    mode: int = 2
    reserved: int = 4


class BenchMessage(Message):
    """One message exercising every (currently working) IRS field kind -- see module docstring."""
    id: int = UInt32
    timestamp: int = UInt64
    kind: E_Kind
    status: StatusFlags
    samples: list[int] = [UInt16, 8]          # static array
    count: int = UInt16
    payload: list[int] = [Byte, "count"]      # dynamic array, bound to `count`
    tail: list[int] = [Byte, None]            # greedy array, must be last


def build_sample() -> bytes:
    """A fully-populated `BenchMessage`, serialized once to a fixed byte buffer."""
    status = StatusFlags()
    status.active = 1
    status.error = 0
    status.mode = 2

    payload = list(range(16))

    msg = BenchMessage()
    msg.id = 42
    msg.timestamp = 1_700_000_000_000
    msg.kind = E_Kind.BETA
    msg.status = status
    msg.samples = list(range(8))
    msg.count = len(payload)
    msg.payload = payload
    msg.tail = list(b"trailing-bytes-consumed-greedily")

    return msg.to_bytes()


def bench_parse(raw: bytes, iterations: int) -> float:
    start = time.perf_counter()
    for _ in range(iterations):
        BenchMessage.from_bytes(BinaryReader(raw))
    return time.perf_counter() - start


def bench_serialize(msg: BenchMessage, iterations: int) -> float:
    start = time.perf_counter()
    for _ in range(iterations):
        msg.to_bytes()
    return time.perf_counter() - start


def _report(label: str, elapsed: float, iterations: int) -> None:
    avg_us = (elapsed / iterations) * 1_000_000
    ops_per_sec = iterations / elapsed
    print(f"{label:<20} total={elapsed:8.4f}s  avg={avg_us:8.3f}us/op  {ops_per_sec:>12,.0f} ops/s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200_000)
    parser.add_argument("--warmup", type=int, default=5_000)
    args = parser.parse_args()

    raw = build_sample()
    msg = BenchMessage.from_bytes(BinaryReader(raw))

    print(f"message size on the wire: {len(raw)} bytes")
    print(f"iterations: {args.iterations:,} (warmup: {args.warmup:,})\n")

    # Warm up: absorbs first-call effects (e.g. CPython's import/attribute
    # caches settling) so the timed loop measures steady-state cost only.
    bench_parse(raw, args.warmup)
    bench_serialize(msg, args.warmup)

    _report("from_bytes", bench_parse(raw, args.iterations), args.iterations)
    _report("to_bytes", bench_serialize(msg, args.iterations), args.iterations)


if __name__ == "__main__":
    main()
