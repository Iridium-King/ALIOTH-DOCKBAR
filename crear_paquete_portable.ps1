param(
    [string]$PythonSource = "",
    [switch]$RebuildRuntime,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $ProjectDir "runtime"
$DistDir = Join-Path $ProjectDir "dist"

function Resolve-PythonSource {
    if ($PythonSource -and (Test-Path (Join-Path $PythonSource "pythonw.exe"))) {
        return $PythonSource
    }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python315",
        "$env:LOCALAPPDATA\Programs\Python\Python314",
        "$env:LOCALAPPDATA\Programs\Python\Python313",
        "$env:LOCALAPPDATA\Programs\Python\Python312",
        "$env:LOCALAPPDATA\Programs\Python\Python311",
        "C:\Program Files\Python315",
        "C:\Program Files\Python314",
        "C:\Program Files\Python313",
        "C:\Program Files\Python312"
    )
    foreach ($c in $candidates) {
        if ((Test-Path (Join-Path $c "pythonw.exe")) -and (Test-Path (Join-Path $c "Lib\encodings\__init__.py"))) {
            return $c
        }
    }
    throw "No se encontró una instalación de Python con Tcl/Tk. Pasa -PythonSource <ruta>."
}

if ($RebuildRuntime -or -not (Test-Path (Join-Path $RuntimeDir "pythonw.exe"))) {
    $src = Resolve-PythonSource
    Write-Host "[1/3] Preparando runtime portable desde: $src"
    if (Test-Path $RuntimeDir) {
        Remove-Item -Recurse -Force $RuntimeDir
    }
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

    # Copy root executables/DLLs/licenses (skip NEWS.txt)
    Get-ChildItem -Path $src -File | Where-Object { $_.Name -ne "NEWS.txt" } | ForEach-Object {
        Copy-Item $_.FullName -Destination $RuntimeDir -Force
    }

    # Copy essential runtime directories (DLLs, tcl, Lib) excluding heavy dev/test folders
    foreach ($sub in @("DLLs", "tcl")) {
        $sp = Join-Path $src $sub
        if (Test-Path $sp) {
            Copy-Item $sp -Destination (Join-Path $RuntimeDir $sub) -Recurse -Force
        }
    }

    $srcLib = Join-Path $src "Lib"
    $dstLib = Join-Path $RuntimeDir "Lib"
    New-Item -ItemType Directory -Force -Path $dstLib | Out-Null
    $excludeLibDirs = @("test", "idlelib", "turtledemo", "ensurepip", "__pycache__")
    Get-ChildItem -Path $srcLib | ForEach-Object {
        if ($_.PSIsContainer -and ($excludeLibDirs -contains $_.Name)) {
            return
        }
        Copy-Item $_.FullName -Destination (Join-Path $dstLib $_.Name) -Recurse -Force
    }

    # Strip pip/setuptools/test caches inside site-packages and __pycache__ across runtime
    $spDir = Join-Path $dstLib "site-packages"
    if (Test-Path $spDir) {
        Get-ChildItem -Path $spDir | Where-Object {
            $_.Name -match '^(pip|pip-.*|_pytest|pytest|setuptools|wheel|pkg_resources)'
        } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
    Get-ChildItem -Path $RuntimeDir -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    # Verify portable runtime completely isolated from host PATH/PYTHONPATH
    Write-Host "[2/3] Verificando runtime portable aislado (Tkinter + Pillow + pywin32)..."
    $pyExe = Join-Path $RuntimeDir "python.exe"
    $env:PYTHONHOME = ""
    $env:PYTHONPATH = ""
    & $pyExe -s -c "import sys, tkinter, PIL, win32com.client, win32gui, pythoncom; print('RUNTIME OK:', sys.executable, 'PIL:', PIL.__version__)"
    if ($LASTEXITCODE -ne 0) {
        throw "La verificación del runtime portable falló."
    }
}

if ($NoZip) {
    Write-Host "Runtime portable listo en: $RuntimeDir"
    exit 0
}

Write-Host "[3/3] Generando paquete limpio de producción en dist\Alioth-Dockbar-Portable.zip..."
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
$stage = Join-Path $DistDir "Alioth-Dockbar-Portable"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
$zipPath = Join-Path $DistDir "Alioth-Dockbar-Portable.zip"
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

$pyExe = Join-Path $RuntimeDir "python.exe"
$zipScript = @'
import fnmatch, os, sys, zipfile

proj, zip_path = sys.argv[1], sys.argv[2]
prefix = "Alioth-Dockbar-Portable"
patterns = [
    "*.py", "settings.html", "favicon.ico",
    "iniciar.vbs", "iniciar.bat", "detener.bat",
    "requirements.txt", "README.md", "POWER_AUTOMATE_OUTLOOK.md",
    "todo_inbox.example.json",
]
already_compressed = {".png", ".jpg", ".jpeg", ".webp", ".zip", ".7z", ".gz"}

with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for entry in sorted(os.listdir(proj)):
        full = os.path.join(proj, entry)
        if os.path.isfile(full) and any(fnmatch.fnmatch(entry, p) for p in patterns):
            zf.write(full, f"{prefix}/{entry}")
    for folder in ("assets", "runtime"):
        base = os.path.join(proj, folder)
        for dp, dns, fs in os.walk(base):
            dns[:] = [d for d in dns if d != "__pycache__"]
            for fn in sorted(fs):
                fp = os.path.join(dp, fn)
                rel = os.path.relpath(fp, proj).replace("\\", "/")
                ext = os.path.splitext(fn)[1].lower()
                ctype = zipfile.ZIP_STORED if ext in already_compressed else zipfile.ZIP_DEFLATED
                zf.write(fp, f"{prefix}/{rel}", compress_type=ctype)
'@
$env:PYTHONHOME = ""
$env:PYTHONPATH = ""
$zipScript | & $pyExe -s - $ProjectDir $zipPath
if ($LASTEXITCODE -ne 0) { throw "Error generando ZIP portable." }

$mb = [math]::Round((Get-Item $zipPath).Length / 1MB, 2)
Write-Host "Listo: $zipPath ($mb MB)"
