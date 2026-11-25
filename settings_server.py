import os
import io
import sys
import json
import uuid
import time
import hashlib
import atexit
import ctypes
import threading
import subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.parse
import tkinter as tk
from tkinter import filedialog, colorchooser

SERVER_PORT = 49222
SERVER_HOST = "127.0.0.1"
PANEL_WINDOW_TITLE = "Configuración - Alioth Dockbar"

class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

_server_instance = None
_server_thread = None
_config_mgr_ref = None
_on_applied_ref = None
_tk_root_ref = None
_on_reveal_ref = None
_on_open_settings_ref = None
_on_preview_effect_ref = None
_ui_dispatch_ref = None

_panel_lock = threading.Lock()
_active_panel_token = None
_panel_close_epoch = 0
_panel_clients = []  # list of (token, start_epoch, threading.Event)
_server_shutting_down = False
_last_panel_open_ts = 0.0
_dock_status_fn = None


def _close_native_panel_windows():
    """Sends WM_CLOSE to existing Edge/Chrome --app= windows belonging to Alioth Dockbar."""
    closed_count = 0
    try:
        user32 = ctypes.windll.user32
        WM_CLOSE = 0x0010
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        targets = []

        def _enum_cb(hwnd, lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                title_buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title_buf, 256)
                title = title_buf.value.strip()
                if not title:
                    return True
                # Only target dedicated app-mode windows titled "Configuración - Alioth Dockbar"
                # (never touch regular multi-tab browser windows whose titles end with "- Microsoft Edge" / "- Google Chrome")
                if title == PANEL_WINDOW_TITLE or (
                    title.startswith(PANEL_WINDOW_TITLE)
                    and "Microsoft" not in title
                    and "Chrome" not in title
                    and "Edge" not in title
                ):
                    cls_buf = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, cls_buf, 128)
                    if cls_buf.value.startswith("Chrome_WidgetWin"):
                        targets.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        for hwnd in targets:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                closed_count += 1
            except Exception:
                pass
    except Exception:
        pass
    return closed_count


def has_active_panel():
    with _panel_lock:
        return bool(_panel_clients) or (_active_panel_token is not None and (time.time() - _last_panel_open_ts) < 15.0)


def close_settings_ui(next_token=None, force=True, reason="unspecified"):
    """Immediately closes all open HTML settings/icon-picker panels via SSE push and Win32 WM_CLOSE."""
    global _active_panel_token, _panel_close_epoch
    # Protect a panel that is literally mid-launching (< 0.9s) from a stray double-click on the dockbar
    if not force and next_token is None:
        with _panel_lock:
            just_opened = (time.time() - _last_panel_open_ts) < 0.9
            has_connected = any(tok == _active_panel_token for tok, _ep, _ev in _panel_clients)
            if just_opened and not has_connected:
                return 0
    with _panel_lock:
        _panel_close_epoch += 1
        _active_panel_token = next_token
        clients = list(_panel_clients)
    for tok, _ep, ev in clients:
        if next_token is None or tok != next_token:
            ev.set()
    closed = _close_native_panel_windows()
    if next_token is None:
        app_search_mod = sys.modules.get("app_search")
        if app_search_mod is not None and hasattr(app_search_mod, "clear_caches"):
            try:
                app_search_mod.clear_caches()
            except Exception:
                pass
    if closed or clients:
        print(f"[Settings UI] close_settings_ui(reason={reason}, next={next_token}, sse={len(clients)}, native={closed})")
    return closed


def stop_server():
    """Closes all open HTML panels and cleanly stops the local settings HTTP server."""
    global _server_instance, _server_thread, _server_shutting_down
    _server_shutting_down = True
    close_settings_ui(next_token=None)
    srv = _server_instance
    _server_instance = None
    _server_thread = None
    if srv is not None:
        try:
            threading.Thread(target=srv.shutdown, daemon=True).start()
            srv.server_close()
        except Exception:
            pass


atexit.register(close_settings_ui)

def _run_on_tk(fn, timeout=180):
    """Executes a Tkinter UI function safely on the mainloop thread via lock-free queue."""
    if _ui_dispatch_ref is None and _tk_root_ref is None:
        return fn()
    ev = threading.Event()
    result = [None, None]

    def _task():
        try:
            result[0] = fn()
        except Exception as ex:
            result[1] = ex
        finally:
            ev.set()

    if _ui_dispatch_ref is not None:
        _ui_dispatch_ref(_task)
    else:
        _tk_root_ref.after(0, _task)
    if not ev.wait(timeout=timeout):
        raise TimeoutError("UI dialog timed out")
    if result[1]:
        raise result[1]
    return result[0]

