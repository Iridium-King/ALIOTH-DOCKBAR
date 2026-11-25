import os
import math
import subprocess
import time
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import simpledialog, filedialog, messagebox
from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageFilter, ImageChops

import icon_extractor
from custom_menu import CustomMenu
from window_utils import set_tool_window, update_layered_pil, release_layered_surface, flatten_pil_for_colorkey, alpha_rounded_rect, alpha_ellipse, alpha_line, resolve_styled_font

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

class GroupFlyout:
    """
    macOS Stacks-style floating dark glass card for Dock groups.
    Renders rounded corners, translucent background, dynamic grid of items,
    vector close button, and reliable click-outside dismissal.
    """
    SHADOW_M = 16      # transparent margin around card for soft drop shadow
    CARET = 10         # pointer arrow toward the dock icon
    CARD_R = 18        # card corner radius
    ICON_SZ = 56
    ICON_SZ_HOVER = 62
    LONG_PRESS_SEC = 0.42
    DRAG_THRESHOLD_PX = 6

    def __init__(self, parent, group_item, item_rect, dock_pos="bottom", on_launch=None, on_group_modified=None, on_close=None, on_pick_child_icon=None, get_item_windows_fn=None, on_activate_window=None, on_close_windows=None, on_kill_process=None, is_locked_fn=None, config_mgr=None):
        self.parent = parent
        self.group_item = group_item
        self.item_rect = item_rect  # (x1, y1, x2, y2)
        self.dock_pos = dock_pos
        self.on_launch = on_launch
        self.on_group_modified = on_group_modified
        self.on_close = on_close
        self.on_pick_child_icon = on_pick_child_icon
        self.get_item_windows_fn = get_item_windows_fn
        self.on_activate_window = on_activate_window
        self.on_close_windows = on_close_windows
        self.on_kill_process = on_kill_process
        self.is_locked_fn = is_locked_fn
        self.config_mgr = config_mgr

        self.window = None
        self.canvas = None
        self.transparent_key = "#010203"
        self.hovered_idx = -1
        self.hovered_action = None
        self.item_boxes = []     # [(x1, y1, x2, y2, sub_item), ...]
        self.action_boxes = []   # [(x1, y1, x2, y2, action_name), ...]
        self.photo_ref = None
        self.child_icons = {}    # id -> PIL.Image (128x128)
        self._icon_variants = {}
        self._bg_cache = None
        self._font_cache = {}
        self.win_x = 0
        self.win_y = 0
        self.caret_side = "bottom"
        self.caret_pos = 120
        self.opened_time = 0
        self.mouse_was_released = False
        self.esc_was_released = False
        self._check_job = None
        self._fade_job = None
        self.active_menu = None
        self.modal_open = False

        # Drag-and-reorder & long-click guard state
        self.press_x = 0
        self.press_y = 0
        self.press_time = 0.0
        self.pressed_idx = -1
        self.pressed_action = None
        self.is_dragging = False
        self.drag_x = 0
        self.drag_y = 0
        self.drag_target_idx = -1

        self._load_icons()
        self._calculate_dimensions()
        self._create_window()

    def _get_items_list(self):
        if "items" not in self.group_item and "children" in self.group_item:
            self.group_item["items"] = self.group_item["children"]
        elif "items" not in self.group_item:
            self.group_item["items"] = []
        return self.group_item["items"]

    def _font(self, key):
        cache = getattr(self, "_font_cache", None)
        if cache is None:
            cache = self._font_cache = {}
        f = cache.get(key)
        if f is None:
            spec = {
                "title": ("segoeuib.ttf", 14),
                "pill": ("segoeuib.ttf", 10),
                "item": ("segoeui.ttf", 11),
                "btn": ("segoeuib.ttf", 11),
                "tip": ("segoeuib.ttf", 11),
                "empty": ("segoeui.ttf", 12),
            }[key]
            try:
                f = ImageFont.truetype(spec[0], spec[1])
            except Exception:
                f = ImageFont.load_default()
            cache[key] = f
        return f

    def _load_icons(self):
        items = self._get_items_list()
        self.child_icons.clear()
        self._icon_variants = {}
        for it in items:
            it_id = it.get("id")
            try:
                self.child_icons[it_id] = icon_extractor.get_or_create_icon(it, size=128)
            except Exception:
                self.child_icons[it_id] = None
        self._bg_cache = None

    def _icon_variant(self, it_id, size):
        """Returns (icon, shadow) pair resized once and cached (keeps hover redraws cheap)."""
        key = (it_id, size)
        v = self._icon_variants.get(key)
        if v is None:
            src = self.child_icons.get(it_id)
            if src is None:
                return None, None
            ic = src.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
            pad = 8
            sh = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
            alpha = ic.getchannel("A").point(lambda a: int(a * 0.42))
            black = Image.new("RGBA", (size, size), (0, 0, 0, 255))
            black.putalpha(alpha)
            sh.alpha_composite(black, (pad, pad + 3))
            sh = sh.filter(ImageFilter.GaussianBlur(4))
            v = (ic, sh)
            self._icon_variants[key] = v
        return v

    def _label_style(self):
        cfg = self.config_mgr
        if cfg is None:
            return True, "Segoe UI", 11, False, False, (218, 226, 238)
        show_labels = bool(cfg.get("appearance", "folder_show_labels", True))
        fam = cfg.get("appearance", "folder_label_font_family", "Segoe UI") or "Segoe UI"
        try:
            fsz = max(8, min(20, int(cfg.get("appearance", "folder_label_font_size", 11) or 11)))
        except Exception:
            fsz = 11
        bold = bool(cfg.get("appearance", "folder_label_bold", False))
        italic = bool(cfg.get("appearance", "folder_label_italic", False))
        fg_hex = str(cfg.get("appearance", "folder_label_fg", "#dae2ee") or "#dae2ee").strip().lstrip("#")
        fg_rgb = (218, 226, 238)
        try:
            if len(fg_hex) == 3:
                fg_hex = "".join(c * 2 for c in fg_hex)
            if len(fg_hex) == 6:
                fg_rgb = tuple(int(fg_hex[i:i+2], 16) for i in (0, 2, 4))
        except Exception:
            pass
        return show_labels, fam, fsz, bold, italic, fg_rgb

    def _calculate_dimensions(self):
        items = self._get_items_list()
        n = len(items)
        if n <= 4:
            self.cols = max(1, n)
        elif n <= 8:
            self.cols = 4
        elif n <= 15:
            self.cols = 5
        else:
            self.cols = 6
        self.rows = max(1, math.ceil(max(1, n) / self.cols))

        show_labels, _fam, fsz, _bold, _italic, _fg = self._label_style()
        if show_labels:
            self.cell_w = max(108, 76 + fsz * 3)
            self.cell_h = max(106, 72 + int(fsz * 2.5) + 12)
        else:
            self.cell_w = 92
            self.cell_h = 80
        self.pad_x = 14
        self.pad_top = 14
        self.footer_h = 38
        self.pad_bottom = 10

        self.card_w = max(210, self.cols * self.cell_w + self.pad_x * 2)
        self.card_h = self.pad_top + self.rows * self.cell_h + self.footer_h + self.pad_bottom
        self.win_w = self.card_w + self.SHADOW_M * 2
        self.win_h = self.card_h + self.SHADOW_M * 2
        self._bg_cache = None

    def refresh_style(self):
        if not self.window or not self.window.winfo_exists():
            return
        self._calculate_dimensions()
        self._apply_geometry()
        self._render()

    def _compute_geometry(self):
        """Positions the card so its caret points at the dock icon, clamped to screen."""
        sw = self.parent.winfo_screenwidth()
        sh = self.parent.winfo_screenheight()
        parent_x = self.parent.winfo_rootx()
        parent_y = self.parent.winfo_rooty()
        ix1, iy1, ix2, iy2 = self.item_rect
        s_ix1, s_ix2 = parent_x + ix1, parent_x + ix2
        s_iy1, s_iy2 = parent_y + iy1, parent_y + iy2
        icx = (s_ix1 + s_ix2) // 2
        icy = (s_iy1 + s_iy2) // 2
        M, C = self.SHADOW_M, self.CARET
        gap = 6

        pos = self.dock_pos if self.dock_pos in ("top", "bottom", "left", "right") else "bottom"
        if pos == "bottom":
            x = icx - self.win_w // 2
            y = s_iy1 - gap - C - M - self.card_h
        elif pos == "top":
            x = icx - self.win_w // 2
            y = s_iy2 + gap + C - M
        elif pos == "left":
            x = s_ix2 + gap + C - M
            y = icy - self.win_h // 2
        else:
            x = s_ix1 - gap - C - M - self.card_w
            y = icy - self.win_h // 2

        x = max(4 - M, min(sw - self.win_w + M - 4, x))
        y = max(4 - M, min(sh - self.win_h + M - 4, y))
        self.win_x, self.win_y = int(x), int(y)
        self.caret_side = pos
        lo = M + self.CARD_R + C
        if pos in ("top", "bottom"):
            self.caret_pos = max(lo, min(self.win_w - lo, icx - self.win_x))
        else:
            self.caret_pos = max(lo, min(self.win_h - lo, icy - self.win_y))

    def _apply_geometry(self):
        self._compute_geometry()
        if self.window and self.window.winfo_exists():
            self.window.geometry(f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")
            if self.canvas:
                self.canvas.config(width=self.win_w, height=self.win_h)

    def _create_window(self):
        self.opened_time = time.time()
        self.mouse_was_released = False
        self._fade_step = 0

        self.window = tk.Toplevel(self.parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.config(bg=self.transparent_key)
        self.top_hwnd = set_tool_window(self.window, owner_hwnd=self.parent.winfo_id(), layered=True)

        self._compute_geometry()
        self.window.geometry(f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")

        self.canvas = tk.Canvas(
            self.window,
            width=self.win_w,
            height=self.win_h,
            bg=self.transparent_key,
            highlightthickness=0,
            bd=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_right_click)

        self.window.bind("<Escape>", lambda e: self.close())

        self.window.update_idletasks()
        self.esc_was_released = False
        self._run_fade_in()
        self._check_click_outside()

    def _run_fade_in(self):
        """Lightweight 4-frame fade + rise-in (approx 70ms total)."""
        self._fade_job = None
        if not self.window or not self.window.winfo_exists():
            return
        steps = (0.35, 0.62, 0.86, 1.0)
        if self._fade_step < len(steps):
            self._render(alpha_mult=steps[self._fade_step], rise=int((1.0 - steps[self._fade_step]) * 8))
            self._fade_step += 1
            if self._fade_step < len(steps):
                self._fade_job = self.parent.after(16, self._run_fade_in)

    def _check_click_outside(self):
        if not self.window or not self.window.winfo_exists():
            return

        now = time.time()
        menu_or_modal = CustomMenu.has_active_menu() or self.modal_open
        esc_down = bool(user32.GetAsyncKeyState(0x1B) & 0x8000)
        if menu_or_modal:
            self.esc_was_released = False
            self.mouse_was_released = False
        elif not esc_down:
            self.esc_was_released = True
        elif self.esc_was_released and (now - self.opened_time > 0.15):
            self.close()
            return

        l_down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        r_down = bool(user32.GetAsyncKeyState(0x02) & 0x8000)

        if not menu_or_modal and not l_down and not r_down:
            self.mouse_was_released = True

        if self.mouse_was_released and not self.is_dragging and (now - self.opened_time > 0.2) and not menu_or_modal:
            if l_down or r_down:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                M = self.SHADOW_M
                inside = (self.win_x + M - 4 <= pt.x <= self.win_x + self.win_w - M + 4
                          and self.win_y + M - 4 <= pt.y <= self.win_y + self.win_h - M + 4)
                if not inside:
                    self.close()
                    return

        self._check_job = self.parent.after(40, self._check_click_outside)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _build_background(self):
        """Static layer: soft shadow + gradient glass card with directional caret. Cached per layout."""
        w, h = self.win_w, self.win_h
        M, C, R = self.SHADOW_M, self.CARET, self.CARD_R
        key = (w, h, self.caret_side, self.caret_pos)
        if self._bg_cache is not None and self._bg_cache[0] == key:
            return self._bg_cache[1]

        ss = 2
        W, H = w * ss, h * ss
        cx1, cy1, cx2, cy2 = M * ss, M * ss, (w - M) * ss, (h - M) * ss
        cp = self.caret_pos * ss
        c = C * ss

        # Card + caret silhouette mask
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle([cx1, cy1, cx2 - 1, cy2 - 1], radius=R * ss, fill=255)
        side = self.caret_side
        if side == "bottom":
            md.polygon([(cp - c * 1.3, cy2 - 2), (cp + c * 1.3, cy2 - 2), (cp, cy2 + c)], fill=255)
        elif side == "top":
            md.polygon([(cp - c * 1.3, cy1 + 2), (cp + c * 1.3, cy1 + 2), (cp, cy1 - c)], fill=255)
        elif side == "left":
            md.polygon([(cx1 + 2, cp - c * 1.3), (cx1 + 2, cp + c * 1.3), (cx1 - c, cp)], fill=255)
        else:
            md.polygon([(cx2 - 2, cp - c * 1.3), (cx2 - 2, cp + c * 1.3), (cx2 + c, cp)], fill=255)

        # Soft drop shadow
        sh_alpha = mask.point(lambda a: int(a * 0.55))
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shadow.putalpha(sh_alpha)
        shadow = shadow.transform((W, H), Image.AFFINE, (1, 0, 0, 0, 1, -10), resample=Image.Resampling.BILINEAR)
        shadow = shadow.filter(ImageFilter.GaussianBlur(14))

        # Vertical gradient glass fill (250 alpha so background chat/desktop text doesn't bleed through)
        grad = Image.new("RGBA", (1, H))
        top_c, bot_c = (34, 39, 53), (16, 19, 27)
        for y in range(H):
            t = y / max(1, H - 1)
            grad.putpixel((0, y), (
                int(top_c[0] + (bot_c[0] - top_c[0]) * t),
                int(top_c[1] + (bot_c[1] - top_c[1]) * t),
                int(top_c[2] + (bot_c[2] - top_c[2]) * t),
                250,
            ))
        grad = grad.resize((W, H))
        card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        card.paste(grad, (0, 0), mask)

        # Crisp 1px hairline border from mask edge
        dil = mask.filter(ImageFilter.MaxFilter(5))
        edge = ImageChops.subtract(dil, mask).point(lambda a: int(a * 0.24))
        border = Image.new("RGBA", (W, H), (255, 255, 255, 0))
        border.putalpha(edge)

        # Subtle smooth inner top-edge highlight (avoids hard horizontal cut band across icons)
        sd_line = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sd_line)
        sd.rounded_rectangle([cx1 + 2, cy1 + 2, cx2 - 3, cy2 - 3], radius=R * ss, outline=(255, 255, 255, 28), width=2)
        sd_line.putalpha(ImageChops.multiply(sd_line.getchannel("A"), mask))

        hi = Image.alpha_composite(shadow, card)
        hi = Image.alpha_composite(hi, sd_line)
        hi = Image.alpha_composite(hi, border)
        bg = hi.resize((w, h), Image.Resampling.LANCZOS)

        # Minimalist layout: subtle footer divider only
        ox = M
        footer_y = h - M - self.footer_h - self.pad_bottom + 8
        alpha_line(bg, [(ox + self.pad_x, footer_y - 7), (w - M - self.pad_x, footer_y - 7)], fill=(255, 255, 255, 22), width=1)

        self._bg_cache = (key, bg)
        return bg

    @staticmethod
    def _wrap_label(draw, text, font, max_w, max_lines=2):
        words = text.replace("_", "_\u200b").split()
        lines, cur = [], ""
        tokens = []
        for wd in words:
            if draw.textlength(wd, font=font) <= max_w:
                tokens.append(wd)
            else:
                chunk = ""
                for ch in wd:
                    if draw.textlength(chunk + ch, font=font) > max_w and chunk:
                        tokens.append(chunk)
                        chunk = ch
                    else:
                        chunk += ch
                if chunk:
                    tokens.append(chunk)
        for tk_ in tokens:
            cand = (cur + " " + tk_).strip() if cur else tk_
            if draw.textlength(cand, font=font) <= max_w:
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                cur = tk_
        if cur:
            lines.append(cur)
        lines = [ln.replace("\u200b", "") for ln in lines]
        truncated = len(lines) > max_lines
        lines = lines[:max_lines]
        if truncated:
            last = lines[-1]
            while last and draw.textlength(last + "…", font=font) > max_w:
                last = last[:-1]
            lines[-1] = last + "…"
        return lines, truncated

    def _render(self, alpha_mult=1.0, rise=0):
        if not self.window or not self.window.winfo_exists():
            return

        w, h = self.win_w, self.win_h
        M = self.SHADOW_M
        ox, oy = M, M
        composite = self._build_background().copy()
        draw = ImageDraw.Draw(composite)
        show_labels, lbl_fam, lbl_sz, lbl_bold, lbl_italic, lbl_rgb = self._label_style()
        item_font = resolve_styled_font(lbl_fam, lbl_sz, bold=lbl_bold, italic=lbl_italic)
        line_step = max(13, int(lbl_sz * 1.28))
        btn_font = self._font("btn")
        tip_font = self._font("tip")
        items = self._get_items_list()

        self.action_boxes = []

        # Grid (centered inside card)
        self.item_boxes = []
        grid_top = oy + self.pad_top
        grid_w = self.cols * self.cell_w
        gx0 = ox + (self.card_w - grid_w) // 2
        hovered_tip = None

        if not items:
            draw.text((w / 2.0, grid_top + self.cell_h / 2.0 - 8), "Carpeta vacía", fill=(203, 213, 225, 235), font=self._font("empty"), anchor="mm")
            draw.text((w / 2.0, grid_top + self.cell_h / 2.0 + 12), "Clic derecho para añadir", fill=(148, 163, 184, 220), font=self._font("item"), anchor="mm")

        for idx, child in enumerate(items):
            row, col = divmod(idx, self.cols)
            cx1 = gx0 + col * self.cell_w
            cy1 = grid_top + row * self.cell_h
            cx2, cy2 = cx1 + self.cell_w, cy1 + self.cell_h
            self.item_boxes.append((cx1, cy1, cx2, cy2, child))

            is_drag_src = self.is_dragging and (idx == self.pressed_idx)
            is_drag_tgt = self.is_dragging and (idx == self.drag_target_idx) and (idx != self.pressed_idx)
            is_hov = (not self.is_dragging) and (idx == self.hovered_idx)
            if is_drag_tgt:
                alpha_rounded_rect(composite, [cx1 + 4, cy1 + 3, cx2 - 4, cy2 - 3], radius=14,
                                   fill=(59, 130, 246, 68), outline=(96, 165, 250, 235), width=2)
            elif is_hov:
                alpha_rounded_rect(composite, [cx1 + 4, cy1 + 3, cx2 - 4, cy2 - 3], radius=14,
                                   fill=(96, 165, 250, 46), outline=(147, 197, 253, 150), width=1)

            sz = self.ICON_SZ_HOVER if is_hov else self.ICON_SZ
            ic, sh = self._icon_variant(child.get("id"), sz)
            ix = cx1 + (self.cell_w - sz) // 2
            if show_labels:
                iy = cy1 + (8 if is_hov else 11)
            else:
                iy = cy1 + (self.cell_h - sz) // 2 - (2 if is_hov else 0)
            if ic is not None:
                if is_drag_src:
                    ic_dim = ic.copy()
                    ic_dim.putalpha(ic_dim.getchannel("A").point(lambda a: int(a * 0.35)))
                    composite.alpha_composite(ic_dim, (int(ix), int(iy)))
                else:
                    composite.alpha_composite(sh, (int(ix - 8), int(iy - 8)))
                    composite.alpha_composite(ic, (int(ix), int(iy)))

            full = child.get("name", "App") or "App"
            truncated = False
            if show_labels:
                lines, truncated = self._wrap_label(draw, full, item_font, self.cell_w - 10)
                ly = cy1 + 11 + self.ICON_SZ + max(9, int(lbl_sz * 0.75))
                col_txt = (255, 255, 255, 255) if (is_hov or is_drag_tgt) else ((148, 163, 184, 210) if is_drag_src else (*lbl_rgb, 255))
                for li, ln in enumerate(lines):
                    draw.text(((cx1 + cx2) / 2.0, ly + li * line_step), ln, fill=col_txt, font=item_font, anchor="mm")

            # Subtle running-app indicator dot below cell when open
            if self.get_item_windows_fn:
                try:
                    wins, _ = self.get_item_windows_fn(child)
                    if wins:
                        dcx = int((cx1 + cx2) // 2)
                        dcy = int(cy2 - 3 if show_labels else cy2 - 5)
                        alpha_ellipse(composite, [dcx - 3, dcy - 3, dcx + 3, dcy + 3], fill=(96, 165, 250, 245), outline=(255, 255, 255, 190), width=1)
                except Exception:
                    pass

            if is_hov and truncated and show_labels and not self.is_dragging:
                hovered_tip = (cx1, cy1, cx2, cy2, full)

        # Minimalist Footer: single centered "Desagrupar" pill button
        footer_y = h - M - self.footer_h - self.pad_bottom + 8
        btn_w = min(136, self.card_w - self.pad_x * 2)
        bh = 28
        bx1 = ox + (self.card_w - btn_w) // 2
        bx2 = bx1 + btn_w
        by1, by2 = footer_y, footer_y + bh
        self.action_boxes.append((bx1, by1, bx2, by2, "unpack"))

        hov = (self.hovered_action == "unpack")
        fill = (239, 68, 68, 155) if hov else (255, 255, 255, 16)
        outl = (252, 165, 165, 225) if hov else (255, 255, 255, 38)
        alpha_rounded_rect(composite, [bx1, by1, bx2, by2], radius=9, fill=fill, outline=outl, width=1)
        txt_col = (255, 255, 255, 255) if hov else (220, 228, 240, 255)
        label = "Desagrupar"
        tw = draw.textlength(label, font=btn_font)
        gw = 14
        sx = (bx1 + bx2) / 2.0 - (tw + gw) / 2.0
        cy = (by1 + by2) / 2.0
        alpha_rounded_rect(composite, [sx, cy - 4, sx + 4, cy], radius=1, fill=txt_col)
        alpha_rounded_rect(composite, [sx + 6, cy - 4, sx + 10, cy], radius=1, fill=txt_col)
        alpha_rounded_rect(composite, [sx, cy + 2, sx + 4, cy + 6], radius=1, fill=txt_col)
        alpha_rounded_rect(composite, [sx + 6, cy + 2, sx + 10, cy + 6], radius=1, fill=txt_col)
        draw.text((sx + gw, cy), label, fill=txt_col, font=btn_font, anchor="lm")

        # Floating lifted icon under cursor while drag-reordering
        if self.is_dragging and 0 <= self.pressed_idx < len(items):
            drag_child = items[self.pressed_idx]
            ic, sh = self._icon_variant(drag_child.get("id"), self.ICON_SZ_HOVER)
            if ic is not None:
                fx = max(4, min(w - self.ICON_SZ_HOVER - 4, int(self.drag_x - self.ICON_SZ_HOVER // 2)))
                fy = max(4, min(h - self.ICON_SZ_HOVER - 4, int(self.drag_y - self.ICON_SZ_HOVER // 2)))
                ghost = ic.copy()
                ghost.putalpha(ghost.getchannel("A").point(lambda a: int(a * 0.84)))
                composite.alpha_composite(sh, (fx - 8, fy - 8))
                composite.alpha_composite(ghost, (fx, fy))

        # Tooltip for truncated names (floats cleanly above hovered cell / top rim without touching labels!)
        if hovered_tip and not self.is_dragging:
            hx1, hy1, hx2, hy2, full = hovered_tip
            tb = draw.textbbox((0, 0), full, font=tip_font)
            tw = (tb[2] - tb[0]) + 20
            th = (tb[3] - tb[1]) + 10
            hcx = (hx1 + hx2) // 2
            tx1 = max(6, min(w - tw - 6, hcx - tw // 2))
            ty1 = max(2, hy1 - th + 6)
            ty2 = ty1 + th
            alpha_rounded_rect(composite, [tx1, ty1, tx1 + tw, ty2], radius=9, fill=(8, 10, 16, 252), outline=(96, 165, 250, 210), width=1)
            draw.text((tx1 + tw / 2.0, (ty1 + ty2) / 2.0), full, fill=(255, 255, 255, 255), font=tip_font, anchor="mm")

        if alpha_mult < 1.0 or rise:
            a = composite.getchannel("A").point(lambda v: int(v * alpha_mult))
            composite.putalpha(a)
            if rise:
                dy = rise if self.caret_side == "bottom" else (-rise if self.caret_side == "top" else 0)
                if 0 < dy < h:
                    shifted = Image.new("RGBA", composite.size, (0, 0, 0, 0))
                    shifted.alpha_composite(composite.crop((0, 0, w, h - dy)), (0, dy))
                    composite = shifted
                elif 0 < -dy < h:
                    shifted = Image.new("RGBA", composite.size, (0, 0, 0, 0))
                    shifted.alpha_composite(composite.crop((0, -dy, w, h)), (0, 0))
                    composite = shifted

        self.window.update_idletasks()
        hwnd = getattr(self, "top_hwnd", None) or user32.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
        if not update_layered_pil(hwnd, self.win_x, self.win_y, composite):
            final_img = flatten_pil_for_colorkey(composite, (1, 2, 3))
            self.photo_ref = ImageTk.PhotoImage(final_img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo_ref)

    def _on_motion(self, event):
        x, y = event.x, event.y
        new_hover = -1
        for idx, (x1, y1, x2, y2, child) in enumerate(self.item_boxes):
            if x1 <= x <= x2 and y1 <= y <= y2:
                new_hover = idx
                break

        new_action = None
        for x1, y1, x2, y2, action in self.action_boxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                new_action = action
                break

        if new_hover != self.hovered_idx or new_action != self.hovered_action:
            self.hovered_idx = new_hover
            self.hovered_action = new_action
            self._render()

    def _on_leave(self, event):
        if self.hovered_idx != -1 or self.hovered_action is not None:
            self.hovered_idx = -1
            self.hovered_action = None
            self._render()

    def _on_press(self, event):
        x, y = event.x, event.y
        self.press_x = x
        self.press_y = y
        self.press_time = time.monotonic()
        self.is_dragging = False
        self.drag_target_idx = -1
        self.pressed_action = None
        self.pressed_idx = -1

        for x1, y1, x2, y2, action in self.action_boxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                self.pressed_action = action
                return

        for idx, (x1, y1, x2, y2, _child) in enumerate(self.item_boxes):
            if x1 <= x <= x2 and y1 <= y <= y2:
                self.pressed_idx = idx
                return

    def _on_drag(self, event):
        if self.pressed_idx < 0:
            return
        if self.is_locked_fn and self.is_locked_fn():
            return
        if not self.is_dragging:
            if math.hypot(event.x - self.press_x, event.y - self.press_y) <= self.DRAG_THRESHOLD_PX:
                return
            self.is_dragging = True

        self.drag_x = event.x
        self.drag_y = event.y
        best_idx = self.pressed_idx
        best_dist = float("inf")
        for idx, (x1, y1, x2, y2, _child) in enumerate(self.item_boxes):
            cx, cy = (x1 + x2) * 0.5, (y1 + y2) * 0.5
            d = math.hypot(event.x - cx, event.y - cy)
            if d < best_dist:
                best_dist = d
                best_idx = idx
        self.drag_target_idx = best_idx
        self._render()

    def _on_release(self, event):
        held = time.monotonic() - self.press_time
        if self.is_dragging:
            src = self.pressed_idx
            dst = self.drag_target_idx
            self.is_dragging = False
            self.pressed_idx = -1
            self.drag_target_idx = -1
            self.pressed_action = None
            items = self._get_items_list()
            if 0 <= src < len(items) and 0 <= dst < len(items) and src != dst:
                moved = items.pop(src)
                items.insert(dst, moved)
                if self.on_group_modified:
                    self.on_group_modified()
            self._render()
            return

        src_idx = self.pressed_idx
        src_action = self.pressed_action
        self.pressed_idx = -1
        self.pressed_action = None
        self.drag_target_idx = -1

        # Guard: ignore long holds (>= LONG_PRESS_SEC) so accidental held clicks never launch
        if held >= self.LONG_PRESS_SEC:
            return

        x, y = event.x, event.y
        if src_action == "unpack":
            for x1, y1, x2, y2, action in self.action_boxes:
                if action == "unpack" and x1 <= x <= x2 and y1 <= y <= y2:
                    self._unpack_group()
                    return
            return

        if 0 <= src_idx < len(self.item_boxes):
            x1, y1, x2, y2, child = self.item_boxes[src_idx]
            if x1 <= x <= x2 and y1 <= y <= y2:
                self.close()
                if self.on_launch:
                    self.on_launch(child)

    def _activate_and_close(self, hwnd):
        self.close()
        if self.on_activate_window:
            self.on_activate_window(hwnd)

    def _close_child_windows(self, child):
        if self.on_close_windows:
            self.on_close_windows(child)
        if self.window and self.window.winfo_exists():
            self.parent.after(300, self._render)

    def _kill_child_process(self, child):
        if self.on_kill_process:
            self.on_kill_process(child)
        if self.window and self.window.winfo_exists():
            self.parent.after(280, self._render)

    def _on_right_click(self, event):
        x, y = event.x, event.y
        clicked_child = None
        for x1, y1, x2, y2, child in self.item_boxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                clicked_child = child
                break

        if self.active_menu:
            try:
                self.active_menu.close()
            except Exception:
                pass
            self.active_menu = None

        menu = CustomMenu(self.window)
        self.active_menu = menu
        if clicked_child:
            name = clicked_child.get("name", "Elemento")
            if self.get_item_windows_fn:
                try:
                    wins, can_kill = self.get_item_windows_fn(clicked_child)
                except Exception:
                    wins, can_kill = [], False
                if wins:
                    menu.add_command("Mostrar ventana abierta", lambda hw=wins[0]["hwnd"]: self._activate_and_close(hw))
                    c_lbl = "Cerrar ventana" if len(wins) == 1 else f"Cerrar ventanas ({len(wins)})"
                    menu.add_command(c_lbl, lambda c=clicked_child: self._close_child_windows(c), danger=not can_kill)
                    if can_kill:
                        menu.add_command("Finalizar tarea", lambda c=clicked_child: self._kill_child_process(c), danger=True)
            menu.add_command(f"Abrir {name}", lambda: self._launch_and_close(clicked_child))

            sec_el = menu.add_section("Editar elemento", expanded=False)
            sec_el.add_command("Cambiar icono...", lambda c=clicked_child: self._pick_child_icon(c))
            sec_el.add_command("Restaurar icono original", lambda c=clicked_child: self._restore_child_icon(c))
            sec_el.add_command("Cambiar nombre...", lambda c=clicked_child: self._rename_child(c))
            sec_el.add_separator()
            sec_el.add_command("Ejecutar como administrador", lambda: self._launch_as_admin(clicked_child))
            sec_el.add_command("Abrir ubicación del archivo", lambda: self._open_location(clicked_child))
            sec_el.add_separator()
            sec_el.add_command("Quitar de esta carpeta", lambda c=clicked_child: self._remove_from_group(c), danger=True)
            menu.add_separator()

        sec_folder = menu.add_section("Opciones de carpeta", expanded=(clicked_child is None))
        sec_folder.add_command("Añadir archivo a esta carpeta...", self._add_file_to_group)
        sec_folder.add_command("Cambiar icono de la carpeta...", self._pick_group_icon)
        if self.group_item.get("icon_path"):
            sec_folder.add_command("Restaurar icono original de la carpeta", self._restore_group_icon)
        sec_folder.add_command("Cambiar nombre de la carpeta...", self._rename_group)
        sec_folder.add_command("Desagrupar todos los elementos", self._unpack_group)
        menu.add_separator()
        menu.add_command("Cerrar", self.close)

        menu.show(event.x_root, event.y_root)

    def _launch_and_close(self, child):
        self.close()
        if self.on_launch:
            self.on_launch(child)

    def _launch_as_admin(self, child):
        self.close()
        target = child.get("target", "")
        args = child.get("args", "")
        try:
            shell32.ShellExecuteW(None, "runas", target, args, None, 1)
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo ejecutar como administrador:\n{e}", parent=self.window)

    def _open_location(self, child):
        target = child.get("target", "")
        if target and os.path.exists(target):
            subprocess.Popen(f'explorer.exe /select,"{os.path.abspath(target)}"')

    def _pick_group_icon(self):
        group_id = self.group_item.get("id")
        if self.on_pick_child_icon and group_id:
            self.close()
            self.on_pick_child_icon(group_id, None)
            return
        base_dir = os.path.dirname(os.path.abspath(__file__))
        initial_dir = os.path.join(base_dir, "assets", "icon_packs", "Carpetas de Colores")
        if not os.path.isdir(initial_dir):
            initial_dir = os.path.join(base_dir, "assets", "icon_packs")
        self.modal_open = True
        try:
            path = filedialog.askopenfilename(
                parent=self.window,
                initialdir=initial_dir,
                title="Seleccionar Icono para la Carpeta",
                filetypes=[("Iconos e imágenes", "*.png;*.ico;*.dll;*.exe;*.jpg;*.jpeg;*.bmp"), ("Todos", "*.*")]
            )
        finally:
            self.modal_open = False
            self.mouse_was_released = False
        if path:
            cached = icon_extractor.import_custom_icon(path) or path
            self.group_item["icon_path"] = cached
            if self.on_group_modified:
                self.on_group_modified()

    def _restore_group_icon(self):
        self.group_item["icon_path"] = ""
        if self.on_group_modified:
            self.on_group_modified()

    def _restore_child_icon(self, child):
        restored = icon_extractor.restore_original_icon(child)
        child["icon_path"] = restored
        self._load_icons()
        if self.on_group_modified:
            self.on_group_modified()
        self._render()

    def _pick_child_icon(self, child):
        group_id = self.group_item.get("id")
        child_id = child.get("id")
        if self.on_pick_child_icon and group_id and child_id:
            self.close()
            self.on_pick_child_icon(group_id, child_id)
            return
        # Fallback to native file dialog if callback not attached
        base_dir = os.path.dirname(os.path.abspath(__file__))
        initial_dir = os.path.join(base_dir, "assets", "icon_packs")
        self.modal_open = True
        try:
            path = filedialog.askopenfilename(
                parent=self.window,
                initialdir=initial_dir,
                title="Seleccionar Icono (PNG, ICO, DLL, EXE)",
                filetypes=[("Iconos e imágenes", "*.png;*.ico;*.dll;*.exe;*.jpg;*.jpeg;*.bmp"), ("Todos", "*.*")]
            )
        finally:
            self.modal_open = False
            self.mouse_was_released = False
        if path:
            cached = icon_extractor.import_custom_icon(path) or path
            child["icon_path"] = cached
            self._load_icons()
            if self.on_group_modified:
                self.on_group_modified()
            self._render()

    def _rename_child(self, child):
        old_name = child.get("name", "Elemento")
        if self.window and self.window.winfo_exists():
            self.window.lift()
        self.modal_open = True
        try:
            new_name = simpledialog.askstring("Cambiar Nombre", "Nuevo nombre del elemento:", initialvalue=old_name, parent=self.window)
        finally:
            self.modal_open = False
            self.mouse_was_released = False
        if new_name and new_name.strip():
            child["name"] = new_name.strip()
            if self.on_group_modified:
                self.on_group_modified()
            self._render()

    def _remove_from_group(self, child):
        items = self._get_items_list()
        if child in items:
            items.remove(child)
        self._load_icons()
        self._calculate_dimensions()
        if self.on_group_modified:
            self.on_group_modified()
        if len(items) == 0:
            self.close()
        else:
            self._apply_geometry()
            self._render()

    def _add_file_to_group(self):
        if self.window and self.window.winfo_exists():
            self.window.lift()
        self.modal_open = True
        try:
            path = filedialog.askopenfilename(
                parent=self.window,
                title="Añadir Programa o Archivo al Grupo",
                filetypes=[("Todos los archivos", "*.*")]
            )
        finally:
            self.modal_open = False
            self.mouse_was_released = False
        if path:
            item_data = icon_extractor.resolve_target_and_icon(path)
            new_child = {
                "id": str(icon_extractor.hashlib.md5(path.encode()).hexdigest()[:12]),
                "type": "app",
                "name": item_data["name"],
                "target": item_data["target"],
                "args": item_data.get("args", ""),
                "icon_path": item_data["icon_path"],
                "run_as_admin": False
            }
            items = self._get_items_list()
            items.append(new_child)
            self._load_icons()
            self._calculate_dimensions()
            self._apply_geometry()
            if self.on_group_modified:
                self.on_group_modified()
            self._render()

    def _rename_group(self):
        old_name = self.group_item.get("name", "Carpeta")
        if self.window and self.window.winfo_exists():
            self.window.lift()
        self.modal_open = True
        try:
            new_name = simpledialog.askstring("Renombrar Carpeta", "Nuevo nombre de la carpeta:", initialvalue=old_name, parent=self.window)
        finally:
            self.modal_open = False
            self.mouse_was_released = False
        if new_name and new_name.strip():
            self.group_item["name"] = new_name.strip()
            self._bg_cache = None
            if self.on_group_modified:
                self.on_group_modified()
            self._render()

    def _unpack_group(self):
        self.close()
        if self.on_group_modified:
            self.on_group_modified("ungroup", self.group_item.get("id"))

    def close(self):
        if getattr(self, "active_menu", None):
            try:
                self.active_menu.close()
            except Exception:
                pass
            self.active_menu = None

        if getattr(self, "_fade_job", None):
            try:
                self.parent.after_cancel(self._fade_job)
            except Exception:
                pass
            self._fade_job = None

        if self._check_job:
            try:
                self.parent.after_cancel(self._check_job)
            except Exception:
                pass
            self._check_job = None

        if getattr(self, "top_hwnd", None):
            release_layered_surface(self.top_hwnd)
            self.top_hwnd = None

        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None

        self.photo_ref = None
        self._bg_cache = None
        self.child_icons.clear()
        self._icon_variants.clear()
        self.item_boxes.clear()
        self.action_boxes.clear()

        if getattr(self, "on_close", None):
            try:
                self.on_close()
            except Exception:
                pass
