import os
import sys
import time
import traceback
import ctypes
from ctypes import wintypes

# Enable True Native Per-Monitor V2 High-DPI Awareness for razor-sharp fonts and graphics
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

# Rotate log if > 256 KB to keep disk usage small and clean
_log_path = os.path.join(BASE_DIR, "dock.log")
try:
    if os.path.exists(_log_path) and os.path.getsize(_log_path) > 262144:
        _old_log = _log_path + ".old"
        if os.path.exists(_old_log):
            os.remove(_old_log)
        os.replace(_log_path, _old_log)
    _dock_log = open(_log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = _dock_log
    sys.stderr = _dock_log
except Exception:
    pass

# Heavy UI modules (Tk, PIL, pywin32 OLE) are imported lazily inside run_dock(), AFTER the
# single-instance mutex check, so duplicate autostart launches exit with minimal RAM usage.

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
ERROR_ALREADY_EXISTS = 183

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
    """Ensures one instance per Windows Desktop using a scoped Win32 Mutex."""
    desk = _get_desktop_name()
    mutex_name = f"Local\\AliothDockbar_Mutex_{desk}"
    mutex = kernel32.CreateMutexW(None, False, mutex_name)
    last_error = kernel32.GetLastError()
    if last_error == ERROR_ALREADY_EXISTS:
        return None
    return mutex

def _notify_running_instance():
    """Signals the already-running instance to reveal the dockbar and open Settings."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:49222/api/reveal", timeout=2):
            return True
    except Exception:
        return False

def run_dock():
    """Runs a single session of DockWindow. Widgets run in their own dedicated process."""
    import queue
    import subprocess
    from config import ConfigManager
    from dock_window import DockWindow

    config_mgr = ConfigManager()
    if config_mgr.get("general", "start_with_windows", False):
        try:
            config_mgr.update_startup_shortcut(True)
        except Exception as e:
            print(f"[Startup] Sync warning: {e}")
    settings_window_ref = [None]
    dock_ref = [None]
    widgets_proc_ref = [None]
    ui_queue = queue.SimpleQueue()

    def _sync_widgets_process():
        widgets_cfg = config_mgr.data.get("widgets", {})
        any_enabled = any(isinstance(v, dict) and v.get("enabled") for v in widgets_cfg.values())
        proc = widgets_proc_ref[0]
        if any_enabled:
            if proc is None or proc.poll() is not None:
                try:
                    w_script = os.path.join(BASE_DIR, "widgets_main.py")
                    if os.path.exists(w_script):
                        widgets_proc_ref[0] = subprocess.Popen(
                            [sys.executable, w_script],
                            cwd=BASE_DIR,
                            creationflags=0x08000000  # CREATE_NO_WINDOW
                        )
                        print(f"[Alioth Dockbar] Proceso independiente de widgets lanzado (PID {widgets_proc_ref[0].pid}).")
                except Exception as ex:
                    print(f"[Alioth Dockbar] No se pudo lanzar widgets_main.py: {ex}")

    def _stop_widgets_process():
        proc = widgets_proc_ref[0]
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        widgets_proc_ref[0] = None

    def open_settings(pick_icon_id=None, child_id=None):
        try:
            import settings_server
            settings_server.open_settings_ui(
                config_mgr,
                on_applied=lambda: ui_queue.put("update"),
                tk_root=dock_ref[0].root if dock_ref[0] else None,
                pick_icon_id=pick_icon_id,
                child_id=child_id
            )
        except Exception as e:
            print(f"[Settings Server] Falling back to Tkinter modal: {e}")
            try:
                from settings_window import SettingsWindow
                if settings_window_ref[0] is None or not settings_window_ref[0].root.winfo_exists():
                    settings_window_ref[0] = SettingsWindow(config_mgr, on_applied=lambda: ui_queue.put("update"))
                else:
                    settings_window_ref[0].root.lift()
            except Exception as e2:
                print(f"[Settings] Error opening Tkinter settings: {e2}")

    dock = DockWindow(config_mgr, on_open_settings=open_settings)
    dock_ref[0] = dock

    def _drain_ui_queue():
        try:
            while not ui_queue.empty():
                action = ui_queue.get_nowait()
                if callable(action):
                    action()
                elif action == "update":
                    if dock_ref[0]:
                        dock_ref[0].live_update()
                    _sync_widgets_process()
                elif action == "reveal":
                    if dock_ref[0]:
                        dock_ref[0].force_reveal()
                elif action == "preview_effect":
                    if dock_ref[0]:
                        dock_ref[0].preview_launch_effect()
                elif action == "open_settings":
                    open_settings()
        except Exception as ex:
            print(f"[UI Queue] Error: {ex}")
        if dock_ref[0] and not getattr(dock_ref[0], "user_wants_exit", False):
            dock_ref[0].root.after(40, _drain_ui_queue)

    dock.root.after(40, _drain_ui_queue)

    # Pre-start lightweight HTML settings API server in background thread
    try:
        import settings_server
        settings_server.start_server(
            config_mgr,
            on_applied=lambda: ui_queue.put("update"),
            tk_root=dock.root,
            on_reveal=lambda: ui_queue.put("reveal"),
            on_open_settings=lambda: ui_queue.put("open_settings"),
            on_preview_effect=lambda: ui_queue.put("preview_effect"),
            ui_dispatch=lambda fn: ui_queue.put(fn),
            dock_status_fn=dock.get_debug_status
        )
    except Exception as e:
        print(f"[Settings Server] Note on pre-start: {e}")

    # Launch standalone widgets process only if at least one widget is enabled
    _sync_widgets_process()

    dock.root.mainloop()

    wants_exit = getattr(dock, "user_wants_exit", False)
    if wants_exit:
        try:
            import settings_server
            settings_server.stop_server()
        except Exception:
            pass
        _stop_widgets_process()

    return wants_exit

def main():
    is_autostart = "--autostart" in sys.argv
    mutex = check_single_instance()
    if not mutex:
        if is_autostart:
            print("[Alioth Dockbar] Ya hay una instancia en ejecución (autostart silencioso).")
            return
        print("[Alioth Dockbar] Ya hay una instancia en ejecución. Mostrando dockbar y panel...")
        _notify_running_instance()
        return

    try:
        crash_count = 0
        last_crash_time = 0

        while True:
            try:
                print(f"[Alioth Dockbar] Iniciando dockbar... ({time.strftime('%Y-%m-%d %H:%M:%S')})")
                user_wants_exit = run_dock()

                if user_wants_exit:
                    print("[Alioth Dockbar] Salida solicitada por el usuario.")
                    break
                else:
                    print("[Alioth Dockbar] Mainloop terminó sin solicitud de salida. Reiniciando...")
                    time.sleep(1.5)
                    continue

            except SystemExit:
                print("[Alioth Dockbar] SystemExit capturado. Saliendo.")
                break

            except Exception as e:
                now = time.time()
                try:
                    with open(os.path.join(BASE_DIR, "crash.log"), "a", encoding="utf-8") as f:
                        f.write(f"\n--- CRASH AT {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                        traceback.print_exc(file=f)
                except Exception:
                    pass

                print(f"[Alioth Dockbar] Crash: {e}")

                if now - last_crash_time < 8:
                    crash_count += 1
                else:
                    crash_count = 1
                last_crash_time = now

                if crash_count > 5:
                    print("[Alioth Dockbar] Demasiados crashes seguidos, esperando 10s...")
                    time.sleep(10)
                    crash_count = 0
                else:
                    time.sleep(2)

                print("[Alioth Dockbar] Auto-reiniciando tras crash inesperado...")

    finally:
        try:
            import settings_server
            settings_server.stop_server()
        except Exception:
            pass
        if mutex:
            kernel32.CloseHandle(mutex)
        print(f"[Alioth Dockbar] Proceso finalizado. ({time.strftime('%Y-%m-%d %H:%M:%S')})")

if __name__ == "__main__":
    main()
