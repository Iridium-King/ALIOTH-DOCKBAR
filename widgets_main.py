import os
import sys
import time
import ctypes
import traceback
from ctypes import wintypes
from local_state import create_instance_mutex, close_instance_mutex, desktop_name
from app_paths import app_dir
import tkinter as tk

# Enable True Native Per-Monitor V2 High-DPI Awareness
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

BASE_DIR = app_dir()
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Rotate widgets.log if > 128 KB
_log_path = os.path.join(BASE_DIR, "widgets.log")
try:
    if os.path.exists(_log_path) and os.path.getsize(_log_path) > 131072:
        _old = _log_path + ".old"
        if os.path.exists(_old):
            os.remove(_old)
        os.replace(_log_path, _old)
    _wlog = open(_log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = _wlog
    sys.stderr = _wlog
except Exception:
    pass

from config import ConfigManager
from widget_runtime import WidgetRuntime, any_widget_enabled


def main():
    config_mgr = ConfigManager()
    if not any_widget_enabled(config_mgr):
        return
    root = tk.Tk()
    root.withdraw()

    def open_settings():
        try:
            import urllib.request
            with urllib.request.urlopen('http://127.0.0.1:49222/api/open_settings', timeout=2):
                return
        except Exception:
            pass
        try:
            import settings_server
            settings_server.open_settings_ui(config_mgr, tk_root=root)
        except Exception as exc:
            print(f'[Widgets] Error al abrir configuración: {exc}')

    runtime = None
    try:
        runtime = WidgetRuntime(root, config_mgr, on_open_settings=open_settings, on_idle=root.quit)
        if runtime.active:
            root.mainloop()
    finally:
        if runtime:
            runtime.close()
        root.destroy()


if __name__ == '__main__':
    main()
