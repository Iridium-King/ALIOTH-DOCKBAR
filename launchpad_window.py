"""
macOS-style Fullscreen Launchpad for Alioth Dockbar (v2 — High-Performance 3-Zone Architecture).
Features:
- True Semitransparent fullscreen frosted backdrop (-alpha configurable)
- Physically clipped 3-zone layout (Header / Scrollable Grid / Right A-Z Index / Footer) — ZERO overlap
- 2x supersampled antialiased rounded pills for Searchbar, Clear (X) button, and large Filter Chips
- Instant open (<25ms) via persistent catalog & persistent PIL icon cache + async background icon loader
- Persistent Tk Canvas items + single movable hover highlight + smooth eased native scrolling (60+ FPS)
- Right-side A-Z quick-jump sidebar with click & drag scrubbing
- Rock-solid Win32 foreground focus management (never loses focus when moving mouse)
- Right-click context menu on apps (Open, Run as Admin, Add to Dockbar, Open File Location)
"""
from collections import OrderedDict
import ctypes
from ctypes import wintypes
import math
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageDraw, ImageTk

import app_launcher
import icon_extractor
from custom_menu import CustomMenu
from window_utils import (
    open_file_location,
    resolve_shortcut_target,
    resolve_styled_font,
    set_tool_window,
)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

HWND_TOPMOST = ctypes.c_void_p(-1).value
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040

# Proper 64-bit ctypes prototypes for safe focus acquisition
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

# Persistent module-level caches (survive across Launchpad opens for instant <25ms reopen!)
_PIL_ICON_CACHE = OrderedDict()   # icon_key -> 112x112 RGBA PIL.Image
_PIL_ICON_MAX = 320
_PIL_LOCK = threading.Lock()
_PLACEHOLDER_PIL_CACHE = {}       # (letter, size) -> RGBA PIL.Image

BG_HEX = "#1d1e22"
BG_RGB = (29, 30, 34)
SEARCH_FILL_HEX = "#2b2d33"
SEARCH_FILL_RGBA = (43, 45, 51, 255)


def force_window_foreground(tk_win, top_hwnd, focus_widget=None):
    """
    Reliably grants Win32 foreground & keyboard focus to an overrideredirect tool window
    even when invoked from a WS_EX_NOACTIVATE dockbar or a background WM_HOTKEY thread.
    Uses standard Win32 AttachThreadInput (zero hooks).
    """
    if not tk_win or not tk_win.winfo_exists():
        return
    try:
        hwnd = top_hwnd or user32.GetAncestor(tk_win.winfo_id(), 2) or tk_win.winfo_id()
        fg_hwnd = user32.GetForegroundWindow()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0

        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(fg_tid, cur_tid, True))
        try:
            user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
            )
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_tid, cur_tid, False)

        tk_win.lift()
        tk_win.focus_force()
        if focus_widget and focus_widget.winfo_exists():
            focus_widget.focus_set()
    except Exception:
        try:
            tk_win.focus_force()
            if focus_widget and focus_widget.winfo_exists():
                focus_widget.focus_set()
        except Exception:
            pass


def _make_rounded_pill_pil(w, h, radius, fill_rgba, border_rgba=None, border_w=1, bg_rgb=BG_RGB):
    """Creates a razor-sharp 2x supersampled antialiased rounded pill flattened onto bg_rgb."""
    scale = 2
    sw, sh = max(2, int(w * scale)), max(2, int(h * scale))
    sr = min(int(radius * scale), min(sw, sh) // 2)
    sbw = max(1, int(border_w * scale)) if border_rgba else 0

    base = Image.new("RGBA", (sw, sh), (*bg_rgb, 255))
    overlay = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay, "RGBA")
    pad = max(1, sbw // 2)
    d.rounded_rectangle(
        [pad, pad, sw - 1 - pad, sh - 1 - pad],
        radius=sr,
        fill=fill_rgba,
        outline=border_rgba,
        width=sbw,
    )
    comp = Image.alpha_composite(base, overlay)
    return comp.resize((int(w), int(h)), Image.Resampling.LANCZOS)


def _make_placeholder_pil(name, size):
    ch = ((name or "A").strip()[:1] or "A").upper()
    key = (ch, size)
    cached = _PLACEHOLDER_PIL_CACHE.get(key)
    if cached is not None:
        return cached
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im, "RGBA")
    palette = [
        (14, 165, 233), (59, 130, 246), (16, 185, 129),
        (139, 92, 246), (245, 158, 11), (236, 72, 153),
    ]
    c = palette[ord(ch) % len(palette)]
    rad = max(8, int(size * 0.24))
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=rad, fill=(*c, 210), outline=(255, 255, 255, 70), width=1)
    f = resolve_styled_font("Segoe UI", max(14, int(size * 0.42)), bold=True)
    d.text((size / 2.0, size / 2.0), ch, fill=(255, 255, 255, 245), font=f, anchor="mm")
    _PLACEHOLDER_PIL_CACHE[key] = im
    return im


def _dedup_launchpad_apps(raw):
    """
    Deduplicates apps cleanly by normalized display name so identical apps
    (e.g., 3 shortcuts for CapCut or 2 for Access) appear once cleanly,
    preferring direct .exe/.lnk entries over generic explorer.exe wrappers.
    """
    best_by_name = {}
    for a in raw or []:
        nm = (a.get("name") or "").strip()
        if not nm:
            continue
        k = nm.lower()
        tgt = (a.get("target") or "").lower()
        score = 0
        if tgt and not tgt.endswith("explorer.exe"):
            score += 3
        if tgt.endswith((".exe", ".lnk")):
            score += 2
        if a.get("icon_src"):
            score += 1
        prev = best_by_name.get(k)
        if prev is None or score > prev[0]:
            best_by_name[k] = (score, a)
    items = [v[1] for v in best_by_name.values()]
    items.sort(key=lambda a: (a.get("name") or "").strip().lower())
    return items


