import time
import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageTk
import ctypes
from ctypes import wintypes

from window_utils import (
    set_tool_window,
    update_layered_pil,
    release_layered_surface,
    flatten_pil_for_colorkey,
    alpha_rounded_rect,
)

user32 = ctypes.windll.user32


class _MenuSection:
    """Builder handle returned by CustomMenu.add_section() for cascading side submenus."""

    def __init__(self, section_dict):
        self._section = section_dict

    def add_command(self, label, command, danger=False, checked=None):
        self._section["children"].append({
            "type": "item",
            "label": label,
            "command": command,
            "danger": danger,
            "checked": checked,
        })

    def add_separator(self):
        self._section["children"].append({"type": "separator"})

    def add_header(self, title):
        self._section["children"].append({
            "type": "header",
            "label": title,
        })


class CustomMenu:
    """
    Sleek macOS/Win11-style rounded, semi-transparent dark context menu.
    Features cascading side-expanding flyout submenus (with automatic left/right screen-edge flip),
    very subtle tinted rounded background bands separating logical sections,
    DPI scaling, razor-sharp fonts, and zero taskbar presence.
    """

    active_count = 0
    _active_menus = set()

    # Subtle translucent section band tints (RGBA fill + faint border)
    _BAND_PALETTE = (
        ((56, 139, 253, 15), (147, 197, 253, 22)),   # Cool Azure
        ((129, 140, 248, 14), (165, 180, 252, 20)),  # Soft Indigo
        ((45, 212, 191, 13), (94, 234, 212, 20)),    # Mist Teal
        ((148, 163, 184, 13), (203, 213, 225, 18)),  # Slate Glass
    )
    _DANGER_BAND = ((239, 68, 68, 15), (248, 113, 113, 24))

    @classmethod
    def sync_active_count(cls):
        stale = []
        for inst in list(cls._active_menus):
            win = getattr(inst, "menu_window", None)
            alive = False
            if win is not None:
                try:
                    alive = bool(win.winfo_exists())
                except Exception:
                    alive = False
            if not alive:
                stale.append(inst)
        for inst in stale:
            cls._active_menus.discard(inst)
            if getattr(inst, "_check_job", None):
                try:
                    inst.parent.after_cancel(inst._check_job)
                except Exception:
                    pass
                inst._check_job = None
            if getattr(inst, "top_hwnd", None):
                release_layered_surface(inst.top_hwnd)
                inst.top_hwnd = None
            inst.menu_window = None
            inst.canvas = None
            inst.photo_ref = None
        cls.active_count = len(cls._active_menus)
        return cls.active_count

    @classmethod
    def has_active_menu(cls):
        return cls.sync_active_count() > 0

    @classmethod
    def close_all(cls, exclude=None):
        for inst in list(cls._active_menus):
            if inst is not exclude:
                try:
                    inst.close()
                except Exception:
                    cls._active_menus.discard(inst)
        return cls.sync_active_count()

    @classmethod
    def is_point_inside_any(cls, x, y, pad=6):
        cls.sync_active_count()
        for inst in list(cls._active_menus):
            if inst._contains_screen_point(x, y, pad=pad):
                return True
        return False

    def _contains_screen_point(self, x, y, pad=6):
        win = getattr(self, "menu_window", None)
        if win is None:
            return False
        rect = wintypes.RECT()
        got_rect = False
        try:
            inner = win.winfo_id()
            top = self.top_hwnd or user32.GetAncestor(inner, 2) or inner
            if top and user32.GetWindowRect(top, ctypes.byref(rect)):
                if rect.right > rect.left and rect.bottom > rect.top:
                    got_rect = True
        except Exception:
            got_rect = False
        if not got_rect:
            rect.left = int(getattr(self, "_cur_x", 0))
            rect.top = int(getattr(self, "_cur_y", 0))
            rect.right = rect.left + int(getattr(self, "width", 0))
            rect.bottom = rect.top + int(getattr(self, "_cur_h", 0))
        return (rect.left - pad <= x <= rect.right + pad) and (rect.top - pad <= y <= rect.bottom + pad)

    def _contains_in_hierarchy(self, x, y, pad=6):
        if self._contains_screen_point(x, y, pad=pad):
            return True
        sub = self._active_submenu
        if sub is not None:
            return sub._contains_in_hierarchy(x, y, pad=pad)
        return False

    def __init__(self, parent, _is_submenu=False, _root_menu=None, _parent_menu=None):
        self.parent = parent
        self._is_submenu = bool(_is_submenu)
        self._root_menu = _root_menu if _root_menu is not None else self
        self._parent_menu = _parent_menu

        self.menu_window = None
        self.canvas = None
        self.items = []  # list of dicts: {'type': 'item'|'separator'|'header'|'section', ...}
        self.hovered_row_idx = -1
        self.transparent_key = "#010203"
        self.photo_ref = None

        # System DPI awareness
        try:
            dpi = user32.GetDpiForSystem()
            self.scale = max(1.0, dpi / 96.0)
        except Exception:
            self.scale = 1.0

        self.base_item_height = int(33 * self.scale)
        self.base_sep_height = int(9 * self.scale)
        self.base_header_height = int(20 * self.scale)

        self.item_height = self.base_item_height
        self.sep_height = self.base_sep_height
        self.header_height = self.base_header_height

        self.width = int(288 * self.scale)
        self.corner_radius = int(14 * self.scale)
        self.top_hwnd = None
        self.opened_time = 0.0
        self.mouse_was_released = False
        self.esc_was_released = False
        self._check_job = None

        # Cascading side submenu state
        self._active_submenu = None
        self._active_section_ref = None
        self._submenu_side = "right"
        self._submenu_open_job = None
        self._submenu_close_job = None
        self._pending_sec = None
        self._pending_row_y = 0

        self.anchor_x = 0
        self.anchor_y = 0
        self.anchor_bottom = False
        self.bottom_anchor_y = 0
        self._cur_x = 0
        self._cur_y = 0
        self._cur_h = 0
        self._layout = []
        self._section_bands = []

    def add_command(self, label, command, danger=False, checked=None):
        self.items.append({
            "type": "item",
            "label": label,
            "command": command,
            "danger": danger,
            "checked": checked,
        })

    def add_separator(self):
        self.items.append({"type": "separator"})

    def add_header(self, title):
        self.items.append({
            "type": "header",
            "label": title,
        })

    def add_section(self, label, expanded=False, exclusive=True):
        """Creates a nested submenu section that expands to the side on hover/click."""
        section_dict = {
            "type": "section",
            "label": label,
            "expanded": bool(expanded),
            "exclusive": bool(exclusive),
            "children": [],
        }
        self.items.append(section_dict)
        return _MenuSection(section_dict)

    @staticmethod
    def _get_fonts(scale):
        font_size = max(11, int(13 * scale))
        font_header_size = max(9, int(10 * scale))
        fcache = getattr(CustomMenu, "_font_cache", None)
        if fcache is None:
            fcache = CustomMenu._font_cache = {}
        fkey = (font_size, font_header_size)
        fonts = fcache.get(fkey)
        if fonts is None:
            try:
                fonts = (
                    ImageFont.truetype("segoeui.ttf", font_size),
                    ImageFont.truetype("seguisb.ttf", font_size),
                    ImageFont.truetype("segoeuib.ttf", font_header_size),
                )
            except Exception:
                d = ImageFont.load_default()
                fonts = (d, d, d)
            if len(fcache) >= 8:
                fcache.clear()
            fcache[fkey] = fonts
        return fonts

    def _compute_width(self):
        s = self.scale
        font, font_sec, font_header = self._get_fonts(s)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        max_w = 0.0
        has_check = False
        has_chev = False
        for it in self.items:
            t = it.get("type")
            lbl = str(it.get("label") or "")
            if t == "separator":
                continue
            if t == "header":
                w_txt = probe.textlength(lbl.upper(), font=font_header) + int(34 * s)
            elif t == "section":
                has_chev = True
                w_txt = probe.textlength(lbl, font=font_sec) + int(66 * s)
            else:
                if it.get("checked") is not None:
                    has_check = True
                w_txt = probe.textlength(lbl, font=font) + int(44 * s) + (int(18 * s) if it.get("checked") is not None else 0)
            if w_txt > max_w:
                max_w = w_txt
        min_w = int((230 if self._is_submenu else 264) * s)
        max_cap = int((340 if self._is_submenu else 356) * s)
        extra = (int(8 * s) if has_check else 0) + (int(8 * s) if has_chev else 0)
        return max(min_w, min(max_cap, int(round(max_w + extra))))

    def _build_layout(self, compact_ratio=1.0):
        s = self.scale
        self.width = self._compute_width()
        if compact_ratio < 1.0:
            self.item_height = max(int(24 * s), int(self.base_item_height * compact_ratio))
            self.header_height = max(int(16 * s), int(self.base_header_height * max(0.85, compact_ratio)))
            self.sep_height = max(int(6 * s), int(self.base_sep_height * max(0.75, compact_ratio)))
        else:
            self.item_height = self.base_item_height
            self.header_height = self.base_header_height
            self.sep_height = self.base_sep_height

        # Filter out leading, trailing, and consecutive duplicate separators cleanly
        cleaned = []
        for it in self.items:
            if it.get("type") == "separator":
                if not cleaned or cleaned[-1].get("type") == "separator":
                    continue
            cleaned.append(it)
        while cleaned and cleaned[-1].get("type") == "separator":
            cleaned.pop()

        rows = []
        groups = []
        current_group = []
        for it in cleaned:
            it_type = it.get("type")
            if it_type == "separator":
                if current_group:
                    groups.append(current_group)
                    current_group = []
            elif it_type == "header" and current_group:
                # Start a fresh tinted section band when a new header appears
                groups.append(current_group)
                current_group = [it]
            else:
                current_group.append(it)
        if current_group:
            groups.append(current_group)

        bands = []
        cur_y = int(8 * s)
        band_pad = int(3 * s)
        band_gap = int(6 * s)

        for g_idx, grp in enumerate(groups):
            band_y1 = cur_y
            cur_y += band_pad
            has_non_danger = False
            has_danger = False
            for it in grp:
                it_type = it.get("type")
                if it_type == "header":
                    y2 = cur_y + self.header_height
                    rows.append((cur_y, y2, {"kind": "header", "label": it["label"]}))
                    cur_y = y2
                elif it_type == "section":
                    y2 = cur_y + self.item_height
                    rows.append((cur_y, y2, {"kind": "section", "label": it["label"], "ref": it}))
                    cur_y = y2
                    has_non_danger = True
                else:
                    y2 = cur_y + self.item_height
                    is_d = bool(it.get("danger", False))
                    if is_d:
                        has_danger = True
                    else:
                        has_non_danger = True
                    rows.append((cur_y, y2, {
                        "kind": "item",
                        "label": it["label"],
                        "command": it.get("command"),
                        "danger": is_d,
                        "checked": it.get("checked"),
                    }))
                    cur_y = y2
            cur_y += band_pad
            band_y2 = cur_y
            band_kind = "danger" if (has_danger and not has_non_danger) else (g_idx % len(self._BAND_PALETTE))
            bands.append((band_y1, band_y2, band_kind))

            if g_idx < len(groups) - 1:
                sep_y1 = cur_y
                sep_y2 = cur_y + band_gap
                rows.append((sep_y1, sep_y2, {"kind": "separator"}))
                cur_y = sep_y2

        total_h = cur_y + int(8 * s)
        self._layout = rows
        self._section_bands = bands
        self._cur_h = total_h
        return rows, bands, total_h

    def _row_index_at(self, y):
        for i, (y1, y2, row) in enumerate(self._layout):
            if y1 <= y < y2 and row["kind"] in ("item", "section"):
                return i
        return -1

    def show(self, x, y):
        if not self._is_submenu:
            CustomMenu.close_all(exclude=self)
        self._close_self_only()

        self.opened_time = time.time()
        self.mouse_was_released = False
        self.esc_was_released = False
        self.hovered_row_idx = -1

        self.menu_window = tk.Toplevel(self.parent)
        self.menu_window.overrideredirect(True)
        self.menu_window.attributes("-topmost", True)
        self.menu_window.config(bg=self.transparent_key)

        self.top_hwnd = set_tool_window(
            self.menu_window,
            owner_hwnd=self.parent.winfo_id(),
            layered=True,
            no_activate=True,
        )
        CustomMenu._active_menus.add(self)
        CustomMenu.sync_active_count()

        sw = self.parent.winfo_screenwidth()
        sh = self.parent.winfo_screenheight()

        _, _, h = self._build_layout(compact_ratio=1.0)
        if h > sh - 44:
            _, _, h = self._build_layout(compact_ratio=max(0.72, (sh - 44) / h))

        x = min(max(8, int(x)), sw - self.width - 10)
        if not self._is_submenu and int(y) + h > sh - 12:
            self.anchor_bottom = True
            self.bottom_anchor_y = min(sh - 12, max(int(y), h + 12))
            win_y = max(10, self.bottom_anchor_y - h)
        else:
            self.anchor_bottom = False
            self.anchor_y = max(10, min(int(y), sh - h - 10))
            win_y = self.anchor_y

        self.anchor_x = x
        self._cur_x = x
        self._cur_y = win_y
        self._cur_h = h

        self.menu_window.geometry(f"{self.width}x{h}+{int(x)}+{int(win_y)}")

        self.canvas = tk.Canvas(
            self.menu_window,
            width=self.width,
            height=h,
            bg=self.transparent_key,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Leave>", self._on_leave)
        self.menu_window.bind("<Escape>", lambda e: self._root_menu.close())
        self.menu_window.bind("<Destroy>", self._on_window_destroy)

        self.menu_window.update_idletasks()
        self._render(h, x=x, y=win_y)
        if not self._is_submenu:
            self._check_click_outside()

    def _on_window_destroy(self, event=None):
        if event is not None and event.widget is not self.menu_window:
            return
        self._cancel_submenu_timers()
        if self._check_job:
            try:
                self.parent.after_cancel(self._check_job)
            except Exception:
                pass
            self._check_job = None
        if self.top_hwnd:
            release_layered_surface(self.top_hwnd)
            self.top_hwnd = None
        self.menu_window = None
        self.canvas = None
        self.photo_ref = None
        self.hovered_row_idx = -1
        CustomMenu._active_menus.discard(self)
        CustomMenu.sync_active_count()

    def _check_click_outside(self):
        if self._is_submenu or not self.menu_window or not self.menu_window.winfo_exists():
            return

        now = time.time()
        esc_down = bool(user32.GetAsyncKeyState(0x1B) & 0x8000)
        if not esc_down:
            self.esc_was_released = True
        elif self.esc_was_released and (now - self.opened_time > 0.10):
            self.close()
            return

        l_down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        r_down = bool(user32.GetAsyncKeyState(0x02) & 0x8000)

        if not l_down and not r_down:
            self.mouse_was_released = True

        if self.mouse_was_released and (now - self.opened_time > 0.15):
            if l_down or r_down:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                if not self._contains_in_hierarchy(pt.x, pt.y, pad=4):
                    self.close()
                    return

        self._check_job = self.parent.after(40, self._check_click_outside)

    def render_to_pil(self):
        if not self._layout:
            self._build_layout(compact_ratio=1.0)
        w = self.width
        h = self._cur_h
        s = self.scale

        # 2x supersampled crisp dark glass card
        ss = 2
        hi = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        ImageDraw.Draw(hi).rounded_rectangle(
            [4, 4, w * ss - 5, h * ss - 5],
            radius=self.corner_radius * ss,
            fill=(18, 21, 28, 244),
            outline=(255, 255, 255, 50),
            width=2,
        )
        img = hi.resize((w, h), Image.Resampling.LANCZOS)

        # Draw very subtle color-tinted rounded section bands
        band_rad = int(9 * s)
        for by1, by2, band_kind in self._section_bands:
            if band_kind == "danger":
                b_fill, b_out = self._DANGER_BAND
            else:
                b_fill, b_out = self._BAND_PALETTE[int(band_kind) % len(self._BAND_PALETTE)]
            alpha_rounded_rect(
                img,
                [8, by1, w - 8, by2],
                radius=band_rad,
                fill=b_fill,
                outline=b_out,
                width=1,
            )

        ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(ov)
        font, font_sec, font_header = self._get_fonts(s)

        for i, (y1, y2, row) in enumerate(self._layout):
            kind = row["kind"]
            if kind == "separator":
                sep_y = (y1 + y2) // 2
                draw.line([(16, sep_y), (w - 16, sep_y)], fill=(255, 255, 255, 22), width=1)

            elif kind == "header":
                header_y = (y1 + y2) // 2
                draw.text((16, header_y), row["label"].upper(), fill=(148, 163, 184, 215), font=font_header, anchor="lm")

            elif kind == "section":
                sec = row["ref"]
                is_open_sub = (self._active_section_ref is sec and self._active_submenu is not None)
                is_hovered = (i == self.hovered_row_idx)
                item_rect = [11, y1 + 1, w - 11, y2 - 1]

                if is_hovered or is_open_sub:
                    fill_c = (59, 130, 246, 112) if is_hovered else (59, 130, 246, 52)
                    out_c = (96, 165, 250, 195) if is_hovered else (96, 165, 250, 115)
                    alpha_rounded_rect(
                        img,
                        item_rect,
                        radius=int(8 * s),
                        fill=fill_c,
                        outline=out_c,
                        width=1,
                    )

                mid_y = (y1 + y2) // 2
                label_color = (255, 255, 255, 255) if (is_hovered or is_open_sub) else (226, 232, 240, 245)
                draw.text((int(19 * s), mid_y), row["label"], fill=label_color, font=font_sec, anchor="lm")

                # Side flyout chevron indicator (> normally, or < when opened on left edge)
                cx = w - int(22 * s)
                cy = mid_y
                chev_color = (255, 255, 255, 255) if is_hovered else ((96, 165, 250, 255) if is_open_sub else (148, 163, 184, 225))
                if is_open_sub and self._submenu_side == "left":
                    pts = [(cx + 2.5 * s, cy - 4 * s), (cx - 2.5 * s, cy), (cx + 2.5 * s, cy + 4 * s)]
                else:
                    pts = [(cx - 2.5 * s, cy - 4 * s), (cx + 2.5 * s, cy), (cx - 2.5 * s, cy + 4 * s)]
                draw.line(pts, fill=chev_color, width=max(2, int(1.8 * s)), joint="curve")

            else:
                is_hovered = (i == self.hovered_row_idx)
                item_rect = [11, y1 + 1, w - 11, y2 - 1]

                if is_hovered:
                    if row.get("danger"):
                        fill_color = (220, 38, 38, 195)
                        border_color = (248, 113, 113, 210)
                    else:
                        fill_color = (59, 130, 246, 112)
                        border_color = (96, 165, 250, 195)
                    alpha_rounded_rect(
                        img,
                        item_rect,
                        radius=int(8 * s),
                        fill=fill_color,
                        outline=border_color,
                        width=1,
                    )

                if row.get("danger"):
                    text_color = (255, 255, 255, 255) if is_hovered else (248, 113, 113, 250)
                else:
                    text_color = (255, 255, 255, 255) if is_hovered else (226, 232, 240, 245)

                mid_y = (y1 + y2) // 2
                checked = row.get("checked")
                base_lx = int(19 * s)
                label_x = base_lx
                if checked is not None:
                    if checked:
                        check_color = (255, 255, 255, 255) if is_hovered else (96, 165, 250, 255)
                        cx = base_lx
                        cy = mid_y
                        draw.line(
                            [(cx, cy), (cx + int(3.5 * s), cy + int(3.5 * s)), (cx + int(9.5 * s), cy - int(4 * s))],
                            fill=check_color,
                            width=max(2, int(2 * s)),
                            joint="curve",
                        )
                    label_x = base_lx + int(16 * s)

                draw.text((label_x, mid_y), row["label"], fill=text_color, font=font, anchor="lm")

        return Image.alpha_composite(img, ov)

    def _render(self, h, x=None, y=None):
        if not self.menu_window or not self.menu_window.winfo_exists():
            return

        img = self.render_to_pil()
        self.menu_window.update_idletasks()
        wx = self._cur_x if x is None else int(x)
        wy = self._cur_y if y is None else int(y)
        hwnd = self.top_hwnd or user32.GetAncestor(self.menu_window.winfo_id(), 2) or self.menu_window.winfo_id()
        if not update_layered_pil(hwnd, wx, wy, img):
            final_img = flatten_pil_for_colorkey(img, (1, 2, 3))
            self.photo_ref = ImageTk.PhotoImage(final_img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo_ref)

    SUBMENU_OPEN_MS = 95
    SUBMENU_CLOSE_MS = 185

    def _cancel_submenu_timers(self):
        for attr in ("_submenu_open_job", "_submenu_close_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.parent.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._pending_sec = None

    def _close_submenu(self):
        sub = self._active_submenu
        self._active_submenu = None
        had_sec = self._active_section_ref is not None
        self._active_section_ref = None
        if sub is not None:
            try:
                sub.close()
            except Exception:
                pass
        if had_sec and self.menu_window and self.menu_window.winfo_exists():
            self._render(self._cur_h)

    def _open_submenu_for(self, sec, row_y1):
        if self._active_section_ref is sec and self._active_submenu is not None:
            if self._active_submenu.menu_window and self._active_submenu.menu_window.winfo_exists():
                return
        self._close_submenu()
        children = sec.get("children") or []
        if not children or not self.menu_window or not self.menu_window.winfo_exists():
            return

        sub = CustomMenu(
            self.parent,
            _is_submenu=True,
            _root_menu=self._root_menu,
            _parent_menu=self,
        )
        for ch in children:
            t = ch.get("type")
            if t == "separator":
                sub.add_separator()
            elif t == "header":
                sub.add_header(ch.get("label", ""))
            else:
                sub.add_command(
                    ch.get("label", ""),
                    ch.get("command"),
                    danger=ch.get("danger", False),
                    checked=ch.get("checked"),
                )

        sw = self.parent.winfo_screenwidth()
        sh = self.parent.winfo_screenheight()
        _, _, sub_h = sub._build_layout(compact_ratio=1.0)
        if sub_h > sh - 44:
            _, _, sub_h = sub._build_layout(compact_ratio=max(0.72, (sh - 44) / sub_h))
        sub_w = sub.width

        overlap = int(4 * self.scale)
        right_x = self._cur_x + self.width - overlap
        if right_x + sub_w <= sw - 8:
            sub_x = right_x
            self._submenu_side = "right"
        else:
            left_x = self._cur_x - sub_w + overlap
            if left_x >= 8:
                sub_x = left_x
                self._submenu_side = "left"
            else:
                # Pick side with more room
                if (sw - (self._cur_x + self.width)) >= self._cur_x:
                    sub_x = max(8, sw - sub_w - 8)
                    self._submenu_side = "right"
                else:
                    sub_x = 8
                    self._submenu_side = "left"

        sub_y = max(10, min(self._cur_y + int(row_y1) - int(6 * self.scale), sh - sub_h - 10))

        self._active_submenu = sub
        self._active_section_ref = sec
        sub.show(sub_x, sub_y)
        self._render(self._cur_h)

    def _on_motion(self, event):
        # Entering/moving inside this menu cancels parent's pending submenu close!
        if self._parent_menu is not None and self._parent_menu._submenu_close_job:
            try:
                self.parent.after_cancel(self._parent_menu._submenu_close_job)
            except Exception:
                pass
            self._parent_menu._submenu_close_job = None

        new_hover = self._row_index_at(event.y)
        if new_hover != self.hovered_row_idx:
            self.hovered_row_idx = new_hover
            self._render(self._cur_h)

        sec = None
        row_y1 = 0
        if 0 <= new_hover < len(self._layout):
            y1, _, row = self._layout[new_hover]
            if row["kind"] == "section" and row["ref"].get("children"):
                sec = row["ref"]
                row_y1 = y1

        if sec is not None:
            if self._submenu_close_job:
                try:
                    self.parent.after_cancel(self._submenu_close_job)
                except Exception:
                    pass
                self._submenu_close_job = None
            if self._active_section_ref is sec and self._active_submenu is not None:
                return
            if self._pending_sec is sec and self._submenu_open_job:
                return
            if self._submenu_open_job:
                try:
                    self.parent.after_cancel(self._submenu_open_job)
                except Exception:
                    pass
            self._pending_sec = sec
            self._pending_row_y = row_y1
            self._submenu_open_job = self.parent.after(
                self.SUBMENU_OPEN_MS,
                lambda s_ref=sec, ry=row_y1: self._run_submenu_open(s_ref, ry),
            )
        else:
            if self._submenu_open_job:
                try:
                    self.parent.after_cancel(self._submenu_open_job)
                except Exception:
                    pass
                self._submenu_open_job = None
                self._pending_sec = None
            if self._active_submenu is not None and not self._submenu_close_job:
                self._submenu_close_job = self.parent.after(self.SUBMENU_CLOSE_MS, self._maybe_close_submenu_if_outside)

    def _run_submenu_open(self, sec_ref, row_y1):
        self._submenu_open_job = None
        self._pending_sec = None
        if not self.menu_window or not self.menu_window.winfo_exists():
            return
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return
        if not self._contains_screen_point(pt.x, pt.y, pad=2):
            return
        idx = self._row_index_at(pt.y - self._cur_y)
        if 0 <= idx < len(self._layout):
            y1, _, row = self._layout[idx]
            if row["kind"] == "section" and row.get("ref") is sec_ref:
                self._open_submenu_for(sec_ref, y1)

    def _maybe_close_submenu_if_outside(self):
        self._submenu_close_job = None
        if self._active_submenu is None:
            return
        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            if self._active_submenu._contains_in_hierarchy(pt.x, pt.y, pad=6):
                return
            if self._contains_screen_point(pt.x, pt.y, pad=2):
                idx = self._row_index_at(pt.y - self._cur_y)
                if 0 <= idx < len(self._layout) and self._layout[idx][2].get("ref") is self._active_section_ref:
                    return
        self._close_submenu()

    def _on_leave(self, _event):
        if self.hovered_row_idx != -1:
            self.hovered_row_idx = -1
            self._render(self._cur_h)
        if self._submenu_open_job:
            try:
                self.parent.after_cancel(self._submenu_open_job)
            except Exception:
                pass
            self._submenu_open_job = None
            self._pending_sec = None
        # Do NOT immediately close open side submenu on Leave; wait subtly so diagonal cursor travel works cleanly
        if self._active_submenu is not None and not self._submenu_close_job:
            self._submenu_close_job = self.parent.after(260, self._maybe_close_submenu_if_outside)

    def _on_click(self, event):
        idx = self._row_index_at(event.y)
        if idx == -1 or not (0 <= idx < len(self._layout)):
            return

        y1, _, row = self._layout[idx]
        if row["kind"] == "section":
            sec = row["ref"]
            self._cancel_submenu_timers()
            if self._active_section_ref is sec and self._active_submenu is not None:
                self._close_submenu()
            else:
                self._open_submenu_for(sec, y1)
            return

        cmd = row.get("command")
        self._root_menu.close()
        if cmd:
            cmd()

    def _close_self_only(self):
        CustomMenu._active_menus.discard(self)
        self._cancel_submenu_timers()
        if self._active_submenu is not None:
            try:
                self._active_submenu.close()
            except Exception:
                pass
            self._active_submenu = None
        self._active_section_ref = None

        if self._check_job:
            try:
                self.parent.after_cancel(self._check_job)
            except Exception:
                pass
            self._check_job = None

        if self.top_hwnd:
            release_layered_surface(self.top_hwnd)
            self.top_hwnd = None

        if self.menu_window:
            win = self.menu_window
            self.menu_window = None
            try:
                win.destroy()
            except Exception:
                pass

        self.canvas = None
        self.photo_ref = None
        self.hovered_row_idx = -1

    def close(self):
        self._close_self_only()
        CustomMenu.sync_active_count()