def _open_native_file_dialog(title="Seleccionar Archivo", filetypes=None):
    def _dlg():
        top = tk.Toplevel(_tk_root_ref) if _tk_root_ref else tk.Tk()
        top.withdraw()
        top.attributes("-topmost", True)
        try:
            top.update_idletasks()
            kw = {"title": title, "parent": top}
            if filetypes:
                kw["filetypes"] = filetypes
            return filedialog.askopenfilename(**kw)
        finally:
            try:
                top.destroy()
            except Exception:
                pass
    return _run_on_tk(_dlg)

class SettingsApiHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _send_bytes(self, content, content_type, status=200, cache_seconds=300):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            if cache_seconds and cache_seconds > 0:
                self.send_header("Cache-Control", f"public, max-age={int(cache_seconds)}")
            else:
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        base_dir = os.path.dirname(os.path.abspath(__file__))

        if path == "/api/config":
            if _config_mgr_ref:
                self._send_json(_config_mgr_ref.data)
            else:
                self._send_json({"error": "No config manager"}, 500)
            return

        elif path == "/api/ping":
            with _panel_lock:
                tok = _active_panel_token
                ep = _panel_close_epoch
                n_cli = len(_panel_clients)
            self._send_json({"ok": not _server_shutting_down, "active_token": tok, "epoch": ep, "clients": n_cli})
            return

        elif path == "/api/dock_status":
            info = {}
            if _dock_status_fn:
                try:
                    info = _dock_status_fn() or {}
                except Exception as ex:
                    info = {"error": str(ex)}
            self._send_json(info)
            return

        elif path == "/api/todo/tasks":
            try:
                import todo_store
                nb = qs.get("notebook", [""])[0] or None
                st = qs.get("status", ["pending"])[0]
                q = qs.get("q", [""])[0]
                self._send_json(todo_store.list_tasks(notebook=nb, status=st, query=q))
            except Exception as ex:
                self._send_json({"error": str(ex)}, 500)
            return

        elif path == "/api/themes":
            try:
                import themes
                self._send_json(themes.get_themes_payload(_config_mgr_ref))
            except Exception as ex:
                self._send_json({"error": str(ex)}, 500)
            return

        elif path == "/api/battery_status":
            try:
                from window_utils import get_battery_status
                self._send_json(get_battery_status())
            except Exception as ex:
                self._send_json({"error": str(ex)}, 500)
            return

        elif path == "/api/panel_events":
            token = qs.get("token", [""])[0]
            ev = threading.Event()
            with _panel_lock:
                start_ep = _panel_close_epoch
                cur_tok = _active_panel_token
                # If this panel was already superseded/closed before connecting, close immediately!
                immediate_close = _server_shutting_down or (bool(token) and cur_tok != token)
                client_entry = (token, start_ep, ev)
                if not immediate_close:
                    _panel_clients.append(client_entry)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if immediate_close:
                    self.wfile.write(b"event: close\ndata: superseded\n\n")
                    self.wfile.flush()
                    return
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while not _server_shutting_down:
                    triggered = ev.wait(timeout=10.0)
                    if triggered or _server_shutting_down:
                        self.wfile.write(b"event: close\ndata: now\n\n")
                        self.wfile.flush()
                        break
                    with _panel_lock:
                        stale = (bool(token) and _active_panel_token != token) or (not token and _panel_close_epoch > start_ep)
                    if stale:
                        self.wfile.write(b"event: close\ndata: stale\n\n")
                        self.wfile.flush()
                        break
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                with _panel_lock:
                    try:
                        _panel_clients.remove(client_entry)
                    except ValueError:
                        pass
            return

        elif path == "/api/reveal":
            if _on_reveal_ref:
                _on_reveal_ref()
            self._send_json({"status": "ok"})
            return

        elif path == "/api/open_settings":
            if _on_open_settings_ref:
                _on_open_settings_ref()
            self._send_json({"status": "ok"})
            return

        elif path == "/api/installed_pwas":
            try:
                import app_search
                force = qs.get("force", ["0"])[0] in ("1", "true", "yes")
                pwas = app_search.get_installed_pwas(force=force)
                self._send_json({"pwas": pwas})
            except Exception as e:
                self._send_json({"pwas": [], "error": str(e)}, 500)
            return

        elif path == "/api/search_apps":
            try:
                import app_search
                q = qs.get("q", [""])[0]
                limit = int(qs.get("limit", ["50"])[0])
                category = qs.get("category", ["all"])[0]
                force = qs.get("force", ["0"])[0] in ("1", "true", "yes")
                hits = app_search.search_apps(q, limit=limit, category=category, force=force)
                self._send_json({"apps": hits})
            except Exception as e:
                self._send_json({"apps": [], "error": str(e)}, 500)
            return

        elif path == "/api/icon_packs":
            try:
                packs_root = os.path.join(base_dir, "assets", "icon_packs")
                packs = []
                if os.path.isdir(packs_root):
                    for pack_name in sorted(os.listdir(packs_root)):
                        pack_dir = os.path.join(packs_root, pack_name)
                        if not os.path.isdir(pack_dir):
                            continue
                        icons = []
                        for fname in sorted(os.listdir(pack_dir)):
                            if fname.lower().endswith(".png"):
                                rel_disk = os.path.join(pack_dir, fname)
                                mtime = int(os.path.getmtime(rel_disk))
                                url_path = "/assets/icon_packs/" + urllib.parse.quote(pack_name) + "/" + urllib.parse.quote(fname) + f"?v={mtime}"
                                with open(rel_disk, "rb") as fh:
                                    content_hash = hashlib.md5(fh.read()).hexdigest()[:12]
                                icons.append({
                                    "name": os.path.splitext(fname)[0],
                                    "url": url_path,
                                    "path": rel_disk,
                                    "hash": content_hash
                                })
                        if icons:
                            packs.append({"name": pack_name, "icons": icons})
                self._send_json({"packs": packs})
            except Exception as e:
                self._send_json({"packs": [], "error": str(e)}, 500)
            return

        elif path == "/api/icon_preview":
            try:
                import icon_extractor
                item_idx = qs.get("idx", [None])[0]
                child_idx = qs.get("child_idx", [None])[0]
                raw_path = qs.get("path", [""])[0]
                name_hint = qs.get("name", ["App"])[0]
                im = None
                if item_idx is not None and _config_mgr_ref:
                    items = _config_mgr_ref.get_items()
                    idx = int(item_idx)
                    if 0 <= idx < len(items):
                        target_it = items[idx]
                        if child_idx is not None:
                            children = target_it.get("items", target_it.get("children", []))
                            cidx = int(child_idx)
                            if 0 <= cidx < len(children):
                                target_it = children[cidx]
                        im = icon_extractor.get_or_create_icon(target_it, size=48)
                elif raw_path:
                    temp_it = {"type": "app", "name": name_hint, "target": raw_path, "icon_path": ""}
                    im = icon_extractor.get_or_create_icon(temp_it, size=48)
                if im is None:
                    im = icon_extractor.create_fallback_icon(name_hint, size=48)
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                self._send_bytes(buf.getvalue(), "image/png", cache_seconds=0)
            except Exception:
                self.send_error(404, "Icon error")
            return

        elif path == "/favicon.ico":
            fav_candidates = [
                os.path.join(base_dir, "assets", "favicon.ico"),
                os.path.join(base_dir, "favicon.ico"),
            ]
            for fc in fav_candidates:
                if os.path.exists(fc):
                    with open(fc, "rb") as f:
                        self._send_bytes(f.read(), "image/x-icon")
                    return

        # Serve static HTML/CSS/JS/Images safely inside base_dir
        if path in ("/", "/index.html", "/settings"):
            file_path = os.path.join(base_dir, "settings.html")
            content_type = "text/html; charset=utf-8"
        else:
            rel_path = path.lstrip("/").replace("/", os.sep)
            file_path = os.path.abspath(os.path.join(base_dir, rel_path))
            if not file_path.startswith(os.path.abspath(base_dir)):
                self.send_error(403, "Forbidden")
                return
            low = file_path.lower()
            if low.endswith(".css"):
                content_type = "text/css"
            elif low.endswith(".js"):
                content_type = "application/javascript"
            elif low.endswith(".png"):
                content_type = "image/png"
            elif low.endswith((".jpg", ".jpeg")):
                content_type = "image/jpeg"
            elif low.endswith(".svg"):
                content_type = "image/svg+xml"
            elif low.endswith(".ico"):
                content_type = "image/x-icon"
            else:
                content_type = "application/octet-stream"

        if os.path.exists(file_path) and os.path.isfile(file_path):
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                cache_sec = 0 if content_type.startswith("text/html") else 300
                self._send_bytes(content, content_type, cache_seconds=cache_sec)
            except Exception as e:
                self.send_error(500, f"Error reading file: {e}")
        else:
            self.send_error(404, "File Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if path == "/api/config":
            if _config_mgr_ref:
                for section, vals in payload.items():
                    if section == "widgets" and isinstance(vals, dict):
                        if "widgets" not in _config_mgr_ref.data or not isinstance(_config_mgr_ref.data["widgets"], dict):
                            _config_mgr_ref.data["widgets"] = {}
                        for w_id, w_vals in vals.items():
                            if isinstance(w_vals, dict):
                                if w_id not in _config_mgr_ref.data["widgets"] or not isinstance(_config_mgr_ref.data["widgets"][w_id], dict):
                                    _config_mgr_ref.data["widgets"][w_id] = {}
                                _config_mgr_ref.data["widgets"][w_id].update(w_vals)
                                _config_mgr_ref.mark_dirty("widgets", w_id)
                            else:
                                _config_mgr_ref.data["widgets"][w_id] = w_vals
                                _config_mgr_ref.mark_dirty("widgets")
                    elif isinstance(vals, dict):
                        if section not in _config_mgr_ref.data or not isinstance(_config_mgr_ref.data[section], dict):
                            _config_mgr_ref.data[section] = {}
                        _config_mgr_ref.data[section].update(vals)
                        _config_mgr_ref.mark_dirty(section)
                    else:
                        _config_mgr_ref.data[section] = vals
                        _config_mgr_ref.mark_dirty(section)
                _config_mgr_ref.save()
                if "general" in payload and isinstance(payload["general"], dict) and "start_with_windows" in payload["general"]:
                    _config_mgr_ref.update_startup_shortcut(payload["general"]["start_with_windows"])
                if _on_applied_ref:
                    _on_applied_ref()
                self._send_json({"status": "ok", "config": _config_mgr_ref.data})
            else:
                self._send_json({"error": "No config manager"}, 500)
            return

        elif path == "/api/set_appearance":
            if _config_mgr_ref:
                for k, v in payload.items():
                    _config_mgr_ref.set("appearance", k, v)
                if _on_applied_ref:
                    _on_applied_ref()
                self._send_json({"status": "ok"})
            else:
                self._send_json({"error": "No config manager"}, 500)
            return

        elif path == "/api/set_widget":
            w_id = payload.get("widget_id")
            settings = payload.get("settings", {})
            if _config_mgr_ref and w_id:
                for k, v in settings.items():
                    _config_mgr_ref.set_widget(w_id, k, v)
                if _on_applied_ref:
                    _on_applied_ref()
                self._send_json({"status": "ok"})
            else:
                self._send_json({"error": "Invalid widget params"}, 400)
            return

        elif path == "/api/save_panel_bounds":
            if _config_mgr_ref:
                bounds = _config_mgr_ref.data.get("panel_bounds")
                if not isinstance(bounds, dict):
                    bounds = {}
                try:
                    if payload.get("width") is not None:
                        bounds["width"] = max(520, min(2400, int(payload["width"])))
                    if payload.get("height") is not None:
                        bounds["height"] = max(420, min(1600, int(payload["height"])))
                    if payload.get("x") is not None:
                        bounds["x"] = int(payload["x"])
                    if payload.get("y") is not None:
                        bounds["y"] = int(payload["y"])
                    _config_mgr_ref.data["panel_bounds"] = bounds
                    _config_mgr_ref.save()
                except Exception:
                    pass
                self._send_json({"status": "ok", "panel_bounds": _config_mgr_ref.data.get("panel_bounds", {})})
            else:
                self._send_json({"error": "No config manager"}, 500)
            return

        elif path == "/api/pick_color":
            try:
                initial = payload.get("initial", "#181a20")
                title = payload.get("title", "Seleccionar Color")
                def _pick():
                    top = tk.Toplevel(_tk_root_ref) if _tk_root_ref else tk.Tk()
                    top.withdraw()
                    top.attributes("-topmost", True)
                    try:
                        return colorchooser.askcolor(initialcolor=initial, title=title, parent=top)[1]
                    finally:
                        top.destroy()
                color = _run_on_tk(_pick)
                self._send_json({"color": color})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/pick_file":
            try:
                title = payload.get("title", "Seleccionar Programa, Archivo o Acceso")
                fpath = _open_native_file_dialog(title=title)
                if fpath:
                    import icon_extractor
                    item_data = icon_extractor.resolve_target_and_icon(fpath)
                    self._send_json({
                        "path": fpath,
                        "name": item_data["name"],
                        "target": item_data["target"],
                        "args": item_data.get("args", ""),
                        "icon_path": item_data["icon_path"]
                    })
                else:
                    self._send_json({"path": None})
            except Exception as e:
                print(f"[Settings Server] pick_file error: {e}")
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/pick_icon":
            try:
                title = payload.get("title", "Seleccionar Icono")
                fpath = _open_native_file_dialog(
                    title=title,
                    filetypes=[("Archivos de icono e imagen", "*.png;*.ico;*.dll;*.exe;*.jpg;*.jpeg;*.bmp"), ("Todos", "*.*")]
                )
                if fpath:
                    import icon_extractor
                    cached = icon_extractor.import_custom_icon(fpath)
                    self._send_json({"icon_path": cached or fpath})
                else:
                    self._send_json({"icon_path": None})
            except Exception as e:
                print(f"[Settings Server] pick_icon error: {e}")
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/add_searched_app":
            try:
                import icon_extractor
                name = payload.get("name", "Aplicación")
                target = payload.get("target", "")
                args = payload.get("args", "")
                icon_src = payload.get("icon_src", "") or target
                if not target:
                    self._send_json({"error": "No target"}, 400)
                    return

                icon_path = ""
                if icon_src and (icon_src.lower().startswith("shell:") or os.path.exists(icon_src)):
                    if os.path.exists(icon_src) and icon_src.lower().endswith(".lnk"):
                        resolved = icon_extractor.resolve_target_and_icon(icon_src)
                        icon_path = resolved.get("icon_path", "")
                    else:
                        temp_it = {"type": "app", "name": name, "target": icon_src, "args": args, "icon_path": ""}
                        im = icon_extractor.get_or_create_icon(temp_it, size=128)
                        if im:
                            import hashlib
                            h = hashlib.md5(f"{target}{args}".encode()).hexdigest()
                            out = os.path.join(icon_extractor.CACHE_DIR, f"v2_search_{h}.png")
                            im.save(out, "PNG")
                            icon_path = out

                new_item = {
                    "type": "app",
                    "name": name,
                    "target": target,
                    "args": args,
                    "icon_path": icon_path,
                    "run_as_admin": False
                }
                if _config_mgr_ref:
                    _config_mgr_ref.add_item(new_item)
                    if _on_applied_ref:
                        _on_applied_ref()
                    self._send_json({"status": "ok", "items": _config_mgr_ref.get_items()})
                else:
                    self._send_json({"error": "No config manager"}, 500)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/set_item_icon":
            try:
                idx = payload.get("index")
                child_idx = payload.get("child_index")
                icon_path = payload.get("icon_path", "")
                if _config_mgr_ref and idx is not None and icon_path:
                    items = _config_mgr_ref.get_items()
                    if 0 <= int(idx) < len(items):
                        import icon_extractor
                        cached = icon_extractor.import_custom_icon(icon_path) or icon_path
                        target_it = items[int(idx)]
                        if child_idx is not None:
                            children = target_it.get("items", target_it.get("children", []))
                            cidx = int(child_idx)
                            if 0 <= cidx < len(children):
                                children[cidx]["icon_path"] = cached
                            else:
                                self._send_json({"error": "Invalid child index"}, 400)
                                return
                        else:
                            target_it["icon_path"] = cached
                        _config_mgr_ref.set_items(items)
                        if _on_applied_ref:
                            _on_applied_ref()
                        self._send_json({"status": "ok", "items": _config_mgr_ref.get_items()})
                        return
                self._send_json({"error": "Invalid params"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/reset_item_icon":
            try:
                idx = payload.get("index")
                child_idx = payload.get("child_index")
                if _config_mgr_ref and idx is not None:
                    items = _config_mgr_ref.get_items()
                    if 0 <= int(idx) < len(items):
                        import icon_extractor
                        target_it = items[int(idx)]
                        if child_idx is not None:
                            children = target_it.get("items", target_it.get("children", []))
                            cidx = int(child_idx)
                            if 0 <= cidx < len(children):
                                children[cidx]["icon_path"] = icon_extractor.restore_original_icon(children[cidx])
                            else:
                                self._send_json({"error": "Invalid child index"}, 400)
                                return
                        else:
                            target_it["icon_path"] = icon_extractor.restore_original_icon(target_it)
                        _config_mgr_ref.set_items(items)
                        if _on_applied_ref:
                            _on_applied_ref()
                        self._send_json({"status": "ok", "items": _config_mgr_ref.get_items()})
                        return
                self._send_json({"error": "Invalid params"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/add_item":
            item = payload.get("item")
            if _config_mgr_ref and item:
                _config_mgr_ref.add_item(item)
                if _on_applied_ref:
                    _on_applied_ref()
                self._send_json({"status": "ok", "items": _config_mgr_ref.get_items()})
            else:
                self._send_json({"error": "No item"}, 400)
            return

        elif path == "/api/delete_item":
            idx = payload.get("index")
            if _config_mgr_ref and idx is not None:
                _config_mgr_ref.remove_item(idx)
                if _on_applied_ref:
                    _on_applied_ref()
                self._send_json({"status": "ok", "items": _config_mgr_ref.get_items()})
            else:
                self._send_json({"error": "No index"}, 400)
            return

        elif path == "/api/move_item":
            src = payload.get("from")
            dst = payload.get("to")
            if _config_mgr_ref and src is not None and dst is not None:
                _config_mgr_ref.move_item(src, dst)
                if _on_applied_ref:
                    _on_applied_ref()
                self._send_json({"status": "ok", "items": _config_mgr_ref.get_items()})
            else:
                self._send_json({"error": "Invalid move indices"}, 400)
            return

        elif path == "/api/add_action":
            action_type = payload.get("action_type")
            name = payload.get("name")
            if _config_mgr_ref and action_type:
                new_act = {
                    "type": "action",
                    "name": name or action_type.replace("_", " ").title(),
                    "action_type": action_type
                }
                _config_mgr_ref.add_item(new_act)
                if _on_applied_ref:
                    _on_applied_ref()
                self._send_json({"status": "ok", "items": _config_mgr_ref.get_items()})
            else:
                self._send_json({"error": "Invalid action"}, 400)
            return

        elif path == "/api/preview_launch_effect":
            if _on_preview_effect_ref:
                _on_preview_effect_ref()
            self._send_json({"status": "ok"})
            return

        elif path == "/api/pick_widget_bg":
            try:
                w_id = payload.get("widget_id", "")
                fpath = _open_native_file_dialog(
                    title="Seleccionar Imagen de Fondo para el Widget",
                    filetypes=[("Imágenes", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"), ("Todos", "*.*")],
                )
                if fpath and w_id and _config_mgr_ref:
                    from desktop_widgets import _import_widget_bg_image
                    cached_bg = _import_widget_bg_image(fpath)
                    _config_mgr_ref.set_widget(w_id, "bg_image", cached_bg or fpath)
                    if _on_applied_ref:
                        _on_applied_ref()
                    self._send_json({"status": "ok", "bg_image": cached_bg or fpath})
                else:
                    self._send_json({"status": "cancelled", "bg_image": None})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/todo/add":
            try:
                import todo_store
                res = todo_store.add_task(
                    text=payload.get("text", ""),
                    notebook=payload.get("notebook", "Outlook"),
                    tag=payload.get("tag", ""),
                    external_id=payload.get("external_id", ""),
                    sender=payload.get("from", ""),
                    source="http_api",
                    deduplicate=payload.get("deduplicate", True),
                )
                if _config_mgr_ref:
                    _config_mgr_ref.data = _config_mgr_ref.load()
                if _on_applied_ref:
                    _on_applied_ref()
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/todo/ingest":
            try:
                import todo_store
                res = todo_store.ingest_todo_inbox()
                if _config_mgr_ref:
                    _config_mgr_ref.data = _config_mgr_ref.load()
                if _on_applied_ref:
                    _on_applied_ref()
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/apply_theme":
            try:
                import themes
                res = themes.apply_theme(
                    _config_mgr_ref,
                    payload.get("theme_id", ""),
                    apply_icons=bool(payload.get("apply_icons", True)),
                )
                if _on_applied_ref and res.get("ok"):
                    _on_applied_ref()
                self._send_json(res, 200 if res.get("ok") else 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/save_custom_theme":
            try:
                import themes
                res = themes.save_custom_theme(_config_mgr_ref, payload.get("name", ""))
                self._send_json(res, 200 if res.get("ok") else 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/delete_custom_theme":
            try:
                import themes
                res = themes.delete_custom_theme(_config_mgr_ref, payload.get("theme_id", ""))
                self._send_json(res, 200 if res.get("ok") else 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        elif path == "/api/add_battery_dock":
            if _config_mgr_ref:
                _config_mgr_ref.add_item({"type": "battery", "name": payload.get("name") or "Batería"})
                if _on_applied_ref:
                    _on_applied_ref()
                self._send_json({"status": "ok", "items": _config_mgr_ref.get_items()})
            else:
                self._send_json({"error": "No config manager"}, 500)
            return

        self.send_error(404, "Unknown API endpoint")


def start_server(config_mgr, on_applied=None, tk_root=None, on_reveal=None, on_open_settings=None, on_preview_effect=None, ui_dispatch=None, dock_status_fn=None):
    global _server_instance, _server_thread, _config_mgr_ref, _on_applied_ref, _tk_root_ref, _on_reveal_ref, _on_open_settings_ref, _on_preview_effect_ref, _ui_dispatch_ref, _server_shutting_down, _dock_status_fn
    _server_shutting_down = False
    _config_mgr_ref = config_mgr
    if on_applied is not None:
        _on_applied_ref = on_applied
    if tk_root is not None:
        _tk_root_ref = tk_root
    if on_reveal is not None:
        _on_reveal_ref = on_reveal
    if on_open_settings is not None:
        _on_open_settings_ref = on_open_settings
    if on_preview_effect is not None:
        _on_preview_effect_ref = on_preview_effect
    if ui_dispatch is not None:
        _ui_dispatch_ref = ui_dispatch
    if dock_status_fn is not None:
        _dock_status_fn = dock_status_fn

    if _server_instance is not None:
        return

    try:
        _server_instance = ReusableThreadingHTTPServer((SERVER_HOST, SERVER_PORT), SettingsApiHandler)
        _server_thread = threading.Thread(target=_server_instance.serve_forever, daemon=True)
        _server_thread.start()
        print(f"[Settings Server] ThreadingHTTPServer running at http://{SERVER_HOST}:{SERVER_PORT}")
    except Exception as e:
        print(f"[Settings Server] Could not start server: {e}")


def open_settings_ui(config_mgr, on_applied=None, tk_root=None, pick_icon_id=None, child_id=None):
    """Closes any previously open Alioth HTML panel and launches a fresh Edge/Chrome app-mode panel."""
    global _last_panel_open_ts
    start_server(config_mgr, on_applied, tk_root=tk_root)

    # Always close any existing Alioth HTML panel before opening a new one
    panel_token = uuid.uuid4().hex[:12]
    _last_panel_open_ts = time.time()
    had_existing = close_settings_ui(next_token=panel_token, force=True, reason="open_new_panel")
    print(f"[Settings UI] open_settings_ui(token={panel_token}, pick_icon_id={pick_icon_id}, child_id={child_id}, replaced={had_existing})")
    if had_existing:
        time.sleep(0.08)

    params = [("panel_token", panel_token)]
    if pick_icon_id:
        params.append(("pick_icon_id", str(pick_icon_id)))
        if child_id:
            params.append(("child_id", str(child_id)))
    url = f"http://{SERVER_HOST}:{SERVER_PORT}/?" + urllib.parse.urlencode(params)
    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    ]

    bounds = config_mgr.data.get("panel_bounds", {}) if config_mgr else {}
    try:
        win_w = max(520, min(2400, int(bounds.get("width") or 980)))
    except Exception:
        win_w = 980
    try:
        win_h = max(420, min(1600, int(bounds.get("height") or 780)))
    except Exception:
        win_h = 780

    for browser_path in edge_paths:
        if os.path.exists(browser_path):
            try:
                cmd = [
                    browser_path,
                    f"--app={url}",
                    f"--window-size={win_w},{win_h}",
                ]
                bx = bounds.get("x")
                by = bounds.get("y")
                if bx is not None and by is not None:
                    cmd.append(f"--window-position={int(bx)},{int(by)}")
                subprocess.Popen(cmd)
                return
            except Exception as e:
                print(f"[Settings UI] Error launching app mode: {e}")

    import webbrowser
    webbrowser.open(url)
