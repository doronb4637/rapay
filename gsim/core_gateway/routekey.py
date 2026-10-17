"""
A route key is an IRS opcode (`int`) or a DDS topic name (`str`) -- core's own
`send_message` / `handle_on_receive` take either, depending on the protocol.
"""
from __future__ import annotations


def route_label(op_code: int | str | None) -> str | None:
    """How a route key is displayed: hex for an opcode, the name for a topic."""
    if op_code is None:
        return None
    if isinstance(op_code, str):
        return op_code
    return f"0x{op_code:04X}"
