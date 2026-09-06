import hashlib
import importlib
import importlib.machinery
import importlib.util
import re
import sys
from pathlib import Path

from core.annotations import *

#: There is deliberately NO structures package, root directory, or dotted
#: prefix anywhere in this module -- and this comment exists because there were
#: two of them, in sequence, and each one broke the same way.
#:
#: A structures file is the USER's data. It lives wherever they keep it,
#: typically `<anywhere on the machine>\<UnitName>\<InterfaceName>.py`, it is
#: picked from a file dialog, and it is edited far more often than the app that
#: loads it. It was first imported as a member of `IRS.Structures` when it
#: happened to sit under `core/IRS/Structures` (the picked PATH WAS DISCARDED),
#: and afterwards it was still *named* as one -- every namespace this module
#: handed out was prefixed `core.IRS.Structures.`, whether or not the entry was
#: a path.
#:
#: That prefix is a lie the moment the folder moves or stops existing, and it
#: is not an inert lie: `core.IRS.Structures.<x>` is a name Python will try to
#: RESOLVE. It is looked up for real for a dotted entry, and it is resolved as
#: the parent package the moment a picked file contains a relative import
#: (`from . import shared`) -- so a file sitting right there on disk fails with
#: `ModuleNotFoundError: No module named 'core.IRS.Structures'`, naming a
#: package the user has never heard of and cannot create.
#:
#: So: a path is loaded from that path, under a synthetic namespace derived
#: from the path itself (`_SYNTHETIC_ROOT` below, which is nothing on disk and
#: is never searched for), and a dotted entry is imported verbatim as the
#: ordinary Python module it names. Nothing in between, and no folder this repo
#: has to keep alive.

#: Root of the synthetic namespace picked-by-path structures files are imported
#: under. Registered directly into `sys.modules`, not on disk anywhere. Below
#: it the package tree mirrors the real filesystem one level per real folder
#: (drive/UNC root, then each directory down to the file), so that a relative
#: import inside a structures file -- including one that goes `..` into a
#: sibling unit's folder -- resolves the same way it would if these folders
#: really were one Python package. See `_ensure_synthetic_package`.
_SYNTHETIC_ROOT = "irs_structures"

#: Anything that cannot appear in a Python identifier. Directory and file names
#: are user-chosen ("Tiful Unit", "dtu-v2"), and a namespace is a dotted module
#: name, so the parts have to be scrubbed before they become one.
_NOT_IDENTIFIER = re.compile(r"\W")


def names_a_file(lib: str) -> bool:
    """Whether `lib` names a `.py` file on disk rather than a dotted module.

    One predicate, called by everything that needs the distinction, because the
    two halves of this decision used to be written out twice -- once to pick the
    NAME and once to pick the IMPORT MECHANISM -- and a file could therefore be
    named as a package member while being handed to the file loader, or the
    reverse. Deciding once removes the possibility.

    Extension first, existence second: a path is still a path when it points at
    a file this machine does not have (a config written on another computer),
    and saying so here is what lets `_import_from_file` report the missing file
    by name instead of this returning False and sending a Windows path into the
    dotted branch to come back out as a nonsense module name.
    """
    path = Path(lib)
    return path.suffix == ".py" or path.exists()


def resolve_module_name(lib: str) -> str:
    """
    The `__name__` this lib will have once imported -- i.e. the namespace
    `IRS.REGISTRY.register_message` captures from it.

    Pure: imports nothing, touches nothing. `import_modules` and
    `ConnectionConfig` both resolve through this one function, which is what
    guarantees the namespace a config declares and the namespace a module
    actually registers under cannot drift apart.

    A dotted entry comes back UNCHANGED -- it names an ordinary importable
    module and is imported as written. Nothing is prefixed onto it; see the
    note at the top of this module for what prefixing cost.
    """
    if names_a_file(lib):
        return _module_name_for_file(Path(lib))
    return lib


def _sanitize(part: str) -> str:
    """One path component -> one legal Python identifier."""
    cleaned = _NOT_IDENTIFIER.sub("_", part)
    return cleaned if cleaned[:1].isidentifier() else f"_{cleaned}"


def _package_name_for_dir(directory: Path) -> str:
    """The synthetic package a structures DIRECTORY is imported as.

    Mirrors the REAL directory chain from the drive root down to `directory`,
    one synthetic package per real folder, each sanitized name matching the
    folder's own name. That is what a relative import needs to actually work:
    interface files import sibling and cousin units --
    `from ..OtherUnit.dataTypes import X` -- and Python resolves `..` by
    chopping a segment off the CURRENT package's dotted name and looking up
    the rest by that literal spelling. If only the leaf folder were a package
    (as an earlier version of this function did, keyed by a hash), `..` landed
    on the synthetic root, which owns no directory and has never heard of
    `OtherUnit` -- so a `.py` picked from a dialog would fail to import a
    sibling that is sitting right next to it on disk. Naming every level after
    the REAL folder means the import spelling the user actually wrote is the
    one that resolves, at any `..` depth up to the drive root.

    Keying on the resolved path (not a digest of it) is what makes two picks
    of the same folder agree on one package, so a config and the registry
    cannot drift.
    """
    return f"{_SYNTHETIC_ROOT}." + ".".join(_sanitize(part) for part in directory.parts)


