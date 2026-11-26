import math
import time
import ctypes
from ctypes import wintypes
import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageTk

from window_utils import animate_window, cancel_window_animation, set_tool_window, alpha_rounded_rect, alpha_line

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

WM_CLOSE = 0x0010
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND = 2

DWM_TNP_RECTDESTINATION = 0x00000001
DWM_TNP_RECTSOURCE = 0x00000002
DWM_TNP_OPACITY = 0x00000004
DWM_TNP_VISIBLE = 0x00000008
DWM_TNP_SOURCECLIENTAREAONLY = 0x00000010


class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("dwFlags", wintypes.DWORD),
        ("rcDestination", wintypes.RECT),
        ("rcSource", wintypes.RECT),
        ("opacity", ctypes.c_ubyte),
        ("fVisible", wintypes.BOOL),
        ("fSourceClientAreaOnly", wintypes.BOOL),
    ]


# Explicit 64-bit ctypes prototypes for DWM Thumbnail APIs
try:
    dwmapi.DwmRegisterThumbnail.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(ctypes.c_void_p)]
    dwmapi.DwmRegisterThumbnail.restype = ctypes.c_long
    dwmapi.DwmUnregisterThumbnail.argtypes = [ctypes.c_void_p]
    dwmapi.DwmUnregisterThumbnail.restype = ctypes.c_long
    dwmapi.DwmUpdateThumbnailProperties.argtypes = [ctypes.c_void_p, ctypes.POINTER(DWM_THUMBNAIL_PROPERTIES)]
    dwmapi.DwmUpdateThumbnailProperties.restype = ctypes.c_long
    dwmapi.DwmQueryThumbnailSourceSize.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.SIZE)]
    dwmapi.DwmQueryThumbnailSourceSize.restype = ctypes.c_long
    dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
except Exception:
    pass


