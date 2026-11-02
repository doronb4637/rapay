"""
`core.DDS` -- DDS topic types (`pyIdl/`), the vocabulary DDS Interface files
are written in (`interface.py`), and `register_idl_folder`, which makes the
bare imports between rtiddsgen's files resolve (`_idl_alias.py`).

Must never import `rti`: only the `pyIdl` modules themselves need Connext,
so `core.DDS` stays importable in a process without it.
"""
from ._idl_alias import register_idl_folder
from .interface import DdsUnit

__all__ = ["DdsUnit", "register_idl_folder"]