def _module_name_for_file(path: Path) -> str:
    """
    A unique, stable module name for a structures file named by path.

    `path.stem` alone is not unique: two `messages.py` under different
    directories both land on `sys.modules['messages']` and the second erases
    the first. The containing directory's package chain (above) is what
    separates them.
    """
    resolved = path.resolve()
    return f"{_package_name_for_dir(resolved.parent)}.{_sanitize(resolved.stem)}"


def import_modules(libs: list[str] | str) -> list[str]:
    """
    Import each of `libs` so it registers its message types, and return the
    namespace each one resolved to, in order.

    A lib is either a filesystem path to a `.py` file -- the ordinary case, and
    what a file dialog hands back -- or a dotted name for a module already
    importable on `sys.path`. They are distinguished by `names_a_file`, not by
    counting dots, since a short dotted name (e.g. "Pkg.io") can have fewer than
    3 characters after its last dot and would otherwise be misclassified.
    """
    if isinstance(libs, str):
        libs = [libs]
    return [_import_one(lib) for lib in libs]


def _import_one(lib: str) -> str:
    """Import one lib, by the mechanism its OWN SPELLING asks for.

    A path is loaded from that path; a dotted name is looked up as a module.
    Nothing about where the file sits enters into it -- that is the whole fix,
    and the reason is written out at the top of this module. Structures files
    are user data that lives wherever the user keeps it, so a file dialog
    handing back an absolute path is the ordinary case, not the exception.
    """
    name = resolve_module_name(lib)
    if names_a_file(lib):
        _import_from_file(Path(lib), name)
    else:
        _import_dotted(lib, name)
    _assert_registered(lib, name)
    return name


def _import_dotted(lib: str, name: str) -> None:
    """Import a dotted lib, saying which config entry failed if it will not.

    A dotted entry is an ordinary import: it works only for a module already
    importable on `sys.path` (a structures module that genuinely ships inside a
    package, the way the test suite's do). The bare `ModuleNotFoundError` names
    the missing package and nothing else, which is several layers from the
    config entry that asked for it -- so re-raise with the spelling the user
    actually wrote, while keeping `.name` intact, since `gsim` renders it.
    """
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"structures entry {lib!r} is not an importable module "
            f"({exc.name!r} was not found). A dotted name only works for a "
            f"module already on sys.path; a structures file kept anywhere else "
            f"-- the normal case -- must be named by its full .py path.",
            name=exc.name,
        ) from exc


def _assert_registered(lib: str, name: str) -> None:
    """Fail loudly if importing `lib` registered nothing under `name`.

    An import that "succeeds" while registering into somewhere nobody reads is
    the single worst failure this module can produce: everything looks fine
    until `irs_parser._get_message_class` raises `IRSNotFoundError` against an
    empty registry, one config load and several layers away from the file that
    actually caused it. This turns that into an error naming the file.

    Imported locally, not at module scope: `core.tools.general` is itself
    imported by `core.connections`, and there is no reason to pull the registry
    in at that point. Reading it at call time also means we read whatever the
    module just wrote, with no load-order assumption at all.
    """
    from core.IRS.REGISTRY import PAIR_REGISTRY, STRUCTURE_REGISTRY

    if name in STRUCTURE_REGISTRY or name in PAIR_REGISTRY:
        return
    # Drop it so a retry after a fix actually re-executes -- both import paths
    # short-circuit on an existing `sys.modules` entry, so leaving it would make
    # the second attempt fail identically no matter what the user changed.
    sys.modules.pop(name, None)
    raise ImportError(
        f"structures module {lib!r} imported as {name!r} but registered no "
        f"messages under that namespace. Either it calls no register_message/"
        f"register_pair, or it passes an explicit namespace= that does not "
        f"match, or it reached a DIFFERENT IRS module object than the one being "
        f"read here (see core/IRS/_alias.py). Known namespaces: "
        f"{sorted(set(STRUCTURE_REGISTRY) | set(PAIR_REGISTRY)) or 'none'}."
    )


