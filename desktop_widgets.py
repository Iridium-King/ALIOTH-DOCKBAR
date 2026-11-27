import os
import sys
import time
import math
import queue
import struct
import ctypes
import subprocess
import uuid
import copy
from collections import OrderedDict
import todo_store
from ctypes import wintypes
import tkinter as tk
from tkinter import messagebox, filedialog, simpledialog
from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageOps, ImageFilter

from window_utils import (
    set_tool_window, update_layered_pil, release_layered_surface, animate_window, cancel_window_animation,
    flatten_pil_for_colorkey, alpha_rounded_rect, alpha_ellipse,
    alpha_line, resolve_styled_font, find_lowest_app_above_desktop,
    get_battery_status, is_hwnd_behind, get_blurred_desktop_backdrop,
)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
comctl32 = ctypes.windll.comctl32

# Lazy-loaded COM/OLE drop support (avoids loading pywin32 COM DLLs unless QuickAccessWidget is enabled)
_OLE_DROP_CLS = None

HWND_BOTTOM = 1
HWND_TOPMOST = ctypes.c_void_p(-1).value
HWND_NOTOPMOST = ctypes.c_void_p(-2).value
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
WM_DROPFILES = 0x0233
DROPEFFECT_COPY = 1
DROPEFFECT_LINK = 4

SUBCLASSPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ctypes.c_size_t, ctypes.c_size_t
)

shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
shell32.DragQueryFileW.restype = wintypes.UINT
shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
shell32.DragAcceptFiles.restype = None
shell32.DragFinish.argtypes = [ctypes.c_void_p]
shell32.DragFinish.restype = None
comctl32.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
comctl32.DefSubclassProc.restype = ctypes.c_longlong
comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t]
comctl32.SetWindowSubclass.restype = wintypes.BOOL


# Win32 Memory Status
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]

# Win32 Filetime for CPU Usage
class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

def _ft_to_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

def get_system_ram():
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    total_gb = stat.ullTotalPhys / (1024 ** 3)
    avail_gb = stat.ullAvailPhys / (1024 ** 3)
    used_gb = total_gb - avail_gb
    return used_gb, total_gb, stat.dwMemoryLoad

def get_disk_usage(drive="C:\\"):
    free_bytes = ctypes.c_uint64()
    total_bytes = ctypes.c_uint64()
    total_free = ctypes.c_uint64()
    res = kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(drive),
        ctypes.byref(free_bytes),
        ctypes.byref(total_bytes),
        ctypes.byref(total_free)
    )
    if res and total_bytes.value > 0:
        free_gb = free_bytes.value / (1024 ** 3)
        total_gb = total_bytes.value / (1024 ** 3)
        used_pct = int(((total_bytes.value - free_bytes.value) / total_bytes.value) * 100)
        return free_gb, total_gb, used_pct
    return 0.0, 0.0, 0

class CpuSampler:
    """Calculates CPU usage delta via Win32 GetSystemTimes without third party packages."""
    def __init__(self):
        self.prev_idle = 0
        self.prev_kernel = 0
        self.prev_user = 0
        self._init_sample()

    def _init_sample(self):
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        self.prev_idle = _ft_to_int(idle)
        self.prev_kernel = _ft_to_int(kernel)
        self.prev_user = _ft_to_int(user)

    def sample(self):
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return 0
        cur_idle = _ft_to_int(idle)
        cur_kernel = _ft_to_int(kernel)
        cur_user = _ft_to_int(user)

        d_idle = cur_idle - self.prev_idle
        d_kernel = cur_kernel - self.prev_kernel
        d_user = cur_user - self.prev_user

        self.prev_idle = cur_idle
        self.prev_kernel = cur_kernel
        self.prev_user = cur_user

        total = d_kernel + d_user
        if total <= 0:
            return 0
        usage = int(((total - d_idle) / total) * 100)
        return max(0, min(100, usage))

DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]


def _hex_to_rgb(hex_str, default=(255, 255, 255)):
    try:
        h = str(hex_str or "").strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        pass
    return default


def _launch_target_item(target, args="", url=""):
    """Safely launches an app, shell:AppsFolder AUMID, Chrome/Edge PWA proxy, file, or URL."""
    target = (target or "").strip()
    args = (args or "").strip()
    url = (url or "").strip()
    if target.lower() in ("explorer.exe", "explorer") and args.lower().startswith("shell:"):
        subprocess.Popen(["explorer.exe", args])
        return
    if target.lower().startswith("shell:"):
        os.startfile(target)
        return
    if target:
        if target.lower().startswith(("http://", "https://")):
            os.startfile(target)
            return
        if os.path.exists(target) or target.lower().endswith(".exe"):
            if args:
                subprocess.Popen(f'"{target}" {args}', shell=True)
            else:
                os.startfile(target)
            return
        os.startfile(target)
        return
    if url:
        for edge_path in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ):
            if os.path.exists(edge_path):
                subprocess.Popen([edge_path, f"--app={url}"])
                return
        import webbrowser
        webbrowser.open(url)


