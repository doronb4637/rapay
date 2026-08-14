"""
The ONE place that teaches Python where `core` lives.

`core`'s internal imports are absolute and rooted at `core/` itself
(`from IRS.irs_parser import ...`, `from tools.general import ...`,
`from annotations import *`), so `core/` -- not the repo root -- has to be on
`sys.path`. That is exactly what `core/connections/test_framework.py` does for
itself and what the repo-root `pytest.ini` does via `pythonpath = core`.

Importing this module is idempotent and has no other side effects. Every other
module in `gsim.core_gateway` imports this first; nothing outside
`gsim.core_gateway` imports `core` at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

#: <repo-root>/core -- gsim/ is a sibling of core/, so two parents up from here.
CORE_ROOT = Path(__file__).resolve().parent.parent.parent / "core"


def ensure_core_importable() -> Path:
    """Put `core/` on `sys.path` once. Returns the path, for diagnostics."""
    if not CORE_ROOT.is_dir():
        raise RuntimeError(
            f"core package not found at {CORE_ROOT}. GSim expects to live "
            f"beside it, as <repo-root>/gsim next to <repo-root>/core."
        )
    core_str = str(CORE_ROOT)
    if core_str not in sys.path:
        sys.path.insert(0, core_str)
    return CORE_ROOT


ensure_core_importable()
