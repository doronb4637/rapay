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

import json
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

#: The read-only copy that ships with the app. Only used to seed the writable
#: directory below on first run; nothing reads from it afterwards.
BUNDLED_CONFIGS = BUNDLE_ROOT / "configs" / "GsimConfig"


def _user_data_root() -> Path:
    """`%LOCALAPPDATA%\GSim`, falling back to `~/.gsim` if the variable is
    missing (a service account, or a non-Windows host running the frozen app)."""
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "GSim" if base else Path.home() / ".gsim"


#: Where session configs are saved. Installed, per-user and writable; in a
#: checkout exactly where they have always been, so nothing about developing
#: GSim changes. Session files are GSim's OWN data -- unlike structures, below.
if FROZEN:
    DATA_ROOT = _user_data_root()
    CONFIGS_DIR = DATA_ROOT / "GsimConfig"
else:
    DATA_ROOT = BUNDLE_ROOT
    CONFIGS_DIR = BUNDLED_CONFIGS


#: There is deliberately no STRUCTURES_DIR. **Structures files are the user's
#: data, not GSim's**: they describe the units being simulated, they are
#: generated and edited constantly, and tying them to the app meant a new GSim
#: version for every change to a message layout. So none ship in the bundle,
#: none are seeded into `%LOCALAPPDATA%`, and a `.py` anywhere on the machine is
#: a valid pick -- `core.tools.general` loads a structures file from the path it
#: is given, wherever that points.
#:
#: (An install from before this change may still have a seeded
#: `%LOCALAPPDATA%\GSim\Structures`. It is left alone rather than deleted --
#: those are the user's files now -- it simply stops being special.)
#:
#: All the pickers need is somewhere to OPEN, which is a UI convenience rather
#: than a location the app depends on: the last directory a structures file was
#: browsed from, then a sensible default.
_UI_STATE_FILE = _user_data_root() / "ui-state.json"

#: Where the structures picker opens when nothing has been browsed yet. A
#: checkout keeps pointing at the repo's own `Structures` folder -- that is the
#: convenience it was always for -- while the installed app, which now ships no
#: structures at all, opens at the user's home directory.
DEFAULT_STRUCTURES_DIR = (
    Path.home() if FROZEN else BUNDLE_ROOT / "core" / "IRS" / "Structures"
)


def _read_ui_state() -> dict:
    try:
        return json.loads(_UI_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}          # absent, unreadable, or corrupt: fall back to defaults


def structures_start_dir() -> Path:
    """Where a structures file dialog should open.

    Purely a convenience, so nothing here is allowed to fail the caller: a
    remembered directory that has since been deleted or moved falls through to
    the default the same way a missing state file does.
    """
    remembered = _read_ui_state().get("structures_dir")
    if remembered:
        candidate = Path(remembered)
        if candidate.is_dir():
            return candidate
    return DEFAULT_STRUCTURES_DIR


def remember_structures_dir(directory: Path | str) -> None:
    """Record where the user last browsed for a structures file.

    Best-effort by design -- a read-only profile must cost a failed pick
    nothing, so every error here is swallowed.
    """
    try:
        state = _read_ui_state()
        state["structures_dir"] = str(directory)
        _UI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _UI_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


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
    checkout, where the "user" directory *is* the repo's own. Called from
    `create_app()`, so both the desktop shell and `--server` get it.

    Only session configs are seeded. Structures deliberately are not shipped at
    all any more -- see the note above `_UI_STATE_FILE`.
    """
    if not FROZEN:
        return
    _seed(BUNDLED_CONFIGS, CONFIGS_DIR)
