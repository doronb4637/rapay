"""
Makes `IRS.x` and `core.IRS.x` the SAME module object, at every depth.

## Why this exists

`IRS` lives at `core/IRS/`, so its real name is `core.IRS`. But a structures
file is written against IRS as a standalone engine and spells its imports
`from IRS import *` -- which is the right spelling, both because IRS genuinely
depends on nothing else in this repo and because that is the only spelling that
survives copying `IRS/` out on its own.

Left alone, those two spellings are two different top-level names, so Python
builds two INDEPENDENT module objects: two `STRUCTURE_REGISTRY` dicts, two
`MessageMeta` metaclasses, two `Field`/`BitField` classes. That failure is
close to undebuggable, because nothing about it looks like an import problem:

  * a structures file prints a fully populated `STRUCTURE_REGISTRY` at its own
    end, while `irs_parser._get_message_class` reads an empty `{}` and raises
    `IRSNotFoundError`. The namespace KEY is correct in both -- `register_message`
    captures the module's real `__name__` via `sys._getframe`, which is the same
    string either way. Only the dict it landed in differs.
  * every `isinstance(field, Field)` check downstream misses, because the
    message's fields are instances of the other copy's classes. GSim's schema
    builder degrades to "No widget for this IRS field type" for every field of
    every message.

## What it does

Nothing at all when IRS is imported under its own name (a copied-out `IRS/`
with no `core` present) -- `install()` returns immediately. That case has one
module object already, and this file is inert weight that costs an import.

When IRS is imported as `core.IRS`, it points the name `IRS` at the module
objects that already exist under `core.IRS`, both eagerly for the package
itself and lazily, via a `sys.meta_path` finder, for every submodule.

The finder is the load-bearing half. Aliasing only `sys.modules["IRS"]` looks
sufficient and is not: `from IRS.REGISTRY import register_message` would then
find the aliased package, walk its `__path__` straight back into `core/IRS/`,
and EXECUTE `REGISTRY.py` a second time under the name `IRS.REGISTRY` -- the
identical two-registries bug wearing a different name. Nor is an eager loop
over already-imported submodules enough: `core/IRS/__init__.py` does not import
`irs_parser`, and `IRS.Structures.<pkg>.<file>` does not exist until a config
names it, so the mapping has to stay live rather than be snapshotted once.

Stdlib only (`sys`, `importlib`). Nothing here imports from `core`, so IRS's
"depends on nothing else in this repo" claim stays literally true.
"""
from __future__ import annotations

import importlib
import sys
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

#: The name a structures file spells, and the name IRS has when standalone.
_ALIAS_ROOT = "IRS"

#: Set once `install()` has done its work, so importing this module twice (or
#: re-importing `core.IRS` after a `sys.modules` cleanup in a test) cannot stack
#: a second finder on `sys.meta_path`.
_installed = False


