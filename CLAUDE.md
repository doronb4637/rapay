# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A repo-root umbrella over three packages with a strict, one-directional dependency graph. Every
package-specific `CLAUDE.md` (`core/IRS/CLAUDE.md`, `core/connections/CLAUDE.md`, `gsim/CLAUDE.md`)
still applies -- this file states the graph that binds them together and the rule that keeps it from
rotting.

## The dependency graph

```
IRS            standalone -- imports nothing else in this repo
  ^
  |
core           uses IRS + connections + tools + core/annotations.py
  ^
  |
gsim           uses core (the whole package -- connections, IRS, tools, annotations)
```

- **`core/IRS`** is a self-contained binary parsing/serialization engine. It imports only from
  inside its own directory (`core/IRS/*.py`, relative imports) plus third-party packages
  (`beartype`). It must never import `core.connections`, `core.tools`, or `core.annotations` --
  doing so would make the "standalone" claim false. `core/IRS/annotations.py` holds IRS's own type
  vocabulary (`IrsMessage`, `UnitCode`, `OpCode`, `Namespace`, `NamespaceScope`) for exactly this
  reason: those names are needed by `IRS.REGISTRY` / `IRS.irs_parser` internally, so they live
  inside IRS rather than being borrowed from `core.annotations`.
- **`core/annotations.py`** re-exports those same names (`from core.IRS.annotations import ...`)
  purely so `core.connections` and `core.tools` keep one familiar import (`from core.annotations
  import *`) for both the IRS vocabulary and the connections-specific `Task`/`Future` aliases. The
  dependency arrow here only ever points from `core.annotations` into `IRS` -- never the reverse.
- **`core`** (as a whole -- `connections`, `tools`, `IRS`, `annotations.py`) is free to depend on
  IRS, and `connections`/`tools` may depend on `core.annotations`. See
  `core/connections/CLAUDE.md` for the connections<->IRS<->tools split of labour in detail.
- **`gsim`** depends on `core` as a whole (`connections`, `IRS`, `tools`, `annotations`), confined
  to `gsim/core_gateway/` -- see `gsim/CLAUDE.md` §"The one rule that matters more than any other in
  this package". `core` is never modified by `gsim`.

## The rule that keeps this real, not aspirational

Whenever you touch an import statement anywhere under `core/IRS/`, check it mechanically before
committing:

```bash
grep -rn "^from core\.\(annotations\|connections\|tools\)\|^import core\.\(annotations\|connections\|tools\)" core/IRS --include=*.py
# must print nothing
```

If IRS ever needs something from `connections`, `tools`, or `core/annotations.py`, that is
backwards -- the fix is to move the shared piece into `IRS` (as was done for the type aliases in
`core/IRS/annotations.py`), not to add the import.

`core/IRS/Structures/*.py` is the one deliberate exception: those are user-defined message layout
files, not part of the IRS engine itself. They are loaded as plugins (by
`tools.general.import_modules`, on `connections`' behalf) and are expected to `from core.IRS import
*` the same way any other consumer of IRS would -- they are consumers of IRS, not internals of it.
