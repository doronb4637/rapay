"""
Desktop entry point: `python -m gsim`.

Runs Uvicorn on a loopback port in a daemon thread and opens a native webview
at it -- one process, no Node runtime, no IPC bridge. The API is a normal HTTP
server the whole time, so external programs can hook into core through it
exactly as they would if GSim were running headless (`--server`).
"""
from __future__ import annotations

import argparse
import socket
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import uvicorn

from gsim.api.app import create_app
from gsim.core_gateway.bootstrap import CORE_ROOT

#: Native window / taskbar icon. Must be a real .ico container -- pywebview's
#: WinForms backend loads it via `System.Drawing.Icon(path)`, which rejects a
#: bare PNG ("Argument 'picture' must be a picture that can be used as an
#: Icon.") even though the file renders fine everywhere else. Regenerate with
#: `python gsim/assets/make_icon.py` (also writes gsim.png alongside it).
ICON = Path(__file__).resolve().parent / "assets" / "gsim.ico"

#: Where the "browse for an IRS structures file" dialog starts, when it exists
#: -- falls back to the OS default (an empty directory argument) otherwise.
STRUCTURES_DIR = CORE_ROOT / "IRS" / "Structures"


class Api:
    """Bound to the window as `js_api`; called from the UI as
    `window.pywebview.api.<method>()`. Only reachable in the desktop app --
    `--server` mode has no window and the React build never calls these
    unless `window.pywebview` exists.
    """

    def browse_structures_file(self) -> str | None:
        """Native "open file" dialog for picking an IRS structures module.
        Opens at STRUCTURES_DIR when present; the picked path is passed
        through as-is and loaded directly by `tools.general.import_modules`,
        which accepts a real filesystem path as well as a dotted module name.
        """
        import webview

        directory = str(STRUCTURES_DIR) if STRUCTURES_DIR.is_dir() else ""
        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN,
            directory=directory,
            file_types=("Python files (*.py)", "All files (*.*)"),
        )
        return result[0] if result else None

    def save_config_file(self, contents: str) -> bool:
        """Native "save file" dialog for the Sidebar's Save button -- writes
        `contents` (a JSON string the caller has already formatted) to the
        chosen path. Returns False if the user cancelled, so the caller can
        tell "saved" from "nothing happened" without a raised exception either
        way -- cancelling a save is not an error.
        """
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename="gsim-connections.json",
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not result:
            return False
        target = result[0] if isinstance(result, (list, tuple)) else result
        Path(target).write_text(contents, encoding="utf-8")
        return True

    def load_config_file(self) -> str | None:
        """Native "open file" dialog for the Sidebar's Load button. Returns the
        file's text content directly, not a path -- unlike
        `browse_structures_file`, the caller here only ever wants the JSON, so
        there is no reason to make it round-trip through a second call.
        """
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN,
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not result:
            return None
        return Path(result[0]).read_text(encoding="utf-8")


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
