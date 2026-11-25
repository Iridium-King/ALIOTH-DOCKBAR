@echo off
echo Cerrando Alioth Dockbar y Widgets...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*ALIOTH DOCKBAR*main.py*' -or $_.CommandLine -like '*ALIOTH DOCKBAR*widgets_main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Listo.
