"""
Spotlight Universal Search Window for Alioth Dockbar (v5).
Features:
- Zero outer rectangle / zero outer border: ONLY the floating rounded Searchbar pill with crisp WHITE border
- Neutral dark charcoal gray surface (matching Launchpad) with true semitransparency
- Configurable dimensions: spotlight_width, spotlight_item_height, spotlight_icon_size, spotlight_font_size
- Real native original icons for Apps, PWAs, Folders, and Documents (Word, Excel, PPTX, PDF, MP4, etc.)
- Full right-click Context Menu on results:
  Abrir, Abrir ubicación, Compartir, Crear acceso directo en Escritorio, Agregar a la Dockbar, Copiar, Copiar ruta, Propiedades
- True alpha compositing (alpha_rounded_rect) — zero colorkey punch-through holes
- Rock-solid Win32 foreground focus (never loses focus on mouse movement)
"""
from collections import OrderedDict
import ctypes
from ctypes import wintypes
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from PIL import Image, ImageDraw, ImageTk

import app_launcher
import app_search
import icon_extractor
from custom_menu import CustomMenu
from launchpad_window import _PIL_ICON_CACHE, _PIL_LOCK, force_window_foreground
from window_utils import (
    alpha_rounded_rect,
    copy_files_to_clipboard,
    copy_text_to_clipboard,
    create_desktop_shortcut,
    flatten_pil_for_colorkey,
    open_file_location,
    resolve_shortcut_target,
    resolve_styled_font,
    set_tool_window,
    share_path_via_email,
    show_file_properties,
)

user32 = ctypes.windll.user32

# Persistent module-level icon cache for Spotlight (stored at 64x64 RGBA so any icon_size scales crisply!)
_SP_ICON_CACHE = OrderedDict()   # key -> 64x64 RGBA PIL.Image
_SP_ICON_MAX = 256
_SP_ICON_LOCK = threading.Lock()


