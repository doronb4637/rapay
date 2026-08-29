"""
Addressing one field inside a decoded message, and comparing it to a value.

Two features ask the same question of the same shape, and this is the one place
that answers it:

  * a received FILTER decides whether to log a message (`filters.py`)
  * a reactive BEHAVIOUR decides whether to respond to one (`behaviours.py`)

Both walk a dotted path into a `Message.to_dict()` payload and apply an
operator. Without this module that walk, the operator table and the enum rule
would exist twice, in two modules that must agree forever -- and the first time
they drifted, a condition would mean something subtly different depending on
which dialog you typed it into.

Paths address the shape `to_dict()` actually produces, which was read out of the
codec rather than assumed (`core/IRS/fields.py`, `core.py`, `bitfields.py`):

    Field       -> the raw number                      full operator set
    EnumField   -> the MEMBER NAME string ("ON")       equality only
    Structure   -> a nested dict                       descend by path
    BitField    -> a nested dict of its BITS           bits are addressable
    ArrayField  -> a list                              not addressable, see below

**A path may not cross an array.** `Areas.fr` cannot name one of 35 elements,
and silently taking the first would be a lie the user could not see -- so
`resolve_path` refuses to index into a list and returns `ABSENT` instead.
`schema.field_targets` is the other half of that rule: it never offers such a
path in the first place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

#: "The field this path names is not in the payload." Distinct from `None`,
#: which is a legitimate decoded value (an enum with no `0` member).
ABSENT = object()


def _numeric(left: Any, right: Any) -> bool:
    """Are both sides orderable as numbers? Enum values arrive as member-name
    strings, so `<` on them would compare alphabetically and mean nothing."""
    return isinstance(left, (int, float)) and isinstance(right, (int, float))


#: Operator -> comparison. Ordering operators refuse non-numeric operands rather
#: than raising: every caller runs on a core executor thread inside the receive
#: callback, and an exception there would take out the decode path for a typo.
OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda left, right: left == right,
    "!=": lambda left, right: left != right,
    "<": lambda left, right: _numeric(left, right) and left < right,
    "<=": lambda left, right: _numeric(left, right) and left <= right,
    ">": lambda left, right: _numeric(left, right) and left > right,
    ">=": lambda left, right: _numeric(left, right) and left >= right,
}

#: The operators that mean anything against a member-name string. Enforced at
#: the API edge (`api/models.py`, the route modules) so a nonsense condition
#: fails in the request, where the modal can show why, rather than silently
#: never matching.
EQUALITY_OPERATORS: tuple[str, ...] = ("==", "!=")


def resolve_path(payload: Any, parts: tuple[str, ...]) -> Any:
    """Walk a dotted path into a `to_dict()` payload, or `ABSENT`."""
    cursor: Any = payload
    for part in parts:
        if not isinstance(cursor, dict) or part not in cursor:
            return ABSENT
        cursor = cursor[part]
    return cursor


def assign_path(payload: dict[str, Any], parts: tuple[str, ...], value: Any) -> None:
    """Write `value` at a dotted path, creating intermediate dicts as needed.

    The write half of `resolve_path`, used by a behaviour's value forwarding.
    Mutates `payload` in place; callers hand it a copy they own.

    A path segment that already holds a non-dict is REPLACED by a dict rather
    than raising, because the alternative is worse: a mapping targeting
    `Header.Id` against a payload where `Header` is somehow a scalar would
    otherwise take out the response path on a core executor thread. The route
    layer validates every target against the real schema before it can ever be
    stored, so reaching that branch means the schema and the payload already
    disagree.
    """
    cursor: Any = payload
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


@dataclass
class Condition:
    """`<path> <op> <value>` against one decoded payload.

    Shared verbatim by a filter rule (which adds an action and a hit count) and
    a behaviour trigger (which uses it as-is). `parts` is pre-split so the hot
    path -- once per inbound message, up to ~1000/s -- does no string work.
    """
    path: str
    op: str
    value: Any
    parts: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        self.parts = tuple(self.path.split("."))

    def matches(self, payload: Any) -> bool:
        """A path that is absent never matches -- including under `!=`, which
        would otherwise report "not equal" for a field that is not there at all
        and make a typo look like a working condition."""
        current = resolve_path(payload, self.parts)
        if current is ABSENT:
            return False
        return OPERATORS[self.op](current, self.value)

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "op": self.op, "value": self.value}


def build_condition(raw: dict[str, Any] | None) -> Condition | None:
    """One validated `Condition`, or `None` for "no condition". Raises
    `ValueError` for an unknown operator or a missing path, so a bad one fails
    where it is configured rather than once per inbound message."""
    if not raw:
        return None
    path = raw.get("path")
    op = raw.get("op", "==")
    if not path:
        raise ValueError("a condition needs a field path")
    if op not in OPERATORS:
        raise ValueError(f"unknown operator {op!r}; known: {sorted(OPERATORS)}")
    return Condition(path=path, op=op, value=raw.get("value"))
