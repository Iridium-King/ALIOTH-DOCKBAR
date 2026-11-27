@echo off
cd /d "%~dp0"
set "PYTHONW="
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
    if not defined PYTHONW (
        if exist "%%~D\pythonw.exe" if exist "%%~D\Lib\encodings\__init__.py" set "PYTHONW=%%~D\pythonw.exe"
    )
)
if not defined PYTHONW set "PYTHONW=pyw.exe"
start "" "%PYTHONW%" "%~dp0main.py"
exit
