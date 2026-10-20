import json
from pathlib import Path
from typing import Any
import sys

from core.annotations import OpCode


def _find_caller_project_root() -> Path:
    """The root of the project whose script is running, which may be a project
    using this one as a package: walk up from `__main__`'s file to a marker.
    """
    main_file = getattr(sys.modules.get("__main__"), "path", None)
    current = Path(main_file).resolve().parent if main_file else Path.cwd()
    markers = {"Pipfile", "Pipfile.lock", ".git", "pyproject.toml", "setup.py", "requirements.txt"}
    for parent in [current, *current.parents]:
        if any((parent / marker).exists() for marker in markers):
            return parent
    return current


PROJECT_ROOT = _find_caller_project_root()


def resolve_project_path(file_path: str | Path) -> Path:
    """`file_path` taken from the calling project's root if it exists there,
    otherwise as given (absolute, or relative to the cwd).
    """
    given = Path(file_path)
    from_root = PROJECT_ROOT / given
    return (from_root if from_root.exists() else given).resolve()


def read_json_data(file_path: str) -> dict[str, Any]:
    """Read and parse a JSON file -- see `resolve_project_path`."""
    resolved = resolve_project_path(file_path)
    if not resolved.is_file():
        raise FileNotFoundError(
            f"could not find {file_path!r} -- tried {PROJECT_ROOT / file_path} and {file_path}."
        )
    with resolved.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_unit_config(file_path: str) -> dict[str, Any]:
    """A unit configuration is a plain JSON file -- see `read_json_data`."""
    return read_json_data(file_path)


def read_message_data(path: str) -> tuple[OpCode, dict]:
    """From the JSON at `path`, the `(opCode, setData)` pair it declares."""
    data = read_json_data(path)
    return data["opCode"], data["setData"]