class UniversalSpotlight:
    """Centered floating macOS-style Spotlight with white-bordered pill, original icons, context menu & configurable sizes."""

    SEARCH_H = 54
    RESULTS_GAP = 8

    KEY_HEX = "#010203"
    KEY_RGB = (1, 2, 3)

    # Neutral dark charcoal gray (no blue cast)
    ENTRY_HEX = "#26282e"
    ENTRY_RGBA = (38, 40, 46, 255)
    CARD_RGBA = (34, 36, 41, 255)

    def __init__(self, parent, on_close=None, config_mgr=None, on_add_to_dock=None, on_toast=None):
        self.parent = parent
        self.on_close = on_close
        self.config_mgr = config_mgr
        self.on_add_to_dock = on_add_to_dock
        self.on_toast = on_toast

        # Configurable result dimensions from appearance settings
        w = 660
        item_h = 56
        icon_sz = 34
        font_sz = 13
        if self.config_mgr:
            w = int(self.config_mgr.get("appearance", "spotlight_width", 660))
            item_h = int(self.config_mgr.get("appearance", "spotlight_item_height", 56))
            icon_sz = int(self.config_mgr.get("appearance", "spotlight_icon_size", 34))
            font_sz = int(self.config_mgr.get("appearance", "spotlight_font_size", 13))

        self.WIDTH = max(500, min(1280, w))
        self.FONT_SZ = max(10, min(28, font_sz))
        self.SUB_FONT_SZ = max(9, min(20, int(round(self.FONT_SZ * 0.76))))
        self.ICON_SZ = max(22, min(88, icon_sz))
        # Ensure row height always comfortably fits icon and title+subtitle
        min_row_h = max(self.ICON_SZ + 14, self.FONT_SZ + self.SUB_FONT_SZ + 24)
        self.ITEM_H = max(min_row_h, min(128, item_h))

        self.window = None
        self.top_hwnd = None
        self.search_entry = None
        self.placeholder_lbl = None
        self.search_var = tk.StringVar(master=parent)
        self._search_trace = None
        self.canvas = None
        self.results = []
        self.selected_idx = 0
        self._closed = False
        self._search_ver = 0
        self._focus_job = None
        self._outside_job = None
        self._drain_job = None
        self._opened_ts = time.time()
        self._clear_rect = None
        self.photo_ref = None

        self._icon_work_q = queue.Queue()
        self._ui_queue = queue.SimpleQueue()
        self._queued_icon_keys = set()

        self._create_window()
        self._start_icon_worker()

    def _create_window(self):
        self.window = tk.Toplevel(self.parent)
        self.window.title("Spotlight — Alioth")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-transparentcolor", self.KEY_HEX)
        self.window.attributes("-alpha", 0.95)
        self.window.configure(bg=self.KEY_HEX)

        sw = self.window.winfo_screenwidth()
        sh = self.window.winfo_screenheight()
        self.WIDTH = min(self.WIDTH, sw - 40)
        self.win_x = (sw - self.WIDTH) // 2
        self.win_y = int(sh * 0.18)
        self.window.geometry(f"{self.WIDTH}x{self.SEARCH_H}+{self.win_x}+{self.win_y}")

        self.top_hwnd = set_tool_window(self.window, owner_hwnd=None, layered=False, no_activate=False)

        self.canvas = tk.Canvas(
            self.window,
            width=self.WIDTH,
            height=self.SEARCH_H,
            bg=self.KEY_HEX,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.place(x=0, y=0, width=self.WIDTH, height=self.SEARCH_H)

        entry_w = self.WIDTH - 114
        self.search_entry = tk.Entry(
            self.window,
            textvariable=self.search_var,
            bg=self.ENTRY_HEX,
            fg="#f8fafc",
            insertbackground="#ffffff",
            selectbackground="#4b5563",
            selectforeground="#ffffff",
            bd=0,
            highlightthickness=0,
            relief=tk.FLAT,
            font=("Segoe UI", 14),
        )
        self.search_entry.place(x=54, y=13, width=entry_w, height=28)

        self.placeholder_lbl = tk.Label(
            self.window,
            text="Buscar apps, archivos o cálculos...",
            bg=self.ENTRY_HEX,
            fg="#a1a1aa",
            font=("Segoe UI", 13),
            anchor="w",
            cursor="xterm",
        )
        self.placeholder_lbl.place(x=58, y=14, width=entry_w - 8, height=26)
        self.placeholder_lbl.bind("<Button-1>", lambda _e: self.search_entry.focus_set())

        self._search_trace = self.search_var.trace_add("write", lambda *_: self._on_query_changed())

        for w in (self.window, self.canvas, self.search_entry, self.placeholder_lbl):
            w.bind("<Escape>", lambda _e: self.close())
        self.search_entry.bind("<Return>", lambda _e: self._launch_selected())
        self.search_entry.bind("<Up>", lambda _e: self._move_selection(-1))
        self.search_entry.bind("<Down>", lambda _e: self._move_selection(1))
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<Button-3>", self._on_canvas_right_click)
        self.canvas.bind("<Motion>", self._on_canvas_motion)

        self._render(self.SEARCH_H)
        force_window_foreground(self.window, self.top_hwnd, self.search_entry)
        self._focus_job = self.window.after(
            35, lambda: (not self._closed) and force_window_foreground(self.window, self.top_hwnd, self.search_entry)
        )
        self._outside_job = self.window.after(200, self._poll_outside_click)
        self._drain_job = self.window.after(25, self._drain_ui_queue)

    def _poll_outside_click(self):
        self._outside_job = None
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        try:
            if not CustomMenu.has_active_menu() and (time.time() - self._opened_ts > 0.35):
                st = user32.GetAsyncKeyState(0x01) | user32.GetAsyncKeyState(0x02)
                if st & 0x8000:
                    pt = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    rect = wintypes.RECT()
                    hwnd = self.top_hwnd or self.window.winfo_id()
                    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        if not (rect.left <= pt.x <= rect.right and rect.top <= pt.y <= rect.bottom):
                            self.close()
                            return
        except Exception:
            pass
        if not self._closed and self.window and self.window.winfo_exists():
            self._outside_job = self.window.after(60, self._poll_outside_click)

    # ------------------------------------------------------------------
    # Async Native Icon Loader (Apps, PWAs, Files, Folders)
    # ------------------------------------------------------------------
    def _item_icon_key(self, res):
        name = (res.get("name") or "").strip().lower()
        src = (res.get("icon_src") or res.get("target") or "").strip().lower()
        args = (res.get("args") or "").strip().lower()
        return f"{name}|{src}|{args}"

    def _get_cached_icon(self, res):
        if res.get("type") == "calc":
            return None
        key = self._item_icon_key(res)
        base = None
        with _SP_ICON_LOCK:
            base = _SP_ICON_CACHE.get(key)
            if base is not None:
                _SP_ICON_CACHE.move_to_end(key)

        if base is None:
            with _PIL_LOCK:
                lp_ic = _PIL_ICON_CACHE.get(key)
            if lp_ic is not None:
                base = lp_ic.resize((96, 96), Image.Resampling.LANCZOS)
                with _SP_ICON_LOCK:
                    _SP_ICON_CACHE[key] = base
                    _SP_ICON_CACHE.move_to_end(key)

        if base is not None:
            return base if base.size == (self.ICON_SZ, self.ICON_SZ) else base.resize(
                (self.ICON_SZ, self.ICON_SZ), Image.Resampling.LANCZOS
            )

        if key not in self._queued_icon_keys:
            self._queued_icon_keys.add(key)
            self._icon_work_q.put((key, dict(res)))
        return None

    def _start_icon_worker(self):
        def _worker():
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            try:
                while not self._closed:
                    try:
                        job = self._icon_work_q.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    if job is None or self._closed:
                        break
                    key, item = job
                    try:
                        pil_ic = None
                        r_type = item.get("type", "app")
                        tgt = (item.get("target") or "").strip()
                        if r_type in ("file", "folder") and tgt and os.path.exists(tgt):
                            pil_ic = icon_extractor.extract_shell_icon(tgt, size=96)
                        if pil_ic is None:
                            pil_ic = icon_extractor.get_or_create_icon(item, size=96)
                        if pil_ic is not None:
                            if pil_ic.mode != "RGBA":
                                pil_ic = pil_ic.convert("RGBA")
                            base = pil_ic.resize((96, 96), Image.Resampling.LANCZOS)
                            with _SP_ICON_LOCK:
                                _SP_ICON_CACHE[key] = base
                                _SP_ICON_CACHE.move_to_end(key)
                                while len(_SP_ICON_CACHE) > _SP_ICON_MAX:
                                    _SP_ICON_CACHE.popitem(last=False)
                            if not self._closed:
                                self._ui_queue.put("icon_ready")
                    except Exception:
                        pass
            finally:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True, name="SpotlightIconWorker").start()

    def _drain_ui_queue(self):
        self._drain_job = None
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        dirty = False
        try:
            while True:
                self._ui_queue.get_nowait()
                dirty = True
        except queue.Empty:
            pass
        if dirty and self.results:
            self._render()
        if not self._closed and self.window and self.window.winfo_exists():
            self._drain_job = self.window.after(30, self._drain_ui_queue)

    # ------------------------------------------------------------------
    # Search Logic
    # ------------------------------------------------------------------
    def _on_query_changed(self):
        q = (self.search_var.get() or "").strip()
        entry_w = self.WIDTH - 114
        if not q:
            if self.placeholder_lbl and self.placeholder_lbl.winfo_exists():
                self.placeholder_lbl.place(x=58, y=14, width=entry_w - 8, height=26)
        else:
            if self.placeholder_lbl and self.placeholder_lbl.winfo_exists():
                self.placeholder_lbl.place_forget()

        self._search_ver += 1
        cur_ver = self._search_ver

        if not q:
            self.results = []
            self.selected_idx = 0
            self._resize_and_render(self.SEARCH_H)
            return

        instant = []
        try:
            allowed = set("0123456789+-*/()., %^")
            if len(q) >= 2 and all(c in allowed for c in q) and any(op in q for op in "+-*/^"):
                expr = q.replace("^", "**").replace("%", "/100")
                ans = eval(expr, {"__builtins__": None}, {})
                ans_str = f"{ans:.6f}".rstrip("0").rstrip(".") if isinstance(ans, float) else str(ans)
                instant.append({
                    "name": f"= {ans_str}",
                    "desc": f"Cálculo rápido: {q} (Enter para copiar)",
                    "target": "",
                    "type": "calc",
                    "source": "Calculadora",
                })
        except Exception:
            pass

        ql = q.lower()
        with app_launcher._CATALOG_LOCK:
            cat = list(app_launcher._CATALOG_CACHE)
        if not cat:
            cat = app_launcher._load_disk_catalog()
        seen_names = set()
        for a in cat:
            nm = (a.get("name") or "").strip()
            if not nm or nm.lower() in seen_names:
                continue
            if ql in nm.lower() or ql in (a.get("target") or "").lower():
                seen_names.add(nm.lower())
                tgt = (a.get("target") or "").strip()
                src = a.get("source") or "Aplicación"
                desc = f"Aplicación · {src}" if tgt.lower() in ("", "explorer.exe") else tgt
                instant.append({
                    "name": nm,
                    "desc": desc,
                    "target": tgt,
                    "args": a.get("args", ""),
                    "icon_src": a.get("icon_src") or tgt,
                    "type": "app",
                    "source": src,
                })
                if len(instant) >= 5:
                    break

        self._apply_results(instant)

        def _do_bg_file_search():
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            combined = list(instant)
            try:
                file_hits = app_search.query_windows_files_index(q, limit=5)
                for h in file_hits:
                    fp = h.get("target") or ""
                    is_dir = bool(h.get("is_dir")) or os.path.isdir(fp)
                    entry = {
                        "name": h.get("name"),
                        "desc": fp,
                        "target": fp,
                        "args": "",
                        "icon_src": fp,
                        "type": "folder" if is_dir else "file",
                        "source": "Carpeta" if is_dir else "Archivo",
                    }
                    combined.append(entry)
                    k = self._item_icon_key(entry)
                    with _SP_ICON_LOCK:
                        already = k in _SP_ICON_CACHE
                    if not already and fp and os.path.exists(fp):
                        try:
                            pil_ic = icon_extractor.extract_shell_icon(fp, size=96)
                            if pil_ic is not None:
                                if pil_ic.mode != "RGBA":
                                    pil_ic = pil_ic.convert("RGBA")
                                base = pil_ic.resize((96, 96), Image.Resampling.LANCZOS)
                                with _SP_ICON_LOCK:
                                    _SP_ICON_CACHE[k] = base
                                    _SP_ICON_CACHE.move_to_end(k)
                                    while len(_SP_ICON_CACHE) > _SP_ICON_MAX:
                                        _SP_ICON_CACHE.popitem(last=False)
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
            if cur_ver == self._search_ver and not self._closed and self.window:
                try:
                    self.window.after(0, lambda: self._apply_results(combined))
                except Exception:
                    pass

        if len(q) >= 2:
            threading.Thread(target=_do_bg_file_search, daemon=True).start()

    def _apply_results(self, res):
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        self.results = res[:8]
        self.selected_idx = min(self.selected_idx, max(0, len(self.results) - 1))
        if self.results:
            new_h = self.SEARCH_H + self.RESULTS_GAP + len(self.results) * self.ITEM_H + 16
        else:
            new_h = self.SEARCH_H
        self._resize_and_render(new_h)

    def _resize_and_render(self, new_h):
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        self.window.geometry(f"{self.WIDTH}x{new_h}+{self.win_x}+{self.win_y}")
        self.canvas.place(x=0, y=0, width=self.WIDTH, height=new_h)
        self._render(new_h)

    def _move_selection(self, delta):
        if not self.results:
            return "break"
        self.selected_idx = (self.selected_idx + delta) % len(self.results)
        self._render()
        return "break"

    def _notify(self, msg):
        if callable(self.on_toast) and msg:
            try:
                self.on_toast(msg)
            except Exception:
                pass

    def _resolve_real_path(self, item):
        for k in ("target", "icon_src"):
            cand = (item.get(k) or "").strip()
            if not cand or cand.lower().startswith(("shell:", "http://", "https://")):
                continue
            if cand.lower() == "explorer.exe":
                continue
            res = resolve_shortcut_target(cand)
            if res and os.path.exists(res):
                return res
            if os.path.exists(cand):
                return cand
        return ""

    def _launch_item(self, item):
        self.close()
        target = (item.get("target") or "").strip()
        args = (item.get("args") or "").strip()
        if not target:
            if item.get("type") == "calc":
                val = item.get("name", "").replace("= ", "")
                copy_text_to_clipboard(val)
                self._notify(f"Copiado: {val}")
            return
        try:
            if target.lower() in ("explorer.exe", "explorer") and args.lower().startswith("shell:"):
                subprocess.Popen(["explorer.exe", args])
            elif args:
                subprocess.Popen(f'"{target}" {args}', shell=True)
            else:
                os.startfile(target)
        except Exception:
            try:
                subprocess.Popen([target], shell=True)
            except Exception as e:
                print(f"[Spotlight] Error al ejecutar: {e}")

    def _launch_selected(self):
        if not self.results:
            return
        self._launch_item(self.results[self.selected_idx])

    def _results_top_y(self):
        return self.SEARCH_H + self.RESULTS_GAP + 8

    def _idx_at_y(self, y):
        top_y = self._results_top_y()
        if y >= top_y and self.results:
            idx = (y - top_y) // self.ITEM_H
            if 0 <= idx < len(self.results):
                return idx
        return -1

    def _on_canvas_click(self, event):
        if self._clear_rect:
            x1, y1, x2, y2 = self._clear_rect
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.search_var.set("")
                self.search_entry.focus_set()
                return
        idx = self._idx_at_y(event.y)
        if idx >= 0:
            self.selected_idx = idx
            self._launch_selected()
            return
        self.search_entry.focus_set()

    def _on_canvas_right_click(self, event):
        idx = self._idx_at_y(event.y)
        if idx < 0:
            return
        self.selected_idx = idx
        self._render()
        item = self.results[idx]
        r_type = item.get("type", "app")
        real_path = self._resolve_real_path(item)
        target = (item.get("target") or "").strip()
        args = (item.get("args") or "").strip()
        name = (item.get("name") or "").strip()

        menu = CustomMenu(self.window)
        if r_type == "calc":
            val = name.replace("= ", "")
            menu.add_command("Copiar resultado", lambda: (copy_text_to_clipboard(val), self._notify(f"Copiado: {val}"), self.close()))
            menu.show(event.x_root, event.y_root)
            return

        menu.add_command("Abrir", lambda it=item: self._launch_item(it))
        if real_path:
            menu.add_command("Abrir ubicación", lambda p=real_path: (self.close(), open_file_location(p)))
            menu.add_command("Compartir", lambda p=real_path, nm=name: (self.close(), share_path_via_email(p, display_name=nm)))

        menu.add_separator()

        if real_path or target:
            def _do_shortcut(t=real_path or target, nm=name, ag="" if real_path else args):
                lnk = create_desktop_shortcut(t, name=nm, args=ag)
                self.close()
                if lnk:
                    self._notify(f"Acceso directo creado en Escritorio")
            menu.add_command("Crear acceso directo en Escritorio", _do_shortcut)

        if self.on_add_to_dock and target:
            def _do_add_dock(it=item):
                try:
                    self.on_add_to_dock(it)
                except Exception as e:
                    print(f"[Spotlight] Add to dock error: {e}")
                self.close()
            menu.add_command("Agregar a la Dockbar", _do_add_dock)

        menu.add_separator()

        if real_path:
            menu.add_command("Copiar", lambda p=real_path: (copy_files_to_clipboard(p), self._notify("Copiado al portapapeles"), self.close()))
            menu.add_command("Copiar ruta", lambda p=real_path: (copy_text_to_clipboard(p), self._notify("Ruta copiada"), self.close()))
            menu.add_command("Propiedades", lambda p=real_path: (self.close(), show_file_properties(p)))
        elif target:
            menu.add_command("Copiar", lambda t=target: (copy_text_to_clipboard(t), self._notify("Copiado al portapapeles"), self.close()))

        menu.show(event.x_root, event.y_root)

    def _on_canvas_motion(self, event):
        idx = self._idx_at_y(event.y)
        if idx >= 0 and idx != self.selected_idx:
            self.selected_idx = idx
            self._render()

    # ------------------------------------------------------------------
    # Rendering (Zero Colorkey Holes, Crisp White Border, Real Icons!)
    # ------------------------------------------------------------------
    def _render(self, forced_h=None):
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        w = self.WIDTH
        h = forced_h or self.window.winfo_height() or self.SEARCH_H

        scale = 2
        sw, sh = w * scale, h * scale
        im_hi = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        d_hi = ImageDraw.Draw(im_hi, "RGBA")

        # 1. Floating Searchbar Pill ONLY — crisp WHITE border, neutral dark gray fill
        sb_h2 = self.SEARCH_H * scale
        d_hi.rounded_rectangle(
            [2, 2, sw - 3, sb_h2 - 3],
            radius=sb_h2 // 2 - 2,
            fill=self.ENTRY_RGBA,
            outline=(255, 255, 255, 240),
            width=3,
        )

        # Crisp white vector magnifying glass
        d_hi.ellipse([44, 34, 72, 62], outline=(255, 255, 255, 245), width=4)
        d_hi.line([(68, 58), (80, 70)], fill=(255, 255, 255, 245), width=4)

        # Vector Clear (X) circular button
        q = self.search_var.get() or ""
        if q:
            cx, cy = w - 28, self.SEARCH_H // 2
            cx2, cy2 = cx * scale, cy * scale
            d_hi.ellipse([cx2 - 24, cy2 - 24, cx2 + 24, cy2 + 24], fill=(74, 78, 88, 255))
            d_hi.line([(cx2 - 8, cy2 - 8), (cx2 + 8, cy2 + 8)], fill=(255, 255, 255, 255), width=4)
            d_hi.line([(cx2 + 8, cy2 - 8), (cx2 - 8, cy2 + 8)], fill=(255, 255, 255, 255), width=4)
            self._clear_rect = (cx - 15, cy - 15, cx + 15, cy + 15)
        else:
            self._clear_rect = None

        # 2. Detached Rounded Results Card (borderless — only the Searchbar keeps the white border!)
        if self.results:
            card_y1 = (self.SEARCH_H + self.RESULTS_GAP) * scale
            d_hi.rounded_rectangle(
                [2, card_y1, sw - 3, sh - 3],
                radius=34,
                fill=self.CARD_RGBA,
                outline=None,
                width=0,
            )

        im = im_hi.resize((w, h), Image.Resampling.LANCZOS)
        d = ImageDraw.Draw(im, "RGBA")

        if self.results:
            f_main = resolve_styled_font("Segoe UI", self.FONT_SZ, bold=True)
            f_sub = resolve_styled_font("Segoe UI", self.SUB_FONT_SZ)
            f_badge = resolve_styled_font("Segoe UI", max(9, min(13, int(round(self.SUB_FONT_SZ * 0.92)))), bold=True)

            row_inner_h = self.ITEM_H - 4
            txt_x = 22 + self.ICON_SZ + 14
            y_cursor = self._results_top_y()
            for i, res in enumerate(self.results):
                is_sel = (i == self.selected_idx)
                item_box = [12, y_cursor, w - 12, y_cursor + row_inner_h]
                if is_sel:
                    alpha_rounded_rect(
                        im, item_box, radius=12,
                        fill=(64, 68, 78, 255),
                        outline=None,
                        width=0,
                    )

                ic_x = 22
                ic_y = y_cursor + (row_inner_h - self.ICON_SZ) // 2
                real_ic = self._get_cached_icon(res)
                if real_ic is not None:
                    im.alpha_composite(real_ic, (ic_x, ic_y))
                else:
                    r_type = res.get("type", "app")
                    if r_type == "calc":
                        bg_c = (180, 120, 20, 255)
                        tag_ch = "="
                    elif r_type == "folder":
                        bg_c = (196, 146, 42, 255)
                        tag_ch = "D"
                    elif r_type == "file":
                        bg_c = (38, 130, 96, 255)
                        tag_ch = "F"
                    else:
                        bg_c = (66, 72, 86, 255)
                        tag_ch = ((res.get("name") or "A").strip()[:1] or "A").upper()
                    alpha_rounded_rect(
                        im, [ic_x, ic_y, ic_x + self.ICON_SZ, ic_y + self.ICON_SZ],
                        radius=max(6, self.ICON_SZ // 4), fill=bg_c, outline=(255, 255, 255, 90), width=1,
                    )
                    d.text(
                        (ic_x + self.ICON_SZ // 2, ic_y + self.ICON_SZ // 2),
                        tag_ch, font=f_main, fill=(255, 255, 255, 255), anchor="mm",
                    )

                badge = res.get("source") or ""
                bw = (int(d.textlength(badge, font=f_badge)) + 18) if badge else 0
                max_txt_w = max(80, w - txt_x - bw - 34)

                # Vertically centered title + subtitle block
                block_h = self.FONT_SZ + self.SUB_FONT_SZ + 8
                title_y = y_cursor + max(4, (row_inner_h - block_h) // 2 - 2)
                sub_y = title_y + self.FONT_SZ + 6

                name = (res.get("name") or "").strip()
                while len(name) > 2 and d.textlength(name, font=f_main) > max_txt_w:
                    name = name[:-1]
                if name != (res.get("name") or "").strip():
                    name = name.rstrip() + "…"
                d.text((txt_x, title_y), name, font=f_main, fill=(255, 255, 255, 255) if is_sel else (238, 240, 244, 255))

                desc = (res.get("desc") or "").strip()
                while len(desc) > 2 and d.textlength(desc, font=f_sub) > max_txt_w:
                    desc = desc[:-1]
                if desc != (res.get("desc") or "").strip():
                    desc = desc.rstrip() + "…"
                d.text((txt_x, sub_y), desc, font=f_sub, fill=(212, 214, 220, 255) if is_sel else (161, 165, 174, 255))

                if badge:
                    bh = 22
                    bx = w - 22 - bw
                    by = y_cursor + (row_inner_h - bh) // 2
                    badge_fill = (82, 86, 98, 255) if is_sel else (52, 55, 64, 255)
                    alpha_rounded_rect(
                        im, [bx, by, bx + bw, by + bh],
                        radius=11, fill=badge_fill, outline=(255, 255, 255, 45), width=1,
                    )
                    d.text((bx + bw // 2, by + bh // 2), badge, font=f_badge, fill=(244, 244, 245, 255), anchor="mm")

                y_cursor += self.ITEM_H

        flat = flatten_pil_for_colorkey(im, key_rgb=self.KEY_RGB, alpha_threshold=64)
        self.photo_ref = ImageTk.PhotoImage(flat, master=self.parent)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo_ref, anchor="nw")

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._icon_work_q.put(None)
        except Exception:
            pass
        for attr in ("_focus_job", "_outside_job", "_drain_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.window.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        if self.search_var is not None and self._search_trace:
            try:
                self.search_var.trace_remove("write", self._search_trace)
            except Exception:
                pass
        self._search_trace = None
        if self.window and self.window.winfo_exists():
            try:
                self.window.destroy()
            except Exception:
                pass
        self.window = None
        if callable(self.on_close):
            try:
                self.on_close()
            except Exception:
                pass
