"""
Standalone IRS parsing/serialization speed benchmark.

Deliberately NOT a pytest test and NOT under `core/tests` (`pytest.ini`'s
`testpaths` only collects `core/tests` anyway) -- this is a repeatable timing
script you run by hand before/after touching `core/IRS`'s hot paths (e.g. the
message compiler in `core/IRS/_compiler.py`) to see whether things actually got
faster, not a pass/fail correctness check.

Two messages, because one shape cannot show both things that matter:

`BenchMessage` exercises every SCALAR field kind in one message, so a single
parse/serialize round covers the whole engine:

  * plain primitive fields (`Field`)              -- id, timestamp
  * an enum field (`EnumField`)                    -- kind
  * a bitfield (`BitField` via `@baseType`)         -- status
  * a nested structure (`Structure`)                -- pos
  * a static/fixed-length array (`ArrayField`)      -- samples  (length=8)
  * a dynamic array, length bound to another field  -- payload  (length="count")
  * a greedy array, consumes the rest of the buffer -- tail     (length=None)

`NestedMessage` exercises ARRAYS OF STRUCTURES, which `BenchMessage` has no
example of and which is the engine's most allocation-heavy path -- one Python
object per element, times the element's own fields.

Usage:
    python -m core.IRS.benchmark.bench_message_parsing [--iterations N]
    python -m core.IRS.benchmark.bench_message_parsing --compare
"""
import argparse
import json
import os
import subprocess
import sys
import time
from enum import IntEnum

