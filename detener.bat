@echo off
echo Cerrando Alioth Dockbar y Widgets...
set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=py"
"%PY_EXE%" -c "import win32com.client; wmi=win32com.client.GetObject('winmgmts:'); procs=wmi.ExecQuery(\"SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'\"); [p.Terminate() for p in procs if (p.CommandLine or '') and 'alioth dockbar' in p.CommandLine.lower() and ('main.py' in p.CommandLine.lower() or 'widgets_main.py' in p.CommandLine.lower())]"
echo Listo.