class BaseDesktopWidget:
    """Base class for floating or pinned glass desktop widgets with layered per-pixel alpha."""
    def __init__(self, root, config_mgr, widget_id, default_x=30, default_y=60, default_w=260, default_h=120, on_open_settings=None):
        self.root = root
        self.config_mgr = config_mgr
        self.widget_id = widget_id
        self.default_x = default_x
        self.default_y = default_y
        self.base_w = default_w
        self.base_h = default_h
        self.on_open_settings = on_open_settings

        try:
            self.dpi_scale = max(1.0, user32.GetDpiForSystem() / 96.0)
        except Exception:
            self.dpi_scale = 1.0

        self.window = None
        self.canvas = None
        self.top_hwnd = None
        self.transparent_key = "#020304"
        self.photo_ref = None
        self._font_cache = {}
        self._card_bg_cache = None
        self._hover_inside = False
        self._collapse_job = None
        self.min_btn_rect = (0, 0, 0, 0)
        self._last_z_state = None

        self.is_dragging = False
        self.is_resizing = False
        self.resize_dir = None
        self._hover_resize_dir = None
        self._cur_cursor = ""
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.resize_start_x = 0
        self.resize_start_y = 0
        self.resize_start_w = 0
        self.resize_start_h = 0
        self.resize_origin_x = 0
        self.resize_origin_y = 0
        self._last_resize_draw = 0.0
        self.update_job = None

        self.win_w, self.win_h = self._compute_dims()

    def get_cfg(self, key, default=None):
        return self.config_mgr.get_widget(self.widget_id, key, default)

    def set_cfg(self, key, value):
        self.config_mgr.set_widget(self.widget_id, key, value)

    def _get_size_scale(self):
        try:
            return max(0.65, min(2.0, float(self.get_cfg("size_scale", 1.0))))
        except Exception:
            return 1.0

    def _get_font_scale(self):
        try:
            return max(0.60, min(1.85, float(self.get_cfg("font_scale", 1.0) or 1.0)))
        except Exception:
            return 1.0

    def _get_spacing_mult(self):
        try:
            return max(0.55, min(2.5, float(self.get_cfg("item_spacing", 1.0) or 1.0)))
        except Exception:
            return 1.0

    def _get_styled_font(self, base_px, bold_default=False, italic_default=False, default_family="Segoe UI"):
        fam = self.get_cfg("font_family", None) or default_family
        bold_cfg = self.get_cfg("font_bold", None)
        italic_cfg = self.get_cfg("font_italic", None)
        use_bold = bool(bold_cfg) if bold_cfg is not None else bool(bold_default)
        use_italic = bool(italic_cfg) if italic_cfg is not None else bool(italic_default)
        px = max(8, int(round(float(base_px) * self._get_font_scale())))
        return resolve_styled_font(fam, px, bold=use_bold, italic=use_italic)

    def _get_effective_scale(self):
        return self._get_size_scale() * self.dpi_scale

    def _min_w(self):
        return max(110, int(120 * self.dpi_scale))

    def _max_h(self):
        return 720

    def _get_screen_and_work_bounds(self):
        """Returns (mon_x1, mon_y1, mon_x2, mon_y2, work_x1, work_y1, work_x2, work_y2) in virtual/primary screen coords."""
        sw = int(self.root.winfo_screenwidth())
        sh = int(self.root.winfo_screenheight())
        mon_box = (0, 0, sw, sh)
        work_box = (0, 0, sw, sh)
        try:
            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]
            hwnd = None
            if self.window and self.window.winfo_exists():
                hwnd = self.top_hwnd or user32.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
            hmon = user32.MonitorFromWindow(hwnd, 2) if hwnd else user32.MonitorFromPoint(wintypes.POINT(int(self.default_x), int(self.default_y)), 2)
            if hmon:
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(_MONITORINFO)
                if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    m, w = mi.rcMonitor, mi.rcWork
                    mon_box = (int(m.left), int(m.top), int(m.right), int(m.bottom))
                    work_box = (int(w.left), int(w.top), int(w.right), int(w.bottom))
                    return (*mon_box, *work_box)
        except Exception:
            pass
        try:
            wr = wintypes.RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wr), 0):
                work_box = (int(wr.left), int(wr.top), int(wr.right), int(wr.bottom))
        except Exception:
            pass
        return (*mon_box, *work_box)

    def _min_h(self):
        return max(34, int(38 * self.dpi_scale))

    def _collapsed_h(self):
        return max(34, int(38 * self.dpi_scale))

    def _is_collapsed_view(self):
        if bool(self.get_cfg("minimized", False)):
            return True
        if bool(self.get_cfg("auto_collapse", False)):
            if self._hover_inside or self.is_resizing or getattr(self, "_active_modal", None) is not None:
                return False
            from custom_menu import CustomMenu
            if CustomMenu.has_active_menu():
                return False
            return True
        return False

    def _compute_full_dims(self):
        cw = self.get_cfg("custom_w", None)
        ch = self.get_cfg("custom_h", None)
        s = self._get_effective_scale()
        w = max(self._min_w(), int(cw)) if cw is not None else max(self._min_w(), int(self.base_w * s))
        h = max(int(52 * self.dpi_scale), int(ch)) if ch is not None else max(int(52 * self.dpi_scale), int(self.base_h * s))
        return w, h

    def _compute_dims(self):
        w, full_h = self._compute_full_dims()
        if self._is_collapsed_view():
            return w, self._collapsed_h()
        return w, full_h

    def _compute_anchored_pos(self, w, h):
        """Computes exact physical screen (x, y), honoring anchor_preset across collapse/expand/restart."""
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        preset = (self.get_cfg("anchor_preset", "") or "").strip()
        mx, mt, mb = 22, 42, 74
        if preset == "top_left":
            return mx, mt
        if preset == "top_center":
            return max(0, (sw - w) // 2), mt
        if preset == "top_right":
            return max(0, sw - w - mx), mt
        if preset == "bottom_left":
            return mx, max(mt, sh - h - mb)
        if preset == "bottom_right":
            return max(0, sw - w - mx), max(mt, sh - h - mb)
        x = self.get_cfg("x", self.default_x)
        y = self.get_cfg("y", self.default_y)
        mx1, my1, mx2, my2, wx1, wy1, wx2, wy2 = self._get_screen_and_work_bounds()
        top_limit = (wy1 if wy1 > my1 else my1) if self.widget_id == "todo_list" else max(32, wy1)
        x = max(mx1, min(mx2 - w, int(x)))
        y = max(top_limit, min(my2 - h, int(y)))
        return x, y

    def _set_window_size_live(self, new_w, new_h):
        new_w = max(self._min_w(), int(new_w))
        new_h = max(self._min_h(), int(new_h))
        if self.window and self.window.winfo_exists():
            if self.is_resizing:
                x, y = self.window.winfo_x(), self.window.winfo_y()
            else:
                x, y = self._compute_anchored_pos(new_w, new_h)
            cur_x, cur_y = self.window.winfo_x(), self.window.winfo_y()
            if new_w != self.win_w or new_h != self.win_h or x != cur_x or y != cur_y:
                self.win_w = new_w
                self.win_h = new_h
                self.window.geometry(f"{new_w}x{new_h}+{x}+{y}")
                if self.top_hwnd:
                    user32.SetWindowPos(
                        self.top_hwnd, 0, int(x), int(y), int(new_w), int(new_h),
                        SWP_NOZORDER | SWP_NOACTIVATE
                    )
                if self.canvas:
                    self.canvas.config(width=new_w, height=new_h)
        else:
            self.win_w = new_w
            self.win_h = new_h

    def _refresh_dimensions(self):
        w, h = self._compute_dims()
        self._card_bg_cache = None
        if self.window and self.window.winfo_exists():
            x, y = self._compute_anchored_pos(w, h)
            self.win_w, self.win_h = w, h
            self.window.geometry(f"{w}x{h}+{x}+{y}")
            if self.top_hwnd:
                user32.SetWindowPos(
                    self.top_hwnd, 0, int(x), int(y), int(w), int(h),
                    SWP_NOZORDER | SWP_NOACTIVATE
                )
            if self.canvas:
                self.canvas.config(width=w, height=h)
        else:
            self.win_w, self.win_h = w, h

    def _build_card_surface(self, w, h, style_override=None, accent_rgb=(59, 130, 246), radius_override=None):
        """
        Builds (and caches) the rounded widget card background supporting 6 visual styles,
        custom bg_color, and rounded-masked bg_image with dark readability veil (zero per-tick disk I/O).
        """
        s = self._get_effective_scale()
        w_style = style_override or self.get_cfg("widget_style", "cristal") or "cristal"
        opacity = max(0.08, min(1.0, float(self.get_cfg("opacity", 0.88) or 0.88)))
        bg_alpha = max(8, int(255 * opacity))
        bg_hex = (self.get_cfg("bg_color", "") or "").strip()
        bg_img_path = (self.get_cfg("bg_image", "") or "").strip()
        bg_dim = max(0.0, min(0.92, float(self.get_cfg("bg_image_dim", 0.45) if self.get_cfg("bg_image_dim", None) is not None else 0.45)))
        ext_hl = bool(getattr(self, "_ext_drag_hover", False))
        img_mtime = os.path.getmtime(bg_img_path) if (bg_img_path and os.path.exists(bg_img_path)) else 0.0
        radius = int(radius_override) if radius_override is not None else int((10 if w_style in ("clasico", "editorial") else 18) * min(1.3, max(0.75, s)))
        radius = max(6, min(radius, (min(w, h) - 4) // 2))

        cache_key = (w, h, radius, w_style, bg_alpha, bg_hex, bg_img_path, img_mtime, round(bg_dim, 2), tuple(accent_rgb), ext_hl)
        if self._card_bg_cache is not None and self._card_bg_cache[0] == cache_key:
            return self._card_bg_cache[1].copy()

        surf = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(surf, "RGBA")

        style_presets = {
            "cristal": ((18, 20, 26, bg_alpha), (255, 255, 255, 48), 1),
            "moderno": ((18, 20, 26, bg_alpha), (255, 255, 255, 48), 1),
            "neon": ((9, 13, 24, min(255, max(18, bg_alpha))), (56, 189, 248, 220), 2),
            "minimal": ((16, 18, 24, max(6, int(bg_alpha * 0.58))), (255, 255, 255, 20), 1),
            "oled": ((4, 5, 8, min(255, max(30, bg_alpha))), (*accent_rgb, 150), 1),
            "acrilico": ((15, 23, 42, bg_alpha), (147, 197, 253, 95), 1),
            "clasico": ((24, 22, 20, bg_alpha), (212, 175, 55, 145), 1),
            "editorial": ((24, 22, 20, bg_alpha), (212, 175, 55, 145), 1),
        }
        fill_rgba, outline_rgba, border_w = style_presets.get(w_style, style_presets["cristal"])
        if bg_hex:
            custom_rgb = _hex_to_rgb(bg_hex, fill_rgba[:3])
            fill_rgba = (*custom_rgb, fill_rgba[3])
        if ext_hl:
            outline_rgba = (96, 165, 250, 225)
            border_w = 2

        if w_style == "neon":
            draw.rounded_rectangle([3, 3, w - 4, h - 4], radius=radius, outline=(*accent_rgb, 68), width=4)

        inner_w, inner_h = max(4, w - 5), max(4, h - 5)
        placed_img = False
        if bg_img_path and img_mtime > 0:
            try:
                with Image.open(bg_img_path) as raw_im:
                    fitted = ImageOps.fit(raw_im.convert("RGBA"), (inner_w, inner_h), Image.Resampling.LANCZOS)
                if bg_dim > 0.01:
                    veil = Image.new("RGBA", (inner_w, inner_h), (8, 10, 16, int(255 * bg_dim)))
                    fitted = Image.alpha_composite(fitted, veil)
                mask = Image.new("L", (inner_w, inner_h), 0)
                ImageDraw.Draw(mask).rounded_rectangle([0, 0, inner_w - 1, inner_h - 1], radius=max(4, radius - 1), fill=bg_alpha)
                fitted.putalpha(mask)
                surf.alpha_composite(fitted, (2, 2))
                placed_img = True
            except Exception as e:
                print(f"[Widget BG] Image load error: {e}")

        draw.rounded_rectangle(
            [2, 2, w - 3, h - 3],
            radius=radius,
            fill=None if placed_img else fill_rgba,
            outline=outline_rgba,
            width=border_w,
        )
        if w_style == "oled" and w > 32:
            alpha_line(surf, [(radius + 4, 3), (w - radius - 4, 3)], fill=(*accent_rgb, 215), width=2)

        self._card_bg_cache = (cache_key, surf)
        return surf.copy()

    def _draw_min_button(self, composite, draw, bx1, by1, bx2, by2, is_collapsed=False):
        self.min_btn_rect = (int(bx1), int(by1), int(bx2), int(by2))
        alpha_rounded_rect(
            composite, [bx1, by1, bx2, by2], radius=max(4, (by2 - by1) // 4),
            fill=(255, 255, 255, 26), outline=(148, 163, 184, 95), width=1
        )
        cx, cy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
        if is_collapsed:
            r = max(3, int((bx2 - bx1) * 0.22))
            draw.rounded_rectangle([cx - r, cy - r, cx + r, cy + r], radius=2, outline=(226, 232, 240, 235), width=1)
        else:
            hw = max(3, int((bx2 - bx1) * 0.24))
            draw.line([(cx - hw, cy), (cx + hw, cy)], fill=(226, 232, 240, 235), width=2)

    def _render_collapsed_pill(self, title_text, summary_text="", accent_rgb=(59, 130, 246)):
        w = self._compute_full_dims()[0]
        h = self._collapsed_h()
        self._set_window_size_live(w, h)
        composite = self._build_card_surface(w, h, accent_rgb=accent_rgb, radius_override=h // 2 - 2)
        draw = ImageDraw.Draw(composite, "RGBA")
        s = self.dpi_scale
        pad_x = int(12 * s)
        cy = h // 2
        dot_r = max(3, int(4 * s))
        alpha_ellipse(composite, [pad_x, cy - dot_r, pad_x + dot_r * 2, cy + dot_r], fill=(*accent_rgb, 255))

        btn_s = max(18, int(20 * s))
        bx2 = w - pad_x
        bx1 = bx2 - btn_s
        by1 = cy - btn_s // 2
        by2 = by1 + btn_s
        self._draw_min_button(composite, draw, bx1, by1, bx2, by2, is_collapsed=True)

        f_t = self._get_styled_font(10.5 * s, bold_default=True)
        f_s = self._get_styled_font(9.5 * s, bold_default=False)
        tx = pad_x + dot_r * 2 + int(7 * s)
        full_lbl = f"{title_text}  •  {summary_text}" if summary_text else title_text
        max_tw = max(30, bx1 - tx - int(8 * s))
        while full_lbl and draw.textlength(full_lbl, font=f_t) > max_tw:
            full_lbl = full_lbl[:-1]
        draw.text((tx, cy), full_lbl, fill=(241, 245, 249, 250), font=f_t, anchor="lm")
        self._render_to_canvas(composite, w, h)

    def _toggle_minimized(self):
        nxt = not bool(self.get_cfg("minimized", False))
        def apply():
            if not self.window or not self.window.winfo_exists():
                return
            self.set_cfg('minimized', nxt)
            self._redraw_now()
            animate_window(self.window, layered_hwnd=self.top_hwnd)
        if self.window and self.window.winfo_exists():
            animate_window(self.window, showing=False, on_done=apply, layered_hwnd=self.top_hwnd)

    def _toggle_auto_collapse(self):
        nxt = not bool(self.get_cfg("auto_collapse", False))
        self.set_cfg("auto_collapse", nxt)
        self._redraw_now()

    def _toggle_lock_position(self):
        nxt = not bool(self.get_cfg("lock_position", False))
        self.set_cfg("lock_position", nxt)

    def _apply_anchor_preset(self, preset):
        if preset not in ("top_left", "top_center", "top_right", "bottom_left", "bottom_right"):
            return
        self.set_cfg("anchor_preset", preset)
        w, h = self.win_w, self.win_h
        nx, ny = self._compute_anchored_pos(w, h)
        self.set_cfg("x", int(nx))
        self.set_cfg("y", int(ny))
        if self.window and self.window.winfo_exists():
            self.window.geometry(f"{w}x{h}+{int(nx)}+{int(ny)}")
            if self.top_hwnd:
                user32.SetWindowPos(self.top_hwnd, 0, int(nx), int(ny), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            self._redraw_now()

    def _on_base_enter(self, _event=None):
        if self._collapse_job:
            try:
                self.root.after_cancel(self._collapse_job)
            except Exception:
                pass
            self._collapse_job = None
        if not self._hover_inside:
            self._hover_inside = True
            if (bool(self.get_cfg("auto_collapse", False)) and not bool(self.get_cfg("minimized", False))) or self.widget_id == "clock" or not bool(self.get_cfg("lock_position", False)):
                self._redraw_now()

    def _on_base_leave(self, _event=None):
        if self._collapse_job:
            try:
                self.root.after_cancel(self._collapse_job)
            except Exception:
                pass
        self._collapse_job = self.root.after(260, self._deferred_leave_check)

    def _deferred_leave_check(self):
        self._collapse_job = None
        if not self.window or not self.window.winfo_exists():
            return
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        wx, wy = self.window.winfo_rootx(), self.window.winfo_rooty()
        if wx <= pt.x <= wx + self.win_w and wy <= pt.y <= wy + self.win_h:
            return
        changed_hl = self._hover_resize_dir is not None
        self._hover_resize_dir = None
        self._set_cursor("")
        if self._hover_inside:
            self._hover_inside = False
            if (bool(self.get_cfg("auto_collapse", False)) and not bool(self.get_cfg("minimized", False))) or self.widget_id == "clock" or not bool(self.get_cfg("lock_position", False)):
                self._redraw_now()
        elif changed_hl:
            self._redraw_now()

    def is_enabled(self):
        return bool(self.get_cfg("enabled", True))

    def show(self):
        if self.window and self.window.winfo_exists():
            self._hiding = False
            animate_window(self.window, layered_hwnd=self.top_hwnd)
            return

        self.win_w, self.win_h = self._compute_dims()

        self.window = tk.Toplevel(self.root)
        self.window.overrideredirect(True)
        self.window.config(bg=self.transparent_key)

        # Guarantee zero taskbar / Alt+Tab presence + WS_EX_LAYERED true alpha + Z-order independence from Dockbar
        self.top_hwnd = set_tool_window(self.window, owner_hwnd=None, layered=True)

        x, y = self._compute_anchored_pos(self.win_w, self.win_h)
        self.window.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.window,
            width=self.win_w,
            height=self.win_h,
            bg=self.transparent_key,
            highlightthickness=0,
            bd=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<Button-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.canvas.bind("<Button-3>", self._on_context_menu)
        self.canvas.bind("<Motion>", self._on_base_motion, add="+")
        self.canvas.bind("<Enter>", self._on_base_enter, add="+")
        self.canvas.bind("<Leave>", self._on_base_leave, add="+")

        self._apply_z_order()
        self._start_update_loop()
        animate_window(self.window, layered_hwnd=self.top_hwnd)

    def hide(self, animate=True):
        if animate and self.window and self.window.winfo_exists():
            if not getattr(self, '_hiding', False):
                self._hiding = True
                animate_window(self.window, showing=False, on_done=lambda: self.hide(animate=False), layered_hwnd=self.top_hwnd)
            return
        self._hiding = False
        if self.window:
            cancel_window_animation(self.window)
        if self._collapse_job:
            try:
                self.root.after_cancel(self._collapse_job)
            except Exception:
                pass
            self._collapse_job = None
        if self.update_job:
            try:
                self.root.after_cancel(self.update_job)
            except Exception:
                pass
            self.update_job = None

        if self.top_hwnd:
            release_layered_surface(self.top_hwnd)
        if self.window and self.window.winfo_exists():
            try:
                self.window.destroy()
            except Exception:
                pass
        self.window = None
        self.canvas = None
        self.top_hwnd = None
        self._card_bg_cache = None
        self.photo_ref = None
        self._font_cache.clear()
        for name in ('_qa_icon_cache', '_icon_cache'):
            cache = getattr(self, name, None)
            if cache is not None:
                cache.clear()
        self._last_z_state = None

    def _apply_z_order(self, lowest_app_override=None, desk_cover_override=None, exclude_hwnds=()):
        """
        Places pinned desktop widgets strictly BELOW open normal app windows AND strictly ABOVE
        Windows 11's raised WorkerW Show-Desktop sheet (so Taskbar Show Desktop, Dockbar Show Desktop,
        and Win+D NEVER hide Clock, Battery, To-Do, or any other desktop widget!).
        """
        if not self.window or not self.window.winfo_exists():
            return
        try:
            hwnd = self.top_hwnd or user32.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
            if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                self._last_z_state = None
            pinned = self.get_cfg("pinned_to_desktop", True)
            if not pinned:
                if self._last_z_state != ("floating_top", 0) or (desk_cover_override and is_hwnd_behind(hwnd, desk_cover_override)):
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                    self._last_z_state = ("floating_top", 0)
                return

            if lowest_app_override is None:
                excl = set(exclude_hwnds or ()) | {int(hwnd)}
                lowest_app, desk_cover = find_lowest_app_above_desktop(exclude_hwnds=excl)
            else:
                lowest_app = lowest_app_override
                desk_cover = desk_cover_override

            behind_desk = bool(desk_cover and is_hwnd_behind(hwnd, desk_cover))
            if behind_desk:
                self._last_z_state = None

            if lowest_app:
                state_sig = ("below_app", int(lowest_app))
                if self._last_z_state != state_sig or behind_desk:
                    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                    user32.SetWindowPos(hwnd, int(lowest_app), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                    self._last_z_state = state_sig
            else:
                # Empty desktop or Windows 'Mostrar escritorio' (raised WorkerW): ALWAYS ensure widget stays above WorkerW!
                has_topmost = bool(user32.GetWindowLongW(hwnd, -20) & 0x00000008)
                if self._last_z_state != ("desktop_top", 0) or behind_desk or not has_topmost:
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                    self._last_z_state = ("desktop_top", 0)
        except Exception:
            pass

    _RESIZE_CURSORS = {
        "nw": "size_nw_se",
        "se": "size_nw_se",
        "ne": "size_ne_sw",
        "sw": "size_ne_sw",
        "n": "sb_v_double_arrow",
        "s": "sb_v_double_arrow",
        "e": "sb_h_double_arrow",
        "w": "sb_h_double_arrow",
    }

    def _set_cursor(self, cursor_name):
        if self.canvas and self._cur_cursor != cursor_name:
            try:
                self.canvas.config(cursor=cursor_name)
                self._cur_cursor = cursor_name
            except Exception:
                pass

    def _detect_resize_dir(self, x, y):
        """Returns 8-point resize direction ('nw','n','ne','e','se','s','sw','w') or None."""
        if bool(self.get_cfg("lock_position", False)) or self._is_collapsed_view():
            return None
        if self._hit_min_button(x, y):
            return None
        w, h = self.win_w, self.win_h
        if w < 60 or h < 36:
            return None
        c_hit = max(12, int(14 * self.dpi_scale))
        e_hit = max(7, int(9 * self.dpi_scale))

        # Check explicit 8 handle positions first (corners + midpoints) for crisp magnetic grab
        handles = {
            "nw": (6, 6),
            "n": (w // 2, 6),
            "ne": (w - 7, 6),
            "e": (w - 7, h // 2),
            "se": (w - 7, h - 7),
            "s": (w // 2, h - 7),
            "sw": (6, h - 7),
            "w": (6, h // 2),
        }
        for d, (hx, hy) in handles.items():
            if abs(x - hx) <= c_hit and abs(y - hy) <= c_hit:
                return d

        # Also support entire outer edge perimeter
        on_l = x <= e_hit
        on_r = x >= w - e_hit
        on_t = y <= e_hit
        on_b = y >= h - e_hit
        if on_t and on_l:
            return "nw"
        if on_t and on_r:
            return "ne"
        if on_b and on_l:
            return "sw"
        if on_b and on_r:
            return "se"
        if on_t:
            return "n"
        if on_b:
            return "s"
        if on_l:
            return "w"
        if on_r:
            return "e"
        return None

    def _on_base_motion(self, event):
        if self.is_resizing or self.is_dragging:
            return
        rdir = self._detect_resize_dir(event.x, event.y)
        self._set_cursor(self._RESIZE_CURSORS.get(rdir, "") if rdir else "")
        if rdir != self._hover_resize_dir:
            self._hover_resize_dir = rdir
            if not bool(self.get_cfg("lock_position", False)) and not self._is_collapsed_view():
                self._redraw_now()

    def _is_in_resize_zone(self, x, y):
        return self._detect_resize_dir(x, y) is not None

    def _hit_min_button(self, x, y):
        mb = self.min_btn_rect
        return mb[2] > mb[0] and mb[0] <= x <= mb[2] and mb[1] <= y <= mb[3]

    def _on_drag_start(self, event):
        if self._hit_min_button(event.x, event.y):
            self._toggle_minimized()
            return
        if bool(self.get_cfg("lock_position", False)):
            self.is_resizing = False
            self.resize_dir = None
            self.is_dragging = False
            return
        rdir = self._detect_resize_dir(event.x, event.y)
        if rdir:
            self.is_resizing = True
            self.resize_dir = rdir
            self.is_dragging = False
            self.resize_start_x = self.window.winfo_x() if self.window else int(self.get_cfg("x", self.default_x))
            self.resize_start_y = self.window.winfo_y() if self.window else int(self.get_cfg("y", self.default_y))
            self.resize_start_w = self.win_w
            self.resize_start_h = self.win_h
            self.resize_origin_x = event.x_root
            self.resize_origin_y = event.y_root
            return
        self.is_resizing = False
        self.resize_dir = None
        self.is_dragging = True
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def _on_drag_move(self, event):
        if self.is_resizing and self.window and self.window.winfo_exists():
            rdir = self.resize_dir or "se"
            dx = event.x_root - self.resize_origin_x
            dy = event.y_root - self.resize_origin_y
            mx1, my1, mx2, my2, wx1, wy1, wx2, wy2 = self._get_screen_and_work_bounds()
            min_w, max_w = self._min_w(), 960
            min_h, max_h = self._min_h(), self._max_h()
            if self.widget_id == "todo_list":
                top_limit = wy1 if wy1 > my1 else my1
                bot_limit = my2
            else:
                top_limit = max(32, wy1)
                bot_limit = my2

            new_x = self.resize_start_x
            new_y = self.resize_start_y
            new_w = self.resize_start_w
            new_h = self.resize_start_h

            if "e" in rdir:
                new_w = max(min_w, min(max_w, int(self.resize_start_w + dx)))
            if "w" in rdir:
                right_edge = self.resize_start_x + self.resize_start_w
                cand_w = max(min_w, min(max_w, int(self.resize_start_w - dx)))
                new_w = cand_w
                new_x = right_edge - new_w
            if "s" in rdir:
                avail_down = max(min_h, bot_limit - new_y)
                new_h = max(min_h, min(max_h, avail_down, int(self.resize_start_h + dy)))
            if "n" in rdir:
                bottom_edge = self.resize_start_y + self.resize_start_h
                max_up_h = max(min_h, bottom_edge - top_limit)
                cand_h = max(min_h, min(max_h, max_up_h, int(self.resize_start_h - dy)))
                new_h = cand_h
                new_y = bottom_edge - new_h

            new_x = max(mx1, min(mx2 - new_w, int(new_x)))
            new_y = max(top_limit, min(bot_limit - new_h, int(new_y)))

            if (new_w != self.win_w or new_h != self.win_h
                    or new_x != self.window.winfo_x() or new_y != self.window.winfo_y()):
                self.win_w = new_w
                self.win_h = new_h
                self.window.geometry(f"{new_w}x{new_h}+{new_x}+{new_y}")
                if self.top_hwnd:
                    user32.SetWindowPos(
                        self.top_hwnd, 0, int(new_x), int(new_y), int(new_w), int(new_h),
                        SWP_NOZORDER | SWP_NOACTIVATE
                    )
                if self.canvas:
                    self.canvas.config(width=new_w, height=new_h)
                now_t = time.time()
                if now_t - self._last_resize_draw >= 0.028:
                    self._last_resize_draw = now_t
                    if hasattr(self, "_update_clock"):
                        self._update_clock(force_draw=True)
                    elif hasattr(self, "_update_stats"):
                        self._update_stats()
                    elif hasattr(self, "_render"):
                        self._render()
            return

        if not self.is_dragging or not self.window:
            return
        cur_x = self.window.winfo_x()
        cur_y = self.window.winfo_y()
        dx = event.x - self.drag_start_x
        dy = event.y - self.drag_start_y
        new_x = cur_x + dx
        new_y = cur_y + dy
        self.window.geometry(f"{self.win_w}x{self.win_h}+{new_x}+{new_y}")
        if self.top_hwnd:
            user32.SetWindowPos(self.top_hwnd, 0, int(new_x), int(new_y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def _finish_resize(self):
        self.is_resizing = False
        self.resize_dir = None
        self.set_cfg("custom_w", int(self.win_w))
        self.set_cfg("custom_h", int(self.win_h))
        if self.window and self.window.winfo_exists():
            self.set_cfg("x", int(self.window.winfo_x()))
            self.set_cfg("y", int(self.window.winfo_y()))
        if self.widget_id == "todo_list":
            self.set_cfg("auto_grow", False)
        self._apply_z_order()
        if hasattr(self, "_update_clock"):
            self._update_clock(force_draw=True)
        elif hasattr(self, "_update_stats"):
            self._update_stats()
        elif hasattr(self, "_render"):
            self._render()

    def _on_drag_end(self, event):
        if self.is_resizing:
            self._finish_resize()
            return
        if not self.is_dragging:
            return
        self.is_dragging = False
        if self.window and self.window.winfo_exists():
            x = int(self.window.winfo_x())
            y = int(self.window.winfo_y())
            old_x = int(self.get_cfg("x", self.default_x))
            old_y = int(self.get_cfg("y", self.default_y))
            if abs(x - old_x) >= 2 or abs(y - old_y) >= 2:
                self.set_cfg("anchor_preset", "")
                self.set_cfg("x", x)
                self.set_cfg("y", y)
            self._apply_z_order()

    def _render_to_canvas(self, composite, w, h):
        """Renders true 32-bit per-pixel alpha layered glass card directly to screen."""
        if not self.window or not self.window.winfo_exists():
            return
        # Draw 8-point resize handle dots directly on widget when hovered/resizing & unlocked
        if w >= 100 and h >= 48 and not bool(self.get_cfg("lock_position", False)) and not self._is_collapsed_view():
            if self._hover_inside or self.is_resizing:
                active_d = self.resize_dir or self._hover_resize_dir
                handles = [
                    ("nw", 6, 6), ("n", w // 2, 6), ("ne", w - 7, 6),
                    ("e", w - 7, h // 2), ("se", w - 7, h - 7),
                    ("s", w // 2, h - 7), ("sw", 6, h - 7), ("w", 6, h // 2),
                ]
                for d, hx, hy in handles:
                    is_act = (d == active_d)
                    r = 4 if is_act else 3
                    fill_c = (56, 189, 248, 248) if is_act else (241, 245, 249, 195)
                    out_c = (15, 23, 42, 235)
                    alpha_ellipse(composite, [hx - r, hy - r, hx + r, hy + r], fill=fill_c, outline=out_c, width=1)
        self.window.update_idletasks()
        x = self.window.winfo_x()
        y = self.window.winfo_y()
        hwnd = self.top_hwnd or user32.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
        if not update_layered_pil(hwnd, x, y, composite):
            final_img = flatten_pil_for_colorkey(composite, (2, 3, 4))
            self.photo_ref = ImageTk.PhotoImage(final_img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo_ref)

    def _redraw_now(self):
        self._refresh_dimensions()
        if hasattr(self, "_update_clock"):
            self._update_clock(force_draw=True)
        elif hasattr(self, "_update_stats"):
            self._update_stats()
        elif hasattr(self, "_render"):
            self._render()

    def _set_size_preset(self, scale_val):
        self.set_cfg("custom_w", None)
        self.set_cfg("custom_h", None)
        if self.widget_id == "todo_list":
            self.set_cfg("auto_grow", True)
        self.set_cfg("size_scale", float(scale_val))
        self._redraw_now()

    def _set_opacity_preset(self, opacity_val):
        self.set_cfg("opacity", float(opacity_val))
        self._redraw_now()

    def _set_widget_style(self, st_name):
        self.set_cfg("widget_style", st_name)
        self._redraw_now()

    def _pick_widget_bg_color(self):
        from tkinter import colorchooser
        cur = self.get_cfg("bg_color", "#12141a") or "#12141a"
        res = colorchooser.askcolor(color=cur, title="Color de Fondo del Widget", parent=self.window)
        if res and res[1]:
            self.set_cfg("bg_color", res[1])
            self._redraw_now()

    def _pick_widget_bg_image(self):
        path = filedialog.askopenfilename(
            title="Seleccionar Imagen de Fondo del Widget",
            parent=self.window,
            filetypes=[("Imágenes", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"), ("Todos", "*.*")],
        )
        if path and os.path.exists(path):
            cached_p = _import_widget_bg_image(path)
            self.set_cfg("bg_image", cached_p or path)
            self._redraw_now()

    def _clear_widget_bg_custom(self):
        self.set_cfg("bg_color", "")
        self.set_cfg("bg_image", "")
        self._redraw_now()

    def _append_common_menu_items(self, menu):
        is_min = bool(self.get_cfg("minimized", False))
        menu.add_command("Expandir widget" if is_min else "Minimizar widget (barra compacta)", self._toggle_minimized, checked=is_min)
        menu.add_command("Auto-desplegar al pasar el mouse", self._toggle_auto_collapse, checked=bool(self.get_cfg("auto_collapse", False)))
        menu.add_command("Bloquear posición fija", self._toggle_lock_position, checked=bool(self.get_cfg("lock_position", False)))

        sec_pos = menu.add_section("Posición rápida en pantalla", expanded=False)
        cur_anc = self.get_cfg("anchor_preset", "")
        sec_pos.add_command("Superior Izquierda", lambda: self._apply_anchor_preset("top_left"), checked=(cur_anc == "top_left"))
        sec_pos.add_command("Superior Centro", lambda: self._apply_anchor_preset("top_center"), checked=(cur_anc == "top_center"))
        sec_pos.add_command("Superior Derecha", lambda: self._apply_anchor_preset("top_right"), checked=(cur_anc == "top_right"))
        sec_pos.add_command("Inferior Izquierda", lambda: self._apply_anchor_preset("bottom_left"), checked=(cur_anc == "bottom_left"))
        sec_pos.add_command("Inferior Derecha", lambda: self._apply_anchor_preset("bottom_right"), checked=(cur_anc == "bottom_right"))
        sec_pos.add_separator()
        sec_pos.add_command("Restablecer posición inicial", self.reset_position)

        sec_sty = menu.add_section("Estilo y Fondo del Widget", expanded=False)
        cur_ws = self.get_cfg("widget_style", "cristal")
        sec_sty.add_command("Cristal Oscuro", lambda: self._set_widget_style("cristal"), checked=(cur_ws == "cristal"))
        sec_sty.add_command("Neón Ciberpunk", lambda: self._set_widget_style("neon"), checked=(cur_ws == "neon"))
        sec_sty.add_command("Minimalista", lambda: self._set_widget_style("minimal"), checked=(cur_ws == "minimal"))
        sec_sty.add_command("Negro OLED", lambda: self._set_widget_style("oled"), checked=(cur_ws == "oled"))
        sec_sty.add_command("Acrílico Azul", lambda: self._set_widget_style("acrilico"), checked=(cur_ws == "acrilico"))
        sec_sty.add_command("Editorial Dorado", lambda: self._set_widget_style("editorial"), checked=(cur_ws == "editorial"))
        sec_sty.add_separator()
        sec_sty.add_command("Elegir color de fondo...", self._pick_widget_bg_color)
        sec_sty.add_command("Elegir imagen de fondo...", self._pick_widget_bg_image)
        if self.get_cfg("bg_color") or self.get_cfg("bg_image"):
            sec_sty.add_command("Restaurar fondo original", self._clear_widget_bg_custom, danger=True)

        sec_sz = menu.add_section("Tamaño y Transparencia", expanded=False)
        cur_scale = round(self._get_size_scale(), 2)
        sec_sz.add_command("Pequeño (80%)", lambda: self._set_size_preset(0.8), checked=(abs(cur_scale - 0.8) < 0.08))
        sec_sz.add_command("Mediano (100%)", lambda: self._set_size_preset(1.0), checked=(abs(cur_scale - 1.0) < 0.08))
        sec_sz.add_command("Grande (130%)", lambda: self._set_size_preset(1.3), checked=(abs(cur_scale - 1.3) < 0.08))
        sec_sz.add_command("Extra Grande (160%)", lambda: self._set_size_preset(1.6), checked=(abs(cur_scale - 1.6) < 0.08))
        sec_sz.add_separator()
        cur_op = round(float(self.get_cfg("opacity", 0.88)), 2)
        sec_sz.add_command("Muy Transparente (35%)", lambda: self._set_opacity_preset(0.35), checked=(abs(cur_op - 0.35) < 0.12))
        sec_sz.add_command("Cristal Suave (65%)", lambda: self._set_opacity_preset(0.65), checked=(abs(cur_op - 0.65) < 0.12))
        sec_sz.add_command("Estándar (88%)", lambda: self._set_opacity_preset(0.88), checked=(abs(cur_op - 0.88) < 0.08))
        sec_sz.add_command("Sólido (100%)", lambda: self._set_opacity_preset(1.0), checked=(cur_op >= 0.96))

        menu.add_separator()
        pinned = self.get_cfg("pinned_to_desktop", True)
        menu.add_command("Fijar al fondo del escritorio", self._toggle_pinned, checked=pinned)
        if self.on_open_settings:
            menu.add_command("Configuración General...", self.on_open_settings)
        menu.add_command("Ocultar este widget", self._hide_widget, danger=True)

    def _on_context_menu(self, event):
        from custom_menu import CustomMenu
        menu = CustomMenu(self.window)
        self._append_common_menu_items(menu)
        menu.show(event.x_root, event.y_root)

    def _toggle_pinned(self):
        cur = self.get_cfg("pinned_to_desktop", True)
        self.set_cfg("pinned_to_desktop", not cur)
        self._apply_z_order()

    def reset_position(self):
        self.set_cfg("x", self.default_x)
        self.set_cfg("y", self.default_y)
        self.set_cfg("custom_w", None)
        self.set_cfg("custom_h", None)
        self.set_cfg("size_scale", 1.0)
        self._refresh_dimensions()
        if self.window and self.window.winfo_exists():
            self.window.geometry(f"{self.win_w}x{self.win_h}+{self.default_x}+{self.default_y}")
            self._apply_z_order()
            self._redraw_now()

    def _hide_widget(self):
        self.set_cfg("enabled", False)
        self.hide()

    def _start_update_loop(self):
        pass


def _import_widget_bg_image(src_path, max_dim=960):
    """Downscales large wallpapers once to cache/widget_bgs/ so widget RAM stays tiny (<1 MB)."""
    try:
        import hashlib
        from app_paths import app_dir as _app_dir
        base_dir = _app_dir()
        bg_dir = os.path.join(base_dir, "cache", "widget_bgs")
        os.makedirs(bg_dir, exist_ok=True)
        h = hashlib.md5(f"{src_path}|{os.path.getmtime(src_path)}".encode("utf-8", "ignore")).hexdigest()[:14]
        out_path = os.path.join(bg_dir, f"wbg_{h}.jpg")
        if not os.path.exists(out_path):
            with Image.open(src_path) as im:
                rgb = im.convert("RGB")
                rgb.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                rgb.save(out_path, "JPEG", quality=88, optimize=True)
        return out_path
    except Exception:
        return src_path


class ClockWidget(BaseDesktopWidget):
    """Modern frosted glass digital and analog clock desktop widget."""
    _ASPECT_DIMS = {
        "auto": (274, 122),
        "wide": (330, 114),
        "card": (224, 168),
        "square": (196, 196),
    }

    def __init__(self, root, config_mgr, on_open_settings=None):
        super().__init__(root, config_mgr, "clock", default_x=24, default_y=50, default_w=274, default_h=122, on_open_settings=on_open_settings)
        self._last_clock_sig = None

    def _compute_full_dims(self):
        cw = self.get_cfg("custom_w", None)
        ch = self.get_cfg("custom_h", None)
        s = self._get_effective_scale()
        ap = (self.get_cfg("aspect_preset", "auto") or "auto").lower()
        bw, bh = self._ASPECT_DIMS.get(ap, (274, 122))
        style = (self.get_cfg("clock_style", "moderno") or "moderno").lower()
        f_sc = self._get_font_scale()
        sp = self._get_spacing_mult()
        extra_h = 0
        if ch is None and not (ap == "square" and style in ("analogico", "analogico_suizo", "analogico_clasico", "analogico_neon")):
            extra_h = max(0, int((f_sc - 1.0) * 58 * s) + int((sp - 1.0) * 28 * s))
        w = max(self._min_w(), int(cw)) if cw is not None else max(self._min_w(), int(bw * s))
        h = max(int(56 * self.dpi_scale), int(ch)) if ch is not None else max(int(56 * self.dpi_scale), int(bh * s) + extra_h)
        return w, h

    def _set_aspect_preset(self, preset):
        self.set_cfg("aspect_preset", preset)
        self.set_cfg("custom_w", None)
        self.set_cfg("custom_h", None)
        self._update_clock(force_draw=True)

    def _start_update_loop(self):
        self._update_clock()

    def _toggle_24h(self):
        self.set_cfg("format_24h", not self.get_cfg("format_24h", True))
        self._update_clock()

    def _toggle_seconds(self):
        self.set_cfg("show_seconds", not self.get_cfg("show_seconds", False))
        self._update_clock()

    def _set_style(self, style_name):
        self.set_cfg("clock_style", style_name)
        self._update_clock()

    def _set_alignment(self, align):
        self.set_cfg("alignment", align)
        self._update_clock()

    def _pick_color(self, key, title, default_hex):
        from tkinter import colorchooser
        cur = self.get_cfg(key, default_hex)
        res = colorchooser.askcolor(color=cur, title=title, parent=self.window)
        if res and res[1]:
            self.set_cfg(key, res[1])
            self._update_clock()

    def _on_context_menu(self, event):
        from custom_menu import CustomMenu
        menu = CustomMenu(self.window)
        cur_style = self.get_cfg("clock_style", "moderno")
        cur_align = self.get_cfg("alignment", "left")
        menu.add_header("Opciones del Reloj")
        menu.add_command("Formato 24 Horas", self._toggle_24h, checked=bool(self.get_cfg("format_24h", True)))
        menu.add_command("Mostrar Segundero", self._toggle_seconds, checked=bool(self.get_cfg("show_seconds", False)))
        menu.add_separator()
        menu.add_header("Estilo Visual del Reloj")
        for st_id, st_lbl in (
            ("moderno", "Moderno (Cristal)"),
            ("minimal", "Minimalista"),
            ("neon", "Neón Futurista"),
            ("clasico", "Clásico Editorial"),
            ("flip", "Flip Retro"),
            ("led", "LED Estudio"),
            ("analogico", "Analógico Cristal"),
            ("analogico_suizo", "Analógico Suizo (Claro)"),
            ("analogico_clasico", "Analógico Cronógrafo"),
            ("analogico_neon", "Analógico Neón"),
        ):
            menu.add_command(st_lbl, lambda k=st_id: self._set_style(k), checked=(cur_style == st_id))
        sec_ap = menu.add_section("Proporción del Reloj", expanded=False)
        cur_ap = (self.get_cfg("aspect_preset", "auto") or "auto").lower()
        for ap_id, ap_lbl in (
            ("auto", "Automático Estándar"),
            ("wide", "Horizontal (3:1)"),
            ("card", "Tarjeta (4:3)"),
            ("square", "Cuadrado (1:1)"),
        ):
            sec_ap.add_command(ap_lbl, lambda a=ap_id: self._set_aspect_preset(a), checked=(cur_ap == ap_id))
        menu.add_separator()
        menu.add_header("Alineación y Colores")
        menu.add_command("Alinear a la Izquierda", lambda: self._set_alignment("left"), checked=(cur_align == "left"))
        menu.add_command("Centrado", lambda: self._set_alignment("center"), checked=(cur_align == "center"))
        menu.add_command("Alinear a la Derecha", lambda: self._set_alignment("right"), checked=(cur_align == "right"))
        menu.add_command("Color de la Hora...", lambda: self._pick_color("time_color", "Color de la Hora", "#ffffff"))
        menu.add_command("Color de la Fecha...", lambda: self._pick_color("date_color", "Color de la Fecha", "#cbd5e1"))
        menu.add_separator()
        self._append_common_menu_items(menu)
        menu.show(event.x_root, event.y_root)

    def _draw_analog_dial(self, composite, cx, cy, radius, now, style, show_sec, time_rgb, date_rgb):
        """Draws a crisp 2x supersampled analog watch face onto `composite`."""
        d_sz = max(44, int(radius * 2) + 8)
        ss = 2
        hi_s = d_sz * ss
        hi = Image.new("RGBA", (hi_s, hi_s), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hi, "RGBA")
        hcx = hcy = hi_s / 2.0
        r = (radius * ss) - 2

        is_gold = (style == "analogico_clasico")
        is_swiss = (style == "analogico_suizo")
        is_neon_dial = (style == "analogico_neon")
        if is_gold:
            face_fill = (16, 15, 14, 235)
            bezel_col = (212, 175, 55, 225)
            tick_maj = (234, 199, 98, 245)
            tick_min = (180, 145, 55, 130)
            hr_col = (245, 215, 118, 255)
            min_col = (248, 245, 235, 250)
            sec_col = (239, 68, 68, 255)
        elif is_swiss:
            face_fill = (244, 247, 250, 246)
            bezel_col = (100, 116, 139, 235)
            tick_maj = (15, 23, 42, 245)
            tick_min = (100, 116, 139, 155)
            hr_col = (15, 23, 42, 255)
            min_col = (51, 65, 85, 250)
            sec_col = (220, 38, 38, 255)
        elif is_neon_dial:
            face_fill = (8, 12, 22, 242)
            bezel_col = (34, 211, 238, 225)
            tick_maj = (34, 211, 238, 245)
            tick_min = (56, 189, 248, 120)
            hr_col = (34, 211, 238, 255)
            min_col = (224, 242, 254, 250)
            sec_col = (244, 114, 182, 255)
        else:
            face_fill = (14, 21, 36, 225)
            bezel_col = (56, 189, 248, 195)
            tick_maj = (241, 245, 249, 235)
            tick_min = (148, 163, 184, 120)
            hr_col = (*time_rgb, 255)
            min_col = (*date_rgb, 250)
            sec_col = (56, 189, 248, 255)

        hd.ellipse([hcx - r, hcy - r, hcx + r, hcy + r], fill=face_fill, outline=bezel_col, width=max(3, int(r * 0.045)))
        if is_gold:
            r_in = r * 0.90
            hd.ellipse([hcx - r_in, hcy - r_in, hcx + r_in, hcy + r_in], outline=(212, 175, 55, 95), width=2)
        elif is_neon_dial:
            r_arc = r * 0.93
            sweep = max(8.0, (now.tm_sec if show_sec else now.tm_min) * 6.0)
            hd.arc([hcx - r_arc, hcy - r_arc, hcx + r_arc, hcy + r_arc], start=-90, end=-90 + sweep, fill=(34, 211, 238, 245), width=max(4, int(r * 0.055)))

        for k in range(60):
            ang = math.radians(k * 6.0 - 90.0)
            is_q = (k % 15 == 0)
            is_h = (k % 5 == 0)
            if not is_h and r < 52:
                continue
            r2 = r * 0.91
            r1 = r * (0.74 if is_q else (0.80 if is_h else 0.86))
            w_t = max(2, int(r * (0.055 if is_q else (0.035 if is_h else 0.018))))
            col_t = tick_maj if is_h else tick_min
            hd.line(
                [(hcx + math.cos(ang) * r1, hcy + math.sin(ang) * r1),
                 (hcx + math.cos(ang) * r2, hcy + math.sin(ang) * r2)],
                fill=col_t, width=w_t
            )

        if is_gold and r >= 64:
            # Subtle day-of-month window at 3 o'clock
            win_w, win_h = int(r * 0.28), int(r * 0.20)
            wx1, wy1 = int(hcx + r * 0.44 - win_w / 2), int(hcy - win_h / 2)
            hd.rounded_rectangle([wx1, wy1, wx1 + win_w, wy1 + win_h], radius=4, fill=(32, 28, 22, 240), outline=(212, 175, 55, 180), width=2)
            f_win = self._get_styled_font(max(9, int(r * 0.12)), bold_default=True, default_family="Georgia")
            hd.text((wx1 + win_w / 2, hcy), str(now.tm_mday), fill=(248, 240, 215, 255), font=f_win, anchor="mm")

        h_ang = math.radians(((now.tm_hour % 12) + now.tm_min / 60.0) * 30.0 - 90.0)
        m_ang = math.radians((now.tm_min + now.tm_sec / 60.0) * 6.0 - 90.0)
        hd.line([(hcx, hcy), (hcx + math.cos(h_ang) * (r * 0.52), hcy + math.sin(h_ang) * (r * 0.52))], fill=hr_col, width=max(4, int(r * 0.085)))
        hd.line([(hcx, hcy), (hcx + math.cos(m_ang) * (r * 0.74), hcy + math.sin(m_ang) * (r * 0.74))], fill=min_col, width=max(3, int(r * 0.055)))
        if show_sec:
            s_ang = math.radians(now.tm_sec * 6.0 - 90.0)
            hd.line(
                [(hcx - math.cos(s_ang) * (r * 0.16), hcy - math.sin(s_ang) * (r * 0.16)),
                 (hcx + math.cos(s_ang) * (r * 0.80), hcy + math.sin(s_ang) * (r * 0.80))],
                fill=sec_col, width=max(2, int(r * 0.028))
            )
        cap_r = max(4, int(r * 0.075))
        hd.ellipse([hcx - cap_r, hcy - cap_r, hcx + cap_r, hcy + cap_r], fill=sec_col if show_sec else hr_col)

        dial = hi.resize((d_sz, d_sz), Image.Resampling.LANCZOS)
        composite.alpha_composite(dial, (int(round(cx - d_sz / 2.0)), int(round(cy - d_sz / 2.0))))

    def _update_clock(self, force_draw=False):
        if not self.window or not self.canvas:
            return
        if self.update_job:
            try:
                self.root.after_cancel(self.update_job)
            except Exception:
                pass
            self.update_job = None

        now = time.localtime()
        format_24h = self.get_cfg("format_24h", True)
        show_sec = self.get_cfg("show_seconds", False)

        if format_24h:
            time_str = time.strftime("%H:%M:%S" if show_sec else "%H:%M", now)
            ampm_str = ""
        else:
            time_str = time.strftime("%I:%M:%S" if show_sec else "%I:%M", now)
            ampm_str = time.strftime("%p", now)

        dia_sem = DIAS_SEMANA[now.tm_wday]
        mes_nom = MESES[now.tm_mon - 1]
        date_str = f"{dia_sem}, {now.tm_mday} de {mes_nom}"

        if self._is_collapsed_view():
            sig = ("collapsed", time_str, date_str, self._compute_full_dims()[0], self.get_cfg("widget_style"), self.get_cfg("bg_color"), self.get_cfg("bg_image"))
            if force_draw or sig != self._last_clock_sig:
                self._last_clock_sig = sig
                self._render_collapsed_pill(time_str, f"{dia_sem[:3]} {now.tm_mday}", accent_rgb=(56, 189, 248))
            self.update_job = self.root.after(500 if show_sec else 1000, self._update_clock)
            return

        if not self.is_resizing:
            fw, fh = self._compute_full_dims()
            self._set_window_size_live(fw, fh)

        w, h = self.win_w, self.win_h
        style = (self.get_cfg("clock_style", "moderno") or "moderno").lower()
        ap_preset = (self.get_cfg("aspect_preset", "auto") or "auto").lower()
        is_analog = style in ("analogico", "analogico_suizo", "analogico_clasico", "analogico_neon")
        w_style = self.get_cfg("widget_style", None) or (
            "clasico" if style == "analogico_clasico" else ("neon" if style in ("led", "analogico_neon") else style)
        )
        align = self.get_cfg("alignment", "left")
        sig = (
            "full", w, h, time_str, ampm_str, date_str, now.tm_sec if (show_sec or is_analog) else -1, style, w_style, ap_preset, align,
            self.get_cfg("opacity"), self.get_cfg("bg_color"), self.get_cfg("bg_image"),
            self.get_cfg("bg_image_dim"), self.get_cfg("time_color"), self.get_cfg("date_color"),
            self.get_cfg("font_family"), self.get_cfg("font_scale"), self.get_cfg("font_bold"),
            self.get_cfg("font_italic"), self.get_cfg("item_spacing"), self._hover_inside, self.is_resizing
        )
        if not force_draw and not self.is_resizing and sig == self._last_clock_sig:
            self.update_job = self.root.after(500 if show_sec else 1000, self._update_clock)
            return
        self._last_clock_sig = sig

        s = min(w / 274.0, h / 122.0)
        accent_map = (212, 175, 55) if style in ("clasico", "analogico_clasico") else ((16, 185, 129) if style == "led" else (56, 189, 248))
        composite = self._build_card_surface(w, h, style_override=w_style, accent_rgb=accent_map)
        draw = ImageDraw.Draw(composite, "RGBA")

        time_rgb = _hex_to_rgb(self.get_cfg("time_color", "#ffffff"), (255, 255, 255))
        date_rgb = _hex_to_rgb(self.get_cfg("date_color", "#cbd5e1"), (203, 213, 225))
        if self._hover_inside:
            bs = max(16, int(19 * self.dpi_scale))
            self._draw_min_button(composite, draw, w - bs - int(10 * s), int(8 * s), w - int(10 * s), int(8 * s) + bs, is_collapsed=False)
        else:
            self.min_btn_rect = (0, 0, 0, 0)

        sp = self._get_spacing_mult()
        pad_x = int(18 * s)

        if is_analog:
            wide_mode = w >= h * 1.38
            if wide_mode:
                dial_r = max(24, min((h - int(20 * s)) / 2.0, w * 0.21))
                gap_h = max(int(12 * s), int(16 * s * sp))
                if align == "right":
                    dial_cx = w - pad_x - dial_r - int(4 * s)
                    txt_left = pad_x
                    txt_right = dial_cx - dial_r - gap_h
                else:
                    dial_cx = pad_x + dial_r + int(2 * s)
                    txt_left = dial_cx + dial_r + gap_h
                    txt_right = w - pad_x
                self._draw_analog_dial(composite, dial_cx, h / 2.0, dial_r, now, style, show_sec, time_rgb, date_rgb)
                draw = ImageDraw.Draw(composite, "RGBA")
                def_fam = "Georgia" if style == "analogico_clasico" else "Segoe UI"
                f_dig = self._get_styled_font(22 * s, bold_default=True, default_family=def_fam)
                f_dt = self._get_styled_font(11.5 * s, bold_default=False, default_family=def_fam)
                dig_txt = f"{time_str} {ampm_str}".strip()
                max_tw = max(40, txt_right - txt_left)
                while draw.textlength(dig_txt, font=f_dig) > max_tw and f_dig.size > 11:
                    f_dig = self._get_styled_font(f_dig.size - 1, bold_default=True, default_family=def_fam)
                dt_short = f"{dia_sem[:3]} {now.tm_mday} {mes_nom[:3]}"
                while draw.textlength(dt_short, font=f_dt) > max_tw and f_dt.size > 9:
                    f_dt = self._get_styled_font(f_dt.size - 1, bold_default=False, default_family=def_fam)
                tb = draw.textbbox((0, 0), dig_txt, font=f_dig)
                db = draw.textbbox((0, 0), dt_short, font=f_dt)
                th_ink, dh_ink = tb[3] - tb[1], db[3] - db[1]
                vgap = max(int(8 * s), int(14 * s * sp))
                blk_h = th_ink + vgap + dh_ink
                vis_y1 = (h - blk_h) / 2.0
                tx_pos = (txt_left + txt_right) / 2.0 if align == "center" else (txt_right if align == "right" else txt_left)
                anc_top = "ma" if align == "center" else ("ra" if align == "right" else "la")
                draw.text((tx_pos, vis_y1 - tb[1]), dig_txt, fill=(*time_rgb, 255), font=f_dig, anchor=anc_top)
                draw.text((tx_pos, vis_y1 + th_ink + vgap - db[1]), dt_short, fill=(*date_rgb, 235), font=f_dt, anchor=anc_top)
            else:
                vgap_c = max(int(8 * s), int(12 * s * sp))
                f_dt = self._get_styled_font(10.5 * s, bold_default=True, default_family="Georgia" if style == "analogico_clasico" else "Segoe UI")
                dt_short = f"{dia_sem[:3]} {now.tm_mday} {mes_nom[:3]}"
                db = draw.textbbox((0, 0), dt_short, font=f_dt)
                dh_ink = db[3] - db[1]
                avail_dial = h - int(18 * s) - vgap_c - dh_ink
                dial_r = max(20, min(w * 0.38, avail_dial / 2.0))
                dial_cy = int(10 * s) + dial_r
                self._draw_analog_dial(composite, w / 2.0, dial_cy, dial_r, now, style, show_sec, time_rgb, date_rgb)
                draw = ImageDraw.Draw(composite, "RGBA")
                draw.text((w / 2.0, min(h - int(6 * s) - dh_ink, dial_cy + dial_r + vgap_c) - db[1]), dt_short, fill=(*date_rgb, 235), font=f_dt, anchor="ma")
            self._render_to_canvas(composite, w, h)
            self.update_job = self.root.after(500 if show_sec else 1000, self._update_clock)
            return

        if style == "flip":
            hr_s, mn_s = time_str.split(":")[:2]
            sec_s = time_str.split(":")[2] if show_sec and len(time_str.split(":")) > 2 else ""
            f_flip = self._get_styled_font(31 * s, bold_default=True, default_family="Segoe UI")
            f_dt = self._get_styled_font(11 * s, bold_default=False)
            db = draw.textbbox((0, 0), date_str, font=f_dt)
            dh_ink = db[3] - db[1]
            gap_fd = max(int(10 * s), int(16 * s * sp))
            card_w = int(66 * s)
            card_h = max(int(36 * s), min(int(54 * s), int(h - int(18 * s) - gap_fd - dh_ink)))
            gap_c = max(int(8 * s), int(11 * s * sp))
            tot_w = card_w * 2 + gap_c + (int(34 * s) if (sec_s or ampm_str) else 0)
            sx0 = (w - tot_w) // 2 if align == "center" else (w - pad_x - tot_w if align == "right" else pad_x)
            cy0 = max(int(10 * s), int((h - (card_h + gap_fd + dh_ink)) / 2.0))
            for idx_c, val_c in enumerate((hr_s, mn_s)):
                cx1 = sx0 + idx_c * (card_w + gap_c)
                cx2 = cx1 + card_w
                alpha_rounded_rect(composite, [cx1, cy0, cx2, cy0 + card_h], radius=max(6, int(10 * s)), fill=(10, 14, 24, 240), outline=(148, 163, 184, 95), width=1)
                draw = ImageDraw.Draw(composite, "RGBA")
                draw.text(((cx1 + cx2) / 2.0, cy0 + card_h / 2.0), val_c, fill=(*time_rgb, 255), font=f_flip, anchor="mm")
                alpha_line(composite, [(cx1 + 2, cy0 + card_h // 2), (cx2 - 2, cy0 + card_h // 2)], fill=(6, 9, 16, 215), width=max(1, int(1.8 * s)))
            draw = ImageDraw.Draw(composite, "RGBA")
            if sec_s or ampm_str:
                rx_badge = sx0 + card_w * 2 + gap_c + int(16 * s)
                f_sm = self._get_styled_font(13 * s, bold_default=True)
                if sec_s:
                    draw.text((rx_badge, cy0 + card_h * 0.36), f":{sec_s}", fill=(56, 189, 248, 250), font=f_sm, anchor="mm")
                if ampm_str:
                    draw.text((rx_badge, cy0 + card_h * (0.72 if sec_s else 0.5)), ampm_str, fill=(*date_rgb, 235), font=f_sm, anchor="mm")
            dt_x = w / 2.0 if align == "center" else (w - pad_x if align == "right" else pad_x)
            dt_anc = "ma" if align == "center" else ("ra" if align == "right" else "la")
            vis_dy = min(h - int(6 * s) - dh_ink, cy0 + card_h + gap_fd)
            draw.text((dt_x, vis_dy - db[1]), date_str, fill=(*date_rgb, 245), font=f_dt, anchor=dt_anc)
            self._render_to_canvas(composite, w, h)
            self.update_job = self.root.after(500 if show_sec else 1000, self._update_clock)
            return

        if style == "led":
            f_led = self._get_styled_font(34 * s, bold_default=True, default_family="Consolas")
            f_dt = self._get_styled_font(10.5 * s, bold_default=True, default_family="Consolas")
            led_col = time_rgb if self.get_cfg("time_color", "#ffffff") != "#ffffff" else (52, 211, 153)
            disp = f"{time_str} {ampm_str}".strip()
            max_tw = max(60, w - pad_x * 2)
            while draw.textlength(disp, font=f_led) > max_tw and f_led.size > 14:
                f_led = self._get_styled_font(f_led.size - 1, bold_default=True, default_family="Consolas")
            while draw.textlength(date_str.upper(), font=f_dt) > max_tw and f_dt.size > 9:
                f_dt = self._get_styled_font(f_dt.size - 1, bold_default=True, default_family="Consolas")
            tb = draw.textbbox((0, 0), disp, font=f_led)
            db = draw.textbbox((0, 0), date_str.upper(), font=f_dt)
            th_ink, dh_ink = tb[3] - tb[1], db[3] - db[1]
            vgap = max(int(10 * s), int(16 * s * sp))
            gap_bar = max(int(8 * s), int(11 * s * sp))
            blk_h = th_ink + vgap + dh_ink + gap_bar + max(3, int(3.5 * s))
            vis_ty = max(int(10 * s), (h - blk_h) // 2)
            tx_pos = w / 2.0 if align == "center" else (w - pad_x if align == "right" else pad_x)
            anc_top = "ma" if align == "center" else ("ra" if align == "right" else "la")
            draw.text((tx_pos + 1, vis_ty - tb[1] + 1), disp, fill=(*led_col, 85), font=f_led, anchor=anc_top)
            draw.text((tx_pos, vis_ty - tb[1]), disp, fill=(*led_col, 255), font=f_led, anchor=anc_top)
            vis_dy = vis_ty + th_ink + vgap
            draw.text((tx_pos, vis_dy - db[1]), date_str.upper(), fill=(*date_rgb, 225), font=f_dt, anchor=anc_top)
            prog_y = min(h - int(8 * s), vis_dy + dh_ink + gap_bar)
            prog_w = w - pad_x * 2
            alpha_rounded_rect(composite, [pad_x, prog_y, pad_x + prog_w, prog_y + max(3, int(3.5 * s))], radius=2, fill=(255, 255, 255, 26))
            fill_pw = max(4, int(prog_w * (now.tm_sec / 59.0)))
            alpha_rounded_rect(composite, [pad_x, prog_y, pad_x + fill_pw, prog_y + max(3, int(3.5 * s))], radius=2, fill=(*led_col, 225))
            self._render_to_canvas(composite, w, h)
            self.update_job = self.root.after(500 if show_sec else 1000, self._update_clock)
            return

        def_fam = "Georgia" if style == "clasico" else "Segoe UI"
        base_time_px = 34 * s if style == "clasico" else (40 * s if style == "minimal" else 36 * s)
        f_time = self._get_styled_font(base_time_px, bold_default=(style != "minimal"), default_family=def_fam)
        f_sec = self._get_styled_font(14 * s, bold_default=False, default_family=def_fam)
        f_date = self._get_styled_font(12 * s, bold_default=False, default_family=def_fam)

        pad_x = int(20 * s)
        max_avail_w = max(80, w - pad_x * 2)
        while draw.textlength(time_str, font=f_time) > max_avail_w and f_time.size > 14:
            f_time = self._get_styled_font(f_time.size - 1, bold_default=(style != "minimal"), default_family=def_fam)
        while draw.textlength(date_str, font=f_date) > max_avail_w and f_date.size > 9:
            f_date = self._get_styled_font(f_date.size - 1, bold_default=False, default_family=def_fam)

        t_bbox = draw.textbbox((0, 0), time_str, font=f_time)
        t_left, t_top = t_bbox[0], t_bbox[1]
        t_w = t_bbox[2] - t_bbox[0]
        t_h = t_bbox[3] - t_bbox[1]
        d_bbox = draw.textbbox((0, 0), date_str, font=f_date)
        d_left, d_top = d_bbox[0], d_bbox[1]
        d_w = d_bbox[2] - d_bbox[0]
        d_h = d_bbox[3] - d_bbox[1]
        ampm_w = 0
        a_top = 0
        a_h = 0
        if ampm_str:
            a_bbox = draw.textbbox((0, 0), ampm_str, font=f_sec)
            a_top = a_bbox[1]
            a_h = a_bbox[3] - a_bbox[1]
            ampm_w = (a_bbox[2] - a_bbox[0]) + int(6 * s)

        total_time_w = t_w + ampm_w
        if align == "center":
            tx = (w - total_time_w) // 2
            dx = (w - d_w) // 2
        elif align == "right":
            tx = w - pad_x - total_time_w
            dx = w - pad_x - d_w
        else:
            tx = pad_x
            dx = pad_x

        gap_td = max(int(10 * s), int(18 * s * sp))
        gap_bar = max(int(8 * s), int(12 * s * sp))
        has_bar = style in ("moderno", "neon")
        block_h = t_h + gap_td + d_h + (gap_bar + max(2, int(2.5 * s)) if has_bar else 0)
        vis_ty = max(int(10 * s), (h - block_h) // 2)
        vis_dy = vis_ty + t_h + gap_td

        if style == "neon":
            draw.text((tx - t_left + 1, vis_ty - t_top + 1), time_str, fill=(56, 189, 248, 120), font=f_time)
        draw.text((tx - t_left, vis_ty - t_top), time_str, fill=(*time_rgb, 255), font=f_time)

        if ampm_str:
            ampm_rgb = (56, 189, 248) if style == "neon" else date_rgb
            draw.text((tx + t_w + int(6 * s), vis_ty + t_h - a_h - a_top), ampm_str, fill=(*ampm_rgb, 240), font=f_sec)

        draw.text((dx - d_left, vis_dy - d_top), date_str, fill=(*date_rgb, 255), font=f_date)

        bar_w = int(50 * s)
        bar_y = min(h - int(7 * s), vis_dy + d_h + gap_bar)
        if align == "center":
            bx = (w - bar_w) // 2
        elif align == "right":
            bx = w - pad_x - bar_w
        else:
            bx = pad_x

        if style == "moderno":
            draw.line([(bx, bar_y), (bx + bar_w, bar_y)], fill=(59, 130, 246, 220), width=max(2, int(2.5 * s)))
        elif style == "neon":
            draw.line([(bx, bar_y), (bx + bar_w, bar_y)], fill=(34, 211, 238, 245), width=max(2, int(2.5 * s)))
        elif style == "clasico":
            div_y = vis_ty + t_h + max(4, gap_td // 2)
            alpha_line(composite, [(pad_x, div_y), (w - pad_x, div_y)], fill=(212, 175, 55, 140), width=1)

        self._render_to_canvas(composite, w, h)

        interval = 500 if show_sec else 1000
        self.update_job = self.root.after(interval, self._update_clock)


class SysMonitorWidget(BaseDesktopWidget):
    """Live CPU, RAM and Storage desktop monitor widget."""
    def __init__(self, root, config_mgr, on_open_settings=None):
        super().__init__(root, config_mgr, "system_monitor", default_x=24, default_y=175, default_w=270, default_h=175, on_open_settings=on_open_settings)
        self.cpu_sampler = CpuSampler()

    def _start_update_loop(self):
        self._update_stats()

    def _set_interval(self, sec):
        self.set_cfg("update_interval_sec", sec)
        self._update_stats()

    def _on_context_menu(self, event):
        from custom_menu import CustomMenu
        menu = CustomMenu(self.window)
        cur_int = int(self.get_cfg("update_interval_sec", 2))
        menu.add_header("Frecuencia de Muestreo")
        menu.add_command("Cada 1 segundo", lambda: self._set_interval(1), checked=(cur_int == 1))
        menu.add_command("Cada 2 segundos", lambda: self._set_interval(2), checked=(cur_int == 2))
        menu.add_command("Cada 5 segundos", lambda: self._set_interval(5), checked=(cur_int == 5))
        menu.add_separator()
        self._append_common_menu_items(menu)
        menu.show(event.x_root, event.y_root)

    def _update_stats(self):
        if not self.window or not self.canvas:
            return
        if self.update_job:
            try:
                self.root.after_cancel(self.update_job)
            except Exception:
                pass
            self.update_job = None

        cpu_pct = self.cpu_sampler.sample()
        used_ram, tot_ram, ram_pct = get_system_ram()
        if self._is_collapsed_view():
            self._render_collapsed_pill("Rendimiento", f"CPU {cpu_pct}% • RAM {ram_pct}%", accent_rgb=(56, 189, 248))
            interval = max(1000, int(float(self.get_cfg("update_interval_sec", 2)) * 1000))
            self.update_job = self.root.after(interval, self._update_stats)
            return

        if not self.is_resizing:
            fw, fh = self._compute_full_dims()
            self._set_window_size_live(fw, fh)

        w, h = self.win_w, self.win_h
        s = self._get_effective_scale()
        composite = self._build_card_surface(w, h, accent_rgb=(56, 189, 248))
        draw = ImageDraw.Draw(composite, "RGBA")

        sp = self._get_spacing_mult()
        f_title = self._get_styled_font(10 * s, bold_default=True)
        f_label = self._get_styled_font(11 * s, bold_default=False)
        f_val = self._get_styled_font(11 * s, bold_default=False)

        pad_x = int(18 * s)
        draw.text((pad_x, int(13 * s)), "RENDIMIENTO DEL SISTEMA", fill=(148, 163, 184, 255), font=f_title)
        bs = max(17, int(19 * s))
        self._draw_min_button(composite, draw, w - pad_x - bs, int(9 * s), w - pad_x, int(9 * s) + bs, is_collapsed=False)
        t_bb = draw.textbbox((0, 0), "A", font=f_title)
        hdr_div = max(int(30 * s), int(15 * s) + (t_bb[3] - t_bb[1]) + int(5 * s))
        alpha_line(composite, [(pad_x, hdr_div), (w - pad_x, hdr_div)], fill=(255, 255, 255, 25), width=1)

        top_y = hdr_div + int(8 * s)
        avail_h = max(int(68 * s), h - top_y - int(8 * s))
        row_step = max(int(26 * s), min(int(avail_h / 2.65), int((avail_h / 3.0) * sp)))

        self._draw_bar(draw, pad_x, top_y, w - pad_x * 2, "CPU", f"{cpu_pct}%", cpu_pct, (56, 189, 248), f_label, f_val, s, sp)

        ram_text = f"{used_ram:.1f}/{tot_ram:.1f} GB ({ram_pct}%)"
        self._draw_bar(draw, pad_x, top_y + row_step, w - pad_x * 2, "RAM", ram_text, ram_pct, (168, 85, 247), f_label, f_val, s, sp)

        free_c, tot_c, disk_pct = get_disk_usage("C:\\")
        disk_text = f"{free_c:.0f} GB libres ({disk_pct}%)"
        self._draw_bar(draw, pad_x, top_y + row_step * 2, w - pad_x * 2, "Disco C:", disk_text, disk_pct, (52, 211, 153), f_label, f_val, s, sp)

        self._render_to_canvas(composite, w, h)

        interval = max(1000, int(float(self.get_cfg("update_interval_sec", 2)) * 1000))
        self.update_job = self.root.after(interval, self._update_stats)

    def _draw_bar(self, draw, x, y, width, label, val_text, pct, color_rgb, f_lbl, f_val, s, sp=1.0):
        draw.text((x, y), label, fill=(241, 245, 249, 255), font=f_lbl)
        v_bbox = draw.textbbox((0, 0), val_text, font=f_val)
        vw = v_bbox[2] - v_bbox[0]
        lh = max(int(12 * s), v_bbox[3] - v_bbox[1])
        draw.text((x + width - vw, y), val_text, fill=(148, 163, 184, 255), font=f_val)

        bar_y = y + lh + max(int(4 * s), int(6 * s * min(1.5, sp)))
        bar_h = max(4, int(7 * s))
        draw.rounded_rectangle([x, bar_y, x + width, bar_y + bar_h], radius=bar_h // 2, fill=(30, 41, 59, 200))
        fill_w = int(width * (max(0, min(100, pct)) / 100.0))
        if fill_w > 2:
            draw.rounded_rectangle([x, bar_y, x + fill_w, bar_y + bar_h], radius=bar_h // 2, fill=(*color_rgb, 255))


def _apply_win11_rounded_modal(top_win):
    """Applies Windows 11 DWM rounded corners + dark immersive border to a custom Toplevel modal."""
    try:
        top_win.update_idletasks()
        hwnd = user32.GetAncestor(top_win.winfo_id(), 2) or top_win.winfo_id()
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        dark = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
    except Exception:
        pass


class TodoListWidget(BaseDesktopWidget):
    """Interactive floating glass task list with notebook tabs, priority tags, and dark-glass modals."""
    NOTEBOOK_PALETTE = [
        "#3b82f6", "#10b981", "#a855f7", "#f59e0b",
        "#ef4444", "#06b6d4", "#ec4899", "#6366f1",
    ]
    PRIORITY_TAGS = [
        ("", "Sin etiqueta", "#64748b"),
        ("urgente", "Urgente", "#ef4444"),
        ("importante", "Importante", "#f59e0b"),
        ("normal", "Normal", "#3b82f6"),
        ("enfoque", "Enfoque", "#a855f7"),
        ("rapida", "Rápida", "#10b981"),
    ]

    def __init__(self, root, config_mgr, on_open_settings=None):
        super().__init__(root, config_mgr, "todo_list", default_x=24, default_y=360, default_w=320, default_h=260, on_open_settings=on_open_settings)
        self.check_task_rects = []
        self.task_rects = []
        self.edit_task_rects = []
        self.del_task_rects = []
        self.tab_rects = []
        self.add_btn_rect = (0, 0, 0, 0)
        self.gear_btn_rect = (0, 0, 0, 0)
        self.clear_btn_rect = (0, 0, 0, 0)
        self.filter_done_rect = (0, 0, 0, 0)
        self.quick_tab_btn_rect = (0, 0, 0, 0)
        self.scroll_up_rect = (0, 0, 0, 0)
        self.scroll_dn_rect = (0, 0, 0, 0)
        self.hovered_zone = None
        self.scroll_offset = 0
        self._active_modal = None
        self._task_map = {}
        self._tip_win = None
        self._tip_hwnd = None
        self._tip_job = None
        self._tip_tid = None
        self._tip_photo = None
        # Task drag-and-drop reorder state & variable-height row spans [(y1, y2, t_id), ...]
        self._drag_task_id = None
        self._drag_task_start_y = 0
        self._drag_task_cur_y = 0
        self._drag_task_active = False
        self._drag_task_target_slot = None
        self._visible_row_spans = []

    def _max_h(self):
        _mx1, my1, _mx2, my2, _wx1, wy1, _wx2, _wy2 = self._get_screen_and_work_bounds()
        top_limit = wy1 if wy1 > my1 else my1
        return max(720, int(my2 - top_limit))

    def show(self):
        self._ensure_notebooks()
        super().show()
        if self.canvas:
            self.canvas.bind("<Button-1>", self._on_canvas_click)
            self.canvas.bind("<B1-Motion>", self._on_canvas_b1_motion)
            self.canvas.bind("<ButtonRelease-1>", self._on_canvas_b1_release)
            self.canvas.bind("<Motion>", self._on_motion, add="+")
            self.canvas.bind("<Leave>", self._on_leave, add="+")
            self.canvas.bind("<MouseWheel>", self._on_mousewheel)

    def hide(self, animate=True):
        self._hide_task_tooltip()
        self._close_modal()
        super().hide(animate=animate)

    def _close_modal(self):
        if self._active_modal is not None:
            try:
                if self._active_modal.winfo_exists():
                    modal = self._active_modal
                    animate_window(modal, showing=False, on_done=modal.destroy)
            except Exception:
                pass
            self._active_modal = None

    def _ensure_notebooks(self):
        notebooks = self.get_cfg("notebooks", None)
        if not isinstance(notebooks, list) or len(notebooks) == 0:
            legacy_tasks = list(self.get_cfg("tasks", []) or [])
            notebooks = [
                {"id": "nb_general", "name": "General", "color": "#3b82f6", "tasks": legacy_tasks},
                {"id": "nb_trabajo", "name": "Trabajo", "color": "#10b981", "tasks": []},
                {"id": "nb_outlook", "name": "Outlook", "color": "#0ea5e9", "tasks": []},
                {"id": "nb_ideas", "name": "Ideas", "color": "#a855f7", "tasks": []},
            ]
            self.set_cfg("notebooks", notebooks)
            self.set_cfg("active_notebook_id", "nb_general")
        active_id = self.get_cfg("active_notebook_id", "")
        if not any(nb.get("id") == active_id for nb in notebooks):
            active_id = notebooks[0].get("id", "nb_general")
            self.set_cfg("active_notebook_id", active_id)
        return notebooks, active_id

    def _get_active_notebook(self):
        notebooks, active_id = self._ensure_notebooks()
        for nb in notebooks:
            if nb.get("id") == active_id:
                nb.setdefault("tasks", [])
                return notebooks, nb
        notebooks[0].setdefault("tasks", [])
        return notebooks, notebooks[0]

    def _save_notebooks(self, notebooks, active_id=None, baseline=None):
        if baseline is None:
            baseline = self.config_mgr._baseline.get('widgets', {}).get(self.widget_id, {}).get('notebooks', [])
        todo_store.commit_notebooks(notebooks, baseline, active_id, self.config_mgr.config_path)
        self.config_mgr.load()

    def _start_update_loop(self):
        self._render()

    def _on_mousewheel(self, event):
        _nbs, nb = self._get_active_notebook()
        tasks = nb.get("tasks", [])
        if not tasks:
            return
        step = -1 if event.delta > 0 else 1
        new_off = max(0, min(max(0, len(tasks) - 1), self.scroll_offset + step))
        if new_off != self.scroll_offset:
            self.scroll_offset = new_off
            self._render()

    def _hit_zone(self, x, y):
        if self._hit_min_button(x, y):
            return ("btn", "min")
        ab = self.add_btn_rect
        if ab[0] <= x <= ab[2] and ab[1] <= y <= ab[3]:
            return ("btn", "add")
        gb = self.gear_btn_rect
        if gb[0] <= x <= gb[2] and gb[1] <= y <= gb[3]:
            return ("btn", "gear")
        fb = self.filter_done_rect
        if fb[0] <= x <= fb[2] and fb[1] <= y <= fb[3]:
            return ("btn", "toggle_done_view")
        cb = self.clear_btn_rect
        if cb[0] <= x <= cb[2] and cb[1] <= y <= cb[3]:
            return ("btn", "clear")
        qb = self.quick_tab_btn_rect
        if qb[0] <= x <= qb[2] and qb[1] <= y <= qb[3]:
            return ("btn", "new_tab")
        su = self.scroll_up_rect
        if su[0] <= x <= su[2] and su[1] <= y <= su[3]:
            return ("btn", "scroll_up")
        sd = self.scroll_dn_rect
        if sd[0] <= x <= sd[2] and sd[1] <= y <= sd[3]:
            return ("btn", "scroll_dn")
        for (x1, y1, x2, y2, nb_id) in self.tab_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return ("tab", nb_id)
        for (x1, y1, x2, y2, t_id) in self.del_task_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return ("del_task", t_id)
        for (x1, y1, x2, y2, t_id) in self.edit_task_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return ("edit_task", t_id)
        for (x1, y1, x2, y2, t_id) in self.check_task_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return ("check_task", t_id)
        for (x1, y1, x2, y2, t_id) in self.task_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return ("task", t_id)
        return None

    def _hide_task_tooltip(self):
        if self._tip_job:
            try:
                self.root.after_cancel(self._tip_job)
            except Exception:
                pass
            self._tip_job = None
        self._tip_tid = None
        if self._tip_hwnd:
            release_layered_surface(self._tip_hwnd)
            self._tip_hwnd = None
        if self._tip_win:
            try:
                if self._tip_win.winfo_exists():
                    self._tip_win.destroy()
            except Exception:
                pass
            self._tip_win = None
        self._tip_photo = None

    def _show_task_tooltip(self, t_id, rx, ry):
        self._tip_job = None
        if not self.window or not self.window.winfo_exists() or self._is_collapsed_view():
            return
        task = self._task_map.get(t_id)
        if not task:
            return
        full_txt = (task.get("text") or "").strip()
        if not full_txt:
            return

        _, active_nb = self._get_active_notebook()
        nb_rgb = _hex_to_rgb(active_nb.get("color", "#3b82f6"), (59, 130, 246))
        tag_lbl = (task.get("tag_label") or "").strip()
        tag_col = _hex_to_rgb(task.get("tag_color", "#3b82f6"), nb_rgb) if tag_lbl else nb_rgb
        sender = (task.get("from") or "").strip()
        status_str = "Completada" if task.get("done") else "Pendiente"

        max_w = int(340 * self.dpi_scale)
        pad_x = int(14 * self.dpi_scale)
        pad_y = int(10 * self.dpi_scale)
        f_body = resolve_styled_font("Segoe UI", max(10, int(11 * self.dpi_scale)), bold=False)
        f_meta = resolve_styled_font("Segoe UI", max(9, int(9.5 * self.dpi_scale)), bold=True)

        tmp = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        td = ImageDraw.Draw(tmp, "RGBA")
        # Word-wrap full task text cleanly
        wrap_w = max_w - pad_x * 2 - 8
        words = full_txt.split()
        lines = []
        cur = ""
        for w_tok in words:
            cand = (cur + " " + w_tok).strip() if cur else w_tok
            if td.textlength(cand, font=f_body) <= wrap_w:
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                while td.textlength(w_tok, font=f_body) > wrap_w and len(w_tok) > 12:
                    cut = max(8, int(len(w_tok) * wrap_w / max(1, td.textlength(w_tok, font=f_body))))
                    lines.append(w_tok[:cut])
                    w_tok = w_tok[cut:]
                cur = w_tok
        if cur:
            lines.append(cur)
        if not lines:
            lines = [full_txt]

        max_line_w = max(td.textlength(ln, font=f_body) for ln in lines)
        meta_parts = [active_nb.get("name", "To-Do"), status_str]
        if tag_lbl:
            meta_parts.append(tag_lbl)
        if sender:
            meta_parts.append(sender[:28])
        meta_line = "  •  ".join(meta_parts)
        meta_w = td.textlength(meta_line, font=f_meta)

        box_w = min(max_w, max(int(160 * self.dpi_scale), int(max(max_line_w, meta_w) + pad_x * 2 + 12)))
        line_h = max(16, int(17 * self.dpi_scale))
        meta_h = max(15, int(16 * self.dpi_scale))
        box_h = pad_y * 2 + meta_h + 6 + len(lines) * line_h

        img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([1, 1, box_w - 2, box_h - 2], radius=12, fill=(14, 19, 30, 246), outline=(*tag_col, 190), width=1)
        # Left comment accent bar
        d.rounded_rectangle([5, 8, 9, box_h - 8], radius=2, fill=(*tag_col, 240))
        d.text((pad_x + 6, pad_y), meta_line, fill=(*tag_col, 245), font=f_meta)
        y_ln = pad_y + meta_h + 4
        for ln in lines:
            d.text((pad_x + 6, y_ln), ln, fill=(248, 250, 252, 255), font=f_body)
            y_ln += line_h

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        wx = self.window.winfo_x()
        # Place comment bubble to left of widget if near right edge, otherwise right/below cursor
        if wx + self.win_w + box_w + 14 <= sw:
            tx = wx + self.win_w + 8
        elif wx - box_w - 8 >= 8:
            tx = wx - box_w - 8
        else:
            tx = max(8, min(sw - box_w - 8, int(rx - box_w // 2)))
        ty = max(36, min(sh - box_h - 14, int(ry - 16)))

        self._hide_task_tooltip()
        self._tip_tid = t_id
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.config(bg="#010203")
        hwnd = set_tool_window(win, owner_hwnd=None, layered=True, no_activate=True)
        win.geometry(f"{box_w}x{box_h}+{tx}+{ty}")
        win.update_idletasks()
        self._tip_win = win
        self._tip_hwnd = hwnd
        if not update_layered_pil(hwnd, tx, ty, img):
            c = tk.Canvas(win, width=box_w, height=box_h, bg="#010203", highlightthickness=0, bd=0)
            c.pack()
            self._tip_photo = ImageTk.PhotoImage(flatten_pil_for_colorkey(img, (1, 2, 3)))
            c.create_image(0, 0, anchor=tk.NW, image=self._tip_photo)

    def _on_motion(self, event):
        if self._drag_task_active:
            return
        hz = self._hit_zone(event.x, event.y)
        if hz != self.hovered_zone:
            self.hovered_zone = hz
            self._render()
        if not self._is_in_resize_zone(event.x, event.y):
            if hz and hz[0] in ("btn", "tab", "check_task", "edit_task", "del_task", "task"):
                self._set_cursor("hand2")
            else:
                self._set_cursor("")
        if hz and hz[0] in ("task", "check_task", "edit_task", "del_task"):
            t_id = hz[1]
            if t_id != self._tip_tid:
                self._hide_task_tooltip()
                self._tip_tid = t_id
                self._tip_job = self.root.after(220, lambda tid=t_id, rx=event.x_root, ry=event.y_root: self._show_task_tooltip(tid, rx, ry))
        else:
            if self._tip_tid is not None or self._tip_job is not None:
                self._hide_task_tooltip()

    def _on_leave(self, event):
        if self._drag_task_active:
            return
        self._hide_task_tooltip()
        if self.hovered_zone is not None:
            self.hovered_zone = None
            self._render()

    def _on_canvas_click(self, event):
        self._hide_task_tooltip()
        self._drag_task_id = None
        self._drag_task_active = False
        self._drag_task_target_slot = None
        if self._is_in_resize_zone(event.x, event.y):
            self._on_drag_start(event)
            return
        hz = self._hit_zone(event.x, event.y)
        if hz is not None:
            kind, val = hz
            if kind == "btn":
                if val == "min":
                    self._toggle_minimized()
                elif val == "add":
                    self._open_new_task_modal()
                elif val == "gear":
                    self._open_notebooks_modal()
                elif val == "toggle_done_view":
                    self.scroll_offset = 0
                    self.set_cfg("show_completed", not bool(self.get_cfg("show_completed", False)))
                    self._render()
                elif val == "clear":
                    self._clear_completed()
                elif val == "new_tab":
                    self._open_notebooks_modal(focus_new=True)
                elif val == "scroll_up":
                    self.scroll_offset = max(0, self.scroll_offset - 1)
                    self._render()
                elif val == "scroll_dn":
                    self.scroll_offset += 1
                    self._render()
                return
            if kind == "tab":
                if val != self.get_cfg("active_notebook_id", ""):
                    self.scroll_offset = 0
                    notebooks, _ = self._ensure_notebooks()
                    self._save_notebooks(notebooks, active_id=val)
                    self._render()
                return
            if kind == "check_task":
                # Clicking strictly the checkbox completes / uncompletes the task
                self._toggle_task(val)
                return
            if kind == "edit_task":
                self._open_edit_task_modal(val)
                return
            if kind == "del_task":
                self._delete_task(val)
                return
            if kind == "task":
                # Clicking task text opens Edit on release; dragging vertically > 6px reorders the task
                self._drag_task_id = val
                self._drag_task_start_y = event.y
                self._drag_task_cur_y = event.y
                self._drag_task_active = False
                self._drag_task_target_slot = None
                return
        self._on_drag_start(event)

    def _on_canvas_b1_motion(self, event):
        if self._drag_task_id is not None:
            self._drag_task_cur_y = event.y
            if not self._drag_task_active and abs(event.y - self._drag_task_start_y) > 6:
                self._drag_task_active = True
                self._hide_task_tooltip()
                self._set_cursor("sb_v_double_arrow")
            if self._drag_task_active and self._visible_row_spans:
                # Find insertion slot across variable-height (1-3 line) rows
                slot = len(self._visible_row_spans)
                for idx_s, (ry1, ry2, _tid) in enumerate(self._visible_row_spans):
                    if event.y < (ry1 + ry2) * 0.5:
                        slot = idx_s
                        break
                if slot != self._drag_task_target_slot:
                    self._drag_task_target_slot = slot
                    self._render()
            return
        self._on_drag_move(event)

    def _on_canvas_b1_release(self, event):
        if self._drag_task_id is not None:
            t_id = self._drag_task_id
            was_drag = self._drag_task_active
            tgt_slot = self._drag_task_target_slot
            self._drag_task_id = None
            self._drag_task_active = False
            self._drag_task_target_slot = None
            self._set_cursor("")
            if was_drag:
                if tgt_slot is not None:
                    self._reorder_task_to_visible_slot(t_id, tgt_slot)
                else:
                    self._render()
                return
            # Single click on task text opens Edit modal directly!
            self._open_edit_task_modal(t_id)
            return
        self._on_drag_end(event)

    def _reorder_task_to_visible_slot(self, t_id, target_visible_slot):
        notebooks, nb = self._get_active_notebook()
        all_tasks = nb.get("tasks", [])
        show_completed = bool(self.get_cfg("show_completed", False))
        filtered_indices = [i for i, t in enumerate(all_tasks) if bool(t.get("done", False)) == show_completed]
        src_all_idx = next((i for i, t in enumerate(all_tasks) if t.get("id") == t_id), None)
        if src_all_idx is None or src_all_idx not in filtered_indices:
            self._render()
            return
        src_f_pos = filtered_indices.index(src_all_idx)
        tgt_f_pos = max(0, min(len(filtered_indices), self.scroll_offset + int(target_visible_slot)))
        if tgt_f_pos == src_f_pos or tgt_f_pos == src_f_pos + 1:
            self._render()
            return
        moved = all_tasks.pop(src_all_idx)
        rem_filtered = [i for i, t in enumerate(all_tasks) if bool(t.get("done", False)) == show_completed]
        if src_f_pos < tgt_f_pos:
            tgt_f_pos -= 1
        tgt_f_pos = max(0, min(len(rem_filtered), tgt_f_pos))
        if not rem_filtered:
            all_tasks.append(moved)
        elif tgt_f_pos >= len(rem_filtered):
            all_tasks.insert(rem_filtered[-1] + 1, moved)
        else:
            all_tasks.insert(rem_filtered[tgt_f_pos], moved)
        self._save_notebooks(notebooks, nb["id"])
        self._render()

    def _move_task_step(self, t_id, delta):
        notebooks, nb = self._get_active_notebook()
        all_tasks = nb.get("tasks", [])
        show_completed = bool(self.get_cfg("show_completed", False))
        filtered_indices = [i for i, t in enumerate(all_tasks) if bool(t.get("done", False)) == show_completed]
        src_all_idx = next((i for i, t in enumerate(all_tasks) if t.get("id") == t_id), None)
        if src_all_idx is None or src_all_idx not in filtered_indices:
            return
        src_f_pos = filtered_indices.index(src_all_idx)
        dst_f_pos = src_f_pos + delta
        if not (0 <= dst_f_pos < len(filtered_indices)):
            return
        dst_all_idx = filtered_indices[dst_f_pos]
        all_tasks[src_all_idx], all_tasks[dst_all_idx] = all_tasks[dst_all_idx], all_tasks[src_all_idx]
        self._save_notebooks(notebooks, nb["id"])
        self._render()

    def _open_edit_task_modal(self, t_id):
        notebooks, active_nb = self._get_active_notebook()
        for t in active_nb.get("tasks", []):
            if t.get("id") == t_id:
                self._open_new_task_modal(default_nb_id=active_nb["id"], edit_task=t, edit_nb_id=active_nb["id"])
                return

    def _toggle_task(self, t_id):
        notebooks, nb = self._get_active_notebook()
        for t in nb.get("tasks", []):
            if t.get("id") == t_id:
                t["done"] = not t.get("done", False)
                break
        self._save_notebooks(notebooks, nb["id"])
        self._render()

    def _delete_task(self, t_id):
        notebooks, nb = self._get_active_notebook()
        nb["tasks"] = [t for t in nb.get("tasks", []) if t.get("id") != t_id]
        self._save_notebooks(notebooks, nb["id"])
        self._render()

    def _clear_completed(self):
        notebooks, nb = self._get_active_notebook()
        nb["tasks"] = [t for t in nb.get("tasks", []) if not t.get("done", False)]
        self.scroll_offset = 0
        self._save_notebooks(notebooks, nb["id"])
        self._render()

    def _position_modal(self, modal, mw, mh):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        if self.window and self.window.winfo_exists():
            wx = self.window.winfo_rootx()
            wy = self.window.winfo_rooty()
            mx = wx + self.win_w + 14
            if mx + mw > sw - 16:
                mx = wx - mw - 14
            if mx < 16:
                mx = (sw - mw) // 2
            my = max(36, min(sh - mh - 48, wy))
        else:
            mx = (sw - mw) // 2
            my = (sh - mh) // 2
        modal.geometry(f"{mw}x{mh}+{int(mx)}+{int(my)}")

    @staticmethod
    def _make_rounded_btn(parent, text, command, bg, fg="#ffffff", hover_bg=None, border_col="", radius=12, pad_x=14, pad_y=7, font_spec=("Segoe UI", 9, "bold"), parent_bg="#111622"):
        """Creates a sleek canvas-drawn rounded pill button (zero sharp rectangular Win32 borders)."""
        import tkinter.font as tkfont
        f_obj = tkfont.Font(family=font_spec[0], size=font_spec[1], weight=font_spec[2] if len(font_spec) > 2 else "normal")
        tw = f_obj.measure(text)
        th = f_obj.metrics("linespace")
        bw = max(48, tw + pad_x * 2)
        bh = max(28, th + pad_y * 2)
        c = tk.Canvas(parent, width=bw, height=bh, bg=parent_bg, highlightthickness=0, bd=0, cursor="hand2")
        state = {"bg": bg, "fg": fg, "hover_bg": hover_bg or bg, "border": border_col, "hov": False, "text": text}

        def _round_poly(x1, y1, x2, y2, r):
            r = min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0)
            return [
                x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
                x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
                x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
            ]

        def _draw():
            c.delete("all")
            fill_c = state["hover_bg"] if state["hov"] else state["bg"]
            out_c = state["border"] or fill_c
            pts = _round_poly(1, 1, bw - 2, bh - 2, radius)
            c.create_polygon(pts, smooth=True, splinesteps=18, fill=fill_c, outline=out_c, width=1)
            c.create_text(bw / 2.0, bh / 2.0, text=state["text"], fill=state["fg"], font=font_spec)

        def _set_style(new_bg=None, new_fg=None, new_hover=None, new_border=None, new_text=None):
            if new_bg is not None:
                state["bg"] = new_bg
            if new_fg is not None:
                state["fg"] = new_fg
            if new_hover is not None:
                state["hover_bg"] = new_hover
            if new_border is not None:
                state["border"] = new_border
            if new_text is not None:
                state["text"] = new_text
            _draw()

        c.set_pill_style = _set_style
        c.bind("<Enter>", lambda _e: (state.__setitem__("hov", True), _draw()))
        c.bind("<Leave>", lambda _e: (state.__setitem__("hov", False), _draw()))
        c.bind("<ButtonRelease-1>", lambda _e: command() if callable(command) else None)
        _draw()
        return c

    def _open_new_task_modal(self, default_nb_id=None, edit_task=None, edit_nb_id=None):
        self._close_modal()
        notebooks, active_nb = self._get_active_notebook()
        baseline = copy.deepcopy(notebooks)
        notebooks = copy.deepcopy(notebooks)
        is_edit = edit_task is not None
        sel_nb_id = [edit_nb_id or default_nb_id or active_nb["id"]]
        sel_tag = [(edit_task.get("tag") or "") if is_edit else ""]
        sel_tag_color = [(edit_task.get("tag_color") or "") if is_edit else ""]

        modal = tk.Toplevel(self.root)
        self._active_modal = modal
        modal.overrideredirect(True)
        modal.attributes("-topmost", True)
        modal.configure(bg="#243044")
        mw, mh = 560, 505
        min_w, min_h = 460, 440
        self._position_modal(modal, mw, mh)

        outer = tk.Frame(modal, bg="#111622", highlightthickness=1, highlightbackground="#2b3952")
        outer.pack(fill=tk.BOTH, expand=True)

        # Draggable header bar
        hdr = tk.Frame(outer, bg="#151c2c", height=48)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)

        drag_state = {"x": 0, "y": 0}
        def _start_m(e):
            drag_state["x"], drag_state["y"] = e.x_root - modal.winfo_x(), e.y_root - modal.winfo_y()
        def _move_m(e):
            nx = e.x_root - drag_state["x"]
            ny = e.y_root - drag_state["y"]
            modal.geometry(f"+{nx}+{ny}")

        hdr.bind("<Button-1>", _start_m)
        hdr.bind("<B1-Motion>", _move_m)

        init_nb = next((nb for nb in notebooks if nb.get("id") == sel_nb_id[0]), active_nb)
        dot_lbl = tk.Label(hdr, text="●", fg=init_nb.get("color", "#3b82f6"), bg="#151c2c", font=("Segoe UI", 13, "bold"))
        dot_lbl.pack(side=tk.LEFT, padx=(18, 8))
        title_txt = "Editar Tarea" if is_edit else "Nueva Tarea"
        title_lbl = tk.Label(hdr, text=title_txt, fg="#f8fafc", bg="#151c2c", font=("Segoe UI", 13, "bold"))
        title_lbl.pack(side=tk.LEFT)
        title_lbl.bind("<Button-1>", _start_m)
        title_lbl.bind("<B1-Motion>", _move_m)

        close_btn = self._make_rounded_btn(
            hdr, "×", self._close_modal,
            bg="#1e293b", fg="#cbd5e1", hover_bg="#ef4444", border_col="#334155",
            radius=9, pad_x=11, pad_y=3, font_spec=("Segoe UI", 12, "bold"), parent_bg="#151c2c"
        )
        close_btn.pack(side=tk.RIGHT, padx=(0, 12), pady=8)

        body = tk.Frame(outer, bg="#111622", padx=20, pady=14)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(body, text="Descripción de la tarea  (Enter para guardar, Shift+Enter para salto de línea)", fg="#cbd5e1", bg="#111622", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        entry_wrap = tk.Frame(body, bg="#0b0e16", highlightthickness=1, highlightbackground="#3b82f6", padx=12, pady=9)
        entry_wrap.pack(fill=tk.BOTH, expand=True, pady=(6, 14))
        text_box = tk.Text(
            entry_wrap, bg="#0b0e16", fg="#f8fafc", height=3, wrap=tk.WORD,
            insertbackground="#34d399", relief=tk.FLAT, bd=0, font=("Segoe UI", 11)
        )
        text_box.pack(fill=tk.BOTH, expand=True)
        if is_edit and edit_task.get("text"):
            text_box.insert("1.0", edit_task.get("text", ""))

        # Notebook selector rounded pills (wrapping grid so none ever clip)
        nb_hdr = tk.Frame(body, bg="#111622")
        nb_hdr.pack(fill=tk.X)
        tk.Label(nb_hdr, text="Cuaderno / Pestaña", fg="#cbd5e1", bg="#111622", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        manage_lnk = tk.Label(nb_hdr, text="Editar pestañas", fg="#60a5fa", bg="#111622", font=("Segoe UI", 9, "bold"), cursor="hand2")
        manage_lnk.pack(side=tk.RIGHT)
        manage_lnk.bind("<Button-1>", lambda _e: self._open_notebooks_modal())

        nb_grid = tk.Frame(body, bg="#111622")
        nb_grid.pack(fill=tk.X, pady=(6, 12))
        nb_btns = {}

        def _refresh_nb_pills():
            for nb in notebooks:
                nid = nb["id"]
                b = nb_btns.get(nid)
                if not b:
                    continue
                is_sel = (nid == sel_nb_id[0])
                col = nb.get("color", "#3b82f6")
                b.set_pill_style(
                    new_bg=col if is_sel else "#1a2234",
                    new_fg="#ffffff" if is_sel else "#cbd5e1",
                    new_hover=col if is_sel else "#25324a",
                    new_border=col if is_sel else "#30405a",
                )
                if is_sel:
                    dot_lbl.configure(fg=col)

        nb_row_cur = tk.Frame(nb_grid, bg="#111622")
        nb_row_cur.pack(fill=tk.X, pady=2)
        for idx_nb, nb in enumerate(notebooks[:8]):
            if idx_nb > 0 and idx_nb % 4 == 0:
                nb_row_cur = tk.Frame(nb_grid, bg="#111622")
                nb_row_cur.pack(fill=tk.X, pady=2)
            nid = nb["id"]
            col = nb.get("color", "#3b82f6")
            pill = self._make_rounded_btn(
                nb_row_cur, f"● {nb.get('name', 'Lista')[:14]}",
                command=lambda k=nid: (sel_nb_id.__setitem__(0, k), _refresh_nb_pills()),
                bg="#1a2234", fg="#cbd5e1", hover_bg="#25324a", border_col=col,
                radius=12, pad_x=12, pad_y=5, font_spec=("Segoe UI", 9, "bold"), parent_bg="#111622"
            )
            pill.pack(side=tk.LEFT, padx=(0, 7))
            nb_btns[nid] = pill
        _refresh_nb_pills()

        # Optional Priority / Color Tag selector + Custom Color Picker
        tk.Label(body, text="Etiqueta y color personalizado (opcional)", fg="#cbd5e1", bg="#111622", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tag_grid = tk.Frame(body, bg="#111622")
        tag_grid.pack(fill=tk.X, pady=(6, 6))
        tag_btns = {}

        tag_color_swatches = [
            "#ef4444", "#f97316", "#f59e0b", "#10b981", "#06b6d4",
            "#3b82f6", "#6366f1", "#a855f7", "#ec4899", "#e11d48",
        ]
        swatch_canvases = {}

        def _active_tag_color():
            if sel_tag_color[0]:
                return sel_tag_color[0]
            for tk_id, _tl, tc in self.PRIORITY_TAGS:
                if tk_id == sel_tag[0] and tk_id:
                    return tc
            return ""

        def _refresh_tag_pills():
            cur_col = _active_tag_color()
            for t_key, _t_lbl, t_col in self.PRIORITY_TAGS:
                b = tag_btns.get(t_key)
                if not b:
                    continue
                is_sel = (t_key == sel_tag[0]) or (t_key == "" and sel_tag[0] == "" and not cur_col)
                fill_c = (cur_col or t_col) if (is_sel and t_key) else (t_col if is_sel else "#182030")
                b.set_pill_style(
                    new_bg=fill_c,
                    new_fg="#ffffff" if is_sel else "#cbd5e1",
                    new_hover=fill_c if is_sel else "#243047",
                    new_border=(cur_col or t_col) if is_sel else "#2c3a52",
                )
            for sw_hex, sw_c in swatch_canvases.items():
                sw_c.delete("all")
                is_sw_sel = (cur_col.lower() == sw_hex.lower())
                if is_sw_sel:
                    sw_c.create_oval(2, 2, 22, 22, fill=sw_hex, outline="#ffffff", width=2)
                else:
                    sw_c.create_oval(4, 4, 20, 20, fill=sw_hex, outline="#334155", width=1)
            if cur_col:
                color_preview.configure(bg=cur_col)
            else:
                color_preview.configure(bg="#263246")

        def _select_tag(k):
            sel_tag[0] = k
            if not k:
                sel_tag_color[0] = ""
            else:
                for tk_id, _tl, tc in self.PRIORITY_TAGS:
                    if tk_id == k:
                        sel_tag_color[0] = tc
                        break
            _refresh_tag_pills()

        def _select_tag_color(hex_c):
            sel_tag_color[0] = hex_c
            if not sel_tag[0]:
                sel_tag[0] = "custom"
            _refresh_tag_pills()

        def _pick_custom_tag_color():
            from tkinter import colorchooser
            init_c = _active_tag_color() or "#3b82f6"
            res = colorchooser.askcolor(color=init_c, title="Color de la etiqueta", parent=modal)
            if res and res[1]:
                _select_tag_color(res[1])

        tag_row_cur = tk.Frame(tag_grid, bg="#111622")
        tag_row_cur.pack(fill=tk.X, pady=2)
        for idx_t, (t_key, t_lbl, t_col) in enumerate(self.PRIORITY_TAGS):
            if idx_t > 0 and idx_t % 3 == 0:
                tag_row_cur = tk.Frame(tag_grid, bg="#111622")
                tag_row_cur.pack(fill=tk.X, pady=2)
            txt = "Sin etiqueta" if not t_key else f"● {t_lbl}"
            tp = self._make_rounded_btn(
                tag_row_cur, txt,
                command=lambda k=t_key: _select_tag(k),
                bg="#182030", fg="#cbd5e1", hover_bg="#243047", border_col=t_col,
                radius=11, pad_x=12, pad_y=5, font_spec=("Segoe UI", 9, "bold"), parent_bg="#111622"
            )
            tp.pack(side=tk.LEFT, padx=(0, 7))
            tag_btns[t_key] = tp

        # Color palette row for tags (10 preset swatches + custom color wheel button)
        col_row = tk.Frame(body, bg="#111622")
        col_row.pack(fill=tk.X, pady=(4, 6))
        tk.Label(col_row, text="Color:", fg="#94a3b8", bg="#111622", font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        for sw_hex in tag_color_swatches:
            sw_c = tk.Canvas(col_row, width=24, height=24, bg="#111622", highlightthickness=0, bd=0, cursor="hand2")
            sw_c.pack(side=tk.LEFT, padx=2)
            sw_c.bind("<ButtonRelease-1>", lambda _e, hx=sw_hex: _select_tag_color(hx))
            swatch_canvases[sw_hex] = sw_c

        color_preview = tk.Frame(col_row, width=14, height=14, bg="#263246", highlightthickness=1, highlightbackground="#64748b")
        color_preview.pack(side=tk.LEFT, padx=(8, 6))
        pick_col_btn = self._make_rounded_btn(
            col_row, "Más colores...", _pick_custom_tag_color,
            bg="#1e293b", fg="#e2e8f0", hover_bg="#334155", border_col="#475569",
            radius=10, pad_x=10, pad_y=4, font_spec=("Segoe UI", 8, "bold"), parent_bg="#111622"
        )
        pick_col_btn.pack(side=tk.LEFT)
        _refresh_tag_pills()

        # Footer action buttons + bottom-right resize grip
        foot = tk.Frame(outer, bg="#151c2c", padx=18, pady=12)
        foot.pack(fill=tk.X, side=tk.BOTTOM)

        def _submit(_event=None):
            txt = text_box.get("1.0", "end-1c").strip()
            if not txt:
                text_box.focus_set()
                return "break"
            tag_color = _active_tag_color()
            tag_label = ""
            for tk_id, tl, _tc in self.PRIORITY_TAGS:
                if tk_id == sel_tag[0] and tk_id:
                    tag_label = tl
                    break
            if not tag_label and tag_color:
                tag_label = "Etiqueta"
            target_nb_id = sel_nb_id[0]
            if is_edit:
                orig_tid = edit_task.get("id")
                orig_nb_id = edit_nb_id or active_nb["id"]
                if target_nb_id == orig_nb_id:
                    for nb in notebooks:
                        if nb.get("id") == orig_nb_id:
                            for t in nb.get("tasks", []):
                                if t.get("id") == orig_tid:
                                    t["text"] = txt
                                    t["tag"] = sel_tag[0]
                                    t["tag_label"] = tag_label
                                    t["tag_color"] = tag_color
                                    break
                            break
                else:
                    moved_obj = None
                    for nb in notebooks:
                        if nb.get("id") == orig_nb_id:
                            for idx_k, t in enumerate(nb.get("tasks", [])):
                                if t.get("id") == orig_tid:
                                    moved_obj = nb["tasks"].pop(idx_k)
                                    break
                            break
                    if moved_obj is None:
                        moved_obj = {"id": orig_tid or str(uuid.uuid4())[:8], "done": False}
                    moved_obj["text"] = txt
                    moved_obj["tag"] = sel_tag[0]
                    moved_obj["tag_label"] = tag_label
                    moved_obj["tag_color"] = tag_color
                    for nb in notebooks:
                        if nb.get("id") == target_nb_id:
                            nb.setdefault("tasks", []).append(moved_obj)
                            break
            else:
                for nb in notebooks:
                    if nb.get("id") == target_nb_id:
                        nb.setdefault("tasks", []).append({
                            "id": str(uuid.uuid4())[:8],
                            "text": txt,
                            "done": False,
                            "tag": sel_tag[0],
                            "tag_label": tag_label,
                            "tag_color": tag_color,
                        })
                        break
            self._save_notebooks(notebooks, active_id=target_nb_id, baseline=baseline)
            self._close_modal()
            self._render()
            return "break"

        # Bottom-right visual resize grip for intuitive resizing
        grip = tk.Canvas(foot, width=18, height=18, bg="#151c2c", highlightthickness=0, bd=0, cursor="size_nw_se")
        grip.create_line(5, 16, 16, 5, fill="#64748b", width=1.5)
        grip.create_line(10, 16, 16, 10, fill="#94a3b8", width=1.5)
        grip.create_line(14, 16, 16, 14, fill="#cbd5e1", width=1.5)
        grip.pack(side=tk.RIGHT, padx=(8, 0), anchor="se")

        cancel_btn = self._make_rounded_btn(
            foot, "Cancelar", self._close_modal,
            bg="#222b3d", fg="#cbd5e1", hover_bg="#334155", border_col="#3b4963",
            radius=13, pad_x=16, pad_y=7, font_spec=("Segoe UI", 9, "bold"), parent_bg="#151c2c"
        )
        cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))

        save_lbl = "Guardar Cambios" if is_edit else "+ Guardar Tarea"
        save_btn = self._make_rounded_btn(
            foot, save_lbl, _submit,
            bg="#10b981", fg="#ffffff", hover_bg="#059669", border_col="#34d399",
            radius=13, pad_x=18, pad_y=7, font_spec=("Segoe UI", 9, "bold"), parent_bg="#151c2c"
        )
        save_btn.pack(side=tk.RIGHT)

        # Edge + grip resizing support on the frameless modal
        rsz = {"active": False, "dir": "", "sx": 0, "sy": 0, "sw": mw, "sh": mh, "wx": 0, "wy": 0}

        def _detect_edge(e):
            if e.widget is grip:
                return "se"
            rx = e.x_root - modal.winfo_x()
            ry = e.y_root - modal.winfo_y()
            cw, ch = modal.winfo_width(), modal.winfo_height()
            m = 7
            d = ""
            if ry >= ch - m:
                d += "s"
            elif ry <= m:
                d += "n"
            if rx >= cw - m:
                d += "e"
            elif rx <= m:
                d += "w"
            return d

        def _rsz_start(e, forced_dir=""):
            d = forced_dir or _detect_edge(e)
            if not d:
                return
            rsz.update({
                "active": True, "dir": d,
                "sx": e.x_root, "sy": e.y_root,
                "sw": modal.winfo_width(), "sh": modal.winfo_height(),
                "wx": modal.winfo_x(), "wy": modal.winfo_y(),
            })

        def _rsz_move(e):
            if not rsz["active"]:
                return
            dx = e.x_root - rsz["sx"]
            dy = e.y_root - rsz["sy"]
            nw, nh, nx, ny = rsz["sw"], rsz["sh"], rsz["wx"], rsz["wy"]
            if "e" in rsz["dir"]:
                nw = max(min_w, min(920, rsz["sw"] + dx))
            if "w" in rsz["dir"]:
                nw = max(min_w, min(920, rsz["sw"] - dx))
                nx = rsz["wx"] + (rsz["sw"] - nw)
            if "s" in rsz["dir"]:
                nh = max(min_h, min(760, rsz["sh"] + dy))
            if "n" in rsz["dir"]:
                nh = max(min_h, min(760, rsz["sh"] - dy))
                ny = rsz["wy"] + (rsz["sh"] - nh)
            modal.geometry(f"{int(nw)}x{int(nh)}+{int(nx)}+{int(ny)}")

        def _rsz_end(_e):
            rsz["active"] = False

        grip.bind("<Button-1>", lambda e: _rsz_start(e, "se"))
        grip.bind("<B1-Motion>", _rsz_move)
        grip.bind("<ButtonRelease-1>", _rsz_end)
        outer.bind("<Button-1>", _rsz_start)
        outer.bind("<B1-Motion>", _rsz_move)
        outer.bind("<ButtonRelease-1>", _rsz_end)

        def _on_text_return(e):
            if e.state & 0x0001:  # Shift+Enter inserts newline
                return None
            return _submit(e)

        text_box.bind("<Return>", _on_text_return)
        modal.bind("<Escape>", lambda _e: self._close_modal())
        _apply_win11_rounded_modal(modal)
        animate_window(modal)
        modal.after(30, lambda: (modal.focus_force(), text_box.focus_set(), text_box.mark_set(tk.INSERT, tk.END)))

    @staticmethod
    def _make_trash_btn(parent, command, parent_bg="#171f30"):
        """Creates a crisp rounded-corner vector trash-can icon button using 4x supersampled PIL rendering."""
        bw, bh = 30, 28
        sc = 4
        c = tk.Canvas(parent, width=bw, height=bh, bg=parent_bg, highlightthickness=0, bd=0, cursor="hand2")

        def _render_state(bg_rgba, out_rgba, ico_rgba):
            im = Image.new("RGBA", (bw * sc, bh * sc), (0, 0, 0, 0))
            d = ImageDraw.Draw(im, "RGBA")
            d.rounded_rectangle([2, 2, bw * sc - 3, bh * sc - 3], radius=8 * sc, fill=bg_rgba, outline=out_rgba, width=sc)
            # Clean modern trash-can icon (handle + lid + bin + two vertical ribs)
            cx = (bw * sc) / 2.0
            cy = (bh * sc) / 2.0
            d.rounded_rectangle([cx - 12, cy - 30, cx + 12, cy - 18], radius=5, outline=ico_rgba, width=5)
            d.rounded_rectangle([cx - 28, cy - 20, cx + 28, cy - 12], radius=4, fill=ico_rgba)
            d.rounded_rectangle([cx - 22, cy - 10, cx + 22, cy + 30], radius=10, outline=ico_rgba, width=5)
            d.line([(cx - 8, cy - 1), (cx - 8, cy + 21)], fill=ico_rgba, width=5)
            d.line([(cx + 8, cy - 1), (cx + 8, cy + 21)], fill=ico_rgba, width=5)
            im = im.resize((bw, bh), Image.LANCZOS)
            return ImageTk.PhotoImage(im)

        ph_norm = _render_state((46, 27, 38, 255), (96, 44, 60, 255), (248, 113, 113, 255))
        ph_hov = _render_state((239, 68, 68, 255), (252, 165, 165, 255), (255, 255, 255, 255))
        c._trash_imgs = (ph_norm, ph_hov)
        img_item = c.create_image(0, 0, anchor=tk.NW, image=ph_norm)

        c.bind("<Enter>", lambda _e: c.itemconfig(img_item, image=ph_hov))
        c.bind("<Leave>", lambda _e: c.itemconfig(img_item, image=ph_norm))
        c.bind("<ButtonRelease-1>", lambda e: command() if (0 <= e.x <= bw and 0 <= e.y <= bh and callable(command)) else None)
        return c

    def _open_notebooks_modal(self, focus_new=False):
        self._close_modal()
        notebooks_orig, active_id = self._ensure_notebooks()
        baseline = copy.deepcopy(notebooks_orig)
        working = [
            {
                "id": nb.get("id") or f"nb_{uuid.uuid4().hex[:6]}",
                "name": nb.get("name", "Pestaña"),
                "color": nb.get("color", "#3b82f6"),
                "tasks": copy.deepcopy(nb.get("tasks", [])),
            }
            for nb in notebooks_orig
        ]

        modal = tk.Toplevel(self.root)
        self._active_modal = modal
        modal.overrideredirect(True)
        modal.attributes("-topmost", True)
        modal.configure(bg="#243044")
        mw = 470
        mh = max(410, min(650, 238 + max(1, min(7, len(working))) * 48))
        self._position_modal(modal, mw, mh)

        outer = tk.Frame(modal, bg="#111622", highlightthickness=1, highlightbackground="#2b3952")
        outer.pack(fill=tk.BOTH, expand=True)

        hdr = tk.Frame(outer, bg="#151c2c", height=46)
        hdr.pack(side=tk.TOP, fill=tk.X)
        hdr.pack_propagate(False)

        drag_state = {"x": 0, "y": 0}
        def _start_m(e):
            drag_state["x"], drag_state["y"] = e.x, e.y
        def _move_m(e):
            nx = modal.winfo_x() + (e.x - drag_state["x"])
            ny = modal.winfo_y() + (e.y - drag_state["y"])
            modal.geometry(f"+{nx}+{ny}")

        hdr.bind("<Button-1>", _start_m)
        hdr.bind("<B1-Motion>", _move_m)

        hdr_lbl = tk.Label(hdr, text="⚙  Configuración de Pestañas", fg="#f8fafc", bg="#151c2c", font=("Segoe UI", 11, "bold"))
        hdr_lbl.pack(side=tk.LEFT, padx=16)
        hdr_lbl.bind("<Button-1>", _start_m)
        hdr_lbl.bind("<B1-Motion>", _move_m)

        close_btn = self._make_rounded_btn(
            hdr, "×", self._close_modal,
            bg="#1e293b", fg="#cbd5e1", hover_bg="#ef4444", border_col="#334155",
            radius=9, pad_x=10, pad_y=3, font_spec=("Segoe UI", 11, "bold"), parent_bg="#151c2c"
        )
        close_btn.pack(side=tk.RIGHT, padx=(0, 12), pady=8)

        # Pack footer before expanding body so footer buttons are never squished
        foot = tk.Frame(outer, bg="#151c2c", padx=16, pady=12)
        foot.pack(side=tk.BOTTOM, fill=tk.X)

        body = tk.Frame(outer, bg="#111622", padx=16, pady=12)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            body,
            text="Haz clic en el color para cambiar el tono, edita el nombre o arrastra una pestaña para reordenar:",
            fg="#94a3b8", bg="#111622", font=("Segoe UI", 8), anchor="w",
            wraplength=mw - 36, justify=tk.LEFT
        ).pack(fill=tk.X, pady=(0, 8))

        list_frame = tk.Frame(body, bg="#111622")
        list_frame.pack(fill=tk.BOTH, expand=True)
        row_vars = []
        row_frames = []
        drag_nb = {"active": False, "src": None, "slot": None, "start_y": 0, "moved": False, "bar": None}

        def _sync_vars_to_working():
            for idx_v, var in enumerate(row_vars):
                if idx_v < len(working):
                    nm = var.get().strip()
                    if nm:
                        working[idx_v]["name"] = nm[:22]

        def _update_modal_height():
            target_h = max(410, min(650, 238 + max(1, min(7, len(working))) * 48))
            try:
                if modal.winfo_exists():
                    modal.geometry(f"{mw}x{target_h}")
            except Exception:
                pass

        def _rebuild_rows():
            for child in list_frame.winfo_children():
                child.destroy()
            row_vars.clear()
            row_frames.clear()
            drag_nb["bar"] = tk.Frame(list_frame, bg="#38bdf8", height=3)

            for idx_nb, nb in enumerate(working[:7]):
                r = tk.Frame(list_frame, bg="#171f30", highlightthickness=1, highlightbackground="#253148", padx=8, pady=6)
                r.pack(fill=tk.X, pady=3)
                row_frames.append(r)

                # 6-dot drag grip handle
                grip_c = tk.Canvas(r, width=12, height=20, bg="#171f30", highlightthickness=0, bd=0, cursor="fleur")
                grip_c.pack(side=tk.LEFT, padx=(0, 6))
                for gx in (3, 8):
                    for gy in (4, 10, 16):
                        grip_c.create_oval(gx - 1, gy - 1, gx + 1, gy + 1, fill="#64748b", outline="")

                col = nb.get("color", "#3b82f6")
                c_btn = tk.Label(r, text="●", fg=col, bg="#171f30", font=("Segoe UI", 14, "bold"), cursor="hand2")
                c_btn.pack(side=tk.LEFT, padx=(0, 8))

                def _cycle_color(i=idx_nb):
                    _sync_vars_to_working()
                    cur_c = working[i].get("color", "#3b82f6").lower()
                    pal = self.NOTEBOOK_PALETTE
                    nxt = pal[(pal.index(cur_c) + 1) % len(pal)] if cur_c in pal else pal[0]
                    working[i]["color"] = nxt
                    _rebuild_rows()

                c_btn.bind("<Button-1>", lambda _e, i=idx_nb: _cycle_color(i))

                sv = tk.StringVar(value=nb.get("name", "Pestaña"))
                row_vars.append(sv)
                ent = tk.Entry(
                    r, textvariable=sv, width=14, bg="#0d121d", fg="#f8fafc",
                    insertbackground="#60a5fa", bd=0, relief=tk.FLAT, font=("Segoe UI", 10)
                )
                ent.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))

                cnt_txt = f"{len(nb.get('tasks', []))} tareas"
                cnt_lbl = tk.Label(r, text=cnt_txt, fg="#64748b", bg="#171f30", font=("Segoe UI", 8), cursor="fleur")
                cnt_lbl.pack(side=tk.LEFT, padx=(0, 8))

                if len(working) > 1:
                    del_btn = self._make_trash_btn(
                        r,
                        command=lambda i=idx_nb: (_sync_vars_to_working(), working.pop(i), _rebuild_rows()),
                        parent_bg="#171f30"
                    )
                    del_btn.pack(side=tk.LEFT, padx=(2, 0))

                def _drag_press(e, i=idx_nb, row_w=r):
                    if len(row_frames) <= 1:
                        return
                    drag_nb.update({"active": True, "src": i, "slot": i, "start_y": e.y_root, "moved": False})

                def _drag_move(e, row_w=r):
                    if not drag_nb["active"] or not row_frames:
                        return
                    if not drag_nb["moved"] and abs(e.y_root - drag_nb["start_y"]) >= 4:
                        drag_nb["moved"] = True
                        row_w.configure(bg="#1e293b", highlightbackground="#60a5fa", highlightthickness=2)
                    if not drag_nb["moved"]:
                        return
                    slot = len(row_frames)
                    for k, rf in enumerate(row_frames):
                        mid_y = rf.winfo_rooty() + rf.winfo_height() / 2.0
                        if e.y_root < mid_y:
                            slot = k
                            break
                    drag_nb["slot"] = slot
                    if slot <= 0:
                        ind_y = max(0, row_frames[0].winfo_y() - 2)
                    elif slot >= len(row_frames):
                        last_rf = row_frames[-1]
                        ind_y = last_rf.winfo_y() + last_rf.winfo_height() - 1
                    else:
                        prev_rf = row_frames[slot - 1]
                        cur_rf = row_frames[slot]
                        ind_y = (prev_rf.winfo_y() + prev_rf.winfo_height() + cur_rf.winfo_y()) // 2 - 1
                    bar_w = drag_nb["bar"]
                    if bar_w is not None:
                        bar_w.place(x=2, y=ind_y, width=max(40, list_frame.winfo_width() - 4), height=3)
                        bar_w.lift()

                def _drag_release(_e, row_w=r):
                    if not drag_nb["active"]:
                        return
                    src = drag_nb["src"]
                    slot = drag_nb["slot"]
                    moved = drag_nb["moved"]
                    drag_nb.update({"active": False, "src": None, "slot": None, "moved": False})
                    if drag_nb["bar"] is not None:
                        drag_nb["bar"].place_forget()
                    if moved and src is not None and slot is not None and slot not in (src, src + 1):
                        _sync_vars_to_working()
                        item = working.pop(src)
                        ins = slot - 1 if src < slot else slot
                        ins = max(0, min(len(working), ins))
                        working.insert(ins, item)
                        _rebuild_rows()
                    else:
                        try:
                            row_w.configure(bg="#171f30", highlightbackground="#253148", highlightthickness=1)
                        except Exception:
                            pass

                for dw in (grip_c, r, cnt_lbl):
                    dw.bind("<ButtonPress-1>", _drag_press)
                    dw.bind("<B1-Motion>", _drag_move)
                    dw.bind("<ButtonRelease-1>", _drag_release)

            _update_modal_height()

        _rebuild_rows()

        # Quick add new notebook bar
        add_bar = tk.Frame(body, bg="#0d121d", highlightthickness=1, highlightbackground="#293548", padx=8, pady=6)
        add_bar.pack(fill=tk.X, pady=(10, 2))
        new_col_idx = [len(working) % len(self.NOTEBOOK_PALETTE)]
        new_dot = tk.Label(add_bar, text="●", fg=self.NOTEBOOK_PALETTE[new_col_idx[0]], bg="#0d121d", font=("Segoe UI", 13, "bold"), cursor="hand2")
        new_dot.pack(side=tk.LEFT, padx=(2, 6))
        new_dot.bind("<Button-1>", lambda _e: (
            new_col_idx.__setitem__(0, (new_col_idx[0] + 1) % len(self.NOTEBOOK_PALETTE)),
            new_dot.configure(fg=self.NOTEBOOK_PALETTE[new_col_idx[0]])
        ))
        new_nb_var = tk.StringVar()
        new_nb_entry = tk.Entry(add_bar, textvariable=new_nb_var, bg="#0d121d", fg="#f8fafc", insertbackground="#60a5fa", bd=0, relief=tk.FLAT, font=("Segoe UI", 10))
        new_nb_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def _add_nb(_e=None):
            _sync_vars_to_working()
            nm = new_nb_var.get().strip()
            if not nm or len(working) >= 7:
                return
            working.append({
                "id": f"nb_{uuid.uuid4().hex[:6]}",
                "name": nm[:22],
                "color": self.NOTEBOOK_PALETTE[new_col_idx[0]],
                "tasks": [],
            })
            new_nb_var.set("")
            new_col_idx[0] = len(working) % len(self.NOTEBOOK_PALETTE)
            new_dot.configure(fg=self.NOTEBOOK_PALETTE[new_col_idx[0]])
            _rebuild_rows()

        new_nb_entry.bind("<Return>", _add_nb)
        add_nb_btn = self._make_rounded_btn(
            add_bar, "+ Crear Pestaña", _add_nb,
            bg="#2563eb", fg="#ffffff", hover_bg="#1d4ed8", border_col="#3b82f6",
            radius=10, pad_x=12, pad_y=6, font_spec=("Segoe UI", 8, "bold"), parent_bg="#0d121d"
        )
        add_nb_btn.pack(side=tk.RIGHT, padx=(6, 0))

        def _save_all():
            _sync_vars_to_working()
            if new_nb_var.get().strip() and len(working) < 7:
                _add_nb()
            self._save_notebooks(working, active_id=active_id, baseline=baseline)
            self._close_modal()
            self._render()

        cancel_btn = self._make_rounded_btn(
            foot, "Cancelar", self._close_modal,
            bg="#222b3d", fg="#cbd5e1", hover_bg="#334155", border_col="#3b4963",
            radius=13, pad_x=18, pad_y=11, font_spec=("Segoe UI", 9, "bold"), parent_bg="#151c2c"
        )
        cancel_btn.pack(side=tk.RIGHT, padx=(10, 0))

        save_btn = self._make_rounded_btn(
            foot, "Guardar Cambios", _save_all,
            bg="#10b981", fg="#ffffff", hover_bg="#059669", border_col="#34d399",
            radius=13, pad_x=20, pad_y=11, font_spec=("Segoe UI", 9, "bold"), parent_bg="#151c2c"
        )
        save_btn.pack(side=tk.RIGHT)

        modal.bind("<Escape>", lambda _e: self._close_modal())
        _apply_win11_rounded_modal(modal)
        animate_window(modal)
        if focus_new:
            modal.after(30, lambda: (modal.focus_force(), new_nb_entry.focus_set()))
        else:
            modal.after(30, modal.focus_force)

    def _on_context_menu(self, event):
        from custom_menu import CustomMenu
        self._hide_task_tooltip()
        menu = CustomMenu(self.window)
        notebooks, active_nb = self._get_active_notebook()
        show_comp = bool(self.get_cfg("show_completed", False))
        auto_gr = bool(self.get_cfg("auto_grow", True))

        hz = self._hit_zone(event.x, event.y)
        if hz and hz[0] in ("task", "check_task", "edit_task", "del_task"):
            tid = hz[1]
            t_obj = self._task_map.get(tid)
            if t_obj:
                t_prev = (t_obj.get("text") or "Tarea")[:26]
                menu.add_header(f"Tarea: {t_prev}")
                menu.add_command("Editar tarea...", lambda k=tid: self._open_edit_task_modal(k))
                menu.add_command("Mover arriba", lambda k=tid: self._move_task_step(k, -1))
                menu.add_command("Mover abajo", lambda k=tid: self._move_task_step(k, 1))
                tog_lbl = "Marcar como pendiente" if t_obj.get("done") else "Marcar como completada"
                menu.add_command(tog_lbl, lambda k=tid: self._toggle_task(k))
                menu.add_command("Eliminar tarea", lambda k=tid: self._delete_task(k), danger=True)
                menu.add_separator()

        menu.add_header(f"Cuaderno: {active_nb.get('name', 'Tareas')}")
        menu.add_command("+ Nueva Tarea...", self._open_new_task_modal)
        menu.add_command("Configuración de Pestañas...", self._open_notebooks_modal)
        menu.add_command("Ver tareas completadas", lambda: (self.set_cfg("show_completed", not show_comp), self._render()), checked=show_comp)
        menu.add_command("Alargar automáticamente según lista", lambda: (self.set_cfg("auto_grow", not auto_gr), self._render()), checked=auto_gr)
        menu.add_command("Importar bandeja Outlook (todo_inbox.json)", self._manual_ingest_inbox)
        menu.add_command("Limpiar Tareas Completadas", self._clear_completed)
        if len(notebooks) > 1:
            sec_nb = menu.add_section("Cambiar de Cuaderno", expanded=False)
            for nb in notebooks:
                nid = nb["id"]
                sec_nb.add_command(
                    nb.get("name", "Lista"),
                    lambda k=nid: (self._save_notebooks(notebooks, active_id=k), self._render()),
                    checked=(nid == active_nb["id"])
                )
        menu.add_separator()
        self._append_common_menu_items(menu)
        menu.show(event.x_root, event.y_root)

    def _manual_ingest_inbox(self):
        try:
            import todo_store
            todo_store.ingest_todo_inbox()
            self.config_mgr.load()
            self._render()
        except Exception as e:
            print(f"[TodoWidget] Inbox ingest error: {e}")

    @staticmethod
    def _wrap_task_lines(draw, text, font, max_w, max_lines=3):
        """Word-wraps a task description up to `max_lines` lines, ellipsizing only if it exceeds `max_lines`."""
        max_w = max(40.0, float(max_w))
        paragraphs = (text or "").replace("\r\n", "\n").split("\n")
        lines = []
        for para in paragraphs:
            words = para.split()
            if not words:
                continue
            cur = ""
            for wd in words:
                cand = (cur + " " + wd).strip() if cur else wd
                if draw.textlength(cand, font=font) <= max_w:
                    cur = cand
                else:
                    if cur:
                        lines.append(cur)
                    if draw.textlength(wd, font=font) <= max_w:
                        cur = wd
                    else:
                        chunk = ""
                        for ch in wd:
                            if draw.textlength(chunk + ch, font=font) > max_w and chunk:
                                lines.append(chunk)
                                chunk = ch
                            else:
                                chunk += ch
                        cur = chunk
            if cur:
                lines.append(cur)
        if not lines:
            return [""]
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            last = lines[-1]
            while len(last) > 1 and draw.textlength(last + "…", font=font) > max_w:
                last = last[:-1]
            lines[-1] = last.rstrip() + "…"
        return lines

    def _render(self):
        if not self.window or not self.canvas:
            return

        notebooks, active_nb = self._get_active_notebook()
        active_id = active_nb.get("id", "")
        nb_rgb = _hex_to_rgb(active_nb.get("color", "#3b82f6"), (59, 130, 246))
        all_tasks = active_nb.get("tasks", [])
        done_count = sum(1 for t in all_tasks if t.get("done", False))
        pend_count = len(all_tasks) - done_count

        if self._is_collapsed_view():
            self.check_task_rects = []
            self.task_rects = []
            self.edit_task_rects = []
            self.del_task_rects = []
            self.tab_rects = []
            self._visible_row_spans = []
            self.add_btn_rect = (0, 0, 0, 0)
            self.gear_btn_rect = (0, 0, 0, 0)
            self.clear_btn_rect = (0, 0, 0, 0)
            self.filter_done_rect = (0, 0, 0, 0)
            self.quick_tab_btn_rect = (0, 0, 0, 0)
            self.scroll_up_rect = (0, 0, 0, 0)
            self.scroll_dn_rect = (0, 0, 0, 0)
            self._render_collapsed_pill(
                active_nb.get("name", "To-Do"),
                f"{pend_count} pendientes",
                accent_rgb=nb_rgb,
            )
            return

        s = self._get_effective_scale()
        sp = self._get_spacing_mult()
        show_completed = bool(self.get_cfg("show_completed", False))
        filtered_tasks = [t for t in all_tasks if bool(t.get("done", False)) == show_completed]

        base_row_h = max(int(24 * s), int(28 * s * sp))
        line_h = max(int(14 * s), int(15.5 * s * self._get_font_scale()))
        header_block_h = int(70 * s)
        fw, fh = self._compute_full_dims()
        w = self.win_w if self.is_resizing else fw

        f_tab = self._get_styled_font(9.5 * s, bold_default=True)
        f_item = self._get_styled_font(11.5 * s, bold_default=False)
        f_clr = self._get_styled_font(9.0 * s, bold_default=True)
        f_tag = self._get_styled_font(8.2 * s, bold_default=True)

        pad_x = int(14 * s)
        cb_s = max(12, int(14 * s))
        action_gutter = int(42 * s)
        # Pre-measure wrapped lines (up to 3 lines/task) to compute accurate row heights & auto-grow height
        tmp_im = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        tmp_d = ImageDraw.Draw(tmp_im, "RGBA")
        est_txt_w = max(60, w - pad_x * 2 - cb_s - action_gutter - int(18 * s))
        row_pad_v = max(6, int(8 * s * min(1.3, sp)))
        pre_heights = []
        for t in filtered_tasks:
            t_lines = self._wrap_task_lines(tmp_d, t.get("text", ""), f_item, est_txt_w, max_lines=3)
            rh = max(base_row_h, len(t_lines) * line_h + row_pad_v)
            pre_heights.append(rh)

        # Auto-elongation (auto_grow) so the widget expands vertically with multi-line tasks up to taskbar/screen bottom, then scrolls
        if not self.is_resizing and bool(self.get_cfg("auto_grow", True)):
            _mx1, my1, _mx2, my2, _wx1, wy1, _wx2, wy2 = self._get_screen_and_work_bounds()
            cur_y = self.window.winfo_y() if (self.window and self.window.winfo_exists()) else int(self.get_cfg("y", self.default_y))
            avail_work_h = max(int(220 * s), int(wy2 - max(wy1, cur_y) - 8))
            cfg_max_auto = self.get_cfg("max_auto_h", None)
            if cfg_max_auto is not None:
                max_auto_h = min(avail_work_h, int(float(cfg_max_auto) * self.dpi_scale))
            else:
                max_auto_h = avail_work_h
            min_auto_h = max(int(130 * s), header_block_h + base_row_h + int(20 * s))
            desired_h = header_block_h + (sum(pre_heights) if pre_heights else base_row_h) + int(18 * s)
            h = max(min_auto_h, min(max_auto_h, desired_h))
        else:
            h = self.win_h if self.is_resizing else fh

        self._set_window_size_live(w, h)
        w, h = self.win_w, self.win_h

        composite = self._build_card_surface(w, h, accent_rgb=nb_rgb)
        draw = ImageDraw.Draw(composite, "RGBA")

        btn_s = max(20, int(23 * s))
        tab_h = max(20, int(23 * s))
        row1_top = int(10 * s)
        by1 = row1_top
        by2 = by1 + btn_s
        tab_y1 = row1_top
        tab_y2 = tab_y1 + tab_h

        # Top-right header buttons: [⚙] [—] (the green '+' Nueva Tarea button moved to Row 2 left!)
        # 1. '—' Minimize button at far right
        mx2 = w - pad_x
        mx1 = mx2 - btn_s
        self._draw_min_button(composite, draw, mx1, by1, mx2, by2, is_collapsed=False)

        # 2. '⚙' Notebook Settings button next to Minimize
        gx2 = mx1 - int(5 * s)
        gx1 = gx2 - btn_s
        self.gear_btn_rect = (gx1, by1, gx2, by2)
        hov_gear = (self.hovered_zone == ("btn", "gear"))
        alpha_rounded_rect(
            composite, [gx1, by1, gx2, by2], radius=int(7 * s),
            fill=(59, 130, 246, 125) if hov_gear else (255, 255, 255, 22),
            outline=(147, 197, 253, 210) if hov_gear else (148, 163, 184, 85), width=1
        )
        gcx, gcy = (gx1 + gx2) / 2.0, (by1 + by2) / 2.0
        gr_out = max(4.0, btn_s * 0.26)
        gr_in = max(2.0, btn_s * 0.12)
        gear_col = (255, 255, 255, 250) if hov_gear else (203, 213, 225, 230)
        for k_t in range(6):
            ang = k_t * (math.pi / 3.0)
            tx1 = gcx + math.cos(ang) * (gr_out - 1.2)
            ty1 = gcy + math.sin(ang) * (gr_out - 1.2)
            tx2 = gcx + math.cos(ang) * (gr_out + 2.0)
            ty2 = gcy + math.sin(ang) * (gr_out + 2.0)
            alpha_line(composite, [(tx1, ty1), (tx2, ty2)], fill=gear_col, width=max(1, int(2 * s)))
        alpha_ellipse(composite, [gcx - gr_out, gcy - gr_out, gcx + gr_out, gcy + gr_out], outline=gear_col, width=max(1, int(1.6 * s)))
        alpha_ellipse(composite, [gcx - gr_in, gcy - gr_in, gcx + gr_in, gcy + gr_in], fill=(16, 19, 27, 255))

        # Row 1 Left: Notebook Tabs Strip directly in header (no redundant widget title!)
        self.tab_rects = []
        tx_cur = pad_x
        q_w = max(18, int(21 * s))
        max_tabs_x = gx1 - int(10 * s) - q_w

        for nb in notebooks:
            nid = nb.get("id", "")
            nname = nb.get("name", "Lista")
            ncol = _hex_to_rgb(nb.get("color", "#3b82f6"), (59, 130, 246))
            pend_n = sum(1 for t in nb.get("tasks", []) if not t.get("done", False))
            lbl_tab = f"{nname[:10]} ({pend_n})" if pend_n > 0 else nname[:11]
            tw_tab = int(draw.textbbox((0, 0), lbl_tab, font=f_tab)[2]) + int(18 * s)
            if tx_cur + tw_tab > max_tabs_x and self.tab_rects:
                break
            tw_tab = min(tw_tab, max(int(42 * s), max_tabs_x - tx_cur))
            if tx_cur + tw_tab > gx1 - int(6 * s):
                break
            tx2 = tx_cur + tw_tab
            is_act = (nid == active_id)
            is_hov_t = (self.hovered_zone == ("tab", nid))
            if is_act:
                alpha_rounded_rect(
                    composite, [tx_cur, tab_y1, tx2, tab_y2], radius=int(7 * s),
                    fill=(*ncol, 82), outline=(*ncol, 230), width=1
                )
                draw.line([(tx_cur + int(6 * s), tab_y2 - 1), (tx2 - int(6 * s), tab_y2 - 1)], fill=(*ncol, 255), width=max(2, int(2 * s)))
                t_fg = (255, 255, 255, 255)
            else:
                alpha_rounded_rect(
                    composite, [tx_cur, tab_y1 + int(1 * s), tx2, tab_y2], radius=int(7 * s),
                    fill=(255, 255, 255, 32 if is_hov_t else 14),
                    outline=(148, 163, 184, 95 if is_hov_t else 42), width=1
                )
                d_r = max(2, int(2.5 * s))
                alpha_ellipse(composite, [tx_cur + int(6 * s), (tab_y1 + tab_y2) // 2 - d_r, tx_cur + int(6 * s) + d_r * 2, (tab_y1 + tab_y2) // 2 + d_r], fill=(*ncol, 225))
                t_fg = (226, 232, 240, 240) if is_hov_t else (148, 163, 184, 215)

            txt_cx = (tx_cur + tx2) / 2.0 + (0 if is_act else int(4 * s))
            draw.text((txt_cx, (tab_y1 + tab_y2) / 2.0), lbl_tab, fill=t_fg, font=f_tab, anchor="mm")
            self.tab_rects.append((tx_cur, tab_y1, tx2, tab_y2, nid))
            tx_cur = tx2 + int(5 * s)

        # Mini '+' button at end of tab strip
        if tx_cur + q_w <= gx1 - int(6 * s):
            qx1, qx2 = tx_cur, tx_cur + q_w
            self.quick_tab_btn_rect = (qx1, tab_y1 + int(1 * s), qx2, tab_y2)
            hov_qt = (self.hovered_zone == ("btn", "new_tab"))
            alpha_rounded_rect(
                composite, list(self.quick_tab_btn_rect), radius=int(6 * s),
                fill=(59, 130, 246, 95) if hov_qt else (255, 255, 255, 16),
                outline=(96, 165, 250, 180) if hov_qt else (148, 163, 184, 55), width=1
            )
            qcx, qcy = (qx1 + qx2) / 2.0, (tab_y1 + tab_y2) / 2.0 + 0.5
            qarm = max(3.0, q_w * 0.22)
            alpha_line(composite, [(qcx - qarm, qcy), (qcx + qarm, qcy)], fill=(226, 232, 240, 240), width=max(1, int(1.8 * s)))
            alpha_line(composite, [(qcx, qcy - qarm), (qcx, qcy + qarm)], fill=(226, 232, 240, 240), width=max(1, int(1.8 * s)))
        else:
            self.quick_tab_btn_rect = (0, 0, 0, 0)

        # Row 2 (Compact reorganized action bar):
        # Left: Green [+ Agregar nueva tarea]  |  Center: [✓ Ver completados (N)]  |  Right: [Limpiar (N)]
        sub_y1 = max(by2, tab_y2) + int(6 * s)
        sub_h = max(21, int(23 * s))
        sub_y2 = sub_y1 + sub_h
        sub_cy = (sub_y1 + sub_y2) / 2.0

        # 1) Green '+ Nueva tarea' button on Row 2 Left
        add_lbl = "Nueva tarea"
        add_ico_w = int(13 * s)
        add_w = int(draw.textbbox((0, 0), add_lbl, font=f_clr)[2]) + add_ico_w + int(18 * s)
        ax1 = pad_x
        ax2 = min(w - pad_x, ax1 + add_w)
        self.add_btn_rect = (ax1, sub_y1, ax2, sub_y2)
        hov_add = (self.hovered_zone == ("btn", "add"))
        alpha_rounded_rect(
            composite, [ax1, sub_y1, ax2, sub_y2], radius=int(sub_h // 2),
            fill=(16, 185, 129, 245 if hov_add else 205),
            outline=(110, 231, 183, 245 if hov_add else 190), width=1
        )
        ap_cx = ax1 + int(11 * s)
        ap_arm = max(3.2, 3.8 * s)
        alpha_line(composite, [(ap_cx - ap_arm, sub_cy), (ap_cx + ap_arm, sub_cy)], fill=(255, 255, 255, 255), width=max(2, int(2.0 * s)))
        alpha_line(composite, [(ap_cx, sub_cy - ap_arm), (ap_cx, sub_cy + ap_arm)], fill=(255, 255, 255, 255), width=max(2, int(2.0 * s)))
        draw.text((ap_cx + int(8 * s), sub_cy), add_lbl, fill=(255, 255, 255, 255), font=f_clr, anchor="lm")

        # 2) Optional 'Limpiar (N)' on Row 2 Right
        clr_w = 0
        if done_count > 0:
            clr_lbl = f"Limpiar ({done_count})"
            clr_w = int(draw.textbbox((0, 0), clr_lbl, font=f_clr)[2]) + int(16 * s)

        # 3) Centered 'Ver completados (N)' / 'Ver pendientes (N)' between Left & Right pills
        flt_lbl = f"Ver pendientes ({pend_count})" if show_completed else f"Ver completados ({done_count})"
        ico_gap = int(14 * s)
        flt_w = int(draw.textbbox((0, 0), flt_lbl, font=f_clr)[2]) + ico_gap + int(18 * s)
        min_flt_x1 = ax2 + int(8 * s)
        max_flt_x2 = (w - pad_x - clr_w - int(8 * s)) if clr_w > 0 else (w - pad_x)
        fx1 = int((w - flt_w) / 2.0)
        if fx1 < min_flt_x1:
            fx1 = min_flt_x1
        if fx1 + flt_w > max_flt_x2:
            fx1 = max(min_flt_x1, max_flt_x2 - flt_w)
        fx2 = min(max_flt_x2, fx1 + flt_w)
        if fx2 - fx1 >= int(52 * s):
            self.filter_done_rect = (fx1, sub_y1, fx2, sub_y2)
            hov_flt = (self.hovered_zone == ("btn", "toggle_done_view"))
            flt_fill = (16, 185, 129, 105 if hov_flt else 74) if show_completed else (255, 255, 255, 34 if hov_flt else 18)
            flt_out = (52, 211, 153, 225) if show_completed else (148, 163, 184, 95)
            flt_fg = (236, 253, 245, 255) if show_completed else (226, 232, 240, 240)
            alpha_rounded_rect(composite, [fx1, sub_y1, fx2, sub_y2], radius=int(sub_h // 2), fill=flt_fill, outline=flt_out, width=1)
            ix = fx1 + int(10 * s)
            if show_completed:
                alpha_line(composite, [(ix + 4 * s, sub_cy - 3.5 * s), (ix, sub_cy), (ix + 4 * s, sub_cy + 3.5 * s)], fill=flt_fg, width=max(1, int(1.8 * s)))
                alpha_line(composite, [(ix, sub_cy), (ix + 7 * s, sub_cy)], fill=flt_fg, width=max(1, int(1.8 * s)))
            else:
                alpha_line(composite, [(ix, sub_cy + 0.5 * s), (ix + 2.8 * s, sub_cy + 3.2 * s), (ix + 7.2 * s, sub_cy - 3.0 * s)], fill=(52, 211, 153, 250), width=max(1, int(2.0 * s)))
            draw.text((ix + ico_gap - int(2 * s), sub_cy), flt_lbl, fill=flt_fg, font=f_clr, anchor="lm")
        else:
            self.filter_done_rect = (0, 0, 0, 0)

        if done_count > 0:
            cx2 = w - pad_x
            cx1 = cx2 - clr_w
            if cx1 > max(ax2, fx2) + int(6 * s):
                self.clear_btn_rect = (cx1, sub_y1, cx2, sub_y2)
                hov_clr = (self.hovered_zone == ("btn", "clear"))
                alpha_rounded_rect(
                    composite, [cx1, sub_y1, cx2, sub_y2], radius=int(sub_h // 2),
                    fill=(239, 68, 68, 175 if hov_clr else 120),
                    outline=(252, 165, 165, 225), width=1
                )
                draw.text(((cx1 + cx2) / 2.0, sub_cy), clr_lbl, fill=(254, 226, 226, 255), font=f_clr, anchor="mm")
            else:
                self.clear_btn_rect = (0, 0, 0, 0)
        else:
            self.clear_btn_rect = (0, 0, 0, 0)

        div_y = sub_y2 + int(6 * s)
        alpha_line(composite, [(pad_x, div_y), (w - pad_x, div_y)], fill=(*nb_rgb, 65), width=1)

        # Tasks list area (supports up to 3 wrapped lines per task with variable row heights!)
        self.check_task_rects = []
        self.task_rects = []
        self.edit_task_rects = []
        self.del_task_rects = []
        self._visible_row_spans = []
        self.scroll_up_rect = (0, 0, 0, 0)
        self.scroll_dn_rect = (0, 0, 0, 0)
        list_top_y = div_y + int(5 * s)
        avail_list_h = max(base_row_h, h - list_top_y - int(8 * s))

        # Determine if scrolling is needed
        total_h_all = sum(pre_heights)
        needs_scroll = total_h_all > avail_list_h
        scroll_gutter = int(16 * s) if needs_scroll else 0
        avail_txt_w = max(48, w - pad_x * 2 - cb_s - action_gutter - scroll_gutter)

        # Exact per-task layout (wrapped up to 3 lines + optional colored tag badge)
        row_layouts = []
        for t in filtered_tasks:
            t_tag_col = (t.get("tag_color") or "").strip()
            t_tag_lbl = (t.get("tag_label") or "").strip()
            tag_pill_w = 0
            if t_tag_col and t_tag_lbl:
                tag_pill_w = int(draw.textlength(t_tag_lbl, font=f_tag)) + int(12 * s) + int(6 * s)
            t_lines = self._wrap_task_lines(draw, t.get("text", ""), f_item, max(48, avail_txt_w - tag_pill_w), max_lines=3)
            rh = max(base_row_h, len(t_lines) * line_h + row_pad_v)
            row_layouts.append((t, t_lines, rh, tag_pill_w))

        total_h_exact = sum(r[2] for r in row_layouts)
        needs_scroll = total_h_exact > avail_list_h
        if not needs_scroll:
            self.scroll_offset = 0
        else:
            # Clamp scroll_offset so last item remains reachable
            max_off = max(0, len(row_layouts) - 1)
            self.scroll_offset = max(0, min(max_off, self.scroll_offset))

        visible_layouts = []
        acc_h = 0
        for r_item in row_layouts[self.scroll_offset:]:
            if visible_layouts and (acc_h + r_item[2]) > avail_list_h:
                break
            visible_layouts.append(r_item)
            acc_h += r_item[2]

        cur_y = list_top_y
        for (t, t_lines, rh, tag_pill_w) in visible_layouts:
            is_done = t.get("done", False)
            t_id = t.get("id", "")
            tag_hex = (t.get("tag_color") or "").strip()
            tag_lbl = (t.get("tag_label") or "").strip()
            is_drag_src = self._drag_task_active and (self._drag_task_id == t_id)
            is_row_hov = (not self._drag_task_active) and (
                self.hovered_zone in (("task", t_id), ("check_task", t_id), ("edit_task", t_id), ("del_task", t_id))
            )

            row_right = w - pad_x - scroll_gutter
            if is_drag_src:
                alpha_rounded_rect(
                    composite, [pad_x - int(4 * s), cur_y + 1, row_right + int(2 * s), cur_y + rh - 1],
                    radius=int(6 * s), fill=(*nb_rgb, 48), outline=(*nb_rgb, 155), width=1
                )
            elif is_row_hov:
                alpha_rounded_rect(
                    composite, [pad_x - int(4 * s), cur_y + 1, row_right + int(2 * s), cur_y + rh - 1],
                    radius=int(6 * s), fill=(255, 255, 255, 22)
                )

            tc_rgb = _hex_to_rgb(tag_hex, nb_rgb) if tag_hex else None
            if tc_rgb is not None:
                bar_top = cur_y + int(5 * s)
                bar_bot = max(bar_top + cb_s, cur_y + rh - int(5 * s))
                draw.rounded_rectangle(
                    [pad_x - int(6 * s), bar_top, pad_x - int(2 * s), bar_bot],
                    radius=2, fill=(*tc_rgb, 240 if not is_done else 120)
                )

            # Align checkbox vertically with the first line (centered for 1-line rows)
            first_line_cy = cur_y + (rh / 2.0 if len(t_lines) == 1 else (row_pad_v / 2.0 + line_h / 2.0))
            cb_y1 = int(round(first_line_cy - cb_s / 2.0))
            cb_rect = [pad_x, cb_y1, pad_x + cb_s, cb_y1 + cb_s]
            hov_cb = (self.hovered_zone == ("check_task", t_id))
            if is_done:
                draw.rounded_rectangle(cb_rect, radius=max(3, int(4 * s)), fill=(16, 185, 129, 245 if hov_cb else 230))
                alpha_line(composite, [(pad_x + cb_s * 0.22, cb_y1 + cb_s * 0.52), (pad_x + cb_s * 0.44, cb_y1 + cb_s * 0.76), (pad_x + cb_s * 0.78, cb_y1 + cb_s * 0.26)], fill=(255, 255, 255, 255), width=max(2, int(2 * s)))
            else:
                out_c = (52, 211, 153, 245) if hov_cb else ((*(tc_rgb or nb_rgb), 225) if is_row_hov else (148, 163, 184, 205))
                fill_cb = (16, 185, 129, 45) if hov_cb else (22, 30, 46, 215)
                draw.rounded_rectangle(cb_rect, radius=max(3, int(4 * s)), fill=fill_cb, outline=out_c, width=1)

            text_x = pad_x + cb_s + int(8 * s)
            if is_drag_src:
                text_color = (148, 163, 184, 175)
            elif is_done:
                text_color = (148, 163, 184, 185)
            else:
                text_color = (248, 250, 252, 255)

            # Optional colored tag pill badge right before task text
            if tc_rgb is not None and tag_lbl:
                tp_h = max(14, int(15 * s))
                tp_w = int(draw.textlength(tag_lbl, font=f_tag)) + int(10 * s)
                tp_y1 = int(round(first_line_cy - tp_h / 2.0))
                tp_y2 = tp_y1 + tp_h
                alpha_rounded_rect(
                    composite, [text_x, tp_y1, text_x + tp_w, tp_y2],
                    radius=tp_h // 2, fill=(*tc_rgb, 68 if not is_done else 38),
                    outline=(*tc_rgb, 215 if not is_done else 110), width=1
                )
                draw.text((text_x + tp_w / 2.0, (tp_y1 + tp_y2) / 2.0), tag_lbl, fill=(248, 250, 252, 250 if not is_done else 160), font=f_tag, anchor="mm")
                text_x += tp_w + int(6 * s)

            # Render up to 3 wrapped lines cleanly
            total_text_h = len(t_lines) * line_h
            block_y0 = cur_y + (rh - total_text_h) / 2.0
            for li, ln_txt in enumerate(t_lines):
                ly_mid = block_y0 + li * line_h + line_h / 2.0
                draw.text((text_x, ly_mid), ln_txt, fill=text_color, font=f_item, anchor="lm")
                if is_done and ln_txt:
                    tw_done = draw.textlength(ln_txt, font=f_item)
                    draw.line([(text_x, ly_mid), (text_x + tw_done, ly_mid)], fill=(148, 163, 184, 195), width=1)

            # Row hover action icons: crisp vector Edit Pencil + vector Delete '×'
            ty_mid = first_line_cy
            btn_w_r = max(16, int(18 * s))
            dx2 = row_right
            dx1 = dx2 - btn_w_r
            ex2 = dx1 - int(3 * s)
            ex1 = ex2 - btn_w_r
            if is_row_hov:
                # Subtle 6-dot drag grip on far left margin
                gx_d = pad_x - int(10 * s)
                if gx_d >= 3:
                    for dy_g in (-3.5 * s, 0, 3.5 * s):
                        alpha_ellipse(composite, [gx_d - 1.2, ty_mid + dy_g - 1.2, gx_d + 1.2, ty_mid + dy_g + 1.2], fill=(148, 163, 184, 150))
                        alpha_ellipse(composite, [gx_d + 2.6, ty_mid + dy_g - 1.2, gx_d + 5.0, ty_mid + dy_g + 1.2], fill=(148, 163, 184, 150))

                # Edit pencil button
                hov_ed = (self.hovered_zone == ("edit_task", t_id))
                if hov_ed:
                    alpha_rounded_rect(composite, [ex1, ty_mid - btn_w_r / 2.0, ex2, ty_mid + btn_w_r / 2.0], radius=int(5 * s), fill=(59, 130, 246, 115))
                ed_col = (255, 255, 255, 255) if hov_ed else (148, 163, 184, 225)
                ecx, ecy = (ex1 + ex2) / 2.0, ty_mid
                pr = max(3.5, 4.2 * s)
                alpha_line(composite, [(ecx - pr, ecy + pr), (ecx + pr * 0.85, ecy - pr * 0.85)], fill=ed_col, width=max(2, int(2.1 * s)))
                alpha_line(composite, [(ecx - pr - 1, ecy + pr + 1), (ecx - pr + 2, ecy + pr)], fill=ed_col, width=max(1, int(1.6 * s)))
                self.edit_task_rects.append((ex1 - 1, cur_y, ex2 + 1, cur_y + rh, t_id))

                # Delete '×' button
                hov_del = (self.hovered_zone == ("del_task", t_id))
                if hov_del:
                    alpha_rounded_rect(composite, [dx1, ty_mid - btn_w_r / 2.0, dx2, ty_mid + btn_w_r / 2.0], radius=int(5 * s), fill=(239, 68, 68, 130))
                del_col = (254, 202, 202, 255) if hov_del else (148, 163, 184, 220)
                dcx, dcy = (dx1 + dx2) / 2.0, ty_mid
                xr = max(3.2, 3.8 * s)
                alpha_line(composite, [(dcx - xr, dcy - xr), (dcx + xr, dcy + xr)], fill=del_col, width=max(1, int(1.9 * s)))
                alpha_line(composite, [(dcx + xr, dcy - xr), (dcx - xr, dcy + xr)], fill=del_col, width=max(1, int(1.9 * s)))
                self.del_task_rects.append((dx1 - 1, cur_y, dx2 + 2, cur_y + rh, t_id))

            # Separate hitboxes: checkbox completes/uncompletes; text area edits on click / reorders on drag!
            cb_hit_x2 = pad_x + cb_s + int(5 * s)
            self.check_task_rects.append((pad_x - 3, cur_y, cb_hit_x2, cur_y + rh, t_id))
            self.task_rects.append((cb_hit_x2 + 1, cur_y, row_right, cur_y + rh, t_id))
            self._visible_row_spans.append((cur_y, cur_y + rh, t_id))
            self._task_map[t_id] = t
            cur_y += rh

        # Drag-and-drop reorder insertion bar indicator (supports variable 1-3 line heights)
        if self._drag_task_active and self._drag_task_target_slot is not None and self._visible_row_spans:
            slot = self._drag_task_target_slot
            if slot <= 0:
                ins_y = self._visible_row_spans[0][0]
            elif slot >= len(self._visible_row_spans):
                ins_y = self._visible_row_spans[-1][1]
            else:
                ins_y = self._visible_row_spans[slot][0]
            ins_x1 = pad_x
            ins_x2 = w - pad_x - scroll_gutter
            alpha_line(composite, [(ins_x1, ins_y), (ins_x2, ins_y)], fill=(16, 185, 129, 250), width=max(2, int(2.5 * s)))
            r_dot = max(3, int(3.5 * s))
            alpha_ellipse(composite, [ins_x1 - r_dot, ins_y - r_dot, ins_x1 + r_dot, ins_y + r_dot], fill=(110, 231, 183, 255))
            alpha_ellipse(composite, [ins_x2 - r_dot, ins_y - r_dot, ins_x2 + r_dot, ins_y + r_dot], fill=(110, 231, 183, 255))

        # Interactive Scrollbar with clickable ▲ / ▼ arrows + thumb when list overflows
        if needs_scroll:
            sb_x1 = w - int(17 * s)
            sb_x2 = w - int(5 * s)
            arr_h = max(13, int(15 * s))
            top_s = div_y + int(5 * s)
            bot_s = h - int(14 * s)
            self.scroll_up_rect = (sb_x1, top_s, sb_x2, top_s + arr_h)
            self.scroll_dn_rect = (sb_x1, bot_s - arr_h, sb_x2, bot_s)
            alpha_rounded_rect(composite, list(self.scroll_up_rect), radius=3, fill=(255, 255, 255, 25))
            alpha_rounded_rect(composite, list(self.scroll_dn_rect), radius=3, fill=(255, 255, 255, 25))
            draw.text(((sb_x1 + sb_x2) / 2.0, top_s + arr_h / 2.0), "▲", fill=(203, 213, 225, 230), font=f_clr, anchor="mm")
            draw.text(((sb_x1 + sb_x2) / 2.0, bot_s - arr_h / 2.0), "▼", fill=(203, 213, 225, 230), font=f_clr, anchor="mm")

            track_y1 = top_s + arr_h + 3
            track_y2 = bot_s - arr_h - 3
            track_h = max(10, track_y2 - track_y1)
            vis_cnt = max(1, len(visible_layouts))
            thumb_h = max(10, int(track_h * (vis_cnt / float(max(1, len(row_layouts))))))
            max_off = max(1, len(row_layouts) - vis_cnt)
            thumb_y = track_y1 + int((track_h - thumb_h) * min(1.0, self.scroll_offset / float(max_off)))
            tx_mid = int((sb_x1 + sb_x2) // 2)
            draw.rounded_rectangle([tx_mid - 2, thumb_y, tx_mid + 2, thumb_y + thumb_h], radius=2, fill=(*nb_rgb, 210))

        if not filtered_tasks:
            empty_msg = "No hay tareas completadas aún." if show_completed else "Sin pendientes aquí. Clic en '+ Nueva tarea' para crear."
            draw.text((pad_x, cur_y + int(6 * s)), empty_msg, fill=(148, 163, 184, 215), font=f_item)

        self._render_to_canvas(composite, w, h)


def _get_ole_drop_target_cls():
    global _OLE_DROP_CLS
    if _OLE_DROP_CLS is not None:
        return _OLE_DROP_CLS
    try:
        import pythoncom
        import win32clipboard
        import win32com.server.util

        class WidgetOleDropTarget:
            _com_interfaces_ = [pythoncom.IID_IDropTarget]
            _public_methods_ = ["DragEnter", "DragOver", "DragLeave", "Drop"]

            def __init__(self, widget):
                self.widget = widget

            def DragEnter(self, data_obj, key_state, pt, effect):
                self.widget._ext_drag_hover = True
                return DROPEFFECT_COPY | DROPEFFECT_LINK

            def DragOver(self, key_state, pt, effect):
                self.widget._ext_drag_hover = True
                return DROPEFFECT_COPY | DROPEFFECT_LINK

            def DragLeave(self):
                self.widget._ext_drag_hover = False

            def Drop(self, data_obj, key_state, pt, effect):
                self.widget._ext_drag_hover = False
                try:
                    paths = self._extract_paths(data_obj)
                    if paths:
                        self.widget._drop_queue.put(paths)
                except Exception as e:
                    print(f"[QuickAccess OLE Drop] Error: {e}")
                return DROPEFFECT_COPY

            def _extract_paths(self, data_obj):
                paths = []
                try:
                    etc = (win32clipboard.CF_HDROP, None, pythoncom.DVASPECT_CONTENT, -1, pythoncom.TYMED_HGLOBAL)
                    stg = data_obj.GetData(etc)
                    raw = stg.data
                    if isinstance(raw, (bytes, bytearray)) and len(raw) >= 20:
                        p_files, _, _, _, f_wide = struct.unpack_from("<IiiII", raw, 0)
                        payload = raw[p_files:]
                        decoded = payload.decode("utf-16-le" if f_wide else "mbcs", errors="ignore")
                        for part in decoded.split("\x00"):
                            if part:
                                paths.append(part)
                            else:
                                break
                except Exception:
                    pass
                return paths

        _OLE_DROP_CLS = (pythoncom, win32com.server.util, WidgetOleDropTarget)
    except Exception:
        _OLE_DROP_CLS = False
    return _OLE_DROP_CLS


class QuickAccessWidget(BaseDesktopWidget):
    """Floating desktop shelf for favorite apps and folders with hover modes and drag-and-drop."""
    def __init__(self, root, config_mgr, on_open_settings=None):
        super().__init__(root, config_mgr, "quick_access", default_x=320, default_y=60, default_w=280, default_h=78, on_open_settings=on_open_settings)
        self.item_boxes = []
        self.hovered_item = -1
        self.is_expanded = False
        self._ole_target = None
        self._subclass_ref = None
        self._drop_queue = queue.SimpleQueue()
        self._ext_drag_hover = False
        self._last_ext_drag_render = False
        self._drag_item_idx = None
        self._reorder_target_idx = None

    def show(self):
        super().show()
        if self.canvas:
            self.canvas.bind("<Motion>", self._on_motion, add="+")
            self.canvas.bind("<Leave>", self._on_leave, add="+")
            self._setup_drop()

    def _start_update_loop(self):
        self._render()
        self._poll_drops()

    def _poll_drops(self):
        if not self.window or not self.window.winfo_exists():
            return
        try:
            while not self._drop_queue.empty():
                paths = self._drop_queue.get_nowait()
                if paths:
                    self._handle_dropped_files(paths)
            if self._ext_drag_hover != self._last_ext_drag_render:
                self._last_ext_drag_render = self._ext_drag_hover
                if self._ext_drag_hover:
                    self.is_expanded = True
                self._render()
        except Exception as e:
            print(f"[QuickAccess] Drop drain error: {e}")
        self.update_job = self.root.after(70, self._poll_drops)

    def _setup_drop(self):
        if not self.window or not self.canvas:
            return
        try:
            top_hwnd = self.top_hwnd or user32.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
            canvas_hwnd = self.canvas.winfo_id()
            ole_ok = False
            ole_pack = _get_ole_drop_target_cls()
            if ole_pack:
                try:
                    pcom, w32util, DropCls = ole_pack
                    pcom.OleInitialize()
                    self._ole_target = w32util.wrap(DropCls(self), pcom.IID_IDropTarget)
                    pcom.RegisterDragDrop(top_hwnd, self._ole_target)
                    if canvas_hwnd and canvas_hwnd != top_hwnd:
                        try:
                            pcom.RegisterDragDrop(canvas_hwnd, self._ole_target)
                        except Exception:
                            pass
                    ole_ok = True
                except Exception:
                    pass
            if not ole_ok:
                for h in (top_hwnd, canvas_hwnd):
                    shell32.DragAcceptFiles(h, True)

                def wnd_proc(hwnd, msg, wparam, lparam, id_subclass, ref_data):
                    if msg == WM_DROPFILES:
                        hDrop = wparam
                        count = shell32.DragQueryFileW(hDrop, 0xFFFFFFFF, None, 0)
                        paths = []
                        buf = ctypes.create_unicode_buffer(1024)
                        for i in range(count):
                            shell32.DragQueryFileW(hDrop, i, buf, 1024)
                            if buf.value:
                                paths.append(buf.value)
                        shell32.DragFinish(hDrop)
                        if paths:
                            self._drop_queue.put(paths)
                        return 0
                    return comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)

                self._subclass_ref = SUBCLASSPROC(wnd_proc)
                comctl32.SetWindowSubclass(top_hwnd, self._subclass_ref, 201, 0)
                comctl32.SetWindowSubclass(canvas_hwnd, self._subclass_ref, 202, 0)
        except Exception as e:
            print(f"[QuickAccess] Drop setup note: {e}")

    def _handle_dropped_files(self, paths):
        import icon_extractor
        items = list(self.get_cfg("items", []))
        changed = False
        for p in paths:
            if not p:
                continue
            resolved = icon_extractor.resolve_target_and_icon(p)
            items.append({
                "id": str(uuid.uuid4())[:8],
                "type": "app",
                "name": resolved["name"],
                "target": resolved["target"],
                "icon_path": resolved["icon_path"]
            })
            changed = True
        if changed:
            self.set_cfg("items", items)
            self._render()

    def _add_item_dialog(self):
        fpath = filedialog.askopenfilename(title="Añadir Acceso Rápido al Widget", parent=self.window)
        if fpath:
            self._handle_dropped_files([fpath])

    def _set_mode(self, mode):
        self.set_cfg("mode", mode)
        self._render()

    def _remove_item_at(self, idx):
        items = list(self.get_cfg("items", []))
        if 0 <= idx < len(items):
            items.pop(idx)
            self.set_cfg("items", items)
            self.hovered_item = -1
            self._render()

    def _clear_all_items(self):
        self.set_cfg("items", [])
        self.hovered_item = -1
        self._render()

    def _on_context_menu(self, event):
        from custom_menu import CustomMenu
        menu = CustomMenu(self.window)
        cur_mode = self.get_cfg("mode", "hover_expand")
        items = self.get_cfg("items", [])
        hit_idx = -1
        for i, r in enumerate(self.item_boxes):
            if r[0] <= event.x <= r[2] and r[1] <= event.y <= r[3]:
                hit_idx = i
                break

        menu.add_header("Accesos Rápidos")
        if 0 <= hit_idx < len(items):
            it_name = items[hit_idx].get("name", "Elemento")[:24]
            menu.add_command(f"Abrir {it_name}", lambda it=items[hit_idx]: _launch_target_item(it.get("target", ""), it.get("args", ""), it.get("url", "")))
            menu.add_command(f"Eliminar '{it_name}'", lambda i=hit_idx: self._remove_item_at(i), danger=True)
            menu.add_separator()
        menu.add_command("+ Añadir Archivo o Acceso...", self._add_item_dialog)
        if items:
            menu.add_command("Vaciar todos los accesos", self._clear_all_items, danger=True)
        menu.add_separator()
        menu.add_header("Modo de Interacción")
        menu.add_command("Estático (Siempre visible)", lambda: self._set_mode("static"), checked=(cur_mode == "static"))
        menu.add_command("Abrir al pasar el mouse", lambda: self._set_mode("hover_open"), checked=(cur_mode == "hover_open"))
        menu.add_command("Expandir al pasar el mouse", lambda: self._set_mode("hover_expand"), checked=(cur_mode == "hover_expand"))
        menu.add_separator()
        self._append_common_menu_items(menu)
        menu.show(event.x_root, event.y_root)

    def _on_drag_start(self, event):
        if self._is_in_resize_zone(event.x, event.y):
            self._drag_item_idx = None
            self._reorder_target_idx = None
            super()._on_drag_start(event)
            return
        self._drag_item_idx = None
        self._reorder_target_idx = None
        items = self.get_cfg("items", [])
        if len(items) >= 2:
            for i, r in enumerate(self.item_boxes):
                if r[0] <= event.x <= r[2] and r[1] <= event.y <= r[3]:
                    self._drag_item_idx = i
                    self.drag_start_x = event.x
                    self.drag_start_y = event.y
                    self.is_dragging = True
                    return
        super()._on_drag_start(event)

    def _on_drag_move(self, event):
        if self._drag_item_idx is not None and not self.is_resizing:
            # If cursor leaves widget vertically/far away, fall back to moving widget window
            if event.x < -25 or event.x > self.win_w + 25 or event.y < -25 or event.y > self.win_h + 25:
                self._drag_item_idx = None
                self._reorder_target_idx = None
                self._render()
                super()._on_drag_move(event)
                return
            dx = abs(event.x - self.drag_start_x)
            dy = abs(event.y - self.drag_start_y)
            if dx > 6 or dy > 6:
                tgt = len(self.item_boxes)
                for i, r in enumerate(self.item_boxes):
                    if event.x < (r[0] + r[2]) / 2.0 and event.y <= r[3] + 10:
                        tgt = i
                        break
                if tgt != self._reorder_target_idx:
                    self._reorder_target_idx = tgt
                    self._render()
            return
        super()._on_drag_move(event)

    def _on_motion(self, event):
        old_h = self.hovered_item
        self.hovered_item = -1
        for i, r in enumerate(self.item_boxes):
            if r[0] <= event.x <= r[2] and r[1] <= event.y <= r[3]:
                self.hovered_item = i
                break

        mode = self.get_cfg("mode", "hover_expand")
        if mode in ("hover_expand", "hover_open") and not self.is_expanded:
            self.is_expanded = True
            self._render()
        elif old_h != self.hovered_item:
            self._render()

    def _on_leave(self, event):
        self.hovered_item = -1
        mode = self.get_cfg("mode", "hover_expand")
        if mode in ("hover_expand", "hover_open") and self.is_expanded and not self._ext_drag_hover:
            self.is_expanded = False
        self._render()

    def _on_drag_end(self, event):
        if self.is_resizing:
            self._finish_resize()
            return
        if self._drag_item_idx is not None:
            src = self._drag_item_idx
            tgt = self._reorder_target_idx
            self._drag_item_idx = None
            self._reorder_target_idx = None
            self.is_dragging = False
            dx = abs(event.x - self.drag_start_x)
            dy = abs(event.y - self.drag_start_y)
            items = list(self.get_cfg("items", []))
            if (dx >= 6 or dy >= 6) and tgt is not None and 0 <= src < len(items):
                moved = items.pop(src)
                ins = tgt - 1 if tgt > src else tgt
                ins = max(0, min(len(items), ins))
                items.insert(ins, moved)
                self.set_cfg("items", items)
                self._render()
                return
            if dx < 6 and dy < 6 and 0 <= src < len(items):
                it = items[src]
                try:
                    _launch_target_item(it.get("target", ""), it.get("args", ""), it.get("url", ""))
                except Exception as e:
                    print(f"[QuickAccess] Launch error: {e}")
            self._render()
            return

        if not self.is_dragging:
            return
        dx = abs(event.x - self.drag_start_x)
        dy = abs(event.y - self.drag_start_y)
        self.is_dragging = False

        if dx < 5 and dy < 5 and self.hovered_item >= 0:
            items = self.get_cfg("items", [])
            if 0 <= self.hovered_item < len(items):
                it = items[self.hovered_item]
                try:
                    _launch_target_item(it.get("target", ""), it.get("args", ""), it.get("url", ""))
                except Exception as e:
                    print(f"[QuickAccess] Launch error: {e}")
            return

        if self.window and self.window.winfo_exists():
            x = self.window.winfo_x()
            y = self.window.winfo_y()
            self.set_cfg("x", x)
            self.set_cfg("y", y)
            self._apply_z_order()

    def _render(self):
        if not self.window or not self.canvas:
            return

        items = self.get_cfg("items", [])
        if self._is_collapsed_view():
            self.item_boxes = []
            self._render_collapsed_pill("Accesos Rápidos", f"{len(items)} elementos")
            return

        s = self._get_effective_scale()
        sp = self._get_spacing_mult()
        mode = self.get_cfg("mode", "hover_expand")

        collapsed = (mode == "hover_open" and not self.is_expanded and not self.is_resizing and not self._ext_drag_hover)
        size = int((46 if (mode == "hover_expand" and self.is_expanded) else 40) * s)
        spacing = max(4, int(12 * s * sp))
        pad_x = int(16 * s)
        cw = self.get_cfg("custom_w", None)
        ch = self.get_cfg("custom_h", None)

        if collapsed:
            w = int(155 * s)
            h = int(46 * s)
        elif cw is not None and ch is not None:
            w = max(self._min_w(), int(cw))
            cols = max(1, (w - pad_x * 2 + spacing) // (size + spacing))
            rows = max(1, math.ceil(max(1, len(items)) / cols))
            need_h = pad_x + rows * size + max(0, rows - 1) * spacing + pad_x
            h = max(self._min_h(), int(ch), need_h if not self.is_resizing else int(ch))
        else:
            content_w = max(1, len(items)) * size + max(0, len(items) - 1) * spacing
            w = max(int(180 * s), content_w + pad_x * 2)
            h = int(74 * s)

        self._set_window_size_live(w, h)
        composite = self._build_card_surface(w, h)
        draw = ImageDraw.Draw(composite, "RGBA")

        self.item_boxes = []
        if collapsed:
            f_lbl = self._get_styled_font(11 * s, bold_default=True)
            draw.text((w / 2.0, h / 2.0), f"Accesos ({len(items)})", fill=(226, 232, 240, 255), font=f_lbl, anchor="mm")
        else:
            if not hasattr(self, "_qa_icon_cache"):
                self._qa_icon_cache = {}
            cols = max(1, (w - pad_x * 2 + spacing) // (size + spacing))
            total_rows = max(1, math.ceil(max(1, len(items)) / cols)) if items else 1
            total_block_h = total_rows * size + max(0, total_rows - 1) * spacing
            start_y = max(int(10 * s), (h - total_block_h) // 2)
            import icon_extractor
            for i, it in enumerate(items):
                row = i // cols
                col = i % cols
                bx1 = pad_x + col * (size + spacing)
                by1 = start_y + row * (size + spacing)
                bx2 = bx1 + size
                by2 = by1 + size
                self.item_boxes.append((bx1, by1, bx2, by2))

                if self.hovered_item == i:
                    alpha_rounded_rect(composite, [bx1 - 3, by1 - 3, bx2 + 3, by2 + 3], radius=int(10 * s), fill=(59, 130, 246, 85), outline=(96, 165, 250, 205), width=1)

                ik = (it.get("id") or it.get("target", ""), size)
                resized = self._qa_icon_cache.get(ik)
                if resized is None:
                    ic = icon_extractor.get_or_create_icon(it, size=max(48, size))
                    if ic:
                        if len(self._qa_icon_cache) >= 20:
                            self._qa_icon_cache.clear()
                        resized = ic.resize((size, size), Image.Resampling.LANCZOS)
                        self._qa_icon_cache[ik] = resized
                if resized is not None:
                    composite.alpha_composite(resized, (bx1, by1))

            if self._reorder_target_idx is not None and self.item_boxes:
                if self._reorder_target_idx < len(self.item_boxes):
                    r_box = self.item_boxes[self._reorder_target_idx]
                    lx = r_box[0] - (spacing // 2)
                    ly1, ly2 = r_box[1], r_box[3]
                else:
                    r_box = self.item_boxes[-1]
                    lx = r_box[2] + (spacing // 2)
                    ly1, ly2 = r_box[1], r_box[3]
                draw.line([(lx, ly1), (lx, ly2)], fill=(96, 165, 250, 255), width=max(2, int(3 * s)))

            if not items:
                f_empty = self._get_styled_font(10.5 * s, bold_default=False)
                draw.text((w / 2.0, h / 2.0), "Arrastra archivos aquí", fill=(148, 163, 184, 220), font=f_empty, anchor="mm")

        self._render_to_canvas(composite, w, h)


class PwaWidget(BaseDesktopWidget):
    """Floating launcher for selected installed Chrome/Edge PWAs and custom Web Apps."""
    def __init__(self, root, config_mgr, on_open_settings=None):
        super().__init__(root, config_mgr, "pwa_apps", default_x=320, default_y=165, default_w=260, default_h=74, on_open_settings=on_open_settings)
        self.app_boxes = []
        self.hovered_app = -1
        self._icon_cache = OrderedDict()

    def show(self):
        # On first enable, if user hasn't customized beyond factory placeholder URLs, auto-populate detected PWAs if any
        apps = self.get_cfg("apps", [])
        if not apps:
            try:
                import app_search
                detected = app_search.get_installed_pwas()
                if detected:
                    self.set_cfg("apps", detected[:6])
            except Exception:
                pass
        super().show()
        if self.canvas:
            self.canvas.bind("<Motion>", self._on_motion, add="+")
            self.canvas.bind("<Leave>", self._on_leave, add="+")

    def _start_update_loop(self):
        self._render()

    def _add_pwa_dialog(self):
        from window_utils import ask_styled_text
        name = ask_styled_text("Añadir Web App", "Nombre de la aplicación:", parent=self.window)
        if not name or not name.strip():
            return
        url = ask_styled_text("Añadir Web App", "URL (ej. https://claude.ai):", parent=self.window)
        if url and url.strip():
            apps = list(self.get_cfg("apps", []))
            apps.append({"id": str(uuid.uuid4())[:8], "name": name.strip(), "url": url.strip()})
            self.set_cfg("apps", apps)
            self._render()

    def _toggle_installed_pwa(self, pwa):
        apps = list(self.get_cfg("apps", []))
        pid = pwa.get("id")
        ptgt = (pwa.get("target", "") + pwa.get("args", "")).lower()
        existing_idx = next(
            (i for i, a in enumerate(apps) if a.get("id") == pid or (ptgt and (a.get("target", "") + a.get("args", "")).lower() == ptgt)),
            None
        )
        if existing_idx is not None:
            apps.pop(existing_idx)
        else:
            apps.append(pwa)
        self.set_cfg("apps", apps)
        self._icon_cache.clear()
        self._render()

    def _on_context_menu(self, event):
        from custom_menu import CustomMenu
        menu = CustomMenu(self.window)
        menu.add_header("Aplicaciones Web / PWA")
        try:
            import app_search
            installed = app_search.get_installed_pwas()
            cur_apps = self.get_cfg("apps", [])
            cur_keys = {a.get("id") for a in cur_apps} | {(a.get("target", "") + a.get("args", "")).lower() for a in cur_apps if a.get("target")}
            for pwa in installed[:8]:
                is_on = (pwa["id"] in cur_keys) or ((pwa["target"] + pwa.get("args", "")).lower() in cur_keys)
                menu.add_command(pwa["name"][:32], lambda p=pwa: self._toggle_installed_pwa(p), checked=is_on)
            if installed:
                menu.add_separator()
        except Exception:
            pass
        menu.add_command("+ Añadir Web App (URL)...", self._add_pwa_dialog)
        menu.add_separator()
        self._append_common_menu_items(menu)
        menu.show(event.x_root, event.y_root)

    def _on_motion(self, event):
        old_h = self.hovered_app
        self.hovered_app = -1
        for i, r in enumerate(self.app_boxes):
            if r[0] <= event.x <= r[2] and r[1] <= event.y <= r[3]:
                self.hovered_app = i
                break
        if old_h != self.hovered_app:
            self._render()

    def _on_leave(self, event):
        self.hovered_app = -1
        self._render()

    def _on_drag_end(self, event):
        if self.is_resizing:
            self._finish_resize()
            return
        if not self.is_dragging:
            return
        dx = abs(event.x - self.drag_start_x)
        dy = abs(event.y - self.drag_start_y)
        self.is_dragging = False

        if dx < 5 and dy < 5 and self.hovered_app >= 0:
            apps = self.get_cfg("apps", [])
            if 0 <= self.hovered_app < len(apps):
                app = apps[self.hovered_app]
                try:
                    _launch_target_item(app.get("target", ""), app.get("args", ""), app.get("url", ""))
                except Exception as e:
                    print(f"[PwaWidget] Launch error: {e}")
            return

        if self.window and self.window.winfo_exists():
            x = self.window.winfo_x()
            y = self.window.winfo_y()
            self.set_cfg("x", x)
            self.set_cfg("y", y)
            self._apply_z_order()

    def _get_pwa_icon(self, app, size=64):
        src = app.get('icon_src') or app.get('icon_path') or ''
        mtime = os.stat(src).st_mtime_ns if src and os.path.isfile(src) else 0
        key = (app.get('id'), src, mtime, app.get('target'), app.get('args'), size)
        if key in self._icon_cache:
            self._icon_cache.move_to_end(key)
            return self._icon_cache[key]
        target = app.get("target") or ""
        args = app.get("args") or ""
        ic = None
        if src and (src.lower().startswith("shell:") or os.path.exists(src)):
            try:
                if src.lower().endswith((".png", ".ico")) and os.path.exists(src):
                    with Image.open(src) as raw:
                        if raw.mode in ("P", "1"):
                            raw = raw.convert("RGBA")
                        ic = raw.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
                else:
                    import icon_extractor
                    ic = icon_extractor.get_or_create_icon({
                        "type": "app",
                        "name": app.get("name", "PWA"),
                        "target": src if src.lower().startswith("shell:") else (target or src),
                        "args": args,
                        "icon_path": ""
                    }, size=64)
            except Exception:
                ic = None
        elif target.lower() in ("explorer.exe", "explorer") and args.lower().startswith("shell:"):
            try:
                import icon_extractor
                ic = icon_extractor.get_or_create_icon({
                    "type": "app",
                    "name": app.get("name", "PWA"),
                    "target": target,
                    "args": args,
                    "icon_path": ""
                }, size=64)
            except Exception:
                ic = None
        if ic is not None and ic.size != (size, size):
            ic = ic.resize((size, size), Image.Resampling.LANCZOS)
        while len(self._icon_cache) >= 32:
            self._icon_cache.popitem(last=False)
        self._icon_cache[key] = ic
        return ic

    def _render(self):
        if not self.window or not self.canvas:
            return

        apps = self.get_cfg("apps", [])
        if self._is_collapsed_view():
            self.app_boxes = []
            self._render_collapsed_pill("Web Apps / PWA", f"{len(apps)} apps")
            return

        s = self._get_effective_scale()
        sp = self._get_spacing_mult()
        num_apps = max(1, len(apps))
        app_w = int(44 * s)
        spacing = max(4, int(12 * s * sp))
        pad_x = int(16 * s)
        cw = self.get_cfg("custom_w", None)
        ch = self.get_cfg("custom_h", None)
        if cw is not None and ch is not None:
            w = max(self._min_w(), int(cw))
            cols = max(1, (w - pad_x * 2 + spacing) // (app_w + spacing))
            rows = max(1, math.ceil(num_apps / cols))
            need_h = pad_x + rows * app_w + max(0, rows - 1) * spacing + pad_x
            h = max(self._min_h(), int(ch), need_h if not self.is_resizing else int(ch))
        else:
            w = max(int(210 * s), num_apps * app_w + max(0, num_apps - 1) * spacing + pad_x * 2)
            h = int(74 * s)

        self._set_window_size_live(w, h)
        composite = self._build_card_surface(w, h)
        draw = ImageDraw.Draw(composite, "RGBA")

        self.app_boxes = []
        cols = max(1, (w - pad_x * 2 + spacing) // (app_w + spacing))
        total_rows = max(1, math.ceil(num_apps / cols)) if apps else 1
        row_items_max = min(len(apps), cols) if apps else 1
        actual_pad = max(pad_x, (w - (row_items_max * app_w + max(0, row_items_max - 1) * spacing)) // 2)
        total_block_h = total_rows * app_w + max(0, total_rows - 1) * spacing
        app_start_y = max(int(10 * s), (h - total_block_h) // 2)

        f_pwa = self._get_styled_font(11 * s, bold_default=True)

        if not apps:
            draw.text((w / 2.0, h / 2.0), "Sin PWAs activas", fill=(148, 163, 184, 220), font=f_pwa, anchor="mm")

        for i, app in enumerate(apps):
            row = i // cols
            col = i % cols
            bx1 = actual_pad + col * (app_w + spacing)
            by1 = app_start_y + row * (app_w + spacing)
            bx2 = bx1 + app_w
            by2 = by1 + app_w
            self.app_boxes.append((bx1, by1, bx2, by2))

            if self.hovered_app == i:
                alpha_rounded_rect(composite, [bx1 - 3, by1 - 3, bx2 + 3, by2 + 3], radius=int(11 * s), fill=(59, 130, 246, 90), outline=(96, 165, 250, 215), width=1)

            ic_sz = max(20, app_w - int(4 * s))
            ic = self._get_pwa_icon(app, ic_sz)
            if ic is not None:
                composite.alpha_composite(ic, (bx1 + (app_w - ic_sz) // 2, by1 + (app_w - ic_sz) // 2))
            else:
                draw.rounded_rectangle([bx1 + 2, by1 + 2, bx2 - 2, by2 - 2], radius=int(9 * s), fill=(30, 41, 59, 240), outline=(255, 255, 255, 40), width=1)
                name = app.get("name", "PWA")
                short = name[:3].upper()
                draw.text(((bx1 + bx2) / 2.0, (by1 + by2) / 2.0), short, fill=(255, 255, 255, 255), font=f_pwa, anchor="mm")

        self._render_to_canvas(composite, w, h)


class DesktopMiniDockWidget(BaseDesktopWidget):
    """Floating desktop quick-actions dock with interactive shortcuts."""
    def __init__(self, root, config_mgr, on_open_settings=None):
        super().__init__(root, config_mgr, "desktop_minidock", default_x=24, default_y=585, default_w=290, default_h=68, on_open_settings=on_open_settings)
        self.buttons = [
            {"id": "show_desktop", "label": "Escritorio", "icon": "show_desktop"},
            {"id": "lock_pc", "label": "Bloquear", "icon": "lock_pc"},
            {"id": "taskmgr", "label": "Rendimiento", "icon": "taskmgr"},
            {"id": "recycle_bin", "label": "Papelera", "icon": "recycle_bin"},
            {"id": "snipping_tool", "label": "Recortes", "icon": "snipping_tool"}
        ]
        self.btn_rects = []
        self.hovered_btn = -1
        self.action_icons = {}

    def show(self):
        self._load_icons()
        super().show()
        if self.canvas:
            self.canvas.bind("<Motion>", self._on_motion, add="+")
            self.canvas.bind("<Leave>", self._on_leave, add="+")

    def _load_icons(self):
        import icon_extractor
        for b in self.buttons:
            self.action_icons[b["id"]] = icon_extractor.create_action_icon(b["icon"], size=64)

    def _start_update_loop(self):
        self._render()

    def _on_motion(self, event):
        old_h = self.hovered_btn
        self.hovered_btn = -1
        for i, r in enumerate(self.btn_rects):
            if r[0] <= event.x <= r[2] and r[1] <= event.y <= r[3]:
                self.hovered_btn = i
                break
        if old_h != self.hovered_btn:
            self._render()

    def _on_leave(self, event):
        self.hovered_btn = -1
        self._render()

    def _on_drag_end(self, event):
        if self.is_resizing:
            self._finish_resize()
            return
        if not self.is_dragging:
            return
        dx = abs(event.x - self.drag_start_x)
        dy = abs(event.y - self.drag_start_y)
        self.is_dragging = False

        if dx < 5 and dy < 5 and self.hovered_btn >= 0:
            btn = self.buttons[self.hovered_btn]
            self._execute_btn(btn["id"])
            return

        if self.window and self.window.winfo_exists():
            x = self.window.winfo_x()
            y = self.window.winfo_y()
            self.set_cfg("x", x)
            self.set_cfg("y", y)
            self._apply_z_order()

    def _execute_btn(self, act_id):
        try:
            if act_id == "show_desktop":
                try:
                    import pythoncom
                    import win32com.client
                    try:
                        pythoncom.CoInitialize()
                    except Exception:
                        pass
                    win32com.client.Dispatch("Shell.Application").ToggleDesktop()
                except Exception:
                    try:
                        VK_LWIN = 0x5B
                        VK_D = 0x44
                        KEYEVENTF_KEYUP = 0x0002
                        ctypes.windll.user32.keybd_event(VK_LWIN, 0, 0, 0)
                        ctypes.windll.user32.keybd_event(VK_D, 0, 0, 0)
                        ctypes.windll.user32.keybd_event(VK_D, 0, KEYEVENTF_KEYUP, 0)
                        ctypes.windll.user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
                    except Exception:
                        pass
            elif act_id == "lock_pc":
                ctypes.windll.user32.LockWorkStation()
            elif act_id == "taskmgr":
                subprocess.Popen(["taskmgr.exe"])
            elif act_id == "recycle_bin":
                subprocess.Popen(["explorer.exe", "::{645FF040-5081-101B-9F08-00AA002F954E}"])
            elif act_id == "snipping_tool":
                try:
                    os.startfile("ms-screenclip:")
                except Exception:
                    subprocess.Popen(["snippingtool.exe"])
        except Exception as e:
            print(f"[MiniDock] Error executing action {act_id}: {e}")

    def _render(self):
        if not self.window or not self.canvas:
            return

        if self._is_collapsed_view():
            self.btn_rects = []
            self._render_collapsed_pill("Mini-Dock", f"{len(self.buttons)} acciones")
            return

        if not self.is_resizing:
            fw, fh = self._compute_full_dims()
            self._set_window_size_live(fw, fh)

        w, h = self.win_w, self.win_h
        s = min(w / 290.0, h / 68.0) if self.get_cfg("custom_w") else self._get_effective_scale()
        composite = self._build_card_surface(w, h)
        draw = ImageDraw.Draw(composite, "RGBA")

        num_btns = len(self.buttons)
        sp = self._get_spacing_mult()
        btn_w = int(46 * s)
        btn_h = int(46 * s)
        ic_size = int(32 * s)
        base_gap = max(4, (w - (num_btns * btn_w)) // (num_btns + 1))
        gap = max(3, min(max(4, (w - num_btns * btn_w - 8) // max(1, num_btns - 1)), int(base_gap * sp)))
        total_row_w = num_btns * btn_w + (num_btns - 1) * gap
        cur_x = max(4, (w - total_row_w) // 2)
        btn_y = (h - btn_h) // 2

        self.btn_rects = []

        for i, b in enumerate(self.buttons):
            bx1 = cur_x
            by1 = btn_y
            bx2 = bx1 + btn_w
            by2 = by1 + btn_h
            self.btn_rects.append((bx1, by1, bx2, by2))

            if self.hovered_btn == i:
                alpha_rounded_rect(composite, [bx1, by1, bx2, by2], radius=int(10 * s), fill=(59, 130, 246, 85), outline=(96, 165, 250, 205), width=1)

            ic = self.action_icons.get(b["id"])
            if ic:
                resized = ic.resize((ic_size, ic_size), Image.Resampling.LANCZOS)
                composite.alpha_composite(resized, (bx1 + (btn_w - ic_size) // 2, by1 + (btn_h - ic_size) // 2))

            cur_x += btn_w + gap

        self._render_to_canvas(composite, w, h)


class BatteryWidget(BaseDesktopWidget):
    """Desktop Battery Telemetry Widget with 8 visual styles, frosted-blurred spent track, and low RAM polling."""

    STYLE_LABELS = [
        ("anillo", "Anillo Moderno"),
        ("arco", "Velocímetro 180°"),
        ("barra", "Batería Horizontal"),
        ("segmentado", "Celdas Segmentadas"),
        ("vertical", "Pila Vertical"),
        ("neon", "Neón Energía"),
        ("pildora", "Píldora Compacta"),
        ("tarjeta", "Tarjeta Detallada"),
    ]

    def __init__(self, root, config_mgr, on_open_settings=None):
        super().__init__(root, config_mgr, "battery", default_x=320, default_y=50, default_w=246, default_h=102, on_open_settings=on_open_settings)
        self._last_bat_sig = None
        self._pct_hit_rect = (0, 0, 0, 0)
        self._press_x = 0
        self._press_y = 0
        self._press_moved = False
        self._pressed_on_pct = False

    def _open_power_settings(self):
        for uri in ("ms-settings:powersleep", "ms-settings:batterysaver"):
            try:
                os.startfile(uri)
                return
            except Exception:
                pass
        try:
            subprocess.Popen(["control.exe", "powercfg.cpl"])
        except Exception:
            pass

    def _hit_pct_area(self, x, y):
        r = getattr(self, "_pct_hit_rect", (0, 0, 0, 0))
        return r[2] > r[0] and r[0] <= x <= r[2] and r[1] <= y <= r[3]

    def _on_base_motion(self, event):
        if self.is_resizing or self.is_dragging:
            return
        rdir = self._detect_resize_dir(event.x, event.y)
        if rdir:
            self._set_cursor(self._RESIZE_CURSORS.get(rdir, ""))
        elif not self._hit_min_button(event.x, event.y) and self._hit_pct_area(event.x, event.y):
            self._set_cursor("hand2")
        else:
            self._set_cursor("")
        if rdir != self._hover_resize_dir:
            self._hover_resize_dir = rdir
            if not bool(self.get_cfg("lock_position", False)) and not self._is_collapsed_view():
                self._redraw_now()

    def _on_drag_start(self, event):
        self._press_x = event.x_root
        self._press_y = event.y_root
        self._press_moved = False
        self._pressed_on_pct = (not self._hit_min_button(event.x, event.y)) and self._hit_pct_area(event.x, event.y)
        super()._on_drag_start(event)

    def _on_drag_move(self, event):
        if abs(event.x_root - self._press_x) > 5 or abs(event.y_root - self._press_y) > 5:
            self._press_moved = True
        super()._on_drag_move(event)

    def _on_drag_end(self, event):
        was_resizing = self.is_resizing
        should_open_power = (
            self._pressed_on_pct
            and not self._press_moved
            and not was_resizing
            and not self._hit_min_button(event.x, event.y)
            and self._hit_pct_area(event.x, event.y)
        )
        self._pressed_on_pct = False
        super()._on_drag_end(event)
        if should_open_power:
            self._open_power_settings()

    def _fit_text(self, draw, text, base_px, max_w, max_h=None, bold_default=False, min_px=8):
        """
        Resolves styled font scaled down cleanly until `text` fits strictly within `max_w` (and optional `max_h`),
        truncating with '…' only if still wider at `min_px`, so text NEVER overflows the Battery widget.
        """
        fam = self.get_cfg("font_family", None) or "Segoe UI"
        bold_cfg = self.get_cfg("font_bold", None)
        italic_cfg = self.get_cfg("font_italic", None)
        use_bold = bool(bold_cfg) if bold_cfg is not None else bool(bold_default)
        use_italic = bool(italic_cfg) if italic_cfg is not None else False
        start_px = max(min_px, int(round(float(base_px) * self._get_font_scale())))
        max_w = max(16.0, float(max_w))
        chosen_font = None
        for px in range(start_px, min_px - 1, -1):
            f = resolve_styled_font(fam, px, bold=use_bold, italic=use_italic)
            tw = draw.textlength(text, font=f)
            th_ok = True
            if max_h is not None:
                bb = draw.textbbox((0, 0), text, font=f)
                th_ok = (bb[3] - bb[1]) <= max_h
            if tw <= max_w and th_ok:
                chosen_font = f
                return chosen_font, text
            chosen_font = f
        # Fallback: ellipsize cleanly at min_px
        disp = text
        while len(disp) > 1 and draw.textlength(disp + "…", font=chosen_font) > max_w:
            disp = disp[:-1]
        return chosen_font, (disp.rstrip() + "…" if disp != text else disp)

    def _start_update_loop(self):
        self._update_stats()

    def _update_stats(self):
        if not self.window or not self.window.winfo_exists():
            return
        self._render()
        if self.update_job:
            try:
                self.root.after_cancel(self.update_job)
            except Exception:
                pass
        self.update_job = self.root.after(4000, self._update_stats)

    def _composite_frosted_track(self, composite, draw_fn, blur_r=4.5):
        """
        Draws the empty/spent portion of the battery graph with HEAVY real optical frosted-glass blur
        sampled from the desktop wallpaper beneath the widget + smoked acrylic frosting (never see-through!).
        """
        w, h = composite.size
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        draw_fn(md)

        # Build heavy-blurred frosted optical glass fill
        wx = self.window.winfo_x() if (self.window and self.window.winfo_exists()) else int(self.get_cfg("x", self.default_x))
        wy = self.window.winfo_y() if (self.window and self.window.winfo_exists()) else int(self.get_cfg("y", self.default_y))
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        wp_blur = get_blurred_desktop_backdrop(wx, wy, w, h, sw, sh, blur_radius=20)
        if wp_blur is not None:
            # Blend heavily-blurred wallpaper with cool frosted acrylic tint so icons/wallpaper never bleed sharply
            frost_veil = Image.new("RGBA", (w, h), (16, 26, 44, 150))
            frosted_fill = Image.alpha_composite(wp_blur, frost_veil)
            # Soft frosted light sheen on top
            sheen = Image.new("RGBA", (w, h), (226, 232, 240, 38))
            frosted_fill = Image.alpha_composite(frosted_fill, sheen)
        else:
            frosted_fill = Image.new("RGBA", (w, h), (30, 41, 59, 238))

        # Apply soft feathered mask edge + composite frosted glass strictly into the spent track shape
        soft_mask = mask.filter(ImageFilter.GaussianBlur(radius=max(1.2, blur_r * 0.45)))
        glow_layer = Image.new("RGBA", (w, h), (15, 23, 42, 115))
        glow_layer.putalpha(soft_mask)
        composite.alpha_composite(glow_layer)

        # Crisp opaque frosted-blurred track clipped to exact shape
        frosted_fill.putalpha(mask)
        composite.alpha_composite(frosted_fill)

    def _render(self):
        if not self.window or not self.window.winfo_exists():
            return
        bat = get_battery_status()
        has_bat = bat.get("has_battery", False)
        pct = int(bat.get("percent", 100)) if has_bat else 100
        charging = bool(bat.get("charging", False))
        saver = bool(bat.get("saver", False))
        life_min = bat.get("life_minutes")

        style = (self.get_cfg("style", "anillo") or "anillo").lower()
        accent_hex = self.get_cfg("accent_color", "") or ""
        if accent_hex:
            accent_rgb = _hex_to_rgb(accent_hex, (34, 197, 94))
        else:
            if pct <= 15 and not charging:
                accent_rgb = (239, 68, 68)
            elif pct <= 30 and not charging:
                accent_rgb = (245, 158, 11)
            elif charging:
                accent_rgb = (56, 189, 248)
            else:
                accent_rgb = (34, 197, 94)

        if self._is_collapsed_view():
            st_txt = f"{pct}% {'⚡' if charging else ''}".strip() if has_bat else "AC"
            self._render_collapsed_pill("Batería", st_txt, accent_rgb=accent_rgb)
            # In collapsed mode, clicking the pill body (excluding minimize button) opens Power Options
            self._pct_hit_rect = (0, 0, max(0, self.win_w - 32), self.win_h)
            return

        if not self.is_resizing:
            fw, fh = self._compute_full_dims()
            self._set_window_size_live(fw, fh)

        w, h = self.win_w, self.win_h
        s = min(w / 246.0, h / 102.0) if self.get_cfg("custom_w") else self._get_effective_scale()
        w_style = self.get_cfg("widget_style", "cristal") or "cristal"
        show_sub = bool(self.get_cfg("show_status_text", True))
        wx_cur = self.window.winfo_x() if (self.window and self.window.winfo_exists()) else 0
        wy_cur = self.window.winfo_y() if (self.window and self.window.winfo_exists()) else 0
        sig = (
            w, h, wx_cur, wy_cur, has_bat, pct, charging, saver, life_min, style, accent_rgb, show_sub,
            w_style, self.get_cfg("opacity", 0.88), self.get_cfg("bg_color", ""),
            self.get_cfg("bg_image", ""), self.get_cfg("font_family", ""),
            self.get_cfg("font_scale", 1.0), bool(self._hover_inside),
            self._hover_resize_dir, self.is_resizing, bool(self.get_cfg("lock_position", False)),
        )
        if sig == self._last_bat_sig:
            return
        self._last_bat_sig = sig

        composite = self._build_card_surface(w, h, accent_rgb=accent_rgb)
        draw = ImageDraw.Draw(composite, "RGBA")

        pad = max(8, int(12 * s))
        btn_s = max(16, int(18 * s))
        self._draw_min_button(composite, draw, w - pad - btn_s, pad - 2, w - pad, pad - 2 + btn_s, is_collapsed=False)

        if not has_bat:
            st_label = "Corriente AC"
            mode_badge = "AC"
            pct_label = "AC"
        else:
            pct_label = f"{pct}%"
            if charging:
                st_label = "Cargando" if pct < 100 else "Carga completa"
                mode_badge = "⚡ AC"
            elif saver:
                st_label = "Ahorro de energía"
                mode_badge = "Ahorro"
            elif life_min and life_min > 0:
                hrs, mins = divmod(int(life_min), 60)
                st_label = f"{hrs}h {mins:02d}m rest." if hrs > 0 else f"{mins} min rest."
                mode_badge = "En batería"
            else:
                st_label = "En batería"
                mode_badge = "Normal"

        # Frosted dark-slate outline highlight for spent track regions
        frosted_base = 255
        frosted_sheen = 255
        self._pct_hit_rect = (0, 0, 0, 0)

        if style == "barra":
            tx = pad
            bar_x1 = pad + int(84 * s)
            bar_x2 = w - pad - btn_s - int(10 * s)
            left_max_w = max(44, bar_x1 - tx - 6)
            f_t, t_txt = self._fit_text(draw, "BATERÍA", max(9, int(10 * s)), left_max_w, max_h=max(11, int(14 * s)), bold_default=True)
            draw.text((tx, pad), t_txt, fill=(148, 163, 184, 235), font=f_t)
            f_p, p_txt = self._fit_text(draw, pct_label, max(15, int(24 * s)), left_max_w, max_h=max(18, int(30 * s)), bold_default=True)
            pct_y = pad + int(16 * s)
            draw.text((tx, pct_y), p_txt, fill=(248, 250, 252, 255), font=f_p)
            p_bb = draw.textbbox((tx, pct_y), p_txt, font=f_p)
            self._pct_hit_rect = (max(0, p_bb[0] - 6), max(0, p_bb[1] - 4), min(w, p_bb[2] + 6), min(h, p_bb[3] + 4))
            if show_sub:
                f_s, s_txt = self._fit_text(draw, st_label, max(9, int(10.5 * s)), w - pad * 2, max_h=max(11, int(15 * s)), bold_default=False)
                draw.text((tx, h - pad - max(11, int(14 * s))), s_txt, fill=(*accent_rgb, 245), font=f_s)
            bar_h = max(18, int(26 * s))
            bar_y1 = (h - bar_h) // 2 + int(2 * s)
            bar_y2 = bar_y1 + bar_h
            if bar_x2 > bar_x1 + 28:
                self._composite_frosted_track(
                    composite,
                    lambda ld: ld.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=bar_h // 3, fill=frosted_base, outline=frosted_sheen, width=2),
                    blur_r=2.2,
                )
                draw = ImageDraw.Draw(composite, "RGBA")
                fill_w = max(5, int((bar_x2 - bar_x1 - 8) * (pct / 100.0)))
                alpha_rounded_rect(composite, [bar_x1 + 4, bar_y1 + 4, bar_x1 + 4 + fill_w, bar_y2 - 4], radius=max(2, (bar_h - 8) // 3), fill=(*accent_rgb, 245))
                cap_y1 = bar_y1 + bar_h // 4
                cap_y2 = bar_y2 - bar_h // 4
                draw.rounded_rectangle([bar_x2 + 2, cap_y1, bar_x2 + max(4, int(5 * s)), cap_y2], radius=2, fill=(148, 163, 184, 210))

        elif style == "arco":
            # 180° cockpit speedometer gauge with frosted-blurred spent track & single centered %
            arc_d = min(int(w * 0.68), int((h - pad * 1.4) * 1.65))
            arc_d = max(72, arc_d)
            ax1 = (w - btn_s // 2 - arc_d) // 2
            ay1 = pad + int(4 * s)
            ax2 = ax1 + arc_d
            ay2 = ay1 + arc_d
            th = max(8, int(10 * s))
            self._composite_frosted_track(
                composite,
                lambda ld: ld.arc([ax1, ay1, ax2, ay2], start=180, end=360, fill=255, width=th + 2),
                blur_r=4.0,
            )
            draw = ImageDraw.Draw(composite, "RGBA")
            draw.arc([ax1, ay1, ax2, ay2], start=180, end=360, fill=(226, 232, 240, 42), width=th)
            span = int(180 * (pct / 100.0))
            if span > 0:
                draw.arc([ax1, ay1, ax2, ay2], start=180, end=180 + span, fill=(*accent_rgb, 250), width=th)
            cx_a = (ax1 + ax2) / 2.0
            f_p, p_txt = self._fit_text(draw, pct_label, max(15, int(24 * s)), arc_d - th * 2 - 12, max_h=max(16, int(arc_d * 0.34)), bold_default=True)
            draw.text((cx_a, ay1 + arc_d * 0.34), p_txt, fill=(248, 250, 252, 255), font=f_p, anchor="mm")
            self._pct_hit_rect = (ax1, ay1, ax2, min(h, ay1 + arc_d // 2 + 8))
            if show_sub:
                sub_combo = f"{st_label}  •  {mode_badge}"
                f_s, s_txt = self._fit_text(draw, sub_combo, max(9, int(10.5 * s)), w - pad * 2, max_h=max(11, int(15 * s)), bold_default=False)
                draw.text((cx_a, h - pad - int(7 * s)), s_txt, fill=(203, 213, 225, 235), font=f_s, anchor="mm")

        elif style == "segmentado":
            # 10 segmented glowing cells; spent cells use frosted-blurred smoked glass
            hdr_right = w - pad - btn_s - int(8 * s)
            f_t, t_txt = self._fit_text(draw, "BATERÍA", max(9, int(10 * s)), max(48, (hdr_right - pad) * 0.55), max_h=max(12, int(15 * s)), bold_default=True)
            draw.text((pad, pad), t_txt, fill=(148, 163, 184, 235), font=f_t)
            f_p, p_txt = self._fit_text(draw, pct_label, max(12, int(16 * s)), max(36, (hdr_right - pad) * 0.45), max_h=max(14, int(20 * s)), bold_default=True)
            draw.text((hdr_right, pad + int(7 * s)), p_txt, fill=(248, 250, 252, 255), font=f_p, anchor="rm")
            pw_s = int(draw.textlength(p_txt, font=f_p))
            self._pct_hit_rect = (max(0, hdr_right - pw_s - 8), max(0, pad - 4), min(w, hdr_right + 6), min(h, pad + int(22 * s)))
            seg_y1 = pad + int(26 * s)
            seg_y2 = h - pad - (int(19 * s) if show_sub else int(4 * s))
            seg_y2 = max(seg_y1 + 12, seg_y2)
            n_segs = 10
            gap = max(3, int(4 * s))
            total_w = w - pad * 2
            seg_w = max(6, (total_w - gap * (n_segs - 1)) // n_segs)
            lit = int(round(pct / 10.0))
            self._composite_frosted_track(
                composite,
                lambda ld: [
                    ld.rounded_rectangle(
                        [pad + i * (seg_w + gap), seg_y1, pad + i * (seg_w + gap) + seg_w, seg_y2],
                        radius=max(3, int(4 * s)), fill=frosted_base, outline=frosted_sheen, width=1
                    )
                    for i in range(n_segs)
                ],
                blur_r=1.8,
            )
            draw = ImageDraw.Draw(composite, "RGBA")
            for i in range(lit):
                sx1 = pad + i * (seg_w + gap)
                alpha_rounded_rect(composite, [sx1, seg_y1, sx1 + seg_w, seg_y2], radius=max(3, int(4 * s)), fill=(*accent_rgb, 240), outline=(255, 255, 255, 95), width=1)
            if show_sub:
                f_b, b_txt = self._fit_text(draw, mode_badge, max(8, int(9 * s)), max(36, (w - pad * 2) * 0.36), max_h=max(11, int(14 * s)), bold_default=True)
                bw_b = draw.textlength(b_txt, font=f_b)
                f_s, s_txt = self._fit_text(draw, st_label, max(9, int(10.5 * s)), max(40, w - pad * 2 - bw_b - 8), max_h=max(11, int(14 * s)), bold_default=False)
                draw.text((pad, h - pad - int(11 * s)), s_txt, fill=(203, 213, 225, 235), font=f_s)
                draw.text((w - pad, h - pad - int(11 * s)), b_txt, fill=(*accent_rgb, 240), font=f_b, anchor="rt")

        elif style == "vertical":
            # Tall vertical liquid battery capsule on left with frosted-blurred empty top + clean % right
            vw = max(26, int(36 * s))
            vh = max(44, h - pad * 2 - int(4 * s))
            vx1, vy1 = pad + int(4 * s), (h - vh) // 2 + int(2 * s)
            vx2, vy2 = vx1 + vw, vy1 + vh
            cap_w = vw // 2
            draw.rounded_rectangle([vx1 + (vw - cap_w) // 2, vy1 - max(4, int(5 * s)), vx1 + (vw + cap_w) // 2, vy1 + 1], radius=2, fill=(148, 163, 184, 215))
            self._composite_frosted_track(
                composite,
                lambda ld: ld.rounded_rectangle([vx1, vy1, vx2, vy2], radius=max(6, int(9 * s)), fill=frosted_base, outline=frosted_sheen, width=2),
                blur_r=2.0,
            )
            draw = ImageDraw.Draw(composite, "RGBA")
            inner_h = max(4, vh - 8)
            fill_h = max(4, int(inner_h * (pct / 100.0)))
            alpha_rounded_rect(composite, [vx1 + 4, vy2 - 4 - fill_h, vx2 - 4, vy2 - 4], radius=max(3, int(5 * s)), fill=(*accent_rgb, 240))
            tx = vx2 + max(10, int(14 * s))
            max_tx_w = max(36, w - pad - tx)
            title_max_w = max(30, (w - pad - btn_s - 6) - tx)
            f_t, t_txt = self._fit_text(draw, "BATERÍA", max(9, int(10 * s)), title_max_w, max_h=max(11, int(14 * s)), bold_default=True)
            draw.text((tx, pad + int(1 * s)), t_txt, fill=(148, 163, 184, 235), font=f_t)
            f_p, p_txt = self._fit_text(draw, pct_label, max(15, int(23 * s)), max_tx_w, max_h=max(18, int(28 * s)), bold_default=True)
            pct_y = pad + int(16 * s)
            draw.text((tx, pct_y), p_txt, fill=(248, 250, 252, 255), font=f_p)
            p_bb = draw.textbbox((tx, pct_y), p_txt, font=f_p)
            self._pct_hit_rect = (min(vx1, p_bb[0] - 4), min(vy1, p_bb[1] - 4), max(vx2, p_bb[2] + 6), max(vy2, p_bb[3] + 4))
            if show_sub:
                f_s, s_txt = self._fit_text(draw, st_label, max(9, int(10.5 * s)), max_tx_w, max_h=max(11, int(15 * s)), bold_default=False)
                draw.text((tx, h - pad - max(11, int(14 * s))), s_txt, fill=(203, 213, 225, 235), font=f_s)

        elif style == "pildora":
            bar_h = max(26, int(38 * s))
            bar_y1 = (h - bar_h) // 2 + int(4 * s)
            bar_y2 = bar_y1 + bar_h
            bar_x1 = pad
            bar_x2 = w - pad - btn_s - int(8 * s)
            self._composite_frosted_track(
                composite,
                lambda ld: ld.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=bar_h // 2, fill=255),
                blur_r=4.2,
            )
            draw = ImageDraw.Draw(composite, "RGBA")
            draw.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=bar_h // 2, outline=(*accent_rgb, 175), width=2)
            fill_w = max(bar_h - 8, int((bar_x2 - bar_x1 - 8) * (pct / 100.0)))
            alpha_rounded_rect(composite, [bar_x1 + 4, bar_y1 + 4, bar_x1 + 4 + fill_w, bar_y2 - 4], radius=(bar_h - 8) // 2, fill=(*accent_rgb, 215))
            inner_w = max(36, bar_x2 - bar_x1 - int(24 * s))
            f_p, p_txt = self._fit_text(draw, pct_label, max(12, int(16 * s)), inner_w * 0.44, max_h=bar_h - 8, bold_default=True)
            draw.text((bar_x1 + int(12 * s), (bar_y1 + bar_y2) // 2), p_txt, fill=(255, 255, 255, 255), font=f_p, anchor="lm")
            self._pct_hit_rect = (bar_x1, bar_y1, bar_x2, bar_y2)
            if show_sub and (bar_x2 - bar_x1) > 105:
                pw_p = draw.textlength(p_txt, font=f_p)
                rem_w = max(28, inner_w - pw_p - 10)
                f_s, s_txt = self._fit_text(draw, st_label, max(9, int(10.5 * s)), rem_w, max_h=bar_h - 10, bold_default=False)
                draw.text((bar_x2 - int(10 * s), (bar_y1 + bar_y2) // 2), s_txt, fill=(241, 245, 249, 240), font=f_s, anchor="rm")

        elif style == "tarjeta":
            # Executive card: title + single % + full-width frosted progress bar + status footer
            title_max_w = max(60, w - pad * 2 - btn_s - 8)
            f_t, t_txt = self._fit_text(draw, "ENERGÍA DEL EQUIPO", max(9, int(10 * s)), title_max_w, max_h=max(11, int(14 * s)), bold_default=True)
            draw.text((pad, pad), t_txt, fill=(148, 163, 184, 235), font=f_t)
            bx2_b = w - pad - btn_s - int(8 * s)
            f_b, b_txt = self._fit_text(draw, mode_badge, max(8, int(9 * s)), max(34, (w - pad * 2) * 0.36), max_h=max(11, int(14 * s)), bold_default=True)
            bw = int(draw.textlength(b_txt, font=f_b)) + int(14 * s)
            pct_max_w = max(44, (bx2_b - bw - 8) - pad) if (bx2_b - bw > pad + int(56 * s)) else (w - pad * 2)
            f_p, p_txt = self._fit_text(draw, pct_label, max(15, int(23 * s)), pct_max_w, max_h=max(18, int(28 * s)), bold_default=True)
            pct_y = pad + int(15 * s)
            draw.text((pad, pct_y), p_txt, fill=(248, 250, 252, 255), font=f_p)
            p_bb = draw.textbbox((pad, pct_y), p_txt, font=f_p)
            self._pct_hit_rect = (max(0, p_bb[0] - 6), max(0, p_bb[1] - 4), min(w, p_bb[2] + 8), min(h, p_bb[3] + 4))
            if bx2_b - bw > pad + int(56 * s):
                alpha_rounded_rect(composite, [bx2_b - bw, pad + int(20 * s), bx2_b, pad + int(38 * s)], radius=int(8 * s), fill=(*accent_rgb, 55), outline=(*accent_rgb, 190), width=1)
                draw.text((bx2_b - bw / 2.0, pad + int(29 * s)), b_txt, fill=(248, 250, 252, 250), font=f_b, anchor="mm")
            bar_h = max(10, int(12 * s))
            bar_y1 = h - pad - (int(22 * s) if show_sub else int(12 * s))
            bar_y2 = bar_y1 + bar_h
            self._composite_frosted_track(
                composite,
                lambda ld: ld.rounded_rectangle([pad, bar_y1, w - pad, bar_y2], radius=bar_h // 2, fill=frosted_base, outline=frosted_sheen, width=1),
                blur_r=2.0,
            )
            draw = ImageDraw.Draw(composite, "RGBA")
            fw_t = max(bar_h, int((w - pad * 2) * (pct / 100.0)))
            alpha_rounded_rect(composite, [pad, bar_y1, pad + fw_t, bar_y2], radius=bar_h // 2, fill=(*accent_rgb, 245))
            if show_sub:
                f_s, s_txt = self._fit_text(draw, st_label, max(9, int(10.5 * s)), w - pad * 2, max_h=max(11, int(14 * s)), bold_default=False)
                draw.text((pad, h - pad - int(10 * s)), s_txt, fill=(203, 213, 225, 235), font=f_s)

        else:
            # "anillo" & "neon": Frosted-blurred smoked donut track + SINGLE % inside ring (zero overflow!)
            ring_d = min(h - pad * 2, int(70 * s))
            ring_d = max(44, ring_d)
            rx1 = pad
            ry1 = (h - ring_d) // 2
            rx2 = rx1 + ring_d
            ry2 = ry1 + ring_d
            thick = max(5, int((8 if style == "neon" else 7) * s))

            # Heavy real optical frosted-blur spent track ring + frosted dark center disk
            self._composite_frosted_track(
                composite,
                lambda ld: ld.arc([rx1, ry1, rx2, ry2], start=0, end=360, fill=255, width=thick + 2),
                blur_r=5.0,
            )
            draw = ImageDraw.Draw(composite, "RGBA")
            draw.ellipse([rx1 + thick, ry1 + thick, rx2 - thick, ry2 - thick], fill=(12, 18, 30, 225))
            draw.arc([rx1, ry1, rx2, ry2], start=0, end=360, fill=(226, 232, 240, 45), width=thick)
            span = int(360 * (pct / 100.0))
            if style == "neon" and span > 0:
                draw.arc([rx1 - 2, ry1 - 2, rx2 + 2, ry2 + 2], start=-90, end=-90 + span, fill=(*accent_rgb, 95), width=thick + 5)
            if span > 0:
                draw.arc([rx1, ry1, rx2, ry2], start=-90, end=-90 + span, fill=(*accent_rgb, 252), width=thick)

            inner_ring_w = max(20, ring_d - thick * 2 - 8)
            f_pct, p_txt = self._fit_text(draw, pct_label, max(12, int(15 * s)), inner_ring_w, max_h=inner_ring_w, bold_default=True)
            draw.text(((rx1 + rx2) / 2.0, (ry1 + ry2) / 2.0), p_txt, fill=(248, 250, 252, 255), font=f_pct, anchor="mm")
            self._pct_hit_rect = (rx1, ry1, rx2, ry2)

            # Right column: clean auto-fitted telemetry (never clips past right card edge or overlaps vertically!)
            tx = rx2 + max(8, int(12 * s))
            max_tx_w = max(28, w - pad - tx)
            title_max_w = max(24, (w - pad - btn_s - 6) - tx)

            content_top = pad
            content_bot = h - pad
            avail_h = max(36, content_bot - content_top)

            # 1) Title row ("BATERÍA")
            title_h_cap = max(10, int(avail_h * 0.24))
            f_t, t_txt = self._fit_text(draw, "BATERÍA", max(9, int(10 * s)), title_max_w, max_h=title_h_cap, bold_default=True)
            t_bb = draw.textbbox((0, 0), t_txt, font=f_t)
            t_h = max(10, t_bb[3] - t_bb[1])
            title_y = content_top
            draw.text((tx, title_y), t_txt, fill=(*accent_rgb, 245) if style == "neon" else (148, 163, 184, 235), font=f_t)

            # 2) Compact status pill badge pinned cleanly at bottom of right column
            badge_h_cap = max(10, int(avail_h * 0.24))
            f_b, b_txt = self._fit_text(draw, mode_badge, max(8, int(9 * s)), max(24, max_tx_w - int(12 * s)), max_h=badge_h_cap, bold_default=True)
            b_bb = draw.textbbox((0, 0), b_txt, font=f_b)
            b_h = max(9, b_bb[3] - b_bb[1])
            pb_h = max(16, min(int(avail_h * 0.34), b_h + max(6, int(8 * s))))
            pb_y2 = content_bot
            pb_y1 = pb_y2 - pb_h
            pb_w = min(max_tx_w, int(draw.textlength(b_txt, font=f_b)) + max(12, int(14 * s)))
            alpha_rounded_rect(composite, [tx, pb_y1, tx + pb_w, pb_y2], radius=min(pb_h // 2, max(6, int(8 * s))), fill=(*accent_rgb, 55), outline=(*accent_rgb, 180), width=1)
            draw.text((tx + pb_w / 2.0, (pb_y1 + pb_y2) / 2.0), b_txt, fill=(241, 245, 249, 250), font=f_b, anchor="mm")

            # 3) Status text centered in the vertical band strictly between title and badge
            if show_sub:
                mid_top = title_y + t_h + 2
                mid_bot = pb_y1 - 2
                mid_h = max(10, mid_bot - mid_top)
                f_st, st_txt = self._fit_text(draw, st_label, max(10, int(13 * s)), max_tx_w, max_h=mid_h, bold_default=True)
                draw.text((tx, (mid_top + mid_bot) / 2.0), st_txt, fill=(248, 250, 252, 250), font=f_st, anchor="lm")

        if charging:
            bx = w - pad - btn_s - int(14 * s)
            by = pad + btn_s // 2 - 2
            draw.polygon([(bx + 2, by - 5), (bx - 3, by + 1), (bx + 1, by + 1), (bx - 2, by + 6), (bx + 4, by - 1), (bx, by - 1)], fill=(250, 204, 21, 245))

        self._render_to_canvas(composite, w, h)

    def _set_style(self, style_name):
        self.set_cfg("style", style_name)
        self._last_bat_sig = None
        self._render()

    def _pick_accent(self):
        from tkinter import colorchooser
        cur = self.get_cfg("accent_color", "#22c55e") or "#22c55e"
        res = colorchooser.askcolor(color=cur, title="Color de Acento - Batería", parent=self.window)
        if res and res[1]:
            self.set_cfg("accent_color", res[1])
            self._last_bat_sig = None
            self._render()

    def _on_context_menu(self, event):
        from custom_menu import CustomMenu
        menu = CustomMenu(self.window)
        cur_st = (self.get_cfg("style", "anillo") or "anillo").lower()
        sec_st = menu.add_section("Diseño de Batería", expanded=True)
        for st_id, st_lbl in self.STYLE_LABELS:
            sec_st.add_command(st_lbl, lambda k=st_id: self._set_style(k), checked=(cur_st == st_id))
        sec_st.add_separator()
        sec_st.add_command("Color de acento...", self._pick_accent)
        sec_st.add_command("Configuración de Energía Windows...", self._open_power_settings)
        menu.add_separator()
        self._append_common_menu_items(menu)
        menu.show(event.x_root, event.y_root)


class DesktopWidgetManager:
    """Manages lifecycle and live settings synchronization of all desktop widgets."""
    def __init__(self, root, config_mgr, on_open_settings=None):
        self.root = root
        self.config_mgr = config_mgr
        self.on_open_settings = on_open_settings

        self.widgets = {
            "clock": ClockWidget(root, config_mgr, on_open_settings=on_open_settings),
            "system_monitor": SysMonitorWidget(root, config_mgr, on_open_settings=on_open_settings),
            "battery": BatteryWidget(root, config_mgr, on_open_settings=on_open_settings),
            "desktop_minidock": DesktopMiniDockWidget(root, config_mgr, on_open_settings=on_open_settings),
            "todo_list": TodoListWidget(root, config_mgr, on_open_settings=on_open_settings),
            "quick_access": QuickAccessWidget(root, config_mgr, on_open_settings=on_open_settings),
            "pwa_apps": PwaWidget(root, config_mgr, on_open_settings=on_open_settings)
        }
        self.start()
        self._last_configs = copy.deepcopy(self.config_mgr.data.get("widgets", {}))

    def start(self):
        for w_id, widget in self.widgets.items():
            if widget.is_enabled():
                try:
                    widget.show()
                except Exception as e:
                    print(f"[Widgets] Error showing widget {w_id}: {e}")

    def get_all_hwnds(self):
        hwnds = set()
        for w in self.widgets.values():
            if w.window and w.window.winfo_exists():
                hw = w.top_hwnd or user32.GetAncestor(w.window.winfo_id(), 2) or w.window.winfo_id()
                if hw:
                    hwnds.add(int(hw))
        return hwnds

    def sync_z_order(self):
        """
        Single shared Win32 Z-order scan across all active widgets: keeps widgets visible during
        Windows 'Mostrar escritorio' (Taskbar corner, Dockbar button, or Win+D) and cleanly below
        normal app windows during regular work.
        """
        excl = self.get_all_hwnds()
        if not excl:
            return
        lowest_app, desk_cover = find_lowest_app_above_desktop(exclude_hwnds=excl)
        for widget in self.widgets.values():
            if widget.window and widget.window.winfo_exists():
                widget._apply_z_order(lowest_app_override=lowest_app, desk_cover_override=desk_cover, exclude_hwnds=excl)

    def has_open_modals_or_resizing(self):
        from custom_menu import CustomMenu
        if CustomMenu.has_active_menu():
            return True
        for w in self.widgets.values():
            if w.is_resizing or w.is_dragging or getattr(w, "_active_modal", None) is not None:
                return True
        return False

    def live_update(self):
        """Called whenever settings change in real-time."""
        for w_id, widget in self.widgets.items():
            current = self.config_mgr.data.get("widgets", {}).get(w_id, {})
            previous = self._last_configs.get(w_id)
            if current == previous and not getattr(widget, "_hiding", False) and widget.window and widget.window.winfo_exists():
                continue
            self._last_configs[w_id] = copy.deepcopy(current)
            enabled = widget.is_enabled()
            if enabled:
                if not widget.window or not widget.window.winfo_exists() or getattr(widget, "_hiding", False):
                    widget.show()
                else:
                    widget._last_z_state = None
                    widget._redraw_now()
            else:
                if widget.window and widget.window.winfo_exists():
                    widget.hide()
        self.sync_z_order()

    def close(self):
        for widget in self.widgets.values():
            try:
                widget.hide(animate=False)
            except Exception:
                pass