class WindowPreviewFlyout:
    """
    Windows 11-style live taskbar hover preview flyout powered by Win32 DwmRegisterThumbnail.
    Renders live GPU thumbnails for all open windows belonging to a dock item, with instant
    click-to-activate, per-window X close, "+ Nueva" instance launch, and zero handle leaks.
    """

    LONG_PRESS_SEC = 0.42
    MAX_COLS = 4
    MAX_WINDOWS = 8

    def __init__(
        self,
        parent,
        app_item,
        windows,
        item_rect,
        dock_pos="bottom",
        app_icon_pil=None,
        on_select_window=None,
        on_close_window=None,
        on_new_instance=None,
        on_close_all=None,
        on_close=None,
        refresh_windows_fn=None,
    ):
        self.parent = parent
        self.app_item = app_item
        self.windows = list(windows or [])[: self.MAX_WINDOWS]
        self.item_rect = item_rect
        self.dock_pos = dock_pos
        self.app_icon_pil = app_icon_pil
        self.on_select_window = on_select_window
        self.on_close_window = on_close_window
        self.on_new_instance = on_new_instance
        self.on_close_all = on_close_all
        self.on_close = on_close
        self.refresh_windows_fn = refresh_windows_fn

        self.window = None
        self.canvas = None
        self.canvas_img_id = None
        self.top_hwnd = None
        self.photo_ref = None
        self._thumb_handles = []   # [HTHUMBNAIL, ...]
        self._failed_thumbs = set()
        self._font_cache = {}
        self._bg_img = None

        self.win_x = 0
        self.win_y = 0
        self.win_w = 240
        self.win_h = 180

        self.card_boxes = []       # [(x1, y1, x2, y2, win_dict), ...]
        self.close_boxes = []      # [(x1, y1, x2, y2, win_dict), ...]
        self.well_boxes = []       # [(x1, y1, x2, y2, win_dict), ...]
        self.action_boxes = []     # [(x1, y1, x2, y2, action_id), ...]

        self.hovered_card = -1
        self.hovered_close = -1
        self.hovered_action = None
        self.press_time = 0.0
        self.pressed_target = None  # ("close", idx) | ("card", idx) | ("action", name)
        self._refresh_job = None
        self._esc_job = None
        self._esc_was_released = False
        self._opened_time = time.monotonic()

        if not self.windows:
            if self.on_close:
                self.on_close()
            return

        self._calculate_layout()
        self._create_window()

    def _font(self, key):
        f = self._font_cache.get(key)
        if f is None:
            specs = {
                "hdr": ("segoeuib.ttf", 11),
                "title": ("segoeui.ttf", 11),
                "pill": ("segoeuib.ttf", 10),
                "fb": ("segoeui.ttf", 10),
            }
            fname, fsz = specs[key]
            try:
                f = ImageFont.truetype(fname, fsz)
            except Exception:
                f = ImageFont.load_default()
            self._font_cache[key] = f
        return f

    def _calculate_layout(self):
        n = max(1, len(self.windows))
        single = (n == 1)
        self.cols = min(self.MAX_COLS, n)
        self.rows = math.ceil(n / self.cols)

        if single:
            self.card_w, self.card_h = 224, 134
            self.card_hdr_h = 0
        elif n <= 4:
            self.card_w, self.card_h = 224, 152
            self.card_hdr_h = 24
        else:
            self.card_w, self.card_h = 196, 136
            self.card_hdr_h = 24

        self.pad_x = 10
        self.pad_top = 8
        self.pad_bot = 10
        self.gap = 8
        self.top_bar_h = 32

        grid_w = self.cols * self.card_w + max(0, self.cols - 1) * self.gap
        grid_h = self.rows * self.card_h + max(0, self.rows - 1) * self.gap

        self.win_w = max(244, grid_w + self.pad_x * 2)
        self.win_h = self.pad_top + self.top_bar_h + 6 + grid_h + self.pad_bot

        self.card_boxes = []
        self.close_boxes = []
        self.well_boxes = []
        grid_x0 = (self.win_w - grid_w) // 2
        grid_y0 = self.pad_top + self.top_bar_h + 6

        for idx, win in enumerate(self.windows):
            r, c = divmod(idx, self.cols)
            cx1 = grid_x0 + c * (self.card_w + self.gap)
            cy1 = grid_y0 + r * (self.card_h + self.gap)
            cx2 = cx1 + self.card_w
            cy2 = cy1 + self.card_h
            self.card_boxes.append((cx1, cy1, cx2, cy2, win))
            if single:
                top_cy = self.pad_top + self.top_bar_h // 2
                self.close_boxes.append((self.win_w - self.pad_x - 22, top_cy - 11, self.win_w - self.pad_x, top_cy + 11, win))
                self.well_boxes.append((cx1 + 6, cy1 + 6, cx2 - 6, cy2 - 6, win))
            else:
                self.close_boxes.append((cx2 - 24, cy1 + 2, cx2 - 2, cy1 + 24, win))
                self.well_boxes.append((cx1 + 6, cy1 + self.card_hdr_h + 2, cx2 - 6, cy2 - 6, win))

        self._bg_img = None

    def _compute_geometry(self):
        sw = self.parent.winfo_screenwidth()
        sh = self.parent.winfo_screenheight()
        px = self.parent.winfo_rootx()
        py = self.parent.winfo_rooty()
        ix1, iy1, ix2, iy2 = self.item_rect
        s_ix1, s_ix2 = px + ix1, px + ix2
        s_iy1, s_iy2 = py + iy1, py + iy2
        icx = (s_ix1 + s_ix2) // 2
        icy = (s_iy1 + s_iy2) // 2
        gap = 10

        pos = self.dock_pos if self.dock_pos in ("top", "bottom", "left", "right") else "bottom"
        if pos == "bottom":
            x = icx - self.win_w // 2
            y = s_iy1 - gap - self.win_h
        elif pos == "top":
            x = icx - self.win_w // 2
            y = s_iy2 + gap
        elif pos == "left":
            x = s_ix2 + gap
            y = icy - self.win_h // 2
        else:
            x = s_ix1 - gap - self.win_w
            y = icy - self.win_h // 2

        x = max(6, min(sw - self.win_w - 6, x))
        y = max(6, min(sh - self.win_h - 6, y))
        self.win_x, self.win_y = int(x), int(y)

    def _create_window(self):
        self.window = tk.Toplevel(self.parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.config(bg="#151923")

        # Note: layered=False is REQUIRED so Win32 DwmRegisterThumbnail composites GPU surfaces onto h_dst
        self.top_hwnd = set_tool_window(self.window, owner_hwnd=self.parent.winfo_id(), layered=False, no_activate=True)

        self._compute_geometry()
        self.window.geometry(f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")

        self.canvas = tk.Canvas(
            self.window,
            width=self.win_w,
            height=self.win_h,
            bg="#151923",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.window.bind("<Escape>", lambda e: self.close())

        self.window.update_idletasks()
        hwnd = self.top_hwnd or user32.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
        self.top_hwnd = hwnd
        try:
            corner_pref = ctypes.c_int(DWMWCP_ROUND)
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner_pref), ctypes.sizeof(corner_pref))
            border_bgr = ctypes.c_uint32(0x00523D30)
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(border_bgr), ctypes.sizeof(border_bgr))
        except Exception:
            pass

        self._register_thumbnails()
        self._render()
        animate_window(self.window)
        self._poll_escape()

    def _poll_escape(self):
        if not self.window or not self.window.winfo_exists():
            return
        esc_down = bool(user32.GetAsyncKeyState(0x1B) & 0x8000)
        if not esc_down:
            self._esc_was_released = True
        elif self._esc_was_released and (time.monotonic() - self._opened_time > 0.10):
            self.close()
            return
        self._esc_job = self.parent.after(50, self._poll_escape)

    def _unregister_thumbnails(self):
        for h in self._thumb_handles:
            try:
                dwmapi.DwmUnregisterThumbnail(h)
            except Exception:
                pass
        self._thumb_handles = []
        self._failed_thumbs.clear()

    def _register_thumbnails(self):
        self._unregister_thumbnails()
        if not self.top_hwnd:
            return
        for idx, (wx1, wy1, wx2, wy2, win) in enumerate(self.well_boxes):
            src_hwnd = win.get("hwnd")
            if not src_hwnd or not user32.IsWindow(src_hwnd):
                self._failed_thumbs.add(idx)
                continue
            thumb = ctypes.c_void_p()
            hr = dwmapi.DwmRegisterThumbnail(self.top_hwnd, wintypes.HWND(src_hwnd), ctypes.byref(thumb))
            if hr != 0 or not thumb.value:
                self._failed_thumbs.add(idx)
                continue

            sz = wintypes.SIZE()
            hr_sz = dwmapi.DwmQueryThumbnailSourceSize(thumb, ctypes.byref(sz))
            well_w = max(20, wx2 - wx1)
            well_h = max(20, wy2 - wy1)
            if hr_sz == 0 and sz.cx > 0 and sz.cy > 0:
                scale = min(well_w / float(sz.cx), well_h / float(sz.cy))
                dw = max(16, int(round(sz.cx * scale)))
                dh = max(16, int(round(sz.cy * scale)))
                dx1 = wx1 + (well_w - dw) // 2
                dy1 = wy1 + (well_h - dh) // 2
                dst_rect = wintypes.RECT(dx1, dy1, dx1 + dw, dy1 + dh)
            else:
                dst_rect = wintypes.RECT(wx1, wy1, wx2, wy2)

            props = DWM_THUMBNAIL_PROPERTIES(
                dwFlags=DWM_TNP_RECTDESTINATION | DWM_TNP_OPACITY | DWM_TNP_VISIBLE | DWM_TNP_SOURCECLIENTAREAONLY,
                rcDestination=dst_rect,
                opacity=255,
                fVisible=True,
                fSourceClientAreaOnly=False,
            )
            hr_upd = dwmapi.DwmUpdateThumbnailProperties(thumb, ctypes.byref(props))
            if hr_upd != 0:
                try:
                    dwmapi.DwmUnregisterThumbnail(thumb)
                except Exception:
                    pass
                self._failed_thumbs.add(idx)
                continue
            self._thumb_handles.append(thumb)

    def _build_base_bg(self):
        if self._bg_img is not None:
            return self._bg_img
        img = Image.new("RGBA", (self.win_w, self.win_h), (21, 25, 35, 255))
        px = img.load()
        top_c, bot_c = (30, 35, 48), (15, 18, 26)
        for y in range(self.win_h):
            t = y / max(1, self.win_h - 1)
            row_col = (
                int(top_c[0] + (bot_c[0] - top_c[0]) * t),
                int(top_c[1] + (bot_c[1] - top_c[1]) * t),
                int(top_c[2] + (bot_c[2] - top_c[2]) * t),
                255,
            )
            for x in range(self.win_w):
                px[x, y] = row_col
        self._bg_img = img
        return img

    @staticmethod
    def _truncate_text(draw, text, font, max_w):
        if draw.textlength(text, font=font) <= max_w:
            return text
        out = text
        while out and draw.textlength(out + "…", font=font) > max_w:
            out = out[:-1]
        return (out + "…") if out else text[:1]

    def _render(self):
        if not self.window or not self.window.winfo_exists():
            return
        composite = self._build_base_bg().copy()
        draw = ImageDraw.Draw(composite)
        f_hdr = self._font("hdr")
        f_title = self._font("title")
        f_pill = self._font("pill")
        f_fb = self._font("fb")

        # Top Bar: App Icon + Name + Close X (single) or Cerrar todo (multi)
        bx = self.pad_x
        by = self.pad_top
        top_cy = by + self.top_bar_h // 2
        single = (len(self.windows) == 1)

        if self.app_icon_pil is not None:
            try:
                ic18 = self.app_icon_pil.convert("RGBA").resize((18, 18), Image.Resampling.LANCZOS)
                composite.alpha_composite(ic18, (bx + 2, top_cy - 9))
                bx += 26
            except Exception:
                pass

        self.action_boxes = []
        right_edge = self.win_w - self.pad_x
        if single and self.close_boxes:
            bx1, by1, bx2, by2, _w = self.close_boxes[0]
            close_hov = (self.hovered_close == 0)
            card_hov = (self.hovered_card == 0)
            if close_hov:
                alpha_rounded_rect(composite, [bx1, by1, bx2, by2], radius=6, fill=(239, 68, 68, 230))
            elif card_hov:
                alpha_rounded_rect(composite, [bx1, by1, bx2, by2], radius=6, fill=(255, 255, 255, 24))
            else:
                alpha_rounded_rect(composite, [bx1, by1, bx2, by2], radius=6, fill=(255, 255, 255, 12))
            xcol = (255, 255, 255, 255) if (close_hov or card_hov) else (180, 192, 208, 235)
            mcx, mcy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
            draw.line([(mcx - 4, mcy - 4), (mcx + 4, mcy + 4)], fill=xcol, width=2)
            draw.line([(mcx + 4, mcy - 4), (mcx - 4, mcy + 4)], fill=xcol, width=2)
            right_edge = bx1 - 8
        elif len(self.windows) >= 2:
            label = "Cerrar todo"
            pw = int(draw.textlength(label, font=f_pill)) + 16
            ph = 22
            px2 = right_edge
            px1 = px2 - pw
            py1 = top_cy - ph // 2
            py2 = py1 + ph
            self.action_boxes.append((px1, py1, px2, py2, "close_all"))
            hov = (self.hovered_action == "close_all")
            fill = (239, 68, 68, 185) if hov else (239, 68, 68, 52)
            outl = (252, 165, 165, 230) if hov else (248, 113, 113, 130)
            alpha_rounded_rect(composite, [px1, py1, px2, py2], radius=7, fill=fill, outline=outl, width=1)
            draw.text(((px1 + px2) / 2.0, top_cy), label, fill=(255, 255, 255, 255), font=f_pill, anchor="mm")
            right_edge = px1 - 6

        app_name = (self.app_item.get("name") or "Ventanas")
        max_hdr_w = max(40, right_edge - bx - 8)
        raw_hdr = app_name if single else f"{app_name} ({len(self.windows)})"
        hdr_text = self._truncate_text(draw, raw_hdr, f_hdr, max_hdr_w)
        draw.text((bx, top_cy), hdr_text, fill=(241, 245, 249, 255), font=f_hdr, anchor="lm")
        alpha_line(composite, [(self.pad_x, by + self.top_bar_h), (self.win_w - self.pad_x, by + self.top_bar_h)], fill=(255, 255, 255, 24), width=1)

        # Window Cards
        for idx, (cx1, cy1, cx2, cy2, win) in enumerate(self.card_boxes):
            is_hov = (idx == self.hovered_card)
            c_fill = (255, 255, 255, 22) if is_hov else (255, 255, 255, 10)
            c_outl = (96, 165, 250, 235) if is_hov else (255, 255, 255, 36)
            alpha_rounded_rect(composite, [cx1, cy1, cx2, cy2], radius=10, fill=c_fill, outline=c_outl, width=1)

            if not single:
                raw_t = (win.get("title") or "Ventana").strip()
                suffix = f" - {app_name}"
                if raw_t.lower().endswith(suffix.lower()) and len(raw_t) > len(suffix):
                    raw_t = raw_t[: -len(suffix)].strip()
                prefix = "• " if win.get("iconic") else ""
                title_max_w = max(40, (cx2 - cx1) - 36)
                disp_t = self._truncate_text(draw, prefix + raw_t, f_title, title_max_w)
                draw.text((cx1 + 8, cy1 + 13), disp_t, fill=(255, 255, 255, 255) if is_hov else (210, 220, 232, 240), font=f_title, anchor="lm")

                bx1, by1, bx2, by2, _w = self.close_boxes[idx]
                close_hov = (idx == self.hovered_close)
                if close_hov:
                    alpha_rounded_rect(composite, [bx1, by1, bx2, by2], radius=6, fill=(239, 68, 68, 230))
                elif is_hov:
                    alpha_rounded_rect(composite, [bx1, by1, bx2, by2], radius=6, fill=(255, 255, 255, 24))
                xcol = (255, 255, 255, 255) if (close_hov or is_hov) else (160, 174, 192, 230)
                mcx, mcy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
                draw.line([(mcx - 4, mcy - 4), (mcx + 4, mcy + 4)], fill=xcol, width=2)
                draw.line([(mcx + 4, mcy - 4), (mcx - 4, mcy + 4)], fill=xcol, width=2)

            # Thumbnail Well
            wx1, wy1, wx2, wy2, _ = self.well_boxes[idx]
            alpha_rounded_rect(composite, [wx1, wy1, wx2, wy2], radius=6, fill=(10, 13, 20, 255), outline=(255, 255, 255, 22), width=1)
            if idx in self._failed_thumbs:
                if self.app_icon_pil is not None:
                    try:
                        ic36 = self.app_icon_pil.convert("RGBA").resize((36, 36), Image.Resampling.LANCZOS)
                        composite.alpha_composite(ic36, ((wx1 + wx2) // 2 - 18, (wy1 + wy2) // 2 - 24))
                    except Exception:
                        pass
                draw.text(((wx1 + wx2) / 2.0, (wy1 + wy2) / 2.0 + 22), "Clic para abrir", fill=(148, 163, 184, 235), font=f_fb, anchor="mm")

        rgb_img = composite.convert("RGB")
        self.photo_ref = ImageTk.PhotoImage(rgb_img)
        if self.canvas_img_id is None:
            self.canvas_img_id = self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo_ref)
        else:
            self.canvas.itemconfig(self.canvas_img_id, image=self.photo_ref)

    def _hit_test(self, x, y):
        for act_x1, act_y1, act_x2, act_y2, act_id in self.action_boxes:
            if act_x1 <= x <= act_x2 and act_y1 <= y <= act_y2:
                return ("action", act_id)
        for idx, (bx1, by1, bx2, by2, _w) in enumerate(self.close_boxes):
            if bx1 <= x <= bx2 and by1 <= y <= by2:
                return ("close", idx)
        for idx, (cx1, cy1, cx2, cy2, _w) in enumerate(self.card_boxes):
            if cx1 <= x <= cx2 and cy1 <= y <= cy2:
                return ("card", idx)
        return (None, None)

    def _on_motion(self, event):
        kind, val = self._hit_test(event.x, event.y)
        new_card = val if kind in ("card", "close") else -1
        new_close = val if kind == "close" else -1
        new_act = val if kind == "action" else None
        if new_card != self.hovered_card or new_close != self.hovered_close or new_act != self.hovered_action:
            self.hovered_card = new_card
            self.hovered_close = new_close
            self.hovered_action = new_act
            self._render()

    def _on_leave(self, event):
        if self.hovered_card != -1 or self.hovered_close != -1 or self.hovered_action is not None:
            self.hovered_card = -1
            self.hovered_close = -1
            self.hovered_action = None
            self._render()

    def _on_press(self, event):
        self.press_time = time.monotonic()
        self.pressed_target = self._hit_test(event.x, event.y)

    def _on_release(self, event):
        held = time.monotonic() - self.press_time
        pressed = self.pressed_target
        self.pressed_target = None
        if held >= self.LONG_PRESS_SEC or not pressed or pressed[0] is None:
            return
        current = self._hit_test(event.x, event.y)
        if current != pressed:
            return

        kind, val = current
        if kind == "action":
            if val == "new_instance" and self.on_new_instance:
                self.close()
                self.on_new_instance()
            elif val == "close_all" and self.on_close_all:
                self.close()
                self.on_close_all()
        elif kind == "close" and 0 <= val < len(self.windows):
            hwnd = self.windows[val].get("hwnd")
            if hwnd and self.on_close_window:
                self.on_close_window(hwnd)
            # Optimistically remove closed card or refresh after WM_CLOSE settles
            self.windows = [w for i, w in enumerate(self.windows) if i != val]
            if not self.windows:
                self.close()
            else:
                self._calculate_layout()
                self._compute_geometry()
                if self.window and self.window.winfo_exists():
                    self.window.geometry(f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")
                    self.canvas.config(width=self.win_w, height=self.win_h)
                    self._register_thumbnails()
                    self._render()
                if self.refresh_windows_fn:
                    self._refresh_job = self.parent.after(260, self._refresh_after_close)
        elif kind == "card" and 0 <= val < len(self.windows):
            hwnd = self.windows[val].get("hwnd")
            self.close()
            if hwnd and self.on_select_window:
                self.on_select_window(hwnd)

    def _refresh_after_close(self):
        self._refresh_job = None
        if not self.window or not self.window.winfo_exists() or not self.refresh_windows_fn:
            return
        try:
            latest = list(self.refresh_windows_fn() or [])[: self.MAX_WINDOWS]
        except Exception:
            return
        if not latest:
            self.close()
            return
        if [w.get("hwnd") for w in latest] != [w.get("hwnd") for w in self.windows]:
            self.windows = latest
            self._calculate_layout()
            self._compute_geometry()
            self.window.geometry(f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")
            self.canvas.config(width=self.win_w, height=self.win_h)
            self._register_thumbnails()
            self._render()

    def close(self, animate=True):
        if getattr(self, '_closing', False):
            if not animate:
                self._finish_close()
            return
        self._closing = True
        callback, self.on_close = self.on_close, None
        if callback:
            try:
                callback()
            except Exception:
                pass
        if animate and self.window and self.window.winfo_exists():
            animate_window(self.window, showing=False, on_done=self._finish_close)
        else:
            self._finish_close()

    def _finish_close(self):
        if self.window:
            cancel_window_animation(self.window)
        if self._esc_job:
            try:
                self.parent.after_cancel(self._esc_job)
            except Exception:
                pass
            self._esc_job = None

        if self._refresh_job:
            try:
                self.parent.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None

        self._unregister_thumbnails()
        self.top_hwnd = None

        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None

        self.photo_ref = None
        self.canvas = None
        self._font_cache.clear()
        self._bg_img = None
        self.app_icon_pil = None
        self.card_boxes.clear()
        self.close_boxes.clear()
        self.well_boxes.clear()

        if self.on_close:
            cb = self.on_close
            self.on_close = None
            try:
                cb()
            except Exception:
                pass
