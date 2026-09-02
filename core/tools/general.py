import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path

from core.annotations import *

#: There is deliberately no STRUCTURES_ROOT any more. A structures file used to
#: be imported one of two ways depending on whether it happened to sit under
#: `core/IRS/Structures`: inside, the picked PATH WAS DISCARDED and the file was
#: looked up as a package instead (`core.IRS.Structures.<folder>.<stem>`).
#: That lookup rides on `sys.path`, on `core` being importable from the same
#: physical tree the root was computed from, on PEP 420 namespace resolution,
#: and -- in the PyInstaller build -- on the frozen importer owning `core.IRS`
#: while those subfolders exist only as bundled data. When any of that failed
#: the user got `ModuleNotFoundError: No module named
#: 'core.IRS.Structures.<folder>'` for a file they had just picked in a dialog
#: and which was sitting right there on disk -- and it reproduced on one machine
#: while working on another, because the two branches were selected by location.
#: A path is now always loaded from that path. See `_import_one`.


#: `IRS.Structures` is now reached as `core.IRS.Structures` -- `core/` stopped
#: being a `sys.path` root itself (the repo root is, and `core` is an ordinary
#: package under it). Every namespace this module hands out has to carry that
#: prefix, or `importlib.import_module` simply cannot find it -- and the
#: prefix `IRS.REGISTRY.register_message` captures via `sys._getframe` (the
#: module's real `__name__` once imported) already includes it automatically,
#: so this is the only place that needs to agree on purpose.
STRUCTURES_PACKAGE = "core.IRS.Structures"


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
    """
    if names_a_file(lib):
        return _module_name_for_file(Path(lib))
    dotted = lib.replace("\\", ".").replace("/", ".")
    # Accept the short form ("Test.test_messages"), the fully-qualified new
    # form ("core.IRS.Structures.Test.test_messages"), and -- since existing
    # saved configs and Save/Load session files spell it this way -- the
    # pre-migration form ("IRS.Structures.Test.test_messages") too.
    if dotted.startswith(STRUCTURES_PACKAGE + "."):
        return dotted
    if dotted.startswith("IRS.Structures."):
        return STRUCTURES_PACKAGE + dotted.removeprefix("IRS.Structures")
    return f"{STRUCTURES_PACKAGE}.{dotted}"


def _module_name_for_file(path: Path) -> str:
    """
    A unique, stable module name for a structures file named by path.

    `path.stem` alone is not unique: two `messages.py` under different
    directories both land on `sys.modules['messages']` and the second erases the
    first. The absolute path's digest is what separates them, and keying on the
    resolved path is what makes the name stable -- the same file picked twice
    resolves to the same namespace, so a config and the registry agree.

    Every file gets a synthesised name, wherever it lives. Files under
    `core/IRS/Structures` used to be special-cased into their real dotted name
    on the theory that the path and dotted spellings of one file should not
    become two namespaces -- true, but paid for with the location-dependent
    import above, and the case it protected (one config naming the same file
    both ways) is pathological where the failure it caused was routine.
    """
    resolved = path.resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{STRUCTURES_PACKAGE}._external.{resolved.stem}_{digest}"


def import_modules(libs: list[str] | str) -> list[str]:
    """
    Import each of `libs` so it registers its message types, and return the
    namespace each one resolved to, in order.

    A lib is either a dotted module path relative to `IRS.Structures`
    (e.g. "Test.test_messages") or a filesystem path to a `.py` file --
    distinguished by `names_a_file`, not by counting dots, since a short dotted
    name (e.g. "Pkg.io") can have fewer than 3 characters after its last dot and
    would otherwise be misclassified.
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

    Only a module that genuinely ships inside `core/IRS/Structures` can be
    reached this way. The bare `ModuleNotFoundError` names the missing package
    and nothing else, which is several layers from the config entry that asked
    for it -- so re-raise with the spelling the user actually wrote, while
    keeping `.name` intact, since `gsim` renders it.
    """
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"structures entry {lib!r} resolved to the module {name!r}, which is "
            f"not importable ({exc.name!r} was not found). A dotted name only "
            f"works for a module shipped inside core/IRS/Structures; to load a "
            f"structures file from anywhere else, give its full .py path.",
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


def _import_from_file(path: Path, module_name: str) -> None:
    if module_name in sys.modules:
        return          # already loaded; re-executing would re-register everything
    if not path.is_file():
        # The likeliest way to get here is a config written on another machine:
        # structures files are picked by absolute path, and that path is not
        # portable. Say so plainly -- `spec_from_file_location` does not stat,
        # so without this the failure arrives as a FileNotFoundError raised out
        # of `exec_module`, wrapped as "failed to import", which reads like the
        # file is broken rather than absent.
        raise ImportError(f"structures file does not exist: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
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
