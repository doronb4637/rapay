import json
from pathlib import Path
from typing import Any

from core.annotations import OpCode


def read_json_data(file_path: str) -> dict[str, Any]:
    """Read and parse a JSON file, resolving `file_path` against the calling
    project's root first.
    """
    given = Path(file_path)
    from_root = Path.cwd() / given
    resolved = from_root if from_root.is_file() else given
    if not resolved.is_file():
        raise FileNotFoundError(
            f"could not find {file_path!r} -- tried {from_root} and {given}."
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
