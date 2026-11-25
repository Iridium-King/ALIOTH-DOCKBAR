"""
Custom macOS/App-Drawer style floating Launcher for Alioth Dockbar + Windows Start Key helper.
"""
import ctypes
from ctypes import wintypes
import math
import os
import subprocess
import tkinter as tk
from PIL import Image, ImageDraw

import app_search
import icon_extractor
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

VK_LWIN = 0x5B
KEYEVENTF_KEYUP = 0x0002


def open_windows_start_menu():
    """Sends a clean native Windows-key press/release to toggle the Windows Start Menu."""
    try:
        user32.keybd_event(VK_LWIN, 0, 0, 0)
        user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
    except Exception as e:
        print(f"[Launcher] Start key fallback note: {e}")


class CustomAppLauncher:
    """Sleek frosted dark-glass floating app launcher (grid of apps + instant search)."""

    COLS = 4
    ROWS = 4
    CARD_W = 468
    CARD_H = 540
    CELL_W = 104
    CELL_H = 104
    ICON_SZ = 50

    def __init__(self, parent, dock_pos="bottom", on_close=None, on_launch_feedback=None):
        self.parent = parent
        self.dock_pos = dock_pos
        self.on_close = on_close
        self.on_launch_feedback = on_launch_feedback

        self.window = None
        self.canvas = None
        self.search_entry = None
        self.search_var = tk.StringVar()
        self.top_hwnd = None
        self.transparent_key = "#010203"
        self.photo_ref = None

        self.all_apps = []
        self.filtered_apps = []
        self._icon_cache = {}
        self.item_boxes = []
        self.hovered_idx = -1
        self.scroll_row = 0
        self._bg_cache = None
        self._check_job = None
        self.opened_ts = 0.0
        self.win_x = 0
        self.win_y = 0

        self._load_apps()
        self._create_window()

    def _load_apps(self):
        try:
            raw = app_search.search_apps("", limit=160)
        except Exception:
            raw = []
        seen = set()
        clean = []
        for a in raw:
            key = (a.get("name", "").strip().lower(), (a.get("target", "") + "|" + a.get("args", "")).lower())
            if not a.get("name") or key in seen:
                continue
            seen.add(key)
            clean.append(a)
        self.all_apps = clean
        self.filtered_apps = list(clean)

    def _create_window(self):
        import time
        self.opened_ts = time.time()
        self.window = tk.Toplevel(self.parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.config(bg=self.transparent_key)
        self.top_hwnd = set_tool_window(self.window, owner_hwnd=self.parent.winfo_id(), layered=True)

        sw = self.parent.winfo_screenwidth()
        sh = self.parent.winfo_screenheight()
        self.win_x = max(12, (sw - self.CARD_W) // 2)
        if self.dock_pos == "bottom":
            self.win_y = max(24, sh - self.CARD_H - 92)
        elif self.dock_pos == "top":
            self.win_y = 104
        else:
            self.win_y = max(24, (sh - self.CARD_H) // 2)
        self.window.geometry(f"{self.CARD_W}x{self.CARD_H}+{self.win_x}+{self.win_y}")

        self.canvas = tk.Canvas(
            self.window,
            width=self.CARD_W,
            height=self.CARD_H,
            bg=self.transparent_key,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Embedded dark search Entry inside top search pill area
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
        self.search_entry.place(x=60, y=27, width=self.CARD_W - 108, height=24)
        self.search_var.trace_add("write", lambda *_: self._on_filter_change())

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<MouseWheel>", self._on_scroll)
        self.window.bind("<MouseWheel>", self._on_scroll)
        self.window.bind("<Escape>", lambda _e: self.close())
        self.search_entry.bind("<Escape>", lambda _e: self.close())
        self.search_entry.bind("<Return>", lambda _e: self._launch_first())

        self._render()
        self.window.after(30, lambda: (self.window and self.window.winfo_exists() and self.window.focus_force(), self.search_entry and self.search_entry.focus_set()))
        self._poll_outside_click()

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
        for (x1, y1, x2, y2, app) in self.item_boxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self._launch_app(app)
                return

    def _poll_outside_click(self):
        import time
        if not self.window or not self.window.winfo_exists():
            return
        if time.time() - self.opened_ts > 0.25:
            l_down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
            r_down = bool(user32.GetAsyncKeyState(0x02) & 0x8000)
            if l_down or r_down:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                inside = (self.win_x <= pt.x <= self.win_x + self.CARD_W and self.win_y <= pt.y <= self.win_y + self.CARD_H)
                if not inside:
                    self.close()
                    return
        self._check_job = self.parent.after(45, self._poll_outside_click)

    def _get_app_icon(self, app):
        key = (app.get("name", ""), app.get("icon_src") or app.get("target", ""))
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached
        if len(self._icon_cache) >= 72:
            self._icon_cache.clear()
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
        self._icon_cache[key] = ic
        return ic

    def _build_bg(self):
        if self._bg_cache is not None:
            return self._bg_cache.copy()
        w, h = self.CARD_W, self.CARD_H
        bg = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(bg, "RGBA")
        d.rounded_rectangle([2, 2, w - 3, h - 3], radius=24, fill=(17, 21, 31, 246), outline=(255, 255, 255, 42), width=1)
        # Search bar pill
        d.rounded_rectangle([22, 18, w - 22, 58], radius=16, fill=(28, 34, 49, 255), outline=(96, 165, 250, 95), width=1)
        # Clean outline magnifying glass icon
        alpha_ellipse(bg, [34, 30, 48, 44], outline=(148, 163, 184, 235), width=2)
        alpha_line(bg, [(46, 42), (51, 47)], fill=(148, 163, 184, 235), width=2)
        self._bg_cache = bg
        return bg.copy()

    def _render(self):
        if not self.window or not self.canvas:
            return
        w, h = self.CARD_W, self.CARD_H
        composite = self._build_bg()
        draw = ImageDraw.Draw(composite, "RGBA")
        f_lbl = resolve_styled_font("Segoe UI", 11, bold=False)
        f_sub = resolve_styled_font("Segoe UI", 11, bold=True)

        self.item_boxes = []
        grid_x0 = (w - self.COLS * self.CELL_W) // 2
        grid_y0 = 76
        start_idx = self.scroll_row * self.COLS
        per_page = self.COLS * self.ROWS
        page_apps = self.filtered_apps[start_idx: start_idx + per_page]

        if not page_apps:
            draw.text((w / 2.0, h / 2.0), "Sin aplicaciones que coincidan", fill=(148, 163, 184, 220), font=f_sub, anchor="mm")

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

            ic = self._get_app_icon(app)
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

        # Bottom subtle page dots / count indicator
        total_rows = max(1, math.ceil(len(self.filtered_apps) / float(self.COLS)))
        info_txt = f"{len(self.filtered_apps)} apps  •  Rueda para desplazarse"
        draw.text((w / 2.0, h - 22), info_txt, fill=(148, 163, 184, 185), font=resolve_styled_font("Segoe UI", 10), anchor="mm")
        if total_rows > self.ROWS:
            tr_y1, tr_y2 = 78, h - 44
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
        self._icon_cache.clear()
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass
