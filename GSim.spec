# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build of the GSim desktop app (onedir -- `dist/GSim/GSim.exe`),
which is what `installer/GSim.wxs` turns into the MSI.

Build it with the 3.10 venv, which is the one PyInstaller is installed in:

    .venv3.10/Scripts/python.exe -m PyInstaller --noconfirm GSim.spec

or, together with the UI build and the MSI, `scripts/build_installer.ps1`.

`datas` below is a contract with `gsim/paths.py`: every read-only directory it
resolves under `BUNDLE_ROOT` has to be copied here, at the SAME relative path.
Getting that wrong is silent -- the window opens on a blank page (no
`gsim/web/ui_dist`) or the file pickers start in the wrong place -- so the two
files must be edited together.
"""
import os
import re
from pathlib import Path

REPO = Path(SPECPATH)

#: A windowed build has no stdout/stderr at all, so a frozen app that dies on
#: startup dies silently -- the one failure mode you most need to see. Set
#: GSIM_BUILD_CONSOLE=1 to get the same bundle with a console attached:
#:
#:     $env:GSIM_BUILD_CONSOLE=1
#:     .venv3.10/Scripts/python.exe -m PyInstaller --noconfirm GSim.spec
#:
#: Never ship that build -- the console window opens behind the app.
CONSOLE = os.environ.get("GSIM_BUILD_CONSOLE") == "1"

#: `gsim.__version__`, read without importing gsim (which would drag core, and
#: therefore rti/pywebview, into the build process itself).
VERSION = re.search(r'__version__ = "([^"]+)"',
                    (REPO / "gsim" / "__init__.py").read_text(encoding="utf-8")).group(1)


def version_resource() -> str:
    """Write the Windows VERSIONINFO resource stamped into GSim.exe and return
    its path. Generated rather than checked in so `gsim/__init__.py` stays the
    only place a version number is written down -- the .exe's Properties tab and
    the MSI's ProductVersion then cannot disagree with it."""
    major, minor, patch = (int(part) for part in VERSION.split("."))
    target = REPO / "build" / "version_info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({major}, {minor}, {patch}, 0),
                    prodvers=({major}, {minor}, {patch}, 0),
                    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
        StringStruct('CompanyName', 'Rapat'),
        StringStruct('FileDescription', 'GSim -- Generic Simulator'),
        StringStruct('FileVersion', '{VERSION}.0'),
        StringStruct('InternalName', 'GSim'),
        StringStruct('OriginalFilename', 'GSim.exe'),
        StringStruct('ProductName', 'GSim'),
        StringStruct('ProductVersion', '{VERSION}.0'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ]
)
""", encoding="utf-8")
    return str(target)

def tree(source: Path, target: str, *, skip: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Every file under `source`, as (absolute source, destination dir) pairs,
    minus `__pycache__` and any path segment in `skip`.

    A per-file list rather than one `(dir, dir)` entry because PyInstaller
    copies a directory wholesale: `core` carries a 900 KB test suite and stale
    `.pyc` files from three interpreter versions, none of which belong in an
    installer.
    """
    items = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(source).parts
        if "__pycache__" in parts or parts[0] in skip:
            continue
        items.append((str(path), str(Path(target) / path.relative_to(source).parent)))
    return items

datas = [
    # The built React bundle. Vite writes it to gsim/web/ui_dist (`build.outDir`
    # in vite.config.js) precisely so it does not collide with PyInstaller's own
    # dist/, and `paths.WEB_DIST` looks for it at this same relative path.
    *tree(REPO / "gsim" / "web" / "ui_dist", "gsim/web/ui_dist"),
    # Window/taskbar icon, loaded at runtime by pywebview (`paths.ICON`).
    (str(REPO / "gsim" / "assets" / "gsim.ico"), "gsim/assets"),
    # `core` ships as DATA as well as being imported: bootstrap.py checks that
    # CORE_ROOT exists on disk, and IRS structures modules are loaded from real
    # .py files by path (`tools.general.import_modules`), which the PYZ cannot
    # serve. Tests and the benchmark are not part of the product.
    *tree(REPO / "core", "core", skip=("tests", "temp.py", "pyvenv.cfg")),
    # Seed content for %LOCALAPPDATA%\GSim\GsimConfig on first run.
    *tree(REPO / "configs" / "GsimConfig", "configs/GsimConfig"),
]

a = Analysis(
    ['gsim/__main__.py'],
    pathex=[str(REPO)],
    binaries=[],
    datas=datas,
    # uvicorn/websockets/webview/pydantic/clr_loader all have hooks in
    # pyinstaller-hooks-contrib, so the dynamic imports they do are already
    # covered; this is only what nothing hooks for us.
    hiddenimports=['uvicorn.lifespan.on', 'uvicorn.protocols.http.h11_impl'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='GSim',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(REPO / "gsim" / "assets" / "gsim.ico"),
    version=version_resource(),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='GSim',
)
