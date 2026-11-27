import os
import sys
import time
import traceback
import ctypes
from ctypes import wintypes
from local_state import create_instance_mutex, close_instance_mutex, desktop_name
from app_paths import app_dir

# Enable True Native Per-Monitor V2 High-DPI Awareness for razor-sharp fonts and graphics
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

_get_desktop_name = desktop_name

def check_single_instance():
    """Ensures one instance per Windows Desktop using a scoped Win32 Mutex."""
    desk = _get_desktop_name()
    mutex_name = f"Local\\AliothDockbar_Mutex_{desk}"
    mutex = create_instance_mutex(mutex_name)
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
    """Shares the Tk event loop and image libraries between Dockbar and widgets."""
    import queue
    import copy
    from config import ConfigManager
    from dock_window import DockWindow
    from widget_runtime import WidgetRuntime, any_widget_enabled

    config_mgr = ConfigManager()
    if config_mgr.get("general", "start_with_windows", False):
        try:
            config_mgr.update_startup_shortcut(True)
        except Exception as e:
            print(f"[Startup] Sync warning: {e}")
    settings_window_ref = [None]
    dock_ref = [None]
    widgets_ref = [None]
    ui_queue = queue.SimpleQueue()
    ui_job = [None]

    def _sync_widgets():
        runtime = widgets_ref[0]
        if any_widget_enabled(config_mgr):
            if runtime is None or not runtime.active:
                widgets_ref[0] = WidgetRuntime(dock_ref[0].root, config_mgr, on_open_settings=open_settings)
            else:
                runtime.refresh()
        elif runtime:
            _stop_widgets()

    def _stop_widgets():
        if widgets_ref[0]:
            widgets_ref[0].close()
            widgets_ref[0] = None

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
    def _before_close():
        _stop_widgets()
        if ui_job[0]:
            dock.root.after_cancel(ui_job[0])
            ui_job[0] = None

    dock.on_before_close = _before_close
    dock_config = [copy.deepcopy({key: config_mgr.data.get(key) for key in ("general", "appearance", "items")})]

    def _drain_ui_queue():
        ui_job[0] = None
        try:
            while not ui_queue.empty():
                action = ui_queue.get_nowait()
                if callable(action):
                    action()
                elif action == "update":
                    current = {key: config_mgr.data.get(key) for key in ("general", "appearance", "items")}
                    if dock_ref[0] and current != dock_config[0]:
                        dock_ref[0].live_update()
                        dock_config[0] = copy.deepcopy(current)
                    _sync_widgets()
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
            ui_job[0] = dock_ref[0].root.after(40, _drain_ui_queue)

    ui_job[0] = dock.root.after(40, _drain_ui_queue)

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
        print(f"[Settings Server] No se inició otra instancia: {e}")
        dock.root.destroy()
        return True

    # Global Hotkey Manager (Spotlight & Launchpad)
    hotkey_mgr = None
    try:
        from hotkey_manager import GlobalHotkeyManager
        hotkey_mgr = GlobalHotkeyManager(
            on_spotlight=lambda: ui_queue.put(lambda: dock_ref[0] and dock_ref[0].toggle_spotlight()),
            on_launchpad=lambda: ui_queue.put(lambda: dock_ref[0] and dock_ref[0].toggle_launchpad())
        )
        hotkey_mgr.start()
        print("[Alioth Dockbar] Hotkey Manager iniciado.")
    except Exception as e:
        print(f"[Alioth Dockbar] Nota al registrar hotkeys: {e}")

    # Widgets reuse this process while retaining independent desktop windows.
    try:
        _sync_widgets()
        dock.root.mainloop()
        return getattr(dock, 'user_wants_exit', False)
    finally:
        if hotkey_mgr:
            try:
                hotkey_mgr.stop()
            except Exception:
                pass
        _before_close()
        settings_server.stop_server()
        if not dock.user_wants_exit:
            dock.close()


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

            except (ModuleNotFoundError, ImportError) as e:
                try:
                    with open(os.path.join(BASE_DIR, "crash.log"), "a", encoding="utf-8") as f:
                        f.write(f"\n--- FATAL IMPORT ERROR AT {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                        traceback.print_exc(file=f)
                except Exception:
                    pass
                print(f"[Alioth Dockbar] Error fatal de dependencia: {e}")
                if not is_autostart:
                    try:
                        user32.MessageBoxW(
                            0,
                            f"No se pudo iniciar Alioth Dockbar porque falta una dependencia de Python:\n\n{e}\n\n"
                            "Usa la carpeta portable 'runtime' o instala los requisitos con:\n"
                            "pip install -r requirements.txt",
                            "Alioth Dockbar — Dependencia faltante",
                            0x10,
                        )
                    except Exception:
                        pass
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
            close_instance_mutex(mutex)
        print(f"[Alioth Dockbar] Proceso finalizado. ({time.strftime('%Y-%m-%d %H:%M:%S')})")

if __name__ == "__main__":
    main()
