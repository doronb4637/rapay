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
DDS            standalone -- @idl.struct type modules; imports only rti (third-party)
  ^
  |
core           uses IRS + DDS + connections + tools + core/annotations.py
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
- **`core/DDS`** is the topic-based counterpart to `IRS`, and is deliberately almost empty. It holds
  `Structures/` -- plain Python modules of `@idl.struct` classes -- and no registry, no engine, and
  no code of its own. The asymmetry with `IRS` is the whole point: a binary IRS payload carries no
  type information, so *something* has to look up a layout by `(unitCode, opCode)`; a DDS sample
  carries its type on the wire and RTI matches publishers to subscribers itself, so there is nothing
  to register. `core/DDS/__init__.py` does not import `rti`, so `core.DDS` stays importable in a
  process without Connext; only the `Structures/` modules themselves need it. `core.connections.dds`
  imports the named modules (see `core/connections/CLAUDE.md` section 9) -- the arrow points from
  `connections` into `DDS`, never the reverse.
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

### One IRS, two spellings

IRS is *used* under two names, and this is deliberate rather than a leak in the graph:

- `core.IRS.x` -- its real module name, used by everything inside this repo (`core.connections`,
  `core.tools`, `gsim.core_gateway`).
- `IRS.x` -- the name IRS has when it stands alone, and the **required** spelling in
  `core/IRS/Structures/*.py`. Those files are consumers of IRS written against IRS-as-a-library, and
  the bare spelling is the only one that keeps working when `IRS/` is copied out without `core`.

`core/IRS/_alias.py`, installed at the top of `core/IRS/__init__.py`, makes the two resolve to the
**same module object at every depth**. It is a no-op when IRS is imported standalone, and it uses
only the stdlib, so the "imports nothing else in this repo" rule above is untouched.

This is not cosmetic. Without it the two spellings are two independent module objects -- two
`STRUCTURE_REGISTRY` dicts, two `MessageMeta` metaclasses, two sets of field classes -- and the
failure is silent in a way that looks nothing like an import error: a structures file prints a fully
populated registry while `irs_parser._get_message_class` reads `{}`, and every
`isinstance(field, Field)` downstream misses. The enabling condition is `core/` being on `sys.path`
*in addition to* the repo root (an IDE "sources root" does this); `_alias.py` raises rather than
tolerate a genuine second copy, and `tools.general._assert_registered` catches the same class of
silent failure from the other end, at the config that named the file.

## The rule that keeps this real, not aspirational

Whenever you touch an import statement anywhere under `core/IRS/`, check it mechanically before
committing:

```bash
grep -rn "^from core\.\(annotations\|connections\|tools\)\|^import core\.\(annotations\|connections\|tools\)" core/IRS --include=*.py
# must print nothing
```

And whenever you add or edit a file under `core/IRS/Structures/`, check the other half of the same
rule -- a structures file must reach IRS by the bare name, never through `core`:

```bash
grep -rn "core\.IRS" core/IRS/Structures --include=*.py
# must print nothing -- structures files spell it `from IRS...`, see "One IRS, two spellings" above
```

If IRS ever needs something from `connections`, `tools`, or `core/annotations.py`, that is
backwards -- the fix is to move the shared piece into `IRS` (as was done for the type aliases in
`core/IRS/annotations.py`), not to add the import.

Structures files are the one deliberate exception: those are user-defined message layout files, not
part of the IRS engine itself. They are loaded as plugins (by `tools.general.import_modules`, on
`connections`' behalf) and are expected to `from core.IRS import *` the same way any other consumer
of IRS would -- they are consumers of IRS, not internals of it.

**They do not have to live under `core/IRS/Structures/`, and mostly should not.** A `Structures`
entry is either a path to a `.py` file *anywhere on the machine* -- loaded from that path, which is
how GSim's file dialog feeds it -- or a dotted name, which only resolves for a module that genuinely
ships inside `core/IRS/Structures/`. `core/IRS/Structures/` is now just a convenient place to keep a
few in the checkout; nothing depends on a file being there, and the frozen app ships none at all.
Import used to branch on that location, which is exactly what made a picked file fail to import on
one machine and not another -- see `gsim/CLAUDE.md`'s fourth authorised `core` change.
