import importlib
import importlib.util
import sys
from pathlib import Path

from annotations import *

def import_modules(libs: list[str] | str) -> None:
    """
    Import each of `libs` so it registers its message types.

    A lib is either a dotted module path relative to `IRS.Structures`
    (e.g. "Test.test_messages") or a filesystem path to a `.py` file to load
    directly -- distinguished by whether it looks like/exists as a real file,
    not by counting dots, since a short dotted name (e.g. "Pkg.io") can have
    fewer than 3 characters after its last dot and would otherwise be
    misclassified as a path.
    """
    if isinstance(libs, str):
        libs = [libs]

    for lib in libs:
        path = Path(lib)
        if path.suffix == ".py" or path.exists():
            _import_from_file(path)
        else:
            dotted = lib.replace("\\", ".").replace("/", ".")
            module = dotted if dotted.startswith("IRS.Structures.") else f"IRS.Structures.{dotted}"
            importlib.import_module(module)


def _import_from_file(path: Path) -> None:
    module_name = path.stem
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import structures file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

def validated_opCode(opCode: OpCode | str) -> OpCode:
    if isinstance(opCode, int):
        return opCode
    return int(opCode, 0)


def validated_unitCode(unitCode: UnitCode | str) -> UnitCode:
    return validated_opCode(unitCode)
