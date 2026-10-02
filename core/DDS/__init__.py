"""
`core.DDS` -- DDS topic types (`idl_types/`) and the vocabulary DDS Interface
files are written in (`interface.py`).

Must never import `rti`: only the `idl_types` modules themselves need Connext,
so `core.DDS` stays importable in a process without it.
"""
from .interface import INTERFACE_FORMAT, DdsUnit

__all__ = ["DdsUnit", "INTERFACE_FORMAT"]
