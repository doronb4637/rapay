"""
Desktop entry point: `python -m gsim`.

Runs Uvicorn on a loopback port in a daemon thread and opens a native webview
at it -- one process, no Node runtime, no IPC bridge. The API is a normal HTTP
server the whole time, so external programs can hook into core through it
exactly as they would if GSim were running headless (`--server`).
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import uvicorn

from gsim.api.app import create_app

#: Native window / taskbar icon. Must be a real .ico container -- pywebview's
#: WinForms backend loads it via `System.Drawing.Icon(path)`, which rejects a
#: bare PNG ("Argument 'picture' must be a picture that can be used as an
#: Icon.") even though the file renders fine everywhere else. Regenerate with
#: `python gsim/assets/make_icon.py` (also writes gsim.png alongside it).
#:
#: Where each dialog starts, when the directory exists -- falls back to the OS
#: default (an empty directory argument) otherwise. Deliberately the same
#: constants the server-side picker uses (`gsim/api/routes/files.py`), so the
#: desktop and browser modes open in the same place; `gsim/paths.py` is what
#: makes all three correct in a checkout AND in the installed app.
from gsim.paths import (
    CONFIGS_DIR, DATA_ROOT, ICON, remember_structures_dir, structures_start_dir)

#: Where a windowed build's stdout/stderr go -- see `_attach_streams`.
LOG_FILE = DATA_ROOT / "logs" / "gsim.log"


def _start_dir(directory: Path, create: bool = False) -> str:
    """A dialog's starting directory, or "" to let the OS choose. `create` for
    the save path: a Save dialog pointed at a directory that does not exist
    silently opens somewhere arbitrary instead."""
    if create:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return str(directory) if directory.is_dir() else ""


class Api:
    """Bound to the window as `js_api`; called from the UI as
    `window.pywebview.api.<method>()`. Only reachable in the desktop app --
    `--server` mode has no window and the React build never calls these
    unless `window.pywebview` exists.
    """

    def browse_structures_file(self) -> str | None:
        """Native "open file" dialog for picking an IRS structures module.

        Opens wherever a structures file was last picked from, and remembers
        each new directory. A structures file can live anywhere on the machine
        -- the picked path is passed through as-is and loaded from that path by
        `tools.general.import_modules`.
        """
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN,
            directory=_start_dir(structures_start_dir()),
            file_types=("Python files (*.py)", "All files (*.*)"),
        )
        if not result:
            return None
        remember_structures_dir(Path(result[0]).parent)
        return result[0]

    def save_config_file(self, contents: str) -> str | bool:
        """Native "save file" dialog for the Save button -- writes `contents`
        (a JSON string the caller has already formatted) to the chosen path.

        Returns the path written, or False if the user cancelled, so the caller
        can tell "saved" from "nothing happened" without a raised exception
        either way -- cancelling a save is not an error. The PATH rather than a
        bare True because the title bar names the file this session is saved
        to, and this dialog is the only thing that knows which one was picked.
        Both forms are truthy and falsy in the same places, so a caller testing
        the old boolean contract is unaffected.
        """
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.SAVE,
            directory=_start_dir(CONFIGS_DIR, create=True),
            save_filename="gsim-connections.json",
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not result:
            return False
        target = result[0] if isinstance(result, (list, tuple)) else result
        Path(target).write_text(contents, encoding="utf-8")
        return str(target)

    def load_config_file(self) -> str | None:
        """Native "open file" dialog for the Sidebar's Load button. Returns the
        file's text content directly, not a path -- unlike
        `browse_structures_file`, the caller here only ever wants the JSON, so
        there is no reason to make it round-trip through a second call.
        """
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN,
            directory=_start_dir(CONFIGS_DIR, create=True),
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not result:
            return None
        return Path(result[0]).read_text(encoding="utf-8")


def _attach_streams() -> None:
    """Give the process real `sys.stdout`/`sys.stderr` when Windows has not.

    A PyInstaller **windowed** build starts with no console and both streams set
    to None. That is not merely "no logs": uvicorn configures logging from a
    dict naming `ext://sys.stderr`, so `logging.config.dictConfig` raises before
    the server ever binds and the app exits with status 1 the instant it is
    double-clicked -- silently, because the thing that would have printed the
    traceback is the very stream that is missing. It cost the first packaged
    build, which looked like "the .exe does nothing".

    So point them at a log file under the per-user data directory (the install
    directory is read-only), which doubles as the only diagnostic an installed
    user can send back, and fall back to the null device if even that cannot be
    opened. `line_buffering` so a crash does not lose the lines that explain it.
    """
    if sys.stderr is not None and sys.stdout is not None:
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        stream = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr = stream


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_up(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("GSim API did not come up in time")


def main() -> None:
    # Before argparse: a usage error also writes to the stream that is missing.
    _attach_streams()

    parser = argparse.ArgumentParser(prog="gsim")
    parser.add_argument("--port", type=int, default=None, help="default: an OS-assigned free port")
    parser.add_argument("--server", action="store_true",
                        help="run headless (API only), for external clients or development")
    args = parser.parse_args()

    port = args.port or (8765 if args.server else _free_port())

    if args.server:
        uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="info")
        return

    import webview  # imported lazily so --server needs no GUI stack

    server = threading.Thread(
        target=uvicorn.run,
        args=(create_app(),),
        kwargs={"host": "127.0.0.1", "port": port, "log_level": "warning"},
        daemon=True,
    )
    server.start()
    _wait_until_up(port)
    webview.create_window("GSim — Generic Simulator", f"http://127.0.0.1:{port}",
                          width=1440, height=900, x=100, y=50, js_api=Api())
    # Blocks until the window closes; the daemon server thread then dies with
    # the process, and the app's lifespan shutdown tears down every connection.
    # `icon` replaces the interpreter's default Python logo in the title bar
    # and taskbar (pywebview >= 5).
    webview.start(icon=str(ICON) if ICON.is_file() else None)


if __name__ == "__main__":
    main()