class _AliasFinder(MetaPathFinder, Loader):
    """Resolves `IRS` / `IRS.*` to the already-loaded `<real_root>` / `<real_root>.*`.

    Both finder and loader, because there is nothing to load: `create_module`
    hands back the module object the real name already resolved to, and
    `exec_module` only tidies up after the import machinery. Re-executing the
    source is the very thing this class exists to prevent.
    """

    def __init__(self, real_root: str) -> None:
        self._real_root = real_root
        #: id(module) -> (__spec__, __loader__) as they were before the import
        #: machinery overwrote them, consumed by `exec_module`.
        self._originals: dict[int, tuple] = {}

    def _real_name(self, fullname: str) -> str:
        return self._real_root + fullname[len(_ALIAS_ROOT):]

    def find_spec(self, fullname: str, path=None, target=None) -> ModuleSpec | None:
        # Exact match or a true submodule -- never a merely similar top-level
        # name (`IRSTools`, `IRS_legacy`), which is why this is not a bare
        # `startswith("IRS")`.
        if fullname != _ALIAS_ROOT and not fullname.startswith(_ALIAS_ROOT + "."):
            return None
        return ModuleSpec(fullname, self, is_package=False)

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        # `import_module` rather than a `sys.modules` lookup: the real submodule
        # may genuinely not be imported yet (the first `from IRS.irs_parser
        # import ...` in a process), and letting the ordinary machinery load it
        # under its REAL name is exactly what keeps there being only one of it.
        module = importlib.import_module(self._real_name(spec.name))
        self._originals[id(module)] = (
            getattr(module, "__spec__", None), getattr(module, "__loader__", None))
        # Claim the alias now, so a partially-initialised module during a
        # circular import resolves to the same object rather than recursing.
        sys.modules[spec.name] = module
        return module

    def exec_module(self, module: ModuleType) -> None:
        """Already executed by `create_module`; only undo the machinery's damage.

        `_init_module_attrs` leaves `__name__` alone (it only fills attributes
        that are still `None`) -- which is the one that matters, since
        `REGISTRY._getting_struct_name` reads it to key the registry. But it
        assigns `__spec__`/`__loader__` unconditionally, and this is the very
        module object the REAL name is bound to, so it would be left claiming to
        be `IRS.REGISTRY` when it is `core.IRS.REGISTRY` -- misdirecting
        `importlib.reload` and pickling. Put the originals back.
        """
        original = self._originals.pop(id(module), None)
        if original is not None:
            module.__spec__, module.__loader__ = original


def install() -> None:
    """Point `IRS[.x]` at this package, unless it already IS `IRS`.

    Called from `core/IRS/__init__.py` before anything else in that package's
    body, so the alias is armed before any structures file can be imported.
    """
    global _installed
    if _installed:
        return

    real_root = __name__.partition(".")[0]
    if real_root == _ALIAS_ROOT:
        # Standalone IRS: one module object already, nothing to reconcile.
        _installed = True
        return

    package = sys.modules[__name__.rpartition(".")[0]]
    existing = sys.modules.get(_ALIAS_ROOT)
    if existing is not None and existing is not package:
        # A genuine second copy is already loaded -- the exact state this file
        # exists to prevent, and unfixable from here since its classes are
        # already built. Say so at the point of the mistake instead of leaving
        # an empty registry to be discovered three layers downstream.
        existing_file = getattr(existing, "__file__", "?")
        this_file = getattr(package, "__file__", "?")
        same_source = (
            "  Both names point at the SAME FILE, so it has been executed twice "
            "under two names -- almost always because "
            f"{real_root}/ is on sys.path (an IDE 'sources root') AND is also "
            "reachable as a package from the repo root."
            if existing_file == this_file else ""
        )
        raise ImportError(
            f"a second {_ALIAS_ROOT!r} module is already imported, which would give "
            f"this process two independent IRS registries and two sets of field "
            f"classes -- messages would register where nothing reads them, and "
            f"every isinstance() check against an IRS type would miss.\n"
            f"  already imported {_ALIAS_ROOT}: {existing_file}\n"
            f"  this one ({__name__.rpartition('.')[0]}): {this_file}\n"
            f"{same_source}\n"
            f"  Fix: import {real_root}.{_ALIAS_ROOT} before anything imports bare "
            f"{_ALIAS_ROOT}, or take {real_root}/ off sys.path (the repo root is what "
            f"belongs there)."
        )

    sys.modules[_ALIAS_ROOT] = package
    if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
        # PREPENDED, and it has to be. Appending does nothing: `IRS` in
        # `sys.modules` is this package, so `import IRS.REGISTRY` searches its
        # `__path__` -- which points at core/IRS/ -- and the ordinary
        # `PathFinder` (already on `sys.meta_path`) happily finds REGISTRY.py
        # there and executes it a second time under the name `IRS.REGISTRY`.
        # Winning the lookup is the entire mechanism.
        sys.meta_path.insert(0, _AliasFinder(f"{real_root}.{_ALIAS_ROOT}"))
    _installed = True
