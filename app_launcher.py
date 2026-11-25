"""
Ultra-fast, button-anchored macOS/App-Drawer style floating Launcher for Alioth Dockbar
+ Windows Start Key helper.
"""
import ctypes
from ctypes import wintypes
import json
import math
import os
import subprocess
import threading
import time
import tkinter as tk
from PIL import Image, ImageDraw, ImageTk

import app_search
import icon_extractor
from custom_menu import CustomMenu
from window_utils import (
    alpha_rounded_rect,
    alpha_ellipse,
    alpha_line,
    flatten_pil_for_colorkey,
    release_layered_surface,
    resolve_styled_font,
    set_tool_window,
    update_layered_pil,
)

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

VK_LWIN = 0x5B
KEYEVENTF_KEYUP = 0x0002

# Persistent module-level caches (survives across drawer opens for <20ms open speed!)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CATALOG_DISK_PATH = os.path.join(_BASE_DIR, "cache", "launcher_catalog.json")
_CATALOG_LOCK = threading.Lock()
_CATALOG_CACHE = []
_CATALOG_TS = 0.0
_CATALOG_LOADING = False

_THUMB_CACHE = {}       # (name, icon_src) -> 50x50 RGBA PIL.Image
_THUMB_MAX = 180
_PLACEHOLDER_CACHE = {} # letter -> 50x50 RGBA PIL.Image


def open_windows_start_menu():
    """Sends a clean native Windows-key press/release to toggle the Windows Start Menu."""
    try:
        user32.keybd_event(VK_LWIN, 0, 0, 0)
        user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
    except Exception as e:
        print(f"[Launcher] Start key fallback note: {e}")


def _dedup_apps(raw):
    seen = set()
    clean = []
    for a in raw or []:
        nm = (a.get("name") or "").strip()
        if not nm:
            continue
        key = (nm.lower(), ((a.get("target") or "") + "|" + (a.get("args") or "")).lower())
        if key in seen:
            continue
        seen.add(key)
        clean.append(a)
    return clean


def _load_disk_catalog():
    try:
        if os.path.exists(_CATALOG_DISK_PATH):
            with open(_CATALOG_DISK_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return _dedup_apps(data)
    except Exception:
        pass
    return []


def _save_disk_catalog(apps):
    try:
        os.makedirs(os.path.dirname(_CATALOG_DISK_PATH), exist_ok=True)
        tmp = _CATALOG_DISK_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(apps, f, ensure_ascii=False)
        os.replace(tmp, _CATALOG_DISK_PATH)
    except Exception:
        pass


def warm_launcher_cache(force=False, on_ready=None):
    """Refreshes app catalog in a background daemon thread without blocking UI."""
    global _CATALOG_CACHE, _CATALOG_TS, _CATALOG_LOADING
    with _CATALOG_LOCK:
        if not _CATALOG_CACHE:
            disk = _load_disk_catalog()
            if disk:
                _CATALOG_CACHE = disk
        if _CATALOG_LOADING:
            return
        if not force and _CATALOG_CACHE and (time.monotonic() - _CATALOG_TS) < 300.0 and _CATALOG_TS > 0:
            return
        _CATALOG_LOADING = True

    def _bg():
        global _CATALOG_CACHE, _CATALOG_TS, _CATALOG_LOADING
        try:
            raw = app_search.search_apps("", limit=180, force=force)
            clean = _dedup_apps(raw)
            if clean:
                with _CATALOG_LOCK:
                    _CATALOG_CACHE = clean
                    _CATALOG_TS = time.monotonic()
                _save_disk_catalog(clean)
        except Exception as ex:
            print(f"[Launcher] Background index note: {ex}")
        finally:
            with _CATALOG_LOCK:
                _CATALOG_LOADING = False
            if on_ready:
                try:
                    on_ready()
                except Exception:
                    pass

    threading.Thread(target=_bg, daemon=True).start()


def _make_placeholder_icon(name, size=50):
    ch = ((name or "A").strip()[:1] or "A").upper()
    key = (ch, size)
    cached = _PLACEHOLDER_CACHE.get(key)
    if cached is not None:
        return cached
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im, "RGBA")
    palette = [(0, 120, 212), (16, 185, 129), (139, 92, 246), (245, 158, 11), (236, 72, 153), (14, 165, 233)]
    c = palette[ord(ch) % len(palette)]
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=12, fill=(*c, 195), outline=(255, 255, 255, 90), width=1)
    f = resolve_styled_font("Segoe UI", int(size * 0.44), bold=True)
    d.text((size / 2.0, size / 2.0), ch, fill=(255, 255, 255, 245), font=f, anchor="mm")
    _PLACEHOLDER_CACHE[key] = im
    return im


