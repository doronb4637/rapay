"""
The ONE place that teaches Python where `core` lives.

`core` is an ordinary Python package (it has its own `core/__init__.py`), and
every internal module imports absolutely through it --
`from core.IRS.irs_parser import ...`, `from core.tools.general import ...`,
`from core.annotations import *`. So it is the REPO ROOT that has to be on
`sys.path` for `import core` (or anything under it) to resolve at all --
`core/` itself being on `sys.path` would not help; there is no top-level `IRS`
or `tools` package any more, only `core.IRS`/`core.tools`. This mirrors
`core/connections/test_framework.py`'s own bootstrap (it inserts its
grandparent, i.e. this same repo root) and the repo-root `pytest.ini`'s
`pythonpath = .`.

Importing this module is idempotent and has no other side effects. Every other
module in `gsim.core_gateway` imports this first; nothing outside
`gsim.core_gateway` imports `core` at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

#: <repo-root>/core -- gsim/ is a sibling of core/, so two parents up from here.
#: Filesystem-only after the migration (STRUCTURES_DIR, CONFIGS_DIR, ...);
#: nothing inserts this path itself into `sys.path` any more.
CORE_ROOT = Path(__file__).resolve().parent.parent.parent / "core"
#: The repo root -- CORE_ROOT's parent -- is what actually needs to be
#: importable, since `core` is now a package rooted there rather than a
#: `sys.path` entry itself.
REPO_ROOT = CORE_ROOT.parent


def ensure_core_importable() -> Path:
    """Put the repo root on `sys.path` once, so `import core...` resolves.
    Returns `CORE_ROOT`, for diagnostics (filesystem paths, error messages)."""
    if not CORE_ROOT.is_dir():
        raise RuntimeError(
            f"core package not found at {CORE_ROOT}. GSim expects to live "
            f"beside it, as <repo-root>/gsim next to <repo-root>/core."
        )
    repo_root_str = str(REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return CORE_ROOT


ensure_core_importable()
