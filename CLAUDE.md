# CLAUDE.md

Repo-root umbrella over `core/IRS`, `core/DDS`, `core/connections`+`core/tools`, and `gsim`. Package files (`core/IRS/CLAUDE.md`, `core/DDS/CLAUDE.md`, `core/connections/CLAUDE.md`, `gsim/CLAUDE.md`) add only package-specific rules; cross-cutting rules live here once.

## Core Commands

- Test suite (from repo root): `.venv/Scripts/python.exe -m pytest` -- `pytest.ini` sets `pythonpath = .`; see `core/tests/README.md`.
- Always use the repo-root `.venv` interpreter, not bare `python`. Only the installer build uses `.venv3.10`.
- Never put `core/` itself on `sys.path`; only the repo root. `core` is an ordinary package and its imports are absolute through it.
- Import-boundary check -- IRS: `grep -rn "^from core\.\(annotations\|connections\|tools\)\|^import core\.\(annotations\|connections\|tools\)" core/IRS --include=*.py` must print nothing.
- Import-boundary check -- IRS structures files: `grep -rn "core\.IRS" core/IRS/Structures --include=*.py` must print nothing.
- Import-boundary check -- DDS: `grep -rn "^from core\.\(annotations\|connections\|tools\|IRS\)\|^import core\.\(annotations\|connections\|tools\|IRS\)" core/DDS --include=*.py` must print nothing.
- Run these greps whenever you touch an import under `core/IRS` or `core/DDS`.

## Architecture & Tech Stack

- Dependency graph, one direction only: `IRS` and `DDS` are standalone -> `core` (IRS + DDS + connections + tools + `core/annotations.py`) -> `gsim`.
- `core/IRS`: binary parsing/serialization engine. Imports only itself and third-party packages (`beartype`). Its own type vocabulary is `core/IRS/annotations.py`.
- `core/DDS`: topic-based counterpart; no type registry and no engine. Holds type modules, the `DdsUnit` Interface vocabulary and QoS/security data. The Interface is a routing contract, not a type registry.
- `core/DDS/__init__.py` and `interface.py` must not import `rti`, so `core.DDS` stays importable without Connext. `core.connections.dds_config` loads an Interface; the arrow points from `connections` into `DDS`, never back.
- `core/annotations.py` re-exports IRS names so `connections` and `tools` keep one import; the arrow points from it into IRS only.
- If IRS or DDS needs something from `connections`, `tools` or `core/annotations.py`, move the shared piece into IRS/DDS. Never add the reverse import.
- `gsim` depends on `core` as a whole, only through `gsim/core_gateway/`, and never modifies `core` (see `gsim/CLAUDE.md`).
- IRS has two spellings, `core.IRS.x` and `IRS.x`; `core/IRS/_alias.py` makes them one module object at every depth. Two copies fail silently (empty registry, `isinstance` misses), and `_alias.py` raises on a genuine second copy.
- Structures files are plugins that consume IRS, not IRS internals. A `Structures` entry is either a `.py` path anywhere on disk (loaded from that path) or a dotted name for a module under `core/IRS/Structures/`. Never branch import behaviour on file location.
- Classes shared across an Interface/structures file and application code must be the same objects: use absolute imports and one module object per file (same failure class as `_alias.py`).

## Code Style & Constraints

- Fail loudly at the point of the mistake. Our own bug raises immediately; a third-party fault (bad inbound message) is logged and dropped without killing the link.
- Comments explain *why* only, and only where non-obvious. No speculative abstraction.
- IRS structures files spell the import `IRS.x`, never `core.IRS.x`.
- Everything inside `core` spells IRS as `core.IRS.x`, and uses `from core.annotations import *` for the shared vocabulary.
- Verify claims about `core` behaviour by running real code against it, not by reading it.
- Keep each fact in one file; point to the owning file instead of restating it.

## Reference Codebases

- Dependency boundaries: `core/IRS/_alias.py`, `core/IRS/annotations.py`, `core/annotations.py`.
- IRS architecture: `core/IRS/ARCHITECTURE.md`.
- Structures-file examples: `core/IRS/Structures/Tiful/tiful_to_dtu.py`, `core/IRS/Structures/Test/test_messages.py`.
- DDS Interface example: `core/DDS/Interfaces/Example/example_interface.py`.
- Test layout and fixtures: `core/tests/README.md`, `core/tests/conftest.py`.