def _ensure_synthetic_package(directory: Path) -> str:
    """Put a synthetic package into `sys.modules` for `directory` AND every
    real ancestor down to the drive root, and return `directory`'s own
    package name.

    One level at a time rather than just the leaf, because a `..` in a
    relative import is resolved by the import system, not by us: it chops a
    segment off the current package's `__name__` and looks up whatever is
    left, by that literal name, as an ordinary import. For
    `from ..OtherUnit.dataTypes import X` to find `OtherUnit`, the PARENT of
    the current folder's package has to already exist and have `OtherUnit`
    reachable under it with a `__path__` pointing at the real directory --
    which only happens if every ancestor got the same treatment, not just the
    one folder the picked file happens to sit in.

    The root (`_SYNTHETIC_ROOT` itself) is the one exception: it owns no real
    directory (there is no single folder every structures file shares), so it
    gets an empty `submodule_search_locations` rather than a real one. Nothing
    is ever imported *through* it by name -- only the drive-letter/UNC-root
    packages immediately under it, which this function creates on demand.

    Idempotent: a second file under the same tree joins the packages already
    there. Raises if two REAL directories sanitize to the same package name
    under the same parent (e.g. sibling folders "Unit-B" and "Unit_B") --
    silently aliasing them would misroute a relative import to the wrong one.
    """
    if _SYNTHETIC_ROOT not in sys.modules:
        root_spec = importlib.machinery.ModuleSpec(_SYNTHETIC_ROOT, None, is_package=True)
        root_spec.submodule_search_locations = []
        sys.modules[_SYNTHETIC_ROOT] = importlib.util.module_from_spec(root_spec)

    name = _SYNTHETIC_ROOT
    built = Path(directory.anchor) if directory.anchor else None
    for part in directory.parts:
        name = f"{name}.{_sanitize(part)}"
        built = Path(part) if built is None else built / part
        existing = sys.modules.get(name)
        if existing is None:
            spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
            spec.submodule_search_locations = [str(built)]
            sys.modules[name] = importlib.util.module_from_spec(spec)
        elif list(existing.__path__) != [str(built)]:
            raise ImportError(
                f"two different directories both resolve to the structures "
                f"package {name!r} ({existing.__path__[0]!r} and {built!r}) -- "
                f"rename one of them so a relative import inside a structures "
                f"file cannot be misrouted between them."
            )
    return name


def _import_from_file(path: Path, module_name: str) -> None:
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        if Path(loaded.__file__).resolve() != path.resolve():
            # Two DIFFERENT files sanitized to the same name -- e.g. sibling
            # folders "Unit-B" and "Unit_B" both holding an "f.py". Without
            # this check the "already loaded" shortcut below would silently
            # keep the FIRST file's module and never even look at the second:
            # no error, no registration, just a config that named a real file
            # on disk and got someone else's messages back. Directory-only
            # collisions (different filenames) are already caught by
            # `_ensure_synthetic_package`; this is the same guarantee for the
            # case where the leaf filename collides too.
            raise ImportError(
                f"structures files {loaded.__file__!r} and {str(path)!r} both "
                f"resolve to the same synthetic module {module_name!r} -- "
                f"rename one of their containing folders or filenames so a "
                f"config cannot end up scoped to the wrong one."
            )
        return          # already loaded; re-executing would re-register everything
    if not path.is_file():
        # The likeliest way to get here is a config written on another machine:
        # structures files are picked by absolute path, and that path is not
        # portable. Say so plainly -- `spec_from_file_location` does not stat,
        # so without this the failure arrives as a FileNotFoundError raised out
        # of `exec_module`, wrapped as "failed to import", which reads like the
        # file is broken rather than absent.
        raise ImportError(f"structures file does not exist: {path}")
    resolved = path.resolve()
    # The file's own folder becomes its package, so `from . import shared`
    # inside a structures file works instead of resolving a parent nobody owns.
    _ensure_synthetic_package(resolved.parent)
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import structures file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        # A structures file is arbitrary user code, and since it can be picked
        # from a file dialog it is routinely one nobody has ever imported. Say
        # which file failed and how -- the bare exception surfaces far from the
        # config that named it. Drop the half-initialised module so a retry
        # after a fix actually re-executes.
        del sys.modules[module_name]
        raise ImportError(
            f"structures file {path} failed to import: {type(exc).__name__}: {exc}") from exc



def validated_opcode(opCode: OpCode | str) -> OpCode:
    if isinstance(opCode, int):
        return opCode
    return int(opCode, 0)


def validated_unitcode(unitCode: UnitCode | str) -> UnitCode:
    return validated_opcode(unitCode)


def extract_opcode(opcode: int | str | IrsMessage) -> int:
    valid = getattr(opcode, "_opCode", False)
    if valid:
        return valid
    return validated_opcode(opcode)


def topic_opcode(topic_name: str) -> OpCode:
    """
    The framework's route key is (unit_code, opcode), but DDS puts no opcode on
    the wire -- a topic IS the message identity there. This derives a stable
    local routing handle from the topic name so DDS traffic flows through the
    same `_subscriptions`/`_callbacks` machinery as everything else.

    Never transmitted, and never seen by a remote unit. Deterministic across
    processes and restarts so `@route(opCode=topic_opcode("X"))` in a handler
    and the reader that dispatches under it always agree. Sized to `framing.py`'s
    uint16 OpCode field; `DdsConnection` rejects a collision between two of its
    own topics at load time rather than letting it become a silent misroute.
    """
    digest = hashlib.blake2s(topic_name.encode("utf-8"), digest_size=2).digest()
    return int.from_bytes(digest, "little")
