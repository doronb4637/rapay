"""
Lets rtiddsgen's flat imports resolve inside a generated IDL folder, as ONE
module object per file.

## Why this exists

rtiddsgen writes the includes between its Python files as top-level imports --
`example_topics.py` says `from example_common import *` -- because it assumes
the folder it generated into is on `sys.path`.
Since Gsim imports an IDL file that exists out side if the project scope.
the bare import fails.

We also need to be carefull of importing a file under two diffrent 'MRO'
since then python see them and their inside object's as diffrent
and not compatebale code.

## What it does

A DDS Interface calls `register_idl_folder("..pyIdl", __package__)` before
importing its topics. From then on a bare `import example_common` resolves, via
a `sys.meta_path` finder

The finder sits at the END of `sys.meta_path`, so it only answers a bare name
nothing else on `sys.path` provides; it never takes over a stdlib or
third-party module. A generated file named like one is reported by
`core/tools/validate_interface.py`.

Bare names are global to the process, because that is how rtiddsgen spells
them. So two registered folders may not both contain a file of one name, and
one folder may not be registered under two package names (that would be two
copies of every class in it); both raise `ImportError` at registration.

Stdlib only, and never imports `rti`, so `core.DDS` stays importable without
Connext.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

#: Real package name of each registered IDL folder, keyed by the resolved folder.
_packages: dict[Path, str] = {}
#: Bare module name -> its real dotted name, for every `.py` in a registered folder.
IDL_MODULES: dict[str, str] = {}


class _IdlAliasFinder(MetaPathFinder, Loader):
    """Resolves a registered bare name to the module under its real dotted name.

    Both finder and loader, because there is nothing to load: `create_module`
    hands back the real module and `exec_module` only restores the `__spec__`
    and `__loader__` the import machinery overwrote on it (see `core/IRS/_alias.py`).
    """

    def __init__(self) -> None:
        #: id(module) -> (__spec__, __loader__) before the machinery overwrote them.
        self._originals: dict[int, tuple] = {}

    def find_spec(self, fullname: str, path=None, target=None) -> ModuleSpec | None:
        if fullname not in IDL_MODULES:
            return None
        return ModuleSpec(fullname, self, is_package=False)

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        module = importlib.import_module(IDL_MODULES[spec.name])
        self._originals[id(module)] = (module.__spec__, module.__loader__)
        sys.modules[spec.name] = module
        return module

    def exec_module(self, module: ModuleType) -> None:
        original = self._originals.pop(id(module), None)
        if original is not None:
            module.__spec__, module.__loader__ = original


def register_idl_folder(relative: str, package: str | None) -> None:
    """
    Make the bare imports between the rtiddsgen files in one folder resolve.

    Generated into every DDS Interface, before its topic imports, spelled with
    the same relative name as those imports:

        from core.DDS import DdsUnit, register_idl_folder
        register_idl_folder("..pyIdl", __package__)
        from ..pyIdl.example_topics import Status, Track

    Works the same whether the Interface was imported by dotted name or loaded
    by path, since `__package__` is whatever name it was loaded under. Calling
    it again for a folder already registered under that name does nothing.

    Application code must import the Interface before importing anything from
    the IDL folder; until then the folder's bare imports raise
    `ModuleNotFoundError`.

    Args:
        relative: the IDL folder as a relative package name, e.g. `"..pyIdl"`.
        package: the calling Interface's `__package__`.

    Raises:
        ImportError: the folder spans several directories, is already registered
            under another package name, or shares a file name with another
            registered folder.
    """
    name = importlib.util.resolve_name(relative, package)
    directories = list(importlib.import_module(name).__path__)
    if len(directories) != 1:
        raise ImportError(f"IDL package {name!r} spans several folders {directories}; "
                          f"its bare imports could resolve into any of them")
    folder = Path(directories[0]).resolve()

    known = _packages.get(folder)
    if known == name:
        return
    if known is not None:
        raise ImportError(
            f"IDL folder {folder} is already imported as {known!r} and now as {name!r}, "
            f"which would make two copies of every topic class in it. Name its Interface "
            f"by one spelling only (dotted in the config if the application imports it).")

    stems = {file.stem: f"{name}.{file.stem}" for file in folder.glob("*.py") if file.stem != "__init__"}
    shared = sorted(stems.keys() & IDL_MODULES.keys())
    if shared:
        raise ImportError(
            f"IDL folder {folder} and package {IDL_MODULES[shared[0]].rpartition('.')[0]!r} both have "
            f"{[f'{stem}.py' for stem in shared]}; rtiddsgen imports them by bare name, which can "
            f"only mean one folder per process.")

    _packages[folder] = name
    IDL_MODULES.update(stems)
    if not any(isinstance(finder, _IdlAliasFinder) for finder in sys.meta_path):
        sys.meta_path.append(_IdlAliasFinder())
