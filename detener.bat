@echo off
echo Cerrando Alioth Dockbar y Widgets...
taskkill /F /IM AliothDockbar.exe >nul 2>&1
set "PY_EXE="
for %%D in (
    "%~dp0runtime"
    "%~dp0python"
    "%LOCALAPPDATA%\Programs\Python\Python315"
    "%LOCALAPPDATA%\Programs\Python\Python314"
    "%LOCALAPPDATA%\Programs\Python\Python313"
    "%LOCALAPPDATA%\Programs\Python\Python312"
    "%LOCALAPPDATA%\Programs\Python\Python311"
    "C:\Program Files\Python315"
    "C:\Program Files\Python314"
    "C:\Program Files\Python313"
    "C:\Program Files\Python312"
) do (
    if not defined PY_EXE (
        if exist "%%~D\python.exe" if exist "%%~D\Lib\encodings\__init__.py" set "PY_EXE=%%~D\python.exe"
    )
)
if not defined PY_EXE if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not defined PY_EXE set "PY_EXE=py"
"%PY_EXE%" -c "import win32com.client; wmi=win32com.client.GetObject('winmgmts:'); procs=wmi.ExecQuery(\"SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'\"); [p.Terminate() for p in procs if (p.CommandLine or '') and ('alioth dockbar' in p.CommandLine.lower() or 'alioth-dockbar' in p.CommandLine.lower()) and ('main.py' in p.CommandLine.lower() or 'widgets_main.py' in p.CommandLine.lower())]"
echo Listo.
