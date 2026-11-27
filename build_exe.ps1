param(
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyExe = "$env:LOCALAPPDATA\Programs\Python\Python315\python.exe"
if (-not (Test-Path $PyExe)) {
    throw "No se encontró Python en $PyExe"
}

Write-Host "[1/4] Verificando PyInstaller..."
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $PyExe -m PyInstaller --version 2>&1 | Out-Null
$hasPi = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEap
if (-not $hasPi) {
    & $PyExe -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "No se pudo instalar PyInstaller." }
}

$Version = (& $PyExe -c "import sys; sys.path.insert(0, r'$ProjectDir'); from app_paths import APP_VERSION; print(APP_VERSION)").Trim()
Write-Host "[2/4] Compilando AliothDockbar v$Version (--onedir)..."

$DistExeDir = Join-Path $ProjectDir "dist\AliothDockbar"
$BuildDir = Join-Path $ProjectDir "build"
if (Test-Path $DistExeDir) { Remove-Item -Recurse -Force $DistExeDir }
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }

$hidden = @(
    "app_paths", "local_state", "config", "dock_window", "widget_runtime",
    "desktop_widgets", "settings_server", "settings_window", "app_launcher",
    "app_search", "group_flyout", "window_preview", "media_capsule",
    "themes", "todo_store", "custom_menu", "icon_extractor", "window_utils",
    "PIL._tkinter_finder",
    "win32com.client", "win32gui", "win32con", "win32api", "win32process",
    "pythoncom", "pywintypes", "pywin32_bootstrap"
)
$argsList = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--noconsole",
    "--name", "AliothDockbar",
    "--icon", (Join-Path $ProjectDir "assets\app_icon.ico"),
    "--distpath", (Join-Path $ProjectDir "dist"),
    "--workpath", $BuildDir,
    "--specpath", $BuildDir,
    "--paths", $ProjectDir
)
foreach ($h in $hidden) {
    $argsList += @("--hidden-import", $h)
}
$argsList += (Join-Path $ProjectDir "main.py")

& $PyExe @argsList
if ($LASTEXITCODE -ne 0) { throw "Error al compilar AliothDockbar.exe con PyInstaller." }

Write-Host "[3/4] Copiando recursos externos junto a AliothDockbar.exe..."
foreach ($f in @("settings.html", "favicon.ico", "detener.bat", "README.md", "POWER_AUTOMATE_OUTLOOK.md", "todo_inbox.example.json")) {
    $src = Join-Path $ProjectDir $f
    if (Test-Path $src) {
        Copy-Item $src -Destination $DistExeDir -Force
    }
}
$assetsDst = Join-Path $DistExeDir "assets"
if (Test-Path $assetsDst) { Remove-Item -Recurse -Force $assetsDst }
& robocopy (Join-Path $ProjectDir "assets") $assetsDst /E /MT:8 /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Error copiando assets con robocopy (code $LASTEXITCODE)." }

# Also clean temp PyInstaller build directory
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }

if (-not $NoZip) {
    Write-Host "[4/4] Empaquetando dist\AliothDockbar-v$Version-Portable-EXE.zip..."
    $zipPath = Join-Path $ProjectDir "dist\AliothDockbar-v$Version-Portable-EXE.zip"
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
    $zipScript = @'
import os, sys, zipfile
src_dir, zip_path = sys.argv[1], sys.argv[2]
prefix = os.path.basename(src_dir.rstrip("\\/"))
stored = {".png", ".jpg", ".jpeg", ".webp", ".zip", ".7z", ".gz"}
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for dp, _, fs in os.walk(src_dir):
        for fn in sorted(fs):
            fp = os.path.join(dp, fn)
            rel = os.path.relpath(fp, src_dir).replace("\\", "/")
            ext = os.path.splitext(fn)[1].lower()
            zf.write(fp, f"{prefix}/{rel}", compress_type=zipfile.ZIP_STORED if ext in stored else zipfile.ZIP_DEFLATED)
'@
    $zipScript | & $PyExe -s - $DistExeDir $zipPath
    if ($LASTEXITCODE -ne 0) { throw "Error generando ZIP del ejecutable." }
    $mb = [math]::Round((Get-Item $zipPath).Length / 1MB, 2)
    Write-Host "ZIP EXE listo: $zipPath ($mb MB)"
}

Write-Host "Ejecutable listo en: $(Join-Path $DistExeDir 'AliothDockbar.exe')"
