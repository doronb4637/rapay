# Received filters

Per-message rules on inbound traffic: keep/drop on a field value, or suppress repeats until
something changes. Applied to the **Received** pane only — Sent is never filtered.

Filtering happens server-side, before a message is logged. **A dropped message is gone** — it never
gets a `seq`, never enters the ring buffer, never reaches the console. Disarming a filter reveals
new traffic, not the past.

## Opening it

The filter icon in the Received pane header (next to the eye/hide icon). The connection picker at
the top of the dialog is skipped automatically when there's only one connection.

Left rail: every message the selected connection can *receive* (from the registry — not from what
has actually arrived), searchable by name/opcode/unit, filterable by sender.

## Log this message

- **Every time it arrives** — no suppression.
- **Only when something changes** — compares the whole decoded payload to the last one logged.
- **Only when one field changes** — compares a single field, picked from a dropdown.

## Rules

Add a rule with **+ Add rule**. Each is `Keep|Drop` `<field>` `<op>` `<value>`.

- **Drop wins.** A message matching any Drop rule is rejected regardless of Keep rules.
- **Keep rules are an allow-list.** If any Keep rule exists, a message must match at least one to
  survive.
- Order between rules of the same action doesn't matter.

### What a rule can target

- Scalars and enums: any operator (enums compare by **member name**, e.g. `Flag is ON` — not the
  numeric value, so ordering operators (`<`, `>`) aren't offered for them).
- Bitfield bits: addressable individually (`Area.fr`), same as any other field.
- **Nothing inside an array.** A path can't name one element of a repeating array. Use "log on
  change" against the array (or the whole message) instead — the picker won't offer array-interior
  paths as rule targets.

## Counters

Every rule and every filter tracks what it's actually doing — `dropped`, `logged`, and per-rule
`hits` — live, not just at save time. The Received pane header shows a running total; a filter with
nothing dropped shows nothing there.

**Show all** (dialog header) disarms every filter at once without deleting any of them — the
fastest way to check "is this message actually being lost, or is nothing arriving at all."

## Things that reset a filter's memory

- Clearing the Received log (`Clear` in the pane's context menu).
- Starting a connection (a fresh session has no basis for "changed since last time").
- Re-arming a disarmed filter.

## API

```
GET    /api/filters
PUT    /api/connections/{name}/filters   {unit_name, op_code, mode, change_field?, rules[], armed}
POST   /api/filters/{id}/arm
POST   /api/filters/{id}/disarm
POST   /api/filters/disarm-all
DELETE /api/filters/{id}
```

`rules[]` entries: `{action: "keep"|"drop", path, op: "=="|"!="|"<"|"<="|">"|">=", value}`.

Not persisted in Save/Load session files (same as behaviours).
