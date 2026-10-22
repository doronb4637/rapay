"""
`core.DDS` -- DDS topic types (`pyIdl/`) and the vocabulary DDS Interface
files are written in (`interface.py`).

Must never import `rti`: only the `pyIdl` modules themselves need Connext,
so `core.DDS` stays importable in a process without it.
"""
from .interface import DdsUnit

__all__ = ["DdsUnit"]