class FullscreenLaunchpad:
    """Full-screen semitransparent macOS-style Launchpad with 3-zone clipped layout."""

    ALPHABET = [
        "A", "B", "C", "D", "E", "F", "G", "H", "I", "J",
        "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T",
        "U", "V", "W", "X", "Y", "Z", "#"
    ]

    HEADER_H = 160
    RIGHT_BAR_W = 46

    def __init__(self, parent, on_close=None, config_mgr=None, on_add_to_dock=None, on_open_settings=None):
        self.parent = parent
        self.on_close = on_close
        self.config_mgr = config_mgr
        self.on_add_to_dock = on_add_to_dock
        self.on_open_settings = on_open_settings

        # Load user visual settings
        self.opacity = 0.88
        self.icon_size = 72
        self.label_size = 11
        self.label_color = "#f1f5f9"
        self.dock_pos = "bottom"
        if self.config_mgr:
            self.opacity = float(self.config_mgr.get("appearance", "launchpad_opacity", 0.88))
            self.icon_size = int(self.config_mgr.get("appearance", "launchpad_icon_size", 72))
            self.label_size = int(self.config_mgr.get("appearance", "launchpad_label_size", 11))
            self.label_color = str(self.config_mgr.get("appearance", "launchpad_label_color", "#f1f5f9"))
            self.dock_pos = str(self.config_mgr.get("general", "position", "bottom"))

        self.icon_size = max(44, min(112, self.icon_size))
        self.label_size = max(9, min(18, self.label_size))
        self.opacity = max(0.35, min(1.0, self.opacity))

        # Dynamic cell sizing based on icon_size & label_size
        self.cell_w = max(126, self.icon_size + 58)
        self.cell_h = max(116, self.icon_size + self.label_size * 2 + 34)
        self.footer_h = 52

        self.window = None
        self.top_hwnd = None
        self.header_canvas = None
        self.grid_canvas = None
        self.alpha_canvas = None
        self.footer_canvas = None
        self.search_entry = None
        self.search_var = tk.StringVar(master=parent)
        self._search_trace = None

        self.all_apps = []
        self.filtered_apps = []
        self.cols = 7
        self.grid_x0 = 40
        self.content_h = 100
        self.grid_view_h = 500

        # Canvas item tracking (persistent items = zero full redraw lag!)
        self._sprite_photos = {}       # local Tk PhotoImage refs for pills/hover cards
        self._icon_photos = {}         # icon_key -> PhotoImage at self.icon_size
        self._placeholder_photos = {}  # letter -> PhotoImage at self.icon_size
        self._cell_img_ids = {}        # icon_key -> list of canvas image item IDs
        self._cell_label_ids = []      # idx -> label text item ID
        self._hover_item_id = None
        self._hovered_idx = -1

        # Header & Alpha interactive states
        self.selected_category = "Todas"
        self.hovered_category = None
        self.category_boxes = []
        self._clear_hovered = False
        self._clear_rect = None
        self._alpha_boxes = []
        self._active_letters = set()
        self._hovered_alpha = None
        self._alpha_scrubbing = False

        # Smooth inertial scroll state
        self._scroll_target_y = 0.0
        self._scroll_current_y = 0.0
        self._scroll_anim_job = None

        # Async worker & UI queue
        self._closed = False
        self._modal_open = False
        self._ui_queue = queue.SimpleQueue()
        self._icon_work_q = queue.Queue()
        self._queued_keys = set()
        self._drain_job = None
        self._search_job = None
        self._focus_job = None
        self._focus_job2 = None

        self._raw_categories = []
        self.categories = []
        self._cat_map = {}
        self._load_categories_from_config()

        self._load_apps_instant()
        self._create_window()
        self._start_icon_worker()

    def _load_categories_from_config(self):
        import copy
        from config import DEFAULT_LAUNCHPAD_CATEGORIES
        raw = None
        if self.config_mgr:
            raw = self.config_mgr.get("general", "launchpad_categories", None)
        if not isinstance(raw, list) or not raw:
            raw = copy.deepcopy(DEFAULT_LAUNCHPAD_CATEGORIES)
        self._raw_categories = raw
        self.categories = [("Todas", "Todas", "#38bdf8")]
        self._cat_map = {}
        for c in raw:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or c.get("label") or "").strip()
            lbl = str(c.get("label") or cid).strip()
            if not cid or cid.lower() == "todas":
                continue
            col = str(c.get("color") or "#38bdf8").strip()
            kws = [str(k).strip().lower() for k in (c.get("keywords") or []) if str(k).strip()]
            apps = [str(a).strip().lower() for a in (c.get("apps") or []) if str(a).strip()]
            self._cat_map[cid] = {"id": cid, "label": lbl, "color": col, "keywords": kws, "apps": apps, "raw": c}
            if c.get("enabled", True):
                self.categories.append((cid, lbl, col))
        valid_ids = {x[0] for x in self.categories}
        if self.selected_category not in valid_ids:
            self.selected_category = "Todas"

    # ------------------------------------------------------------------
    # Instant Catalog Loading (reuses app_launcher cache, 0ms blocking!)
    # ------------------------------------------------------------------
    def _load_apps_instant(self):
        with app_launcher._CATALOG_LOCK:
            if not app_launcher._CATALOG_CACHE:
                disk = app_launcher._load_disk_catalog()
                if disk:
                    app_launcher._CATALOG_CACHE = disk
            raw = list(app_launcher._CATALOG_CACHE)

        self.all_apps = _dedup_launchpad_apps(raw)
        self._apply_filter_data()

        # Refresh catalog asynchronously in background if empty or stale
        def _on_catalog_ready():
            if not self._closed:
                try:
                    self._ui_queue.put(("catalog", None))
                except Exception:
                    pass

        app_launcher.warm_launcher_cache(force=not bool(self.all_apps), on_ready=_on_catalog_ready)

    def _app_icon_key(self, app):
        name = (app.get("name") or "").strip().lower()
        src = (app.get("icon_src") or app.get("target") or "").strip().lower()
        args = (app.get("args") or "").strip().lower()
        return f"{name}|{src}|{args}"

    def _apply_filter_data(self):
        q = (self.search_var.get() or "").strip().lower()
        cat = self.selected_category
        cat_info = self._cat_map.get(cat)

        def _matches_cat(app):
            if cat == "Todas" or not cat_info:
                return True
            name = (app.get("name") or "").strip().lower()
            tgt = (app.get("target") or "").strip().lower()
            if name in cat_info["apps"]:
                return True
            return any(k in name or k in tgt for k in cat_info["keywords"])

        matches = []
        letters = set()
        for a in self.all_apps:
            if not _matches_cat(a):
                continue
            if q:
                nm = (a.get("name") or "").lower()
                tgt = (a.get("target") or "").lower()
                src = (a.get("source") or "").lower()
                if q not in nm and q not in tgt and q not in src:
                    continue
            matches.append(a)
            nm_u = (a.get("name") or "").strip().upper()
            if nm_u:
                letters.add(nm_u[0] if nm_u[0].isalpha() else "#")

        self.filtered_apps = matches
        self._active_letters = letters

    # ------------------------------------------------------------------
    # Window & 3-Zone Clipped Layout Construction
    # ------------------------------------------------------------------
    def _create_window(self):
        self.window = tk.Toplevel(self.parent)
        self.window.title("Launchpad — Alioth")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", self.opacity)
        self.window.configure(bg=BG_HEX)

        sw = self.window.winfo_screenwidth()
        sh = self.window.winfo_screenheight()
        self.sw = sw
        self.sh = sh
        self.window.geometry(f"{sw}x{sh}+0+0")

        # Independent hidden owner so Dockbar Z-order/autohide NEVER affects Launchpad!
        self.top_hwnd = set_tool_window(self.window, owner_hwnd=None, layered=False, no_activate=False)

        self.grid_view_h = max(200, sh - self.HEADER_H - self.footer_h)
        grid_canvas_w = sw - self.RIGHT_BAR_W

        # 1. TOP HEADER ZONE (Searchbar + Rounded Filter Pills)
        self.header_canvas = tk.Canvas(
            self.window, width=sw, height=self.HEADER_H,
            bg=BG_HEX, highlightthickness=0, bd=0
        )
        self.header_canvas.place(x=0, y=0, width=sw, height=self.HEADER_H)

        # Rounded Searchbar geometry
        self.sb_w = min(540, sw - 120)
        self.sb_h = 46
        self.sb_x = (sw - self.sb_w) // 2
        self.sb_y = 28

        self.search_entry = tk.Entry(
            self.window,
            textvariable=self.search_var,
            bg=SEARCH_FILL_HEX,
            fg="#f8fafc",
            insertbackground="#38bdf8",
            selectbackground="#0284c7",
            selectforeground="#ffffff",
            bd=0,
            highlightthickness=0,
            relief=tk.FLAT,
            font=("Segoe UI", 14),
        )
        self.search_entry.place(
            x=self.sb_x + 48,
            y=self.sb_y + 9,
            width=self.sb_w - 92,
            height=28,
        )
        self.placeholder_lbl = tk.Label(
            self.window,
            text="Buscar aplicaciones...",
            bg=SEARCH_FILL_HEX,
            fg="#a1a1aa",
            font=("Segoe UI", 13),
            anchor="w",
            cursor="xterm",
        )
        self.placeholder_lbl.place(x=self.sb_x + 52, y=self.sb_y + 10, height=26)
        self.placeholder_lbl.bind("<Button-1>", lambda _e: self.search_entry.focus_set())

        # 2. MIDDLE SCROLLABLE GRID ZONE (Strictly clipped — NEVER overlaps header or footer!)
        self.grid_canvas = tk.Canvas(
            self.window, width=grid_canvas_w, height=self.grid_view_h,
            bg=BG_HEX, highlightthickness=0, bd=0,
            yscrollincrement=1,
        )
        self.grid_canvas.place(x=0, y=self.HEADER_H, width=grid_canvas_w, height=self.grid_view_h)

        # 3. RIGHT ALPHABET QUICK-JUMP SIDEBAR
        self.alpha_canvas = tk.Canvas(
            self.window, width=self.RIGHT_BAR_W, height=self.grid_view_h,
            bg=BG_HEX, highlightthickness=0, bd=0
        )
        self.alpha_canvas.place(x=sw - self.RIGHT_BAR_W, y=self.HEADER_H, width=self.RIGHT_BAR_W, height=self.grid_view_h)

        # 4. BOTTOM FOOTER ZONE (Safe margin above bottom edge / dockbar)
        self.footer_canvas = tk.Canvas(
            self.window, width=sw, height=self.footer_h,
            bg=BG_HEX, highlightthickness=0, bd=0
        )
        self.footer_canvas.place(x=0, y=sh - self.footer_h, width=sw, height=self.footer_h)

        # Prepare reusable rounded pill sprites
        self._build_ui_sprites()

        # Bindings across ALL zones so keyboard, Escape, Enter & MouseWheel NEVER drop!
        self._search_trace = self.search_var.trace_add("write", lambda *_: self._on_search_write())

        for w in (self.window, self.header_canvas, self.grid_canvas, self.alpha_canvas, self.footer_canvas, self.search_entry):
            w.bind("<Escape>", lambda _e: self.close())
            w.bind("<MouseWheel>", self._on_mouse_wheel)
            w.bind("<Prior>", lambda _e: self._scroll_by_px(-self.grid_view_h * 0.85))
            w.bind("<Next>", lambda _e: self._scroll_by_px(self.grid_view_h * 0.85))
        for w in (self.window, self.header_canvas, self.grid_canvas, self.alpha_canvas, self.footer_canvas):
            w.bind("<Key>", self._on_fallback_key)

        self.search_entry.bind("<Return>", lambda _e: self._launch_first())
        self.search_entry.bind("<FocusIn>", lambda _e: self._draw_header())
        self.search_entry.bind("<FocusOut>", lambda _e: self._draw_header())

        # Header interactions
        self.header_canvas.bind("<Motion>", self._on_header_motion)
        self.header_canvas.bind("<Leave>", self._on_header_leave)
        self.header_canvas.bind("<Button-1>", self._on_header_click)
        self.header_canvas.bind("<Button-3>", self._on_header_right_click)

        # Grid interactions
        self.grid_canvas.bind("<Motion>", self._on_grid_motion)
        self.grid_canvas.bind("<Leave>", self._on_grid_leave)
        self.grid_canvas.bind("<Button-1>", self._on_grid_click)
        self.grid_canvas.bind("<Button-3>", self._on_grid_right_click)

        # Alphabet sidebar interactions (click + drag scrubbing!)
        self.alpha_canvas.bind("<Motion>", self._on_alpha_motion)
        self.alpha_canvas.bind("<Leave>", self._on_alpha_leave)
        self.alpha_canvas.bind("<Button-1>", self._on_alpha_press)
        self.alpha_canvas.bind("<B1-Motion>", self._on_alpha_drag)
        self.alpha_canvas.bind("<ButtonRelease-1>", self._on_alpha_release)

        # Footer click closes cleanly
        self.footer_canvas.bind("<Button-1>", lambda _e: self.close())

        # Initial render (instant!)
        self._draw_header()
        self._rebuild_grid()
        self._draw_alpha_bar()
        self._draw_footer()

        # Rock-solid Win32 foreground focus (immediate + post-map verification so user can type IMMEDIATELY!)
        def _ensure_ready_to_type():
            if self._closed or not self.window or not self.window.winfo_exists():
                return
            force_window_foreground(self.window, self.top_hwnd, self.search_entry)
            try:
                if self.search_entry and self.search_entry.winfo_exists():
                    self.search_entry.icursor(tk.END)
            except Exception:
                pass

        _ensure_ready_to_type()
        self._focus_job = self.window.after(25, _ensure_ready_to_type)
        self._focus_job2 = self.window.after(95, _ensure_ready_to_type)
        self._drain_job = self.window.after(25, self._drain_ui_queue)

    def _on_fallback_key(self, event):
        """Guarantees zero dropped keystrokes: routes any typing directly into search_entry."""
        if self._closed or not self.search_entry or not self.search_entry.winfo_exists():
            return
        if self.window.focus_get() == self.search_entry:
            return
        if event.keysym == "Return":
            self._launch_first()
            return "break"
        if event.keysym == "BackSpace":
            self.search_entry.focus_set()
            cur = self.search_var.get() or ""
            if cur:
                self.search_var.set(cur[:-1])
                self.search_entry.icursor(tk.END)
            return "break"
        ch = event.char or ""
        if ch and ch.isprintable() and not (event.state & 0x0004):
            self.search_entry.focus_set()
            self.search_entry.insert(tk.END, ch)
            self.search_entry.icursor(tk.END)
            return "break"

    def _build_ui_sprites(self):
        """Pre-renders crisp 2x antialiased rounded pill sprites once."""
        m = self.parent
        # Searchbar pills (idle & active) — crisp white border on neutral dark gray
        self._sprite_photos["search_idle"] = ImageTk.PhotoImage(
            _make_rounded_pill_pil(self.sb_w, self.sb_h, self.sb_h // 2, SEARCH_FILL_RGBA, (255, 255, 255, 175), 1.5),
            master=m,
        )
        self._sprite_photos["search_active"] = ImageTk.PhotoImage(
            _make_rounded_pill_pil(self.sb_w, self.sb_h, self.sb_h // 2, SEARCH_FILL_RGBA, (255, 255, 255, 240), 1.8),
            master=m,
        )

        # Clear (X) circular pill (idle & hover)
        self._sprite_photos["clear_idle"] = ImageTk.PhotoImage(
            _make_rounded_pill_pil(26, 26, 13, (72, 75, 84, 230), None, 0, bg_rgb=SEARCH_FILL_RGBA[:3]),
            master=m,
        )
        self._sprite_photos["clear_hover"] = ImageTk.PhotoImage(
            _make_rounded_pill_pil(26, 26, 13, (98, 102, 114, 255), (255, 255, 255, 180), 1, bg_rgb=SEARCH_FILL_RGBA[:3]),
            master=m,
        )

        # App hover rounded card
        hw = self.cell_w - 10
        hh = self.cell_h - 8
        self._sprite_photos["cell_hover"] = ImageTk.PhotoImage(
            _make_rounded_pill_pil(hw, hh, 18, (54, 57, 65, 220), (255, 255, 255, 110), 1.4),
            master=m,
        )

        # Alphabet hover rounded dot
        self._sprite_photos["alpha_hover"] = ImageTk.PhotoImage(
            _make_rounded_pill_pil(26, 22, 11, (245, 246, 250, 255), None, 0),
            master=m,
        )

    def _get_cat_pill_sprite(self, w, h, state):
        key = ("cat_pill", w, h, state)
        sp = self._sprite_photos.get(key)
        if sp is not None:
            return sp
        if state == "active":
            fill = (2, 132, 199, 255)
            border = (125, 211, 252, 240)
            bw = 1.6
        elif state == "hover":
            fill = (56, 59, 68, 255)
            border = (161, 165, 176, 190)
            bw = 1.4
        else:
            fill = (43, 45, 51, 255)
            border = (90, 94, 105, 165)
            bw = 1.2
        sp = ImageTk.PhotoImage(_make_rounded_pill_pil(w, h, h // 2, fill, border, bw), master=self.parent)
        self._sprite_photos[key] = sp
        return sp

    # ------------------------------------------------------------------
    # Header Drawing & Interactions (Rounded Searchbar + Rounded Pills)
    # ------------------------------------------------------------------
    def _draw_header(self):
        if self._closed or not self.header_canvas or not self.header_canvas.winfo_exists():
            return
        c = self.header_canvas
        c.delete("all")

        q = self.search_var.get() or ""
        is_focused = (self.window.focus_get() == self.search_entry) if self.window else False
        sp_search = self._sprite_photos["search_active"] if (q or is_focused) else self._sprite_photos["search_idle"]

        # Rounded Searchbar Pill
        c.create_image(self.sb_x, self.sb_y, image=sp_search, anchor="nw")

        # Crisp vector magnifying glass inside left of search pill
        gx = self.sb_x + 25
        gy = self.sb_y + self.sb_h // 2 - 1
        col_g = "#ffffff" if (q or is_focused) else "#d4d4d8"
        c.create_oval(gx - 7, gy - 7, gx + 4, gy + 4, outline=col_g, width=2)
        c.create_line(gx + 3, gy + 3, gx + 8, gy + 8, fill=col_g, width=2, capstyle=tk.ROUND)

        # Placeholder visibility & Clear (X) circular button
        if not q:
            if getattr(self, "placeholder_lbl", None) and self.placeholder_lbl.winfo_exists():
                self.placeholder_lbl.place(x=self.sb_x + 52, y=self.sb_y + 10, height=26)
            self._clear_rect = None
        else:
            if getattr(self, "placeholder_lbl", None) and self.placeholder_lbl.winfo_exists():
                self.placeholder_lbl.place_forget()
            # Rounded Clear (X) circular button on right of search pill (crisp vector X)
            cx = self.sb_x + self.sb_w - 25
            cy = self.sb_y + self.sb_h // 2
            sp_x = self._sprite_photos["clear_hover"] if self._clear_hovered else self._sprite_photos["clear_idle"]
            c.create_image(cx, cy, image=sp_x, anchor="center")
            c.create_line(cx - 4, cy - 4, cx + 4, cy + 4, fill="#f8fafc", width=1.8, capstyle=tk.ROUND)
            c.create_line(cx + 4, cy - 4, cx - 4, cy + 4, fill="#f8fafc", width=1.8, capstyle=tk.ROUND)
            self._clear_rect = (cx - 15, cy - 15, cx + 15, cy + 15)

        # Enlarged Rounded Filter Pills
        pill_h = 40
        pill_y = 96
        pill_gap = 14

        # Compute dynamic pill widths so every label fits comfortably with generous padding
        pill_specs = []
        total_w = 0
        for cat_id, cat_lbl, dot_col in self.categories:
            pw = max(112, len(cat_lbl) * 9 + 46)
            pill_specs.append((cat_id, cat_lbl, dot_col, pw))
            total_w += pw
        total_w += pill_gap * (len(pill_specs) - 1)

        cur_x = (self.sw - total_w) // 2
        self.category_boxes = []
        for cat_id, cat_lbl, dot_col, pw in pill_specs:
            is_active = (cat_id == self.selected_category)
            is_hov = (cat_id == self.hovered_category)
            state = "active" if is_active else ("hover" if is_hov else "idle")
            sp = self._get_cat_pill_sprite(pw, pill_h, state)

            c.create_image(cur_x, pill_y, image=sp, anchor="nw")
            # Small colored accent dot inside pill
            dot_x = cur_x + 18
            dot_y = pill_y + pill_h // 2
            d_col = "#ffffff" if is_active else dot_col
            c.create_oval(dot_x - 4, dot_y - 4, dot_x + 4, dot_y + 4, fill=d_col, outline="")

            c.create_text(
                cur_x + 30 + (pw - 38) // 2,
                pill_y + pill_h // 2,
                text=cat_lbl,
                font=("Segoe UI", 11, "bold"),
                fill="#ffffff" if is_active else ("#f8fafc" if is_hov else "#cbd5e1"),
            )
            self.category_boxes.append((cat_id, (cur_x, pill_y, cur_x + pw, pill_y + pill_h)))
            cur_x += pw + pill_gap

        # Subtle hairline separator below header
        c.create_line(40, self.HEADER_H - 1, self.sw - 40, self.HEADER_H - 1, fill="#32353d", width=1)

    def _on_header_motion(self, event):
        new_clear = False
        if self._clear_rect:
            x1, y1, x2, y2 = self._clear_rect
            new_clear = (x1 <= event.x <= x2 and y1 <= event.y <= y2)

        new_cat = None
        for cat_id, (x1, y1, x2, y2) in self.category_boxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                new_cat = cat_id
                break

        if new_clear != self._clear_hovered or new_cat != self.hovered_category:
            self._clear_hovered = new_clear
            self.hovered_category = new_cat
            cursor = "hand2" if (new_clear or new_cat) else ""
            self.header_canvas.configure(cursor=cursor)
            self._draw_header()

    def _on_header_leave(self, _event):
        if self._clear_hovered or self.hovered_category is not None:
            self._clear_hovered = False
            self.hovered_category = None
            self.header_canvas.configure(cursor="")
            self._draw_header()

    def _on_header_click(self, event):
        # Always keep keyboard focus inside search_entry
        if self.search_entry and self.search_entry.winfo_exists():
            self.search_entry.focus_set()

        # 1. Clear (X) button
        if self._clear_rect:
            x1, y1, x2, y2 = self._clear_rect
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.search_var.set("")
                self._clear_hovered = False
                self._draw_header()
                return

        # 2. Searchbar pill click -> focus entry
        if self.sb_x <= event.x <= self.sb_x + self.sb_w and self.sb_y <= event.y <= self.sb_y + self.sb_h:
            self.search_entry.focus_set()
            return

        # 3. Filter category pills
        for cat_id, (x1, y1, x2, y2) in self.category_boxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                if self.selected_category != cat_id:
                    self.selected_category = cat_id
                    self._apply_filter_data()
                    self._draw_header()
                    self._rebuild_grid()
                    self._draw_alpha_bar()
                    self._draw_footer()
                return

    # ------------------------------------------------------------------
    # Real-Time Search
    # ------------------------------------------------------------------
    def _on_search_write(self):
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        self._draw_header()
        if self._search_job:
            try:
                self.window.after_cancel(self._search_job)
            except Exception:
                pass
        # Ultra-snappy 15ms real-time debounce
        self._search_job = self.window.after(15, self._run_realtime_filter)

    def _run_realtime_filter(self):
        self._search_job = None
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        self._apply_filter_data()
        self._rebuild_grid()
        self._draw_alpha_bar()
        self._draw_footer()

    # ------------------------------------------------------------------
    # Persistent Scrollable Grid (Zero Overlap, 60+ FPS Hover & Scroll)
    # ------------------------------------------------------------------
    def _get_placeholder_photo(self, name):
        ch = ((name or "A").strip()[:1] or "A").upper()
        ph = self._placeholder_photos.get(ch)
        if ph is None:
            pil = _make_placeholder_pil(name, self.icon_size)
            ph = ImageTk.PhotoImage(pil, master=self.parent)
            self._placeholder_photos[ch] = ph
        return ph

    def _get_cached_icon_photo(self, app):
        key = self._app_icon_key(app)
        ph = self._icon_photos.get(key)
        if ph is not None:
            return ph, True

        with _PIL_LOCK:
            pil_base = _PIL_ICON_CACHE.get(key)
            if pil_base is not None:
                _PIL_ICON_CACHE.move_to_end(key)

        if pil_base is not None:
            resized = pil_base if pil_base.size == (self.icon_size, self.icon_size) else pil_base.resize(
                (self.icon_size, self.icon_size), Image.Resampling.LANCZOS
            )
            ph = ImageTk.PhotoImage(resized, master=self.parent)
            self._icon_photos[key] = ph
            return ph, True

        return self._get_placeholder_photo(app.get("name") or "A"), False

    def _rebuild_grid(self):
        if self._closed or not self.grid_canvas or not self.grid_canvas.winfo_exists():
            return
        gc = self.grid_canvas
        gc.delete("all")
        self._cell_img_ids.clear()
        self._cell_label_ids = []
        self._hovered_idx = -1
        self._scroll_target_y = 0.0
        self._scroll_current_y = 0.0

        grid_canvas_w = self.sw - self.RIGHT_BAR_W
        avail_w = max(self.cell_w, grid_canvas_w - 80)
        self.cols = max(4, min(9, avail_w // self.cell_w))
        grid_w = self.cols * self.cell_w
        self.grid_x0 = (grid_canvas_w - grid_w) // 2

        pad_top = 18
        pad_bottom = 28

        if not self.filtered_apps:
            self.content_h = self.grid_view_h
            gc.configure(scrollregion=(0, 0, grid_canvas_w, self.grid_view_h))
            gc.yview_moveto(0.0)
            gc.create_text(
                grid_canvas_w // 2, self.grid_view_h // 2 - 20,
                text="No se encontraron aplicaciones",
                font=("Segoe UI", 15, "bold"), fill="#64748b",
            )
            return

        total_rows = math.ceil(len(self.filtered_apps) / self.cols)
        self.content_h = max(self.grid_view_h, pad_top + total_rows * self.cell_h + pad_bottom)
        gc.configure(scrollregion=(0, 0, grid_canvas_w, self.content_h))
        gc.yview_moveto(0.0)

        # Create single reusable hover highlight sprite (hidden offscreen initially)
        self._hover_item_id = gc.create_image(
            -500, -500, image=self._sprite_photos["cell_hover"], anchor="nw", tags=("hover_card",)
        )

        f_label = ("Segoe UI", self.label_size)
        meas_font = tkfont.Font(root=self.parent, family="Segoe UI", size=self.label_size)
        max_lbl_px = self.cell_w - 22

        for idx, app in enumerate(self.filtered_apps):
            row = idx // self.cols
            col = idx % self.cols
            cx = self.grid_x0 + col * self.cell_w
            cy = pad_top + row * self.cell_h

            ph, is_real = self._get_cached_icon_photo(app)
            ix = cx + self.cell_w // 2
            iy = cy + 12 + self.icon_size // 2
            img_id = gc.create_image(ix, iy, image=ph, anchor="center")

            key = self._app_icon_key(app)
            self._cell_img_ids.setdefault(key, []).append(img_id)
            if not is_real and key not in self._queued_keys:
                self._queued_keys.add(key)
                self._icon_work_q.put((key, app))

            nm = (app.get("name") or "").strip()
            if meas_font.measure(nm) > max_lbl_px:
                while len(nm) > 2 and meas_font.measure(nm + "…") > max_lbl_px:
                    nm = nm[:-1]
                nm = nm.rstrip() + "…"
            ty = cy + self.icon_size + 22 + self.label_size // 2
            lbl_id = gc.create_text(
                cx + self.cell_w // 2, ty,
                text=nm, font=f_label, fill=self.label_color, anchor="center",
            )
            self._cell_label_ids.append(lbl_id)

        self._pad_top = pad_top

    def _hit_test_grid(self, event_x, event_y):
        if not self.filtered_apps:
            return -1
        cy = self.grid_canvas.canvasy(event_y)
        cx = event_x
        grid_w = self.cols * self.cell_w
        if cx < self.grid_x0 or cx >= self.grid_x0 + grid_w:
            return -1
        rel_y = cy - getattr(self, "_pad_top", 18)
        if rel_y < 0:
            return -1
        col = int((cx - self.grid_x0) // self.cell_w)
        row = int(rel_y // self.cell_h)
        idx = row * self.cols + col
        if 0 <= idx < len(self.filtered_apps):
            return idx
        return -1

    def _set_hover_idx(self, new_idx):
        if new_idx == self._hovered_idx:
            return
        gc = self.grid_canvas
        if not gc or not gc.winfo_exists():
            return

        # Reset previous label color
        if 0 <= self._hovered_idx < len(self._cell_label_ids):
            gc.itemconfig(self._cell_label_ids[self._hovered_idx], fill=self.label_color)

        self._hovered_idx = new_idx
        if 0 <= new_idx < len(self.filtered_apps) and self._hover_item_id:
            row = new_idx // self.cols
            col = new_idx % self.cols
            cx = self.grid_x0 + col * self.cell_w + 5
            cy = getattr(self, "_pad_top", 18) + row * self.cell_h + 4
            gc.coords(self._hover_item_id, cx, cy)
            gc.itemconfig(self._cell_label_ids[new_idx], fill="#ffffff")
            gc.configure(cursor="hand2")
        else:
            if self._hover_item_id:
                gc.coords(self._hover_item_id, -500, -500)
            gc.configure(cursor="")

    def _on_grid_motion(self, event):
        idx = self._hit_test_grid(event.x, event.y)
        self._set_hover_idx(idx)

    def _on_grid_leave(self, _event):
        self._set_hover_idx(-1)

    def _on_grid_click(self, event):
        idx = self._hit_test_grid(event.x, event.y)
        if 0 <= idx < len(self.filtered_apps):
            self._launch_app(self.filtered_apps[idx])
        else:
            # Clicking empty area outside icons closes Launchpad cleanly
            self.close()

    def _on_header_right_click(self, event):
        clicked_cat = None
        for cat_id, (x1, y1, x2, y2) in self.category_boxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                clicked_cat = cat_id
                break
        if not clicked_cat:
            return
        menu = CustomMenu(self.window)
        if self.on_open_settings:
            menu.add_command("Editar filtros en Ajustes...", lambda: (self.close(), self.on_open_settings()))
        if clicked_cat != "Todas" and self.config_mgr:
            menu.add_command(
                f"Ocultar filtro '{self._cat_map.get(clicked_cat, {}).get('label', clicked_cat)}'",
                lambda cid=clicked_cat: self._toggle_category_visibility(cid, False),
            )
        menu.show(event.x_root, event.y_root)

    def _toggle_category_visibility(self, cat_id, enabled):
        if not self.config_mgr:
            return
        for c in self._raw_categories:
            if str(c.get("id") or "") == cat_id:
                c["enabled"] = bool(enabled)
        self.config_mgr.set("general", "launchpad_categories", self._raw_categories)
        self._load_categories_from_config()
        self._apply_filter_data()
        self._draw_header()
        self._rebuild_grid()
        self._draw_alpha_bar()
        self._draw_footer()

    def _toggle_app_in_category(self, app, cat_id):
        if not self.config_mgr:
            return
        app_nm = (app.get("name") or "").strip().lower()
        if not app_nm:
            return
        for c in self._raw_categories:
            if str(c.get("id") or "") == cat_id:
                apps = [str(a).strip().lower() for a in (c.get("apps") or []) if str(a).strip()]
                if app_nm in apps:
                    apps = [a for a in apps if a != app_nm]
                else:
                    apps.append(app_nm)
                c["apps"] = apps
                break
        self.config_mgr.set("general", "launchpad_categories", self._raw_categories)
        self._load_categories_from_config()
        self._apply_filter_data()
        self._rebuild_grid()
        self._draw_alpha_bar()
        self._draw_footer()

    def _on_grid_right_click(self, event):
        idx = self._hit_test_grid(event.x, event.y)
        if idx < 0 or idx >= len(self.filtered_apps):
            return
        app = self.filtered_apps[idx]
        app_nm_l = (app.get("name") or "").strip().lower()
        self._modal_open = True
        menu = CustomMenu(self.window)
        menu.add_command("Abrir", lambda: self._launch_app(app))
        menu.add_command("Ejecutar como administrador", lambda: self._launch_as_admin(app))
        if self.on_add_to_dock:
            menu.add_separator()
            menu.add_command("Añadir a la Dockbar", lambda: self._add_to_dock(app))
        if self.config_mgr and self._raw_categories:
            sub = menu.add_submenu("Asignar a filtro / categoría")
            for c in self._raw_categories:
                cid = str(c.get("id") or "").strip()
                lbl = str(c.get("label") or cid).strip()
                if not cid:
                    continue
                assigned = app_nm_l in [str(a).strip().lower() for a in (c.get("apps") or [])]
                prefix = "✓ " if assigned else "   "
                sub.add_command(f"{prefix}{lbl}", lambda a=app, cid_=cid: self._toggle_app_in_category(a, cid_))
            if self.on_open_settings:
                sub.add_separator()
                sub.add_command("Editar filtros en Ajustes...", lambda: (self.close(), self.on_open_settings()))
        menu.add_separator()
        menu.add_command("Abrir ubicación del archivo", lambda: self._open_location(app))
        menu.add_command("Actualizar lista de apps", lambda: self._force_refresh_catalog())
        menu.show(event.x_root, event.y_root)

    # ------------------------------------------------------------------
    # Smooth Inertial Scrolling (Native C-Level Tk Canvas yview_moveto)
    # ------------------------------------------------------------------
    def _max_scroll_px(self):
        return max(0.0, float(self.content_h - self.grid_view_h))

    def _on_mouse_wheel(self, event):
        # Smooth proportional scroll step
        steps = -event.delta / 120.0
        self._scroll_by_px(steps * (self.cell_h * 0.95))
        return "break"

    def _scroll_by_px(self, delta_px):
        max_y = self._max_scroll_px()
        if max_y <= 0:
            return
        self._scroll_target_y = max(0.0, min(max_y, self._scroll_target_y + delta_px))
        if self._scroll_anim_job is None:
            self._tick_smooth_scroll()

    def _scroll_to_px(self, target_px, immediate=False):
        max_y = self._max_scroll_px()
        target = max(0.0, min(max_y, float(target_px)))
        self._scroll_target_y = target
        if immediate or max_y <= 0:
            self._scroll_current_y = target
            if self.grid_canvas and self.grid_canvas.winfo_exists() and self.content_h > 0:
                self.grid_canvas.yview_moveto(target / self.content_h)
            return
        if self._scroll_anim_job is None:
            self._tick_smooth_scroll()

    def _tick_smooth_scroll(self):
        self._scroll_anim_job = None
        if self._closed or not self.grid_canvas or not self.grid_canvas.winfo_exists():
            return
        diff = self._scroll_target_y - self._scroll_current_y
        if abs(diff) < 1.2:
            self._scroll_current_y = self._scroll_target_y
            if self.content_h > 0:
                self.grid_canvas.yview_moveto(self._scroll_current_y / self.content_h)
            return
        # Smooth 60 FPS exponential ease-out
        self._scroll_current_y += diff * 0.34
        if self.content_h > 0:
            self.grid_canvas.yview_moveto(self._scroll_current_y / self.content_h)
        self._scroll_anim_job = self.window.after(14, self._tick_smooth_scroll)

    # ------------------------------------------------------------------
    # Right-Side Alphabet Index Sidebar (Click + Drag Scrubbing)
    # ------------------------------------------------------------------
    def _draw_alpha_bar(self):
        if self._closed or not self.alpha_canvas or not self.alpha_canvas.winfo_exists():
            return
        ac = self.alpha_canvas
        ac.delete("all")
        self._alpha_boxes = []

        n = len(self.ALPHABET)
        h = self.grid_view_h
        pad_v = 18
        step = max(14.0, min(25.0, (h - pad_v * 2) / max(1, n)))
        total_h = step * (n - 1)
        start_y = (h - total_h) / 2.0
        cx = self.RIGHT_BAR_W // 2

        for i, letter in enumerate(self.ALPHABET):
            ly = start_y + i * step
            has_apps = letter in self._active_letters
            is_hov = (letter == self._hovered_alpha)
            self._alpha_boxes.append((letter, ly - step / 2.0, ly + step / 2.0))

            if is_hov:
                ac.create_image(cx, ly, image=self._sprite_photos["alpha_hover"], anchor="center")
                ac.create_text(cx, ly, text=letter, font=("Segoe UI", 9, "bold"), fill="#0f172a")
            else:
                col = "#cbd5e1" if has_apps else "#334155"
                ac.create_text(cx, ly, text=letter, font=("Segoe UI", 9, "bold" if has_apps else "normal"), fill=col)

    def _letter_at_y(self, y):
        for letter, y1, y2 in self._alpha_boxes:
            if y1 <= y <= y2:
                return letter
        return None

    def _jump_to_letter(self, letter):
        if not self.filtered_apps or not letter:
            return
        target_idx = -1
        for idx, app in enumerate(self.filtered_apps):
            nm = (app.get("name") or "").strip().upper()
            if not nm:
                continue
            if letter == "#":
                if not nm[0].isalpha():
                    target_idx = idx
                    break
            elif nm[0] >= letter and nm[0].isalpha():
                target_idx = idx
                break
        if target_idx >= 0:
            row = target_idx // self.cols
            target_px = getattr(self, "_pad_top", 18) + row * self.cell_h - 10
            self._scroll_to_px(target_px, immediate=self._alpha_scrubbing)

    def _on_alpha_motion(self, event):
        letter = self._letter_at_y(event.y)
        if letter != self._hovered_alpha:
            self._hovered_alpha = letter
            self.alpha_canvas.configure(cursor="hand2" if letter else "")
            self._draw_alpha_bar()

    def _on_alpha_leave(self, _event):
        if not self._alpha_scrubbing and self._hovered_alpha is not None:
            self._hovered_alpha = None
            self.alpha_canvas.configure(cursor="")
            self._draw_alpha_bar()

    def _on_alpha_press(self, event):
        if self.search_entry and self.search_entry.winfo_exists():
            self.search_entry.focus_set()
        self._alpha_scrubbing = True
        letter = self._letter_at_y(event.y)
        if letter:
            self._hovered_alpha = letter
            self._draw_alpha_bar()
            self._jump_to_letter(letter)

    def _on_alpha_drag(self, event):
        letter = self._letter_at_y(event.y)
        if letter and letter != self._hovered_alpha:
            self._hovered_alpha = letter
            self._draw_alpha_bar()
            self._jump_to_letter(letter)

    def _on_alpha_release(self, _event):
        self._alpha_scrubbing = False

    # ------------------------------------------------------------------
    # Footer Zone
    # ------------------------------------------------------------------
    def _draw_footer(self):
        if self._closed or not self.footer_canvas or not self.footer_canvas.winfo_exists():
            return
        fc = self.footer_canvas
        fc.delete("all")
        fc.create_line(40, 0, self.sw - 40, 0, fill="#32353d", width=1)
        cnt = len(self.filtered_apps)
        cat_txt = "" if self.selected_category == "Todas" else f" en {self.selected_category}"
        fc.create_text(
            self.sw // 2, self.footer_h // 2,
            text=f"{cnt} aplicaciones{cat_txt}  ·  Escribe para buscar  ·  ESC para cerrar",
            font=("Segoe UI", 10), fill="#9ca3af",
        )

    # ------------------------------------------------------------------
    # Async Background Icon Extraction Worker (Zero UI Stutter!)
    # ------------------------------------------------------------------
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
                        item = self._icon_work_q.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    if item is None or self._closed:
                        break
                    key, app = item
                    try:
                        pil_ic = icon_extractor.get_or_create_icon(app, size=112)
                        if pil_ic is not None:
                            if pil_ic.mode != "RGBA":
                                pil_ic = pil_ic.convert("RGBA")
                            with _PIL_LOCK:
                                _PIL_ICON_CACHE[key] = pil_ic
                                _PIL_ICON_CACHE.move_to_end(key)
                                while len(_PIL_ICON_CACHE) > _PIL_ICON_MAX:
                                    _PIL_ICON_CACHE.popitem(last=False)
                            if not self._closed:
                                self._ui_queue.put(("icon", (key, pil_ic)))
                    except Exception:
                        pass
            finally:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True, name="LaunchpadIconWorker").start()

    def _drain_ui_queue(self):
        self._drain_job = None
        if self._closed or not self.window or not self.window.winfo_exists():
            return
        try:
            processed = 0
            while processed < 14:
                try:
                    kind, payload = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                if kind == "catalog":
                    with app_launcher._CATALOG_LOCK:
                        raw = list(app_launcher._CATALOG_CACHE)
                    new_apps = _dedup_launchpad_apps(raw)
                    if len(new_apps) != len(self.all_apps):
                        self.all_apps = new_apps
                        self._apply_filter_data()
                        self._rebuild_grid()
                        self._draw_alpha_bar()
                        self._draw_footer()
                elif kind == "icon":
                    key, pil_base = payload
                    ids = self._cell_img_ids.get(key)
                    if ids and self.grid_canvas and self.grid_canvas.winfo_exists():
                        resized = pil_base if pil_base.size == (self.icon_size, self.icon_size) else pil_base.resize(
                            (self.icon_size, self.icon_size), Image.Resampling.LANCZOS
                        )
                        ph = ImageTk.PhotoImage(resized, master=self.parent)
                        self._icon_photos[key] = ph
                        for img_id in ids:
                            try:
                                self.grid_canvas.itemconfig(img_id, image=ph)
                            except Exception:
                                pass
        except Exception:
            pass
        if not self._closed and self.window and self.window.winfo_exists():
            self._drain_job = self.window.after(20, self._drain_ui_queue)

    # ------------------------------------------------------------------
    # Actions & Launch
    # ------------------------------------------------------------------
    def _launch_first(self):
        if self.filtered_apps:
            self._launch_app(self.filtered_apps[0])

    def _launch_app(self, app):
        target = (app.get("target") or "").strip()
        args = (app.get("args") or "").strip()
        self.close()
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
            print(f"[Launchpad] Error ejecutando app: {e}")

    def _launch_as_admin(self, app):
        target = (app.get("target") or "").strip()
        args = (app.get("args") or "").strip()
        self.close()
        if not target:
            return
        try:
            shell32.ShellExecuteW(None, "runas", target, args, None, 1)
        except Exception as e:
            print(f"[Launchpad] Admin launch error: {e}")

    def _open_location(self, app):
        path = ""
        for k in ("target", "lnk_path", "icon_src"):
            cand = (app.get(k) or "").strip()
            if not cand or cand.lower().startswith(("shell:", "http://", "https://")):
                continue
            res = resolve_shortcut_target(cand)
            if res and os.path.exists(res):
                path = res
                break
            if os.path.exists(cand):
                path = cand
                break
        self.close()
        if path:
            open_file_location(path)

    def _add_to_dock(self, app):
        if self.on_add_to_dock:
            try:
                self.on_add_to_dock(app)
            except Exception as e:
                print(f"[Launchpad] Add to dock error: {e}")

    def _force_refresh_catalog(self):
        def _on_ready():
            if not self._closed:
                self._ui_queue.put(("catalog", None))
        app_launcher.warm_launcher_cache(force=True, on_ready=_on_ready)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._icon_work_q.put(None)
        except Exception:
            pass
        for job_attr in ("_drain_job", "_search_job", "_focus_job", "_focus_job2", "_scroll_anim_job"):
            job = getattr(self, job_attr, None)
            if job:
                try:
                    self.window.after_cancel(job)
                except Exception:
                    pass
                setattr(self, job_attr, None)
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
        self._sprite_photos.clear()
        self._icon_photos.clear()
        self._placeholder_photos.clear()
        if callable(self.on_close):
            try:
                self.on_close()
            except Exception:
                pass