class CustomAppLauncher:
    """Instant button-anchored frosted dark-glass floating app drawer with search & right-click menu."""

    COLS = 4
    ROWS = 4
    CARD_W = 468
    CARD_H = 540
    CELL_W = 104
    CELL_H = 104
    ICON_SZ = 50
    CARET_H = 10

    def __init__(
        self,
        parent,
        dock_pos="bottom",
        anchor_x=None,
        anchor_y=None,
        on_close=None,
        on_launch_feedback=None,
        on_add_to_dock=None,
    ):
        self.parent = parent
        self.dock_pos = dock_pos
        self.anchor_x = anchor_x
        self.anchor_y = anchor_y
        self.on_close = on_close
        self.on_launch_feedback = on_launch_feedback
        self.on_add_to_dock = on_add_to_dock

        self.window = None
        self.canvas = None
        self.search_entry = None
        self.search_var = tk.StringVar()
        self.top_hwnd = None
        self.transparent_key = "#010203"
        self.photo_ref = None

        self.all_apps = []
        self.filtered_apps = []
        self.item_boxes = []
        self.hovered_idx = -1
        self.scroll_row = 0
        self._bg_cache = None
        self._check_job = None
        self._caret_job = None
        self._caret_visible = True
        self._clear_btn_rect = None
        self._closed = False
        self._loading_icons = set()
        self.opened_ts = 0.0
        self.win_x = 0
        self.win_y = 0
        self.caret_local_x = self.CARD_W // 2

        self._load_apps_instant()
        self._create_window()

    def _load_apps_instant(self):
        global _CATALOG_CACHE
        with _CATALOG_LOCK:
            if not _CATALOG_CACHE:
                disk = _load_disk_catalog()
                if disk:
                    _CATALOG_CACHE = disk
            self.all_apps = list(_CATALOG_CACHE)
        self.filtered_apps = list(self.all_apps)

        # Refresh asynchronously in background if cache empty or stale
        def _on_bg_ready():
            if self._closed or not self.window:
                return
            try:
                self.parent.after(0, self._sync_from_catalog)
            except Exception:
                pass

        warm_launcher_cache(force=False, on_ready=_on_bg_ready)

    def _sync_from_catalog(self):
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        with _CATALOG_LOCK:
            self.all_apps = list(_CATALOG_CACHE)
        self._on_filter_change()

    def _create_window(self):
        self.opened_ts = time.time()
        self.window = tk.Toplevel(self.parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.config(bg=self.transparent_key)
        self.top_hwnd = set_tool_window(self.window, owner_hwnd=self.parent.winfo_id(), layered=True)

        sw = self.parent.winfo_screenwidth()
        sh = self.parent.winfo_screenheight()
        total_h = self.CARD_H + self.CARET_H

        ax = self.anchor_x if self.anchor_x is not None else (sw // 2)
        if self.dock_pos == "bottom":
            self.win_x = max(12, min(sw - self.CARD_W - 12, int(ax - self.CARD_W // 2)))
            ay = self.anchor_y if self.anchor_y is not None else (sh - 86)
            self.win_y = max(20, min(sh - total_h - 12, int(ay - total_h - 6)))
        elif self.dock_pos == "top":
            self.win_x = max(12, min(sw - self.CARD_W - 12, int(ax - self.CARD_W // 2)))
            ay = self.anchor_y if self.anchor_y is not None else 96
            self.win_y = max(20, min(sh - total_h - 12, int(ay + 6)))
        elif self.dock_pos == "left":
            self.win_x = max(12, min(sw - self.CARD_W - 12, int((self.anchor_x or 84) + 14)))
            ay = self.anchor_y if self.anchor_y is not None else (sh // 2)
            self.win_y = max(20, min(sh - total_h - 12, int(ay - total_h // 2)))
        else:
            self.win_x = max(12, min(sw - self.CARD_W - 12, int((self.anchor_x or (sw - 84)) - self.CARD_W - 14)))
            ay = self.anchor_y if self.anchor_y is not None else (sh // 2)
            self.win_y = max(20, min(sh - total_h - 12, int(ay - total_h // 2)))

        self.caret_local_x = max(36, min(self.CARD_W - 36, int(ax - self.win_x)))
        self.window.geometry(f"{self.CARD_W}x{total_h}+{self.win_x}+{self.win_y}")

        self.canvas = tk.Canvas(
            self.window,
            width=self.CARD_W,
            height=total_h,
            bg=self.transparent_key,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        y_off = self.CARET_H if self.dock_pos == "top" else 0
        self.search_entry = tk.Entry(
            self.window,
            textvariable=self.search_var,
            bg="#1c2231",
            fg="#f8fafc",
            insertbackground="#60a5fa",
            bd=0,
            relief=tk.FLAT,
            font=("Segoe UI", 11),
        )
        self.search_entry.place(x=60, y=27 + y_off, width=self.CARD_W - 108, height=24)
        self.search_var.trace_add("write", lambda *_: self._on_filter_change())

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<MouseWheel>", self._on_scroll)
        self.window.bind("<MouseWheel>", self._on_scroll)
        self.window.bind("<Escape>", lambda _e: self.close())
        self.search_entry.bind("<Escape>", lambda _e: self.close())
        self.search_entry.bind("<Return>", lambda _e: self._launch_first())
        self.search_entry.bind("<KeyRelease>", lambda _e: self._on_caret_activity())
        self.search_entry.bind("<ButtonRelease-1>", lambda _e: self._on_caret_activity())

        self._render()
        self.window.after(25, lambda: (self.window and self.window.winfo_exists() and self.window.focus_force(), self.search_entry and self.search_entry.focus_set()))
        self._poll_outside_click()
        self._blink_caret()

    def _on_caret_activity(self):
        self._caret_visible = True
        self._render()

    def _blink_caret(self):
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        self._caret_visible = not self._caret_visible
        self._render()
        self._caret_job = self.parent.after(520, self._blink_caret)

    def _on_filter_change(self):
        q = (self.search_var.get() or "").strip().lower()
        self.scroll_row = 0
        self.hovered_idx = -1
        if not q:
            self.filtered_apps = list(self.all_apps)
        else:
            self.filtered_apps = [
                a for a in self.all_apps
                if q in (a.get("name", "").lower()) or q in (a.get("source", "").lower())
            ]
        self._render()

    def _launch_first(self):
        if self.filtered_apps:
            self._launch_app(self.filtered_apps[0])

    def _launch_app(self, app):
        target = (app.get("target") or "").strip()
        args = (app.get("args") or "").strip()
        self.close()
        if self.on_launch_feedback:
            try:
                self.on_launch_feedback()
            except Exception:
                pass
        if not target:
            return
        try:
            if target.lower() in ("explorer.exe", "explorer") and args.lower().startswith("shell:"):
                subprocess.Popen(["explorer.exe", args])
            elif target.startswith(("http://", "https://", "shell:")):
                os.startfile(target)
            elif args:
                subprocess.Popen(f'"{target}" {args}', shell=True)
            else:
                os.startfile(target)
        except Exception as e:
            print(f"[Launcher] Launch error: {e}")

    def _launch_as_admin(self, app):
        target = (app.get("target") or "").strip()
        args = (app.get("args") or "").strip()
        self.close()
        if not target:
            return
        try:
            shell32.ShellExecuteW(None, "runas", target, args, None, 1)
        except Exception as e:
            print(f"[Launcher] Admin launch error: {e}")

    def _open_location(self, app):
        target = (app.get("target") or "").strip()
        self.close()
        if target and os.path.exists(target):
            try:
                subprocess.Popen(f'explorer.exe /select,"{os.path.abspath(target)}"')
            except Exception:
                pass

    def _add_to_dock(self, app):
        if self.on_add_to_dock:
            try:
                self.on_add_to_dock(app)
            except Exception as e:
                print(f"[Launcher] Add to dock error: {e}")

    def _on_scroll(self, event):
        total_rows = max(1, math.ceil(len(self.filtered_apps) / float(self.COLS)))
        max_top = max(0, total_rows - self.ROWS)
        step = -1 if event.delta > 0 else 1
        nxt = max(0, min(max_top, self.scroll_row + step))
        if nxt != self.scroll_row:
            self.scroll_row = nxt
            self._render()

    def _on_motion(self, event):
        old = self.hovered_idx
        self.hovered_idx = -1
        for i, (x1, y1, x2, y2, _app) in enumerate(self.item_boxes):
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.hovered_idx = i
                break
        if old != self.hovered_idx:
            self._render()

    def _on_leave(self, _event=None):
        if self.hovered_idx != -1:
            self.hovered_idx = -1
            self._render()

    def _on_click(self, event):
        if self._clear_btn_rect:
            x1, y1, x2, y2 = self._clear_btn_rect
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.search_var.set("")
                if self.search_entry:
                    self.search_entry.focus_set()
                self._on_caret_activity()
                return
        y_off = self.CARET_H if self.dock_pos == "top" else 0
        if 22 <= event.x <= self.CARD_W - 22 and (18 + y_off) <= event.y <= (58 + y_off):
            if self.search_entry:
                self.search_entry.focus_set()
            self._on_caret_activity()
            return
        for (x1, y1, x2, y2, app) in self.item_boxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self._launch_app(app)
                return

    def _on_right_click(self, event):
        hit_app = None
        for (x1, y1, x2, y2, app) in self.item_boxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                hit_app = app
                break
        menu = CustomMenu(self.window)
        if hit_app:
            nm = (hit_app.get("name") or "Aplicación").strip()
            menu.add_header(nm[:28])
            menu.add_command(f"Abrir {nm[:22]}", lambda a=hit_app: self._launch_app(a))
            if self.on_add_to_dock:
                menu.add_command("Agregar a la Dockbar", lambda a=hit_app: self._add_to_dock(a))
            tgt = (hit_app.get("target") or "").strip()
            if tgt and not tgt.lower().startswith(("shell:", "http://", "https://")):
                menu.add_command("Ejecutar como administrador", lambda a=hit_app: self._launch_as_admin(a))
            if tgt and os.path.exists(tgt):
                menu.add_command("Abrir ubicación del archivo", lambda a=hit_app: self._open_location(a))
            menu.add_separator()
        menu.add_command("Actualizar lista de apps", lambda: warm_launcher_cache(force=True, on_ready=lambda: self.parent.after(0, self._sync_from_catalog)))
        menu.add_command("Cerrar lanzador", self.close)
        menu.show(event.x_root, event.y_root)

    def _poll_outside_click(self):
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        if CustomMenu.has_active_menu():
            self._check_job = self.parent.after(60, self._poll_outside_click)
            return
        if time.time() - self.opened_ts > 0.25:
            l_down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
            r_down = bool(user32.GetAsyncKeyState(0x02) & 0x8000)
            if l_down or r_down:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                total_h = self.CARD_H + self.CARET_H
                inside = (self.win_x <= pt.x <= self.win_x + self.CARD_W and self.win_y <= pt.y <= self.win_y + total_h)
                if not inside:
                    self.close()
                    return
        self._check_job = self.parent.after(45, self._poll_outside_click)

    def _queue_missing_icons(self, page_apps):
        missing = []
        for app in page_apps:
            key = (app.get("name", ""), app.get("icon_src") or app.get("target", ""))
            if key not in _THUMB_CACHE and key not in self._loading_icons:
                self._loading_icons.add(key)
                missing.append((key, app))
        if not missing:
            return

        def _worker():
            updated = False
            for key, app in missing:
                if self._closed:
                    return
                temp_it = {
                    "type": "app",
                    "name": app.get("name", "App"),
                    "target": app.get("icon_src") or app.get("target", ""),
                    "args": app.get("args", ""),
                    "icon_path": "",
                }
                try:
                    ic = icon_extractor.get_or_create_icon(temp_it, size=64)
                    if ic is not None:
                        ic = ic.convert("RGBA").resize((self.ICON_SZ, self.ICON_SZ), Image.Resampling.LANCZOS)
                except Exception:
                    ic = None
                if len(_THUMB_CACHE) >= _THUMB_MAX:
                    # Evict oldest quarter cleanly
                    for k_old in list(_THUMB_CACHE.keys())[:40]:
                        _THUMB_CACHE.pop(k_old, None)
                _THUMB_CACHE[key] = ic or _make_placeholder_icon(app.get("name", "A"), self.ICON_SZ)
                self._loading_icons.discard(key)
                updated = True
            if updated and not self._closed:
                try:
                    self.parent.after(0, self._render)
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    def _build_bg(self):
        if self._bg_cache is not None:
            return self._bg_cache.copy()
        w = self.CARD_W
        total_h = self.CARD_H + self.CARET_H
        y_off = self.CARET_H if self.dock_pos == "top" else 0

        # 2x supersampled rendering + true alpha_composite borders so semi-transparent outlines
        # NEVER punch translucent alpha cutouts through the dark card or search bar pill!
        ss = 2
        W, H = w * ss, total_h * ss
        hi = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(hi, "RGBA")
        card_top = (y_off + 2) * ss
        card_bot = (y_off + self.CARD_H - 3) * ss
        card_fill = (16, 20, 31, 248)
        d.rounded_rectangle([2 * ss, card_top, W - 3 * ss, card_bot], radius=24 * ss, fill=card_fill)

        # Pointer caret pointing toward Launcher icon on Dockbar
        cx = self.caret_local_x * ss
        if self.dock_pos == "bottom":
            d.polygon([(cx - 10 * ss, card_bot - ss), (cx + 10 * ss, card_bot - ss), (cx, H - ss)], fill=card_fill)
        elif self.dock_pos == "top":
            d.polygon([(cx - 10 * ss, card_top + ss), (cx + 10 * ss, card_top + ss), (cx, ss)], fill=card_fill)

        # Soft outer card border composited cleanly on separate layer (preserves full card opacity)
        border_ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bd = ImageDraw.Draw(border_ov, "RGBA")
        bd.rounded_rectangle([2 * ss, card_top, W - 3 * ss, card_bot], radius=24 * ss, outline=(255, 255, 255, 48), width=ss)
        hi = Image.alpha_composite(hi, border_ov)

        # Search bar pill: crisp 2x antialiased solid interior + alpha-composited subtle focus border
        sb_rect = [22 * ss, (18 + y_off) * ss, (w - 22) * ss, (58 + y_off) * ss]
        sb_rad = 20 * ss
        sb_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sbd = ImageDraw.Draw(sb_layer, "RGBA")
        sbd.rounded_rectangle(sb_rect, radius=sb_rad, fill=(28, 34, 49, 255))
        hi = Image.alpha_composite(hi, sb_layer)

        sb_border = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sb_bd = ImageDraw.Draw(sb_border, "RGBA")
        sb_bd.rounded_rectangle(sb_rect, radius=sb_rad, outline=(96, 165, 250, 140), width=ss)
        hi = Image.alpha_composite(hi, sb_border)

        # Crisp 2x magnifying glass glyph
        ic_ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        icd = ImageDraw.Draw(ic_ov, "RGBA")
        icd.ellipse([35 * ss, (30 + y_off) * ss, 49 * ss, (44 + y_off) * ss], outline=(148, 163, 184, 240), width=2 * ss)
        icd.line([(47 * ss, (42 + y_off) * ss), (52 * ss, (47 + y_off) * ss)], fill=(148, 163, 184, 240), width=2 * ss)
        hi = Image.alpha_composite(hi, ic_ov)

        bg = hi.resize((w, total_h), Image.Resampling.LANCZOS)
        self._bg_cache = bg
        return bg.copy()

    def _render(self):
        if self._closed or not self.window or not self.canvas:
            return
        w = self.CARD_W
        total_h = self.CARD_H + self.CARET_H
        y_off = self.CARET_H if self.dock_pos == "top" else 0
        composite = self._build_bg()
        draw = ImageDraw.Draw(composite, "RGBA")
        f_lbl = resolve_styled_font("Segoe UI", 11, bold=False)
        f_sub = resolve_styled_font("Segoe UI", 11, bold=True)

        # Render search query / placeholder, blinking caret, and clear button directly onto layered surface
        raw_q = self.search_var.get() or ""
        f_search = resolve_styled_font("Segoe UI", 13, bold=False)
        sb_mid_y = 38 + y_off
        txt_x0 = 60
        txt_max_w = (w - 64) - txt_x0
        self._clear_btn_rect = None
        if raw_q:
            # Keep tail visible if query exceeds search box width
            disp_q = raw_q
            while len(disp_q) > 1 and draw.textlength(disp_q, font=f_search) > txt_max_w:
                disp_q = disp_q[1:]
            draw.text((txt_x0, sb_mid_y), disp_q, fill=(248, 250, 252, 255), font=f_search, anchor="lm")
            if self._caret_visible:
                try:
                    c_pos = self.search_entry.index(tk.INSERT) if self.search_entry else len(raw_q)
                except Exception:
                    c_pos = len(raw_q)
                prefix_len = max(0, len(disp_q) - max(0, len(raw_q) - c_pos))
                caret_x = txt_x0 + int(draw.textlength(disp_q[:prefix_len], font=f_search)) + 1
                caret_x = min(txt_x0 + txt_max_w + 2, caret_x)
                draw.line([(caret_x, sb_mid_y - 9), (caret_x, sb_mid_y + 9)], fill=(96, 165, 250, 255), width=2)
            # Clear (×) pill button on right side of search bar
            cb_cx, cb_cy, cb_r = w - 40, sb_mid_y, 9
            self._clear_btn_rect = (cb_cx - cb_r - 4, cb_cy - cb_r - 4, cb_cx + cb_r + 4, cb_cy + cb_r + 4)
            alpha_ellipse(composite, [cb_cx - cb_r, cb_cy - cb_r, cb_cx + cb_r, cb_cy + cb_r], fill=(148, 163, 184, 55))
            draw.line([(cb_cx - 3, cb_cy - 3), (cb_cx + 3, cb_cy + 3)], fill=(226, 232, 240, 235), width=2)
            draw.line([(cb_cx + 3, cb_cy - 3), (cb_cx - 3, cb_cy + 3)], fill=(226, 232, 240, 235), width=2)
        else:
            draw.text((txt_x0, sb_mid_y), "Buscar aplicaciones...", fill=(148, 163, 184, 170), font=f_search, anchor="lm")
            if self._caret_visible:
                draw.line([(txt_x0, sb_mid_y - 9), (txt_x0, sb_mid_y + 9)], fill=(96, 165, 250, 235), width=2)

        self.item_boxes = []
        grid_x0 = (w - self.COLS * self.CELL_W) // 2
        grid_y0 = 76 + y_off
        start_idx = self.scroll_row * self.COLS
        per_page = self.COLS * self.ROWS
        page_apps = self.filtered_apps[start_idx: start_idx + per_page]

        if not page_apps:
            msg = "Indexando aplicaciones..." if not self.all_apps and _CATALOG_LOADING else "Sin aplicaciones que coincidan"
            draw.text((w / 2.0, y_off + self.CARD_H / 2.0), msg, fill=(148, 163, 184, 220), font=f_sub, anchor="mm")
        else:
            self._queue_missing_icons(page_apps)

        for i, app in enumerate(page_apps):
            row, col = divmod(i, self.COLS)
            cx1 = grid_x0 + col * self.CELL_W
            cy1 = grid_y0 + row * self.CELL_H
            cx2, cy2 = cx1 + self.CELL_W, cy1 + self.CELL_H
            self.item_boxes.append((cx1, cy1, cx2, cy2, app))

            if self.hovered_idx == i:
                alpha_rounded_rect(
                    composite, [cx1 + 4, cy1 + 2, cx2 - 4, cy2 - 2], radius=14,
                    fill=(59, 130, 246, 56), outline=(147, 197, 253, 165), width=1
                )

            key = (app.get("name", ""), app.get("icon_src") or app.get("target", ""))
            ic = _THUMB_CACHE.get(key) or _make_placeholder_icon(app.get("name", "A"), self.ICON_SZ)
            if ic is not None:
                ix = cx1 + (self.CELL_W - self.ICON_SZ) // 2
                iy = cy1 + 10
                composite.alpha_composite(ic, (ix, iy))

            name = (app.get("name") or "App").strip()
            disp = name
            while disp and draw.textlength(disp, font=f_lbl) > self.CELL_W - 12:
                disp = disp[:-1]
            if disp != name:
                disp = disp.rstrip() + "…"
            draw.text(((cx1 + cx2) / 2.0, cy1 + 78), disp, fill=(241, 245, 249, 255), font=f_lbl, anchor="mm")

        total_rows = max(1, math.ceil(len(self.filtered_apps) / float(self.COLS)))
        info_txt = f"{len(self.filtered_apps)} apps  •  Clic derecho para opciones"
        draw.text((w / 2.0, y_off + self.CARD_H - 22), info_txt, fill=(148, 163, 184, 185), font=resolve_styled_font("Segoe UI", 10), anchor="mm")
        if total_rows > self.ROWS:
            tr_y1, tr_y2 = 78 + y_off, y_off + self.CARD_H - 44
            tr_h = tr_y2 - tr_y1
            th = max(24, int(tr_h * (self.ROWS / float(total_rows))))
            max_r = max(1, total_rows - self.ROWS)
            ty = tr_y1 + int((tr_h - th) * (self.scroll_row / float(max_r)))
            draw.rounded_rectangle([w - 9, ty, w - 5, ty + th], radius=2, fill=(96, 165, 250, 175))

        self.window.update_idletasks()
        hwnd = self.top_hwnd or user32.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
        if not update_layered_pil(hwnd, self.win_x, self.win_y, composite):
            final_img = flatten_pil_for_colorkey(composite, (1, 2, 3))
            self.photo_ref = ImageTk.PhotoImage(final_img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo_ref)

    def close(self):
        self._closed = True
        if self._caret_job:
            try:
                self.parent.after_cancel(self._caret_job)
            except Exception:
                pass
            self._caret_job = None
        if self._check_job:
            try:
                self.parent.after_cancel(self._check_job)
            except Exception:
                pass
            self._check_job = None
        if self.top_hwnd:
            release_layered_surface(self.top_hwnd)
            self.top_hwnd = None
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None
        self.search_entry = None
        self._bg_cache = None
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass
