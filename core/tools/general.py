import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path

from annotations import *

#: tools/ and IRS/ are siblings under core/.
STRUCTURES_ROOT = Path(__file__).resolve().parent.parent / "IRS" / "Structures"


def resolve_module_name(lib: str) -> str:
    """
    The `__name__` this lib will have once imported -- i.e. the namespace
    `IRS.REGISTRY.register_message` captures from it.

    Pure: imports nothing, touches nothing. `import_modules` and
    `ConnectionConfig` both resolve through this one function, which is what
    guarantees the namespace a config declares and the namespace a module
    actually registers under cannot drift apart.
    """
    path = Path(lib)
    if path.suffix == ".py" or path.exists():
        return _module_name_for_file(path)
    dotted = lib.replace("\\", ".").replace("/", ".")
    return dotted if dotted.startswith("IRS.Structures.") else f"IRS.Structures.{dotted}"


def _module_name_for_file(path: Path) -> str:
    """
    A unique, stable module name for a structures file named by path.

    `path.stem` alone was neither: two `messages.py` under different directories
    both landed on `sys.modules['messages']` and the second erased the first.
    And a file that lives under IRS/Structures picked by path got a DIFFERENT
    name from the same file written dotted -- one file registering under two
    namespaces, which is exactly the confusion this is meant to remove. A file
    inside the package therefore resolves to its own dotted name; only genuinely
    external files get a synthesised one, disambiguated by path digest.
    """
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(STRUCTURES_ROOT)
    except ValueError:
        digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
        return f"IRS.Structures._external.{resolved.stem}_{digest}"
    return "IRS.Structures." + ".".join(relative.with_suffix("").parts)


def import_modules(libs: list[str] | str) -> list[str]:
    """
    Import each of `libs` so it registers its message types, and return the
    namespace each one resolved to, in order.

    A lib is either a dotted module path relative to `IRS.Structures`
    (e.g. "Test.test_messages") or a filesystem path to a `.py` file --
    distinguished by whether it looks like/exists as a real file, not by
    counting dots, since a short dotted name (e.g. "Pkg.io") can have fewer than
    3 characters after its last dot and would otherwise be misclassified.
    """
    if isinstance(libs, str):
        libs = [libs]
    return [_import_one(lib) for lib in libs]


def _import_one(lib: str) -> str:
    name = resolve_module_name(lib)
    path = Path(lib)
    is_file = path.suffix == ".py" or path.exists()
    # A file inside IRS/Structures has a real dotted name, so import it the
    # ordinary way -- that is what makes the path and dotted spellings of one
    # file resolve to a single namespace.
    if is_file and name.startswith("IRS.Structures._external."):
        _import_from_file(path, name)
    else:
        importlib.import_module(name)
    return name


def _import_from_file(path: Path, module_name: str) -> None:
    if module_name in sys.modules:
        return          # already loaded; re-executing would re-register everything
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

def validated_opCode(opCode: OpCode | str) -> OpCode:
    if isinstance(opCode, int):
        return opCode
    return int(opCode, 0)


def validated_unitCode(unitCode: UnitCode | str) -> UnitCode:
    return validated_opCode(unitCode)
