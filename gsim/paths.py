"""
The one place that knows where GSim's files are, in a checkout and installed.

In a checkout every path is derived from the repo root and everything is
writable. The MSI-installed app breaks both of those assumptions, in two
different ways, which is the entire reason this module exists:

* **The bundle is not the source tree.** PyInstaller unpacks (onedir: lays out)
  everything under ``sys._MEIPASS``, so `Path(__file__).parent` inside a frozen
  module points at a notional path in the bundle, not at `<repo-root>/gsim`.
  Data files are only there if `GSim.spec` put them there, at the layout this
  module expects -- the two files are a pair, and `GSim.spec` says so.
* **The install directory is read-only.** A per-machine install lands in
  `C:\Program Files\GSim`, where a non-elevated user cannot write. Saving a
  session or dropping in a new structures file has to go somewhere else, so
  when frozen those two directories move to `%LOCALAPPDATA%\GSim` and are
  seeded once from the copies that shipped in the bundle.

Nothing here imports `core` (or anything else in the repo) -- it is stdlib only
and safe to import from `gsim/api/*` as well as from `__main__`.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: True in the PyInstaller-built app, False when run from a checkout.
FROZEN = bool(getattr(sys, "frozen", False))

#: Root of everything that shipped read-only: `sys._MEIPASS` when frozen (the
#: `_internal` directory beside GSim.exe in a onedir build), the repo root
#: otherwise -- `gsim/paths.py`, so two parents up.
BUNDLE_ROOT = (
    Path(sys._MEIPASS).resolve() if FROZEN            # noqa: SLF001 -- PyInstaller's documented API
    else Path(__file__).resolve().parent.parent
)

#: The built React bundle FastAPI mounts at "/" (`api/app.py`). Vite writes it
#: to `gsim/web/ui_dist` (not `dist/`, which is PyInstaller's) and the spec
#: copies it to the SAME relative path inside the bundle, so this one
#: expression is correct in both worlds.
WEB_DIST = BUNDLE_ROOT / "gsim" / "web" / "ui_dist"

#: Native window / taskbar icon -- see `__main__.ICON` for why it must be a
#: real .ico container and not a PNG.
ICON = BUNDLE_ROOT / "gsim" / "assets" / "gsim.ico"

#: The read-only copies that ship with the app. Only used to seed the writable
#: directories below on first run; nothing reads from them afterwards.
BUNDLED_STRUCTURES = BUNDLE_ROOT / "core" / "IRS" / "Structures"
BUNDLED_CONFIGS = BUNDLE_ROOT / "configs" / "GsimConfig"


def _user_data_root() -> Path:
    """`%LOCALAPPDATA%\GSim`, falling back to `~/.gsim` if the variable is
    missing (a service account, or a non-Windows host running the frozen app)."""
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "GSim" if base else Path.home() / ".gsim"


#: Where the file pickers (native dialogs in `__main__.py`, the in-app browser
#: in `api/routes/files.py`) open, and where Save writes by default. Installed,
#: these are per-user and writable; in a checkout they stay exactly where they
#: have always been, so nothing about developing GSim changes.
if FROZEN:
    DATA_ROOT = _user_data_root()
    STRUCTURES_DIR = DATA_ROOT / "Structures"
    CONFIGS_DIR = DATA_ROOT / "GsimConfig"
else:
    DATA_ROOT = BUNDLE_ROOT
    STRUCTURES_DIR = BUNDLED_STRUCTURES
    CONFIGS_DIR = BUNDLED_CONFIGS


def _seed(source: Path, target: Path) -> None:
    """Copy `source` to `target` once, and only if `target` does not exist yet.

    Deliberately all-or-nothing rather than a per-file merge: once the user owns
    the directory, a later version of GSim silently re-adding a structures file
    they deleted (or overwriting one they edited) would be worse than shipping
    an update they have to copy in by hand.
    """
    if not source.is_dir() or target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    except OSError:
        pass          # first run on a locked-down profile; the pickers still open elsewhere


def seed_user_data() -> None:
    """Populate the per-user data directory on first run. A no-op in a
    checkout, where the "user" directories *are* the repo's own. Called from
    `create_app()`, so both the desktop shell and `--server` get it."""
    if not FROZEN:
        return
    _seed(BUNDLED_STRUCTURES, STRUCTURES_DIR)
    _seed(BUNDLED_CONFIGS, CONFIGS_DIR)
