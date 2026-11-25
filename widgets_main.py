import os
import sys
import gc
import time
import ctypes
import traceback
from ctypes import wintypes
import tkinter as tk

# Enable True Native Per-Monitor V2 High-DPI Awareness
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
from desktop_widgets import DesktopWidgetManager
import todo_store

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL
ERROR_ALREADY_EXISTS = 183


def _trim_working_set():
    try:
        gc.collect()
        minus_one = ctypes.c_size_t(-1).value
        kernel32.SetProcessWorkingSetSize(kernel32.GetCurrentProcess(), minus_one, minus_one)
    except Exception:
        pass


def _get_desktop_name():
    try:
        h_desk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        buf = ctypes.create_unicode_buffer(128)
        needed = wintypes.DWORD()
        if user32.GetUserObjectInformationW(h_desk, 2, buf, 256, ctypes.byref(needed)):
            safe = "".join(c if c.isalnum() else "_" for c in buf.value)
            return safe or "Default"
    except Exception:
        pass
    return "Default"


def check_single_instance():
    desk = _get_desktop_name()
    mutex_name = f"Local\\AliothWidgets_Mutex_{desk}"
    mutex = kernel32.CreateMutexW(None, False, mutex_name)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return mutex


def _any_widget_enabled(config_mgr):
    widgets = config_mgr.data.get("widgets", {})
    return any(isinstance(v, dict) and v.get("enabled") for v in widgets.values())


def main():
    mutex = check_single_instance()
    if not mutex:
        return

    print(f"[Alioth Widgets] Proceso independiente iniciado ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    todo_store.ensure_inbox_file()
    todo_store.ingest_todo_inbox()

    config_mgr = ConfigManager()
    if not _any_widget_enabled(config_mgr):
        print("[Alioth Widgets] Ningún widget activo; saliendo para ahorrar RAM.")
        kernel32.CloseHandle(mutex)
        return

    root = tk.Tk()
    root.withdraw()

    def open_settings():
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:49222/api/open_settings", timeout=2):
                return
        except Exception:
            pass
        try:
            import settings_server
            settings_server.open_settings_ui(config_mgr, tk_root=root)
        except Exception as e:
            print(f"[Alioth Widgets] Error opening settings: {e}")

    widget_mgr = DesktopWidgetManager(root, config_mgr, on_open_settings=open_settings)

    inbox_path = todo_store.DEFAULT_INBOX_PATH
    state = {
        "last_mtime": os.path.getmtime(config_mgr.config_path) if os.path.exists(config_mgr.config_path) else 0.0,
        "last_inbox_mtime": os.path.getmtime(inbox_path) if os.path.exists(inbox_path) else 0.0,
        "empty_since": 0.0,
        "last_trim": time.monotonic(),
    }

    # Initial gentle RAM trim 3.5s after startup once initial surfaces are rendered
    root.after(3500, _trim_working_set)

    def _watch_loop():
        try:
            # 1. Keep widgets visible on Show Desktop (Taskbar, Dockbar, Win+D) and below open apps
            widget_mgr.sync_z_order()

            # 2. Check Power Automate / Outlook inbox file (todo_inbox.json) by mtime (zero overhead)
            if os.path.exists(inbox_path):
                imt = os.path.getmtime(inbox_path)
                if imt != state["last_inbox_mtime"]:
                    state["last_inbox_mtime"] = imt
                    ing = todo_store.ingest_todo_inbox(inbox_path)
                    if os.path.exists(inbox_path):
                        state["last_inbox_mtime"] = os.path.getmtime(inbox_path)
                    if ing.get("added", 0) > 0:
                        print(f"[Alioth Widgets] Ingestadas {ing['added']} tareas desde todo_inbox.json")

            # 3. Sync live config.json updates from Settings UI, CLI, or MCP server
            if os.path.exists(config_mgr.config_path):
                mt = os.path.getmtime(config_mgr.config_path)
                if mt != state["last_mtime"]:
                    state["last_mtime"] = mt
                    config_mgr.data = config_mgr.load()
                    widget_mgr.live_update()

            # 4. Periodic low-RAM working-set trim when completely idle
            now_m = time.monotonic()
            if (now_m - state["last_trim"]) >= 45.0 and not widget_mgr.has_open_modals_or_resizing():
                state["last_trim"] = now_m
                _trim_working_set()

            if not _any_widget_enabled(config_mgr):
                if state["empty_since"] == 0.0:
                    state["empty_since"] = time.time()
                elif time.time() - state["empty_since"] >= 2.0:
                    print("[Alioth Widgets] Todos los widgets desactivados; liberando proceso.")
                    widget_mgr.close()
                    root.quit()
                    return
            else:
                state["empty_since"] = 0.0
        except Exception as e:
            print(f"[Alioth Widgets] Watch error: {e}")
        root.after(220, _watch_loop)

    root.after(220, _watch_loop)
    try:
        root.mainloop()
    except Exception:
        traceback.print_exc()
    finally:
        try:
            widget_mgr.close()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        try:
            kernel32.CloseHandle(mutex)
        except Exception:
            pass
        print(f"[Alioth Widgets] Proceso finalizado ({time.strftime('%Y-%m-%d %H:%M:%S')})")


if __name__ == "__main__":
    main()
