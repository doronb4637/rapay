"""
Received-message filters: which inbound messages are worth logging at all.

A filter is "for THIS message on THIS link, log it only when ...". It exists
because the console became genuinely fast: a 1ms behaviour really does deliver
~1000 entries a second per direction, and every one of them is kept (see
`runtime._log`). That fidelity is exactly what makes the pane useless for the
*other* job -- finding the message that changed. `LOG_LIMIT` is 2000 entries,
which at that rate is **two seconds** of history, and the UI renders 30 rows.

Three design points, all deliberate:

1. **This is not the 60Hz sampler that was tried and reverted.** `runtime._log`
   carries the argument in full: an instrument reporting its own sampling period
   instead of the signal is worse than a slow one. The difference is not "how
   much is dropped" -- it is accountability. That sampler was implicit,
   rate-derived, and silent. This is user-authored, per-message, and COUNTED:
   every rule reports how many messages it decided about (`Rule.hits`), every
   filter reports `dropped` / `logged` / `dropped_by_change`, and the numbers
   reconcile -- `dropped + logged` is exactly what arrived on that route while
   the filter was armed. Nothing disappears without a number saying so, which is
   the property that makes suppression legitimate here.

2. **It runs before the entry exists.** `admits()` is consulted at the very top
   of `_log`, before `next(self._seq)`, before `record.received.append`, and
   before the EventBus. So a dropped message burns no sequence number (the
   console reads a `seq` gap as loss, and this is not loss), never occupies a
   slot in the ring buffer the user wants for changes, and never reaches the
   socket -- which is the whole point at 1kHz. The cost is real and was chosen
   knowingly: a dropped message is *gone*, and disarming a filter reveals new
   traffic, not the past.

3. **Filters are keyed by ROUTE**, `(connection_name, unit_name, op_code)`,
   exactly as behaviours are. Two filters on one route would not compose, they
   would contradict; configuring a route that already has one REPLACES it.

Evaluation is two stages, and the order is what the UI states verbatim:

    1. Rules decide whether the message is INTERESTING.
       Any matching `drop` rule rejects it. If any `keep` rule exists, one of
       them must match.
    2. The change trigger then decides whether it is NEW.
       `change` compares the whole payload against the last logged one;
       `field-change` compares one resolved field path.

Paths are dotted (`Header.Flags.Mode`), and they address the shape
`Message.to_dict()` actually produces -- which was read out of the codec rather
than assumed (`core/IRS/fields.py`, `core.py`, `bitfields.py`):

    Field       -> the raw number                      full operator set
    EnumField   -> the MEMBER NAME string ("ON"), None  equality only
    Structure   -> a nested dict                       descend by path
    BitField    -> a nested dict of its BITS           bits are addressable
    ArrayField  -> a list                              see below

**Fields inside an array are not rule targets.** `Areas.fr` cannot name one of
35 elements, so a rule path may not cross an array -- for `ArrayOfAreas` that
leaves `Len` alone. The change trigger has no such limit: it compares by value,
so an array field (or the whole message) is a perfectly good change subject,
and that is what covers the array case.
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

#: How a message qualifies for logging once the rules have admitted it.
MODE_ALL = "all"                    # every arrival
MODE_CHANGE = "change"              # only when any field differs from the last logged
MODE_FIELD_CHANGE = "field-change"  # only when ONE named field differs
MODES: tuple[str, ...] = (MODE_ALL, MODE_CHANGE, MODE_FIELD_CHANGE)

#: What a rule does with the messages it matches.
ACTIONS: tuple[str, ...] = ("keep", "drop")

#: How often armed filters' counters are pushed to clients. Same rate and same
#: reasoning as `behaviours.STATS_PERIOD_SECONDS`: these are numbers a human
#: reads off a panel, and at 1kHz they change a thousand times a second, so
#: publishing per decision would put the console's own load back on the socket
#: that filtering exists to relieve.
STATS_PERIOD_SECONDS = 0.5

#: "This filter has not logged anything yet", distinct from "the field this
#: filter watches is not present in the payload" -- `None` is a legitimate
#: decoded value (an enum with no 0 member), so neither can be spelt `None`.
_NO_PREVIOUS = object()
_ABSENT = object()


def _numeric(left: Any, right: Any) -> bool:
    """Are both sides orderable as numbers? Enum values arrive as member-name
    strings, so `<` on them would compare alphabetically and mean nothing."""
    return isinstance(left, (int, float)) and isinstance(right, (int, float))


#: Operator -> comparison. Ordering operators refuse non-numeric operands rather
#: than raising: `admits()` runs on a core executor thread inside the receive
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
#: the API edge (`api/models.py`) so a nonsense rule fails in the PUT, where the
#: modal can show why, rather than silently never matching.
EQUALITY_OPERATORS: tuple[str, ...] = ("==", "!=")


def resolve_path(payload: Any, parts: tuple[str, ...]) -> Any:
    """Walk a dotted path into a `to_dict()` payload, or `_ABSENT`.

    Refuses to index into a list on purpose -- see the module docstring: a path
    that crosses an array cannot name one element, and silently taking the first
    would be a lie the user could not see.
    """
    cursor: Any = payload
    for part in parts:
        if not isinstance(cursor, dict) or part not in cursor:
            return _ABSENT
        cursor = cursor[part]
    return cursor


@dataclass
class Rule:
    """One condition, and how many messages it has decided about.

    `hits` reads differently per action, which is why it is not called
    `dropped`: on a `drop` rule it is how many it rejected, on a `keep` rule how
    many it admitted. Both answer the same question -- "is this rule doing
    anything?" -- and both are what the UI puts on the row.
    """
    action: str
    path: str
    op: str
    value: Any
    hits: int = 0
    #: The path pre-split, so the hot path does no string work per message.
    parts: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        self.parts = tuple(self.path.split("."))

    def matches(self, payload: Any) -> bool:
        current = resolve_path(payload, self.parts)
        if current is _ABSENT:
            return False
        return OPERATORS[self.op](current, self.value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "path": self.path,
            "op": self.op,
            "value": self.value,
            "hits": self.hits,
        }


@dataclass
class MessageFilter:
    """Everything configured for one inbound route."""
    id: str
    connection_name: str
    unit_name: str          # the SENDER's configured name, as the Received log shows it
    unit_code: int          # THEIR unit code -- the one the layout is registered under
    op_code: int
    message_name: str
    namespace: str | None
    mode: str = MODE_ALL
    change_field: str | None = None
    rules: list[Rule] = field(default_factory=list)
    #: Whether this filter is currently deciding anything. Disarmed keeps the
    #: configuration but admits everything -- what "Show all" does, so the
    #: fastest action in the dialog is undoing it.
    armed: bool = True
    dropped: int = 0            # total rejected, by any cause
    logged: int = 0             # total admitted
    dropped_by_change: int = 0  # the share of `dropped` the change trigger owns
    change_parts: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        self.change_parts = tuple(self.change_field.split(".")) if self.change_field else ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "connection_name": self.connection_name,
            "unit_name": self.unit_name,
            "unit_code": self.unit_code,
            "unit_code_hex": f"0x{self.unit_code:02X}",
            "op_code": self.op_code,
            "op_code_hex": f"0x{self.op_code:04X}",
            "message_name": self.message_name,
            "namespace": self.namespace,
            "mode": self.mode,
            "change_field": self.change_field,
            "armed": self.armed,
            "rules": [rule.as_dict() for rule in self.rules],
            "dropped": self.dropped,
            "logged": self.logged,
            "dropped_by_change": self.dropped_by_change,
        }


class FilterSet:
    """Owns every filter and the memory the change triggers need.

    `publish` is injected rather than imported, for the same reason
    `BehaviourEngine` takes it: this module must never reach back into
    `runtime`, which owns an instance of it.
    """

    def __init__(self, publish: Callable[[dict[str, Any]], None]) -> None:
        self._publish = publish
        self._filters: dict[str, MessageFilter] = {}
        self._by_route: dict[tuple[str, str, int], MessageFilter] = {}
        #: filter id -> the last value logged for it (whole payload, or the one
        #: resolved change field). This is the entire state a change trigger has.
        self._last: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        #: Bumped on every decision, so the heartbeat can publish only when the
        #: numbers actually moved instead of once every half second forever.
        self._decisions = 0
        self._published_at = 0
        self._closing = threading.Event()
        self._stats = threading.Thread(
            target=self._stats_heartbeat, name="gsim-filter-stats", daemon=True
        )
        self._stats.start()

    # -- the hot path ----------------------------------------------------
    def admits(self, connection_name: str, unit_name: str, op_code: int,
               payload: Any) -> bool:
        """Should this received message be logged? Called once per arrival.

        Runs on a core executor thread, concurrently across routes, up to
        ~1000x a second per route. The lock is held across evaluation rather
        than only around the lookup: the work behind it is a dict walk and a
        comparison, and splitting it would mean a filter could be edited between
        being read and being used.
        """
        with self._lock:
            message_filter = self._by_route.get((connection_name, unit_name, op_code))
            if message_filter is None or not message_filter.armed:
                return True
            self._decisions += 1
            return self._evaluate(message_filter, payload)

    def _evaluate(self, message_filter: MessageFilter, payload: Any) -> bool:
        """Caller holds the lock. See the module docstring for the two stages."""
        # 1. Interesting? Drop wins, so every drop rule gets to reject before a
        #    keep rule is allowed to conclude -- a keep matching earlier in the
        #    list must not save a message a later drop rule rejects.
        matched_keep: Rule | None = None
        keeps = 0
        for rule in message_filter.rules:
            if rule.action == "drop":
                if rule.matches(payload):
                    rule.hits += 1
                    message_filter.dropped += 1
                    return False
            else:
                keeps += 1
                if matched_keep is None and rule.matches(payload):
                    matched_keep = rule
        if keeps and matched_keep is None:
            # Keep rules exist and none matched: this is the allow-list half.
            message_filter.dropped += 1
            return False
        if matched_keep is not None:
            matched_keep.hits += 1

        # 2. New?
        if message_filter.mode == MODE_ALL:
            message_filter.logged += 1
            return True
        current = (
            resolve_path(payload, message_filter.change_parts)
            if message_filter.mode == MODE_FIELD_CHANGE
            else payload
        )
        previous = self._last.get(message_filter.id, _NO_PREVIOUS)
        if previous is not _NO_PREVIOUS and previous == current:
            message_filter.dropped += 1
            message_filter.dropped_by_change += 1
            return False
        # Stored by reference, not copied: `payload` is a dict `to_dict()` just
        # built for this message and nothing mutates it afterwards -- it goes
        # onto the LogEntry as-is. A deepcopy per arrival would be pure cost.
        self._last[message_filter.id] = current
        message_filter.logged += 1
        return True

    # -- queries ---------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [message_filter.as_dict() for message_filter in self._filters.values()]

    def get(self, filter_id: str) -> MessageFilter:
        with self._lock:
            message_filter = self._filters.get(filter_id)
        if message_filter is None:
            raise KeyError(f"no filter with id {filter_id!r}")
        return message_filter

    # -- mutation --------------------------------------------------------
    def set(self, connection_name: str, unit_name: str, unit_code: int, op_code: int,
            message_name: str, namespace: str | None = None, mode: str = MODE_ALL,
            change_field: str | None = None, rules: list[dict[str, Any]] | None = None,
            armed: bool = True) -> MessageFilter:
        """Create or REPLACE the filter on one route.

        Counters and change memory reset, always. They describe the filter as it
        was configured, and carrying them across an edit would attribute drops to
        a rule that never made them.
        """
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; known: {list(MODES)}")
        if mode == MODE_FIELD_CHANGE and not change_field:
            raise ValueError("mode 'field-change' needs a change_field")
        built = [self._build_rule(raw) for raw in (rules or [])]

        route = (connection_name, unit_name, op_code)
        with self._lock:
            existing = self._by_route.get(route)
            if existing is not None:
                # Reuse the id so the modal's selection survives a save.
                message_filter = existing
                message_filter.message_name = message_name
                message_filter.namespace = namespace
                message_filter.unit_code = unit_code
                message_filter.mode = mode
                message_filter.change_field = change_field
                message_filter.change_parts = (
                    tuple(change_field.split(".")) if change_field else ())
                message_filter.rules = built
                message_filter.armed = armed
                message_filter.dropped = 0
                message_filter.logged = 0
                message_filter.dropped_by_change = 0
            else:
                message_filter = MessageFilter(
                    id=f"flt-{next(self._ids)}",
                    connection_name=connection_name,
                    unit_name=unit_name,
                    unit_code=unit_code,
                    op_code=op_code,
                    message_name=message_name,
                    namespace=namespace,
                    mode=mode,
                    change_field=change_field,
                    rules=built,
                    armed=armed,
                )
                self._filters[message_filter.id] = message_filter
                self._by_route[route] = message_filter
            self._last.pop(message_filter.id, None)
        self._publish_all()
        return message_filter

    @staticmethod
    def _build_rule(raw: dict[str, Any]) -> Rule:
        action = raw.get("action")
        op = raw.get("op")
        path = raw.get("path")
        if action not in ACTIONS:
            raise ValueError(f"unknown rule action {action!r}; known: {list(ACTIONS)}")
        if op not in OPERATORS:
            raise ValueError(f"unknown operator {op!r}; known: {sorted(OPERATORS)}")
        if not path:
            raise ValueError("a rule needs a field path")
        return Rule(action=action, path=path, op=op, value=raw.get("value"))

    def set_armed(self, filter_id: str, armed: bool) -> MessageFilter:
        message_filter = self.get(filter_id)
        with self._lock:
            message_filter.armed = armed
            if armed:
                # Re-arming starts a fresh record: the peer's state is unknown
                # after however long this spent disarmed, so the next arrival is
                # a first arrival rather than a repeat of a stale baseline.
                self._last.pop(filter_id, None)
                message_filter.dropped = 0
                message_filter.logged = 0
                message_filter.dropped_by_change = 0
                for rule in message_filter.rules:
                    rule.hits = 0
        self._publish_all()
        return message_filter

    def disarm_all(self) -> None:
        """What 'Show all' does -- every filter stops deciding, none is lost."""
        with self._lock:
            for message_filter in self._filters.values():
                message_filter.armed = False
        self._publish_all()

    def delete(self, filter_id: str) -> None:
        message_filter = self.get(filter_id)      # raises KeyError if unknown
        with self._lock:
            self._filters.pop(filter_id, None)
            self._by_route.pop(
                (message_filter.connection_name, message_filter.unit_name,
                 message_filter.op_code), None)
            self._last.pop(filter_id, None)
        self._publish_all()

    # -- connection lifecycle hooks --------------------------------------
    def remove_connection(self, connection_name: str) -> None:
        """Drop every filter on a DELETED connection -- and an edit is a delete
        (see `runtime.replace`), so an edit drops them too. Same rule as
        behaviours, for the same reason: an edit can rename or remove the very
        peer a filter targets."""
        with self._lock:
            doomed = [
                filter_id for filter_id, message_filter in self._filters.items()
                if message_filter.connection_name == connection_name
            ]
            for filter_id in doomed:
                message_filter = self._filters.pop(filter_id)
                self._by_route.pop(
                    (message_filter.connection_name, message_filter.unit_name,
                     message_filter.op_code), None)
                self._last.pop(filter_id, None)
        if doomed:
            self._publish_all()

    def forget(self, connection_name: str | None = None) -> None:
        """Erase what the change triggers remember, so the next arrival on each
        route is a FIRST arrival.

        Load-bearing in two places, both of which look like the feature is
        broken if it is skipped:

        - **Clearing the Received log.** A link streaming one constant value
          would otherwise leave every `change` filter holding a matching
          baseline, so the freshly cleared pane would sit empty indefinitely.
          Clear means start the record over, and a record that starts with a
          suppressed baseline is not a record.
        - **Starting a connection.** Whatever the peer did while the transport
          was down is unknown, so the old baseline is not evidence about the
          current state.
        """
        with self._lock:
            if connection_name is None:
                self._last.clear()
                return
            for filter_id, message_filter in self._filters.items():
                if message_filter.connection_name == connection_name:
                    self._last.pop(filter_id, None)

    def shutdown(self) -> None:
        self._closing.set()
        with self._lock:
            self._filters.clear()
            self._by_route.clear()
            self._last.clear()

    # -- internals -------------------------------------------------------
    def _stats_heartbeat(self) -> None:
        """Push counters to clients on a timer.

        Every other `_publish_all` is a configuration change. The counters are
        not: at 1kHz they move a thousand times a second, and publishing per
        decision would put exactly the load back on the socket that filtering
        exists to take off it. Publishes only when a decision has actually been
        made since the last one, so an idle process is silent.
        """
        while not self._closing.wait(STATS_PERIOD_SECONDS):
            with self._lock:
                moved = self._decisions != self._published_at
                self._published_at = self._decisions
            if moved:
                self._publish_all()

    def _publish_all(self) -> None:
        """One event carrying the whole list -- a replace for the client, not a
        merge, so a dropped event cannot leave a stale rule on screen."""
        self._publish({"type": "filters", "filters": self.list()})
