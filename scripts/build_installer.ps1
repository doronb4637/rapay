<#
.SYNOPSIS
    Build the GSim MSI end to end: React bundle -> PyInstaller onedir -> MSI.

.DESCRIPTION
    Three steps that must happen in this order, because each consumes the
    previous one's output:

      1. `npm run build` in gsim/web  -> gsim/web/ui_dist   (the UI FastAPI serves)
      2. PyInstaller GSim.spec        -> dist/GSim/         (GSim.exe + _internal)
      3. `wix build` installer/GSim.wxs -> dist/GSim-<version>-x64.msi

    The version is read from gsim/__init__.py and used for the MSI's
    ProductVersion; the .exe's own file-version resource is generated from the
    same place by GSim.spec, so the two cannot drift.

.PARAMETER SkipUi
    Reuse the existing gsim/web/ui_dist instead of running npm. Only safe when
    nothing under gsim/web/src has changed since it was built.

.PARAMETER SkipExe
    Reuse the existing dist/GSim. For iterating on the WiX authoring alone.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
#>
[CmdletBinding()]
param(
    [switch] $SkipUi,
    [switch] $SkipExe
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# PyInstaller lives in .venv3.10, not .venv (which is 3.11 and has no
# PyInstaller installed) -- see gsim/CLAUDE.md on which interpreter is which.
$python = Join-Path $repo '.venv3.10\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "PyInstaller's interpreter not found at $python. Create it, then: .venv3.10\Scripts\python.exe -m pip install -r requirements.txt pyinstaller"
}

$version = & $python -c "import re,pathlib;print(re.search(r'__version__ = \""([^\""]+)\""', pathlib.Path('gsim/__init__.py').read_text(encoding='utf-8')).group(1))"
if (-not $version) { throw 'Could not read __version__ from gsim/__init__.py' }
Write-Host "==> Building GSim $version" -ForegroundColor Cyan

# --- 1. the UI --------------------------------------------------------------
if ($SkipUi) {
    Write-Host '==> [1/3] UI: skipped' -ForegroundColor DarkGray
} else {
    Write-Host '==> [1/3] UI: npm run build' -ForegroundColor Cyan
    Push-Location (Join-Path $repo 'gsim\web')
    try {
        if (-not (Test-Path 'node_modules')) { & npm install; if ($LASTEXITCODE) { throw 'npm install failed' } }
        & npm run build
        if ($LASTEXITCODE) { throw 'npm run build failed' }
    } finally { Pop-Location }
}
if (-not (Test-Path (Join-Path $repo 'gsim\web\ui_dist\index.html'))) {
    throw 'gsim/web/ui_dist/index.html is missing -- the packaged app would open on a blank page.'
}

# --- 2. the frozen app ------------------------------------------------------
if ($SkipExe) {
    Write-Host '==> [2/3] PyInstaller: skipped' -ForegroundColor DarkGray
} else {
    Write-Host '==> [2/3] PyInstaller: GSim.spec' -ForegroundColor Cyan
    # A stale dist/GSim is worse than a slow build: `wix build` harvests whatever
    # is in that directory, so a file removed from the spec would still ship.
    Remove-Item -Recurse -Force (Join-Path $repo 'dist\GSim') -ErrorAction SilentlyContinue
    & $python -m PyInstaller --noconfirm --distpath dist --workpath build --log-level WARN GSim.spec
    if ($LASTEXITCODE) { throw 'PyInstaller failed' }
}
$stage = Join-Path $repo 'dist\GSim'
if (-not (Test-Path (Join-Path $stage 'GSim.exe'))) { throw "dist/GSim/GSim.exe is missing" }

# --- 3. the MSI -------------------------------------------------------------
Write-Host '==> [3/3] MSI: wix build' -ForegroundColor Cyan
if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    throw @'
The WiX CLI is not installed. It is a .NET global tool:

    dotnet tool install --global wix --version 5.0.2
    wix extension add -g WixToolset.UI.wixext/5.0.2

v5 deliberately, not v6/v7: those require accepting the Open Source
Maintenance Fee EULA, which is a licensing decision for whoever ships GSim.
'@
}

$msi = Join-Path $repo "dist\GSim-$version-x64.msi"
& wix build `
    -arch x64 `
    -define "Version=$version" `
    -bindpath "src=$stage" `
    -bindpath "installer=$repo\installer" `
    -ext WixToolset.UI.wixext `
    -out $msi `
    (Join-Path $repo 'installer\GSim.wxs')
if ($LASTEXITCODE) { throw 'wix build failed' }

Write-Host ''
Write-Host "==> $msi" -ForegroundColor Green
Write-Host ("    {0:N1} MB" -f ((Get-Item $msi).Length / 1MB)) -ForegroundColor Green
