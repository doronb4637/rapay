import importlib
import importlib.machinery
import importlib.util
import re
import sys
from pathlib import Path

from core.annotations import *
from core.tools.file_functions import resolve_project_path

# A structures file is user data kept anywhere on disk, so a path entry is
# loaded from that path and never named as a member of a real package
# (e.g. `core.IRS.Structures.x`): Python would try to resolve that name and fail
# once the folder moves. A dotted entry is imported verbatim.

#: Root of the synthetic namespace path-picked structures files are imported
#: under. Lives only in `sys.modules`; below it, one package per real folder.
_SYNTHETIC_ROOT = "irs_structures"

_NOT_IDENTIFIER = re.compile(r"\W")


def names_a_file(lib: str) -> bool:
    """Whether `lib` is a path and not a module."""
    return Path(lib).suffix == ".py" or resolve_project_path(lib).exists()


def resolve_module_name(lib: str) -> str:
    """The `__name__` `lib` will register under once imported. Imports nothing.

    `import_modules` and `ConnectionConfig` both go through this, so a config's
    declared namespace and the registered one cannot drift.
    """
    if names_a_file(lib):
        return _module_name_for_file(resolve_project_path(lib))
    return lib


def _sanitize(part: str) -> str:
    cleaned = _NOT_IDENTIFIER.sub("_", part)
    return cleaned if cleaned[:1].isidentifier() else f"_{cleaned}"


def _package_levels(directory: Path) -> list[Path]:
    """`directory` and every ancestor, drive/UNC root first."""
    return list(reversed([directory, *directory.parents]))


def _level_name(level: Path) -> str:
    return _sanitize(level.name or level.anchor)


def _module_name_for_file(resolved: Path) -> str:
    # Every real folder becomes a package level so a relative `..` import in the
    # file resolves to the real sibling folder, and two same-named files in
    # different folders get different names.
    packages = ".".join(_level_name(level) for level in _package_levels(resolved.parent))
    return f"{_SYNTHETIC_ROOT}.{packages}.{_sanitize(resolved.stem)}"


def import_modules(libs: list[str] | str) -> list[str]:
    """Import each lib so it registers its message types; return each namespace."""
    if isinstance(libs, str):
        libs = [libs]
    return [_import_one(lib) for lib in libs]


def _import_one(lib: str) -> str:
    if names_a_file(lib):
        path = resolve_project_path(lib)
        name = _module_name_for_file(path)
        _import_from_file(path, name)
    else:
        name = lib
        _import_dotted(lib)
    _assert_registered(lib, name)
    return name


def _import_dotted(lib: str) -> None:
    # Re-raise naming the config entry; keep `.name` intact since gsim renders it.
    try:
        importlib.import_module(lib)
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"structures entry {lib!r} is not an importable module "
            f"({exc.name!r} was not found). A dotted name only works for a "
            f"module already on sys.path; a structures file kept anywhere else "
            f"-- the normal case -- must be named by its full .py path.",
            name=exc.name,
        ) from exc


def _assert_registered(lib: str, name: str) -> None:
    """Fail here, naming the file, rather than later against an empty registry."""
    # Local import: core.connections imports this module and needn't load the registry.
    from core.IRS.REGISTRY import PAIR_REGISTRY, STRUCTURE_REGISTRY

    if name in STRUCTURE_REGISTRY or name in PAIR_REGISTRY:
        return
    # Both import paths short-circuit on sys.modules, so a retry after a fix
    # would otherwise never re-execute.
    sys.modules.pop(name, None)
    raise ImportError(
        f"structures module {lib!r} imported as {name!r} but registered no "
        f"messages under that namespace. Either it calls no register_message/"
        f"register_pair, or it passes an explicit namespace= that does not "
        f"match, or it reached a DIFFERENT IRS module object than the one being "
        f"read here (see core/IRS/_alias.py). Known namespaces: "
        f"{sorted(set(STRUCTURE_REGISTRY) | set(PAIR_REGISTRY)) or 'none'}."
    )


def _ensure_synthetic_package(directory: Path) -> None:
    """Register a package in `sys.modules` for `directory` and every ancestor.

    Python resolves `..` by trimming the current package's dotted name, so every
    ancestor must exist with a `__path__` at its real folder. Raises if two real
    folders sanitize to the same name ("Unit-B" vs "Unit_B"), which would
    misroute a relative import.
    """
    if _SYNTHETIC_ROOT not in sys.modules:
        root_spec = importlib.machinery.ModuleSpec(_SYNTHETIC_ROOT, None, is_package=True)
        root_spec.submodule_search_locations = []
        sys.modules[_SYNTHETIC_ROOT] = importlib.util.module_from_spec(root_spec)

    name = _SYNTHETIC_ROOT
    for level in _package_levels(directory):
        name = f"{name}.{_level_name(level)}"
        existing = sys.modules.get(name)
        if existing is None:
            spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
            spec.submodule_search_locations = [str(level)]
            sys.modules[name] = importlib.util.module_from_spec(spec)
        elif list(existing.__path__) != [str(level)]:
            raise ImportError(
                f"two different directories both resolve to the structures "
                f"package {name!r} ({existing.__path__[0]!r} and {str(level)!r}) -- "
                f"rename one of them so a relative import inside a structures "
                f"file cannot be misrouted between them."
            )


def _import_from_file(path: Path, module_name: str) -> None:
    """Load the already-resolved `path` as `module_name`."""
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        if Path(loaded.__file__).resolve() != path:
            # Same-name collision on the leaf file too; without this the second
            # file would silently get the first one's messages.
            raise ImportError(
                f"structures files {loaded.__file__!r} and {str(path)!r} both "
                f"resolve to the same synthetic module {module_name!r} -- "
                f"rename one of their containing folders or filenames so a "
                f"config cannot end up scoped to the wrong one."
            )
        return          # already loaded; re-executing would re-register everything
    if not path.is_file():
        # Likeliest cause: a config written on another machine. Without this the
        # error surfaces from exec_module and reads as a broken file.
        raise ImportError(f"structures file does not exist: {path}")
    _ensure_synthetic_package(path.parent)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import structures file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        # Drop the half-initialised module so a retry after a fix re-executes.
        del sys.modules[module_name]
        raise ImportError(
            f"structures file {path} failed to import: {type(exc).__name__}: {exc}") from exc


def validated_opcode(opCode: OpCode | str) -> OpCode:
    if isinstance(opCode, int):
        return opCode
    return int(opCode, 0)


validated_unitcode = validated_opcode


def extract_opcode(opcode: int | str | IrsMessage) -> int:
    message_opcode = getattr(opcode, "_opCode", None)
    if message_opcode is not None:  # not truthiness: opcode 0 is valid
        return message_opcode
    return validated_opcode(opcode)
