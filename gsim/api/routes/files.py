"""
Filesystem browsing for the UI's file picker.

Exists because the native dialogs in `gsim/__main__.py` are only reachable from
the PyWebView desktop shell. In `--server` mode the UI is an ordinary browser
tab, where `window.pywebview` does not exist and a plain `<input type="file">`
is no help: browsers deliberately withhold the real path, and every path here
is handed straight to core (`Structures` entries are resolved by
`tools.general.resolve_module_name`) or written to disk. So the server -- which
is running on the user's own machine and already has the filesystem -- answers
the browsing instead.

Scope note: this deliberately does NOT sandbox to a root directory. GSim binds
loopback only and runs as the user, who can already open any of these files in
any editor; a jail would mainly stop them pointing at a structures file that
legitimately lives outside the repo, which is a supported case
(`resolve_module_name` has a whole branch for external files). What it does
enforce is that writes look like writes: `save` refuses a directory and creates
only the parent it was given.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

#: Where each kind of picker starts. Configs live in a directory GSim owns
#: (`<repo-root>/configs/GsimConfig`, or `%LOCALAPPDATA%\GSim\...` installed);
#: structures live wherever the USER keeps them, so their picker opens at the
#: last place one was browsed from and remembers each new one. The native
#: dialogs in `gsim/__main__.py` call the same functions, so desktop and browser
#: modes land in the same place.
from gsim.paths import CONFIGS_DIR, remember_structures_dir, structures_start_dir

router = APIRouter(prefix="/api/files", tags=["files"])


class SaveFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    contents: str


def _entry(path: Path) -> dict[str, Any]:
    """One row in a listing. `is_dir` is resolved once here rather than by the
    client, which cannot tell a directory from an extension-less file."""
    try:
        is_dir = path.is_dir()
    except OSError:
        is_dir = False
    return {"name": path.name, "path": str(path), "is_dir": is_dir}


@router.get("/roots")
def roots() -> dict[str, Any]:
    """The directories the pickers open at.

    `configs` is created on demand: Save is the first thing that needs it, and
    a picker that opens at a path which does not exist would silently fall back
    to somewhere arbitrary. `structures` is never created -- it is wherever the
    user's own files already are, and GSim inventing a directory for them is the
    arrangement this replaced.
    """
    try:
        CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass          # read-only checkout; the picker still opens elsewhere
    return {
        "structures": str(structures_start_dir()),
        "configs": str(CONFIGS_DIR),
        "home": str(Path.home()),
        "separator": os.sep,
    }


@router.get("/list")
def list_directory(path: str, suffix: str | None = None) -> dict[str, Any]:
    """Directories plus files matching `suffix`, sorted folders-first by name.

    Filtering server-side keeps the picker honest about what is selectable --
    a `.py` picker that listed `.json` files would let the user choose one and
    only fail later, at import.
    """
    directory = Path(path).expanduser()
    if not directory.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"not a directory: {directory}")

    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    try:
        for child in directory.iterdir():
            # Skip dotfiles and __pycache__: never what the user is looking for
            # here, and they bury the handful of entries that are.
            if child.name.startswith(".") or child.name == "__pycache__":
                continue
            entry = _entry(child)
            if entry["is_dir"]:
                directories.append(entry)
            elif suffix is None or child.name.lower().endswith(suffix.lower()):
                files.append(entry)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    # Remember where structures are being browsed from, so the next pick opens
    # there rather than back at a default the user navigated away from on the
    # first one. Keyed on the suffix because it is the only thing distinguishing
    # this from the configs picker, which has a directory of its own.
    if suffix and suffix.lower() == ".py":
        remember_structures_dir(directory)

    by_name = lambda entry: entry["name"].lower()      # noqa: E731
    parent = directory.parent
    return {
        "path": str(directory),
        # None at a drive/filesystem root, so the client can hide "up".
        "parent": None if parent == directory else str(parent),
        "entries": sorted(directories, key=by_name) + sorted(files, key=by_name),
    }


@router.get("/read")
def read_file(path: str) -> dict[str, str]:
    """Text contents of one file -- how the browser-mode Load gets a config it
    can only name by path."""
    target = Path(path).expanduser()
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"not a file: {target}")
    try:
        return {"path": str(target), "contents": target.read_text(encoding="utf-8")}
    except (OSError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/save")
def save_file(request: SaveFileRequest) -> dict[str, str]:
    """Write text to a chosen path -- the browser-mode Save, which otherwise
    could only trigger a download into the browser's own folder."""
    target = Path(request.path).expanduser()
    if target.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"{target} is a directory, not a file")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(request.contents, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"path": str(target)}
