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
- Validate a structures file or DDS Interface (offline only; never imported by runtime code): `.venv/Scripts/python.exe -m core.tools.validate_interface <path.py | dotted.name> [...]`.

## Architecture & Tech Stack

- Dependency graph, one direction only: `IRS` and `DDS` are standalone -> `core` (IRS + DDS + connections + tools + `core/annotations.py`) -> `gsim`.
- `core/IRS`: binary parsing/serialization engine. Imports only itself and third-party packages (`beartype`). Its own type vocabulary is `core/IRS/annotations.py`.
- `core/DDS`: topic-based counterpart; no type registry and no engine. Holds type modules, the `DdsUnit` Interface vocabulary and QoS/security data. The Interface is a routing contract, not a type registry.
- `core/DDS/__init__.py` and `interface.py` must not import `rti`, so `core.DDS` stays importable without Connext. `core.connections.dds_config` loads an Interface; the arrow points from `connections` into `DDS`, never back.
- `core/annotations.py` re-exports IRS names so `connections` and `tools` keep one import; the arrow points from it into IRS only.
- If IRS or DDS needs something from `connections`, `tools` or `core/annotations.py`, move the shared piece into IRS/DDS. Never add the reverse import.
- `gsim` depends on `core` as a whole, only through `gsim/core_gateway/`, and never modifies `core` (see `gsim/CLAUDE.md`).
- IRS has two spellings, `core.IRS.x` and `IRS.x`; `core/IRS/_alias.py` makes them one module object at every depth. Two copies fail silently (`isinstance`/`is` checks against message classes miss), and `_alias.py` raises on a genuine second copy.
- Structures files are plugins that consume IRS, not IRS internals. A `Structures` entry is either a `.py` path anywhere on disk (loaded from that path) or a dotted name for a module under `core/IRS/Structures/`. Never branch import behaviour on file location.
- Classes shared across an Interface/structures file and application code must be the same objects: use absolute imports and one module object per file (same failure class as `_alias.py`).

## Code Style & Constraints

- **Readability & Simplicity Above All:** Write the most direct, flat, and readable code possible. Cleverness, nested abstractions, and premature optimizations are bugs.
- **Zero-Speculative Abstraction:** Never build helper functions for single-use logic, generalized factories, or future-proofing layers for hypothetical requirements. Write only what is required *today*.
- **Comments & Docstrings Rules:** 
  - **Inline comments:** Strictly limited to 1 line (max 2 lines on rare, special occasions). They must explain *why* (non-obvious architecture, API quirks, performance hacks), never *what*. If code needs a long explanation, refactor the code instead.
  - **Public API Docstrings:** Mandatory to be very clear, comprehensive, and detailed on how to use public classes and functions.
  - **Private / Non-Public Docstrings:** Must be concise and clearly state the function or class's internal purpose, without needing heavy usage guides.
- **Fail Loudly:** Fail loudly at the point of the mistake. Our own bug raises immediately; a third-party fault (bad inbound message) is logged and dropped without killing the link.
- **Import Rules:** 
  - IRS structures files spell the import `IRS.x`, never `core.IRS.x`.
  - Everything inside `core` spells IRS as `core.IRS.x`, and uses `from core.annotations import *` for the shared vocabulary.
- **Verification & Organization:**
  - Verify claims about `core` behaviour by running real code against it, not by reading it.
  - Keep each fact in one file; point to the owning file instead of restating it.
- **No Redundant Hot-Path Validation:**
  - Never re-validate configuration or state variables inside high-frequency runtime functions (e.g., message packing/sending loops) if they are already validated once at startup/load time. Avoid redundant checks that add runtime overhead and dead code.
- **Structures Files Are Correct By Definition:**
  - Everything in a structures/Interface file (`@Definition` messages, `PAIRS`, unit codes, opcodes, layouts) is assumed perfect. It is the author's responsibility; the library does not validate it.
  - Never add a check, guard, `ValueError` or defensive branch for the content of a structures file or an `Interface`, anywhere: not at send, not at receive, not at load. If a file is wrong, the failure belongs to the file, not to code written to catch it.
  - Only genuinely external input is checked: the type of what a caller passes (e.g. `send_message` takes a message instance) and inbound payload bytes.
  - Content checks for these files belong only in `core/tools/validate_interface.py`.

## Reference Codebases

- Dependency boundaries: `core/IRS/_alias.py`, `core/IRS/annotations.py`, `core/annotations.py`.
- IRS architecture: `core/IRS/ARCHITECTURE.md`.
- Structures-file examples: `core/IRS/Structures/Tiful/tiful_to_dtu.py`, `core/IRS/Structures/Test/test_messages.py`.
- DDS Interface example: `core/DDS/Interfaces/Example/example_interface.py`.
- Test layout and fixtures: `core/tests/README.md`, `core/tests/conftest.py`.