from core.IRS import (
    Byte,
    Float32,
    Message,
    Structure,
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


class Position(Structure):
    x: float = Float32
    y: float = Float32
    z: float = Float32


class BenchMessage(Message):
    """One message exercising every scalar IRS field kind -- see module docstring."""
    id: int = UInt32
    timestamp: int = UInt64
    kind: E_Kind
    status: StatusFlags
    pos: Position
    samples: list[int] = [UInt16, 8]          # static array
    count: int = UInt16
    payload: list[int] = [Byte, "count"]      # dynamic array, bound to `count`
    tail: list[int] = [Byte, None]            # greedy array, must be last


class Track(Structure):
    """One element of an array of structures."""
    trackId: int = UInt16
    kind: E_Kind
    flags: StatusFlags
    pos: Position


class NestedMessage(Message):
    """Arrays OF STRUCTURES -- fixed and counted. The allocation-heavy path."""
    fixed: list[Track] = [Track, 6]           # static array of structs
    count: int = Byte
    counted: list[Track] = [Track, "count"]   # counted array of structs


def _status() -> StatusFlags:
    status = StatusFlags()
    status.active = 1
    status.error = 0
    status.mode = 2
    return status


def _position(scale: float = 1.0) -> Position:
    pos = Position()
    pos.x = 1.5 * scale
    pos.y = -2.25 * scale
    pos.z = 3.75 * scale
    return pos


def build_sample() -> bytes:
    """A fully-populated `BenchMessage`, serialized once to a fixed byte buffer."""
    payload = list(range(16))

    msg = BenchMessage()
    msg.id = 42
    msg.timestamp = 1_700_000_000_000
    msg.kind = E_Kind.BETA
    msg.status = _status()
    msg.pos = _position()
    msg.samples = list(range(8))
    msg.count = len(payload)
    msg.payload = payload
    msg.tail = list(b"trailing-bytes-consumed-greedily")

    return msg.to_bytes()


def build_nested_sample() -> bytes:
    """A fully-populated `NestedMessage`, serialized to a fixed byte buffer."""
    def track(index: int) -> Track:
        item = Track()
        item.trackId = 1000 + index
        item.kind = E_Kind.ALPHA
        item.flags = _status()
        item.pos = _position(index + 1)
        return item

    msg = NestedMessage()
    msg.fixed = [track(i) for i in range(6)]
    msg.count = 4
    msg.counted = [track(i) for i in range(4)]
    return msg.to_bytes()


""" Timing """
def bench_parse(message_class, raw: bytes, iterations: int) -> float:
    from_bytes = message_class.from_bytes
    start = time.perf_counter()
    for _ in range(iterations):
        from_bytes(BinaryReader(raw))
    return time.perf_counter() - start


def bench_serialize(msg, iterations: int) -> float:
    to_bytes = msg.to_bytes
    start = time.perf_counter()
    for _ in range(iterations):
        to_bytes()
    return time.perf_counter() - start


def _measure(iterations: int, warmup: int) -> dict[str, float]:
    """Microseconds per operation for every case, in one process."""
    cases = {}
    for label, message_class, raw in (
            ("BenchMessage", BenchMessage, build_sample()),
            ("NestedMessage", NestedMessage, build_nested_sample())):
        msg = message_class.from_bytes(BinaryReader(raw))
        bench_parse(message_class, raw, warmup)
        bench_serialize(msg, warmup)
        cases[f"{label}.from_bytes"] = bench_parse(message_class, raw, iterations) / iterations * 1e6
        cases[f"{label}.to_bytes"] = bench_serialize(msg, iterations) / iterations * 1e6
        cases[f"{label}.size"] = float(len(raw))
    return cases


def _report(label: str, micros: float) -> None:
    print(f"{label:<28} avg={micros:8.3f}us/op  {1e6 / micros:>12,.0f} ops/s")


""" --compare: the same measurement either side of the compiler """
def _measure_in(compile_enabled: bool, iterations: int, warmup: int) -> dict[str, float]:
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    environment = dict(os.environ,
                       IRS_COMPILE="1" if compile_enabled else "0",
                       PYTHONPATH=repo_root)
    completed = subprocess.run(
        [sys.executable, "-m", __spec__.name, "--emit-json",
         "--iterations", str(iterations), "--warmup", str(warmup)],
        cwd=repo_root, env=environment, capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(completed.stderr)
    return json.loads(completed.stdout)


def _compare(iterations: int, warmup: int) -> None:
    """Interpreted vs compiled, each in its own process.

    `IRS_COMPILE` is read once at import and compilation is sticky per class, so
    the only way to time both honestly is one process each.
    """
    interpreted = _measure_in(False, iterations, warmup)
    compiled = _measure_in(True, iterations, warmup)

    print(f"iterations: {iterations:,} (warmup: {warmup:,})\n")
    print(f"{'':<28}{'interpreted':>14}{'compiled':>14}{'speedup':>12}")
    print("-" * 68)
    for key in interpreted:
        if key.endswith(".size"):
            continue
        was, now = interpreted[key], compiled[key]
        print(f"{key:<28}{was:>12.3f}us{now:>12.3f}us{was / now:>11.1f}x")
    print("-" * 68)
    for label in ("BenchMessage", "NestedMessage"):
        print(f"{label} is {int(interpreted[label + '.size'])} bytes on the wire")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200_000)
    parser.add_argument("--warmup", type=int, default=5_000)
    parser.add_argument("--compare", action="store_true",
                        help="time the interpreted and compiled paths side by side")
    parser.add_argument("--emit-json", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.compare:
        _compare(args.iterations, args.warmup)
        return

    # Warm up: absorbs first-call effects (the compiler runs on the first parse
    # of each class, and CPython's attribute caches settle) so the timed loop
    # measures steady-state cost only.
    results = _measure(args.iterations, args.warmup)
    if args.emit_json:
        json.dump(results, sys.stdout)
        return

    from core.IRS import _compiler
    print(f"compiler: {'ON' if _compiler.ENABLED else 'OFF (IRS_COMPILE=0)'}")
    print(f"iterations: {args.iterations:,} (warmup: {args.warmup:,})\n")
    for key, value in results.items():
        if key.endswith(".size"):
            print(f"{key:<28}{int(value)} bytes on the wire")
        else:
            _report(key, value)


if __name__ == "__main__":
    main()
