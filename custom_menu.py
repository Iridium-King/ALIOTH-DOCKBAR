import time
import tkinter as tk
from PIL import Image, ImageTk, ImageDraw, ImageFont
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
    """Builder handle returned by CustomMenu.add_section() for nested commands."""

    def __init__(self, section_dict):
        self._section = section_dict

    def add_command(self, label, command, danger=False, checked=None):
        self._section['children'].append({
            'type': 'item',
            'label': label,
            'command': command,
            'danger': danger,
            'checked': checked,
        })

    def add_separator(self):
        self._section['children'].append({'type': 'separator'})


class CustomMenu:
    """
    Sleek, modern macOS/Win11-style rounded, semi-transparent dark context menu.
    Supports inline collapsible/expandable accordion sections, DPI scaling,
    razor-sharp fonts, vector checkmarks/chevrons, and zero taskbar presence.
    """
    active_count = 0
    _active_menus = set()

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
            win = getattr(inst, "menu_window", None)
            if win is None:
                continue
            rect = wintypes.RECT()
            got_rect = False
            try:
                inner = win.winfo_id()
                top = inst.top_hwnd or user32.GetAncestor(inner, 2) or inner
                if top and user32.GetWindowRect(top, ctypes.byref(rect)):
                    if rect.right > rect.left and rect.bottom > rect.top:
                        got_rect = True
            except Exception:
                got_rect = False
            if not got_rect:
                rect.left = int(getattr(inst, "_cur_x", 0))
                rect.top = int(getattr(inst, "_cur_y", 0))
                rect.right = rect.left + int(getattr(inst, "width", 0))
                rect.bottom = rect.top + int(getattr(inst, "_cur_h", 0))
            if (rect.left - pad <= x <= rect.right + pad) and (rect.top - pad <= y <= rect.bottom + pad):
                return True
        return False

    def __init__(self, parent):
        self.parent = parent
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

        self.base_item_height = int(34 * self.scale)
        self.base_subitem_height = int(29 * self.scale)
        self.base_sep_height = int(8 * self.scale)
        self.base_header_height = int(20 * self.scale)

        self.item_height = self.base_item_height
        self.subitem_height = self.base_subitem_height
        self.sep_height = self.base_sep_height
        self.header_height = self.base_header_height
        self.tray_pad_y = int(4 * self.scale)

        self.width = int(300 * self.scale)
        self.corner_radius = int(14 * self.scale)
        self.top_hwnd = None
        self.opened_time = 0
        self.mouse_was_released = False
        self.esc_was_released = False
        self._check_job = None

        # Hover-to-expand (vertical accordion) state
        self._hover_expand_job = None
        self._hover_expand_sec = None
        self._last_auto_expand_time = 0.0
        self._last_auto_expand_sec = None
        self._last_auto_expand_pt = None

        self.anchor_x = 0
        self.anchor_y = 0
        self.anchor_bottom = False
        self.bottom_anchor_y = 0
        self._cur_x = 0
        self._cur_y = 0
        self._cur_h = 0
        self._layout = []
        self._trays = []

    def add_command(self, label, command, danger=False, checked=None):
        self.items.append({
            'type': 'item',
            'label': label,
            'command': command,
            'danger': danger,
            'checked': checked,
        })

    def add_separator(self):
        self.items.append({'type': 'separator'})

    def add_header(self, title):
        self.items.append({
            'type': 'header',
            'label': title,
        })

    def add_section(self, label, expanded=False, exclusive=True):
        section_dict = {
            'type': 'section',
            'label': label,
            'expanded': bool(expanded),
            'exclusive': bool(exclusive),
            'children': [],
        }
        self.items.append(section_dict)
        return _MenuSection(section_dict)

    def _build_layout(self, compact_ratio=1.0):
        s = self.scale
        if compact_ratio < 1.0:
            self.item_height = max(int(24 * s), int(self.base_item_height * compact_ratio))
            self.subitem_height = max(int(22 * s), int(self.base_subitem_height * compact_ratio))
            self.header_height = max(int(16 * s), int(self.base_header_height * max(0.85, compact_ratio)))
            self.sep_height = max(int(6 * s), int(self.base_sep_height * max(0.75, compact_ratio)))
        else:
            self.item_height = self.base_item_height
            self.subitem_height = self.base_subitem_height
            self.header_height = self.base_header_height
            self.sep_height = self.base_sep_height

        rows = []
        trays = []
        cur_y = int(8 * s)

        for it in self.items:
            it_type = it['type']
            if it_type == 'separator':
                y2 = cur_y + self.sep_height
                rows.append((cur_y, y2, {'kind': 'separator', 'sub': False}))
                cur_y = y2
            elif it_type == 'header':
                y2 = cur_y + self.header_height
                rows.append((cur_y, y2, {'kind': 'header', 'label': it['label']}))
                cur_y = y2
            elif it_type == 'section':
                y2 = cur_y + self.item_height
                rows.append((cur_y, y2, {'kind': 'section', 'label': it['label'], 'ref': it}))
                cur_y = y2
                children = it.get('children') or []
                if it.get('expanded') and children:
                    tray_y1 = cur_y + int(2 * s)
                    cur_y = tray_y1 + self.tray_pad_y
                    for ch in children:
                        if ch['type'] == 'separator':
                            cy2 = cur_y + max(6, self.sep_height - 2)
                            rows.append((cur_y, cy2, {'kind': 'separator', 'sub': True}))
                            cur_y = cy2
                        else:
                            cy2 = cur_y + self.subitem_height
                            rows.append((cur_y, cy2, {
                                'kind': 'subitem',
                                'label': ch['label'],
                                'command': ch.get('command'),
                                'danger': ch.get('danger', False),
                                'checked': ch.get('checked'),
                            }))
                            cur_y = cy2
                    tray_y2 = cur_y + self.tray_pad_y
                    trays.append((tray_y1, tray_y2))
                    cur_y = tray_y2 + int(4 * s)
            else:
                y2 = cur_y + self.item_height
                rows.append((cur_y, y2, {
                    'kind': 'item',
                    'label': it['label'],
                    'command': it.get('command'),
                    'danger': it.get('danger', False),
                    'checked': it.get('checked'),
                }))
                cur_y = y2

        total_h = cur_y + int(8 * s)
        self._layout = rows
        self._trays = trays
        self._cur_h = total_h
        return rows, trays, total_h

    def _row_index_at(self, y):
        for i, (y1, y2, row) in enumerate(self._layout):
            if y1 <= y < y2 and row['kind'] in ('item', 'section', 'subitem'):
                return i
        return -1

    def show(self, x, y):
        CustomMenu.close_all(exclude=self)
        self.close()

        self.opened_time = time.time()
        self.mouse_was_released = False
        self.esc_was_released = False
        self.hovered_row_idx = -1

        self.menu_window = tk.Toplevel(self.parent)
        self.menu_window.overrideredirect(True)
        self.menu_window.attributes("-topmost", True)
        self.menu_window.config(bg=self.transparent_key)

        # Ensure no taskbar button + layered true per-pixel alpha + no focus stealing
        self.top_hwnd = set_tool_window(self.menu_window, owner_hwnd=self.parent.winfo_id(), layered=True, no_activate=True)
        CustomMenu._active_menus.add(self)
        CustomMenu.sync_active_count()

        sw = self.parent.winfo_screenwidth()
        sh = self.parent.winfo_screenheight()

        _, _, h = self._build_layout(compact_ratio=1.0)
        if h > sh - 60:
            _, _, h = self._build_layout(compact_ratio=max(0.75, (sh - 60) / h))

        x = min(max(8, int(x)), sw - self.width - 12)
        if int(y) + h > sh - 12:
            self.anchor_bottom = True
            self.bottom_anchor_y = min(sh - 12, max(int(y), h + 12))
            win_y = max(12, self.bottom_anchor_y - h)
        else:
            self.anchor_bottom = False
            self.anchor_y = max(12, int(y))
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
        self.menu_window.bind("<Escape>", lambda e: self.close())
        self.menu_window.bind("<Destroy>", self._on_window_destroy)

        self.menu_window.update_idletasks()
        self._render(h, x=x, y=win_y)
        self._check_click_outside()

    def _on_window_destroy(self, event=None):
        if event is not None and event.widget is not self.menu_window:
            return
        self._cancel_hover_expand()
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
        self.hovered_row_idx = -1
        CustomMenu._active_menus.discard(self)
        CustomMenu.sync_active_count()

    def _section_row_y(self, sec):
        for y1, y2, row in self._layout:
            if row['kind'] == 'section' and row.get('ref') is sec:
                return y1, y2
        return None

    def _relayout_and_render(self, pin_section=None, pin_screen_y=None):
        if not self.menu_window or not self.menu_window.winfo_exists():
            return
        sw = self.parent.winfo_screenwidth()
        sh = self.parent.winfo_screenheight()

        _, _, h = self._build_layout(compact_ratio=1.0)
        if h > sh - 48:
            _, _, h = self._build_layout(compact_ratio=max(0.75, (sh - 48) / h))

        win_x = min(max(8, int(self.anchor_x)), sw - self.width - 12)
        if self.anchor_bottom:
            win_y = max(12, self.bottom_anchor_y - h)
        else:
            win_y = max(12, min(self.anchor_y, sh - h - 12))

        # Keep the toggled section row pinned under the cursor (children unfold vertically below it)
        if pin_section is not None and pin_screen_y is not None:
            ry = self._section_row_y(pin_section)
            if ry:
                win_y = max(12, min(int(pin_screen_y - ry[0]), sh - h - 12))
                self.anchor_bottom = False
                self.anchor_y = win_y

        self._cur_x = win_x
        self._cur_y = win_y
        self._cur_h = h

        self.menu_window.geometry(f"{self.width}x{h}+{int(win_x)}+{int(win_y)}")
        if self.canvas:
            self.canvas.config(width=self.width, height=h)

        # Re-evaluate hovered row against current cursor position after geometry resize
        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            rel_x = pt.x - win_x
            rel_y = pt.y - win_y
            if 0 <= rel_x <= self.width and 0 <= rel_y <= h:
                self.hovered_row_idx = self._row_index_at(rel_y)
            else:
                self.hovered_row_idx = -1
        else:
            self.hovered_row_idx = -1

        self.menu_window.update_idletasks()
        self._render(h, x=win_x, y=win_y)

    def _check_click_outside(self):
        if not self.menu_window or not self.menu_window.winfo_exists():
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

        # Wait until initial click release before detecting subsequent clicks
        if not l_down and not r_down:
            self.mouse_was_released = True

        if self.mouse_was_released and (now - self.opened_time > 0.15):
            if l_down or r_down:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                rect = wintypes.RECT()
                inner = self.menu_window.winfo_id()
                top = self.top_hwnd or user32.GetAncestor(inner, 2) or inner
                got_rect = bool(top and user32.GetWindowRect(top, ctypes.byref(rect)))
                if not got_rect or rect.right <= rect.left or rect.bottom <= rect.top:
                    rect.left = int(self._cur_x)
                    rect.top = int(self._cur_y)
                    rect.right = rect.left + int(self.width)
                    rect.bottom = rect.top + int(self._cur_h)
                # 3px tolerance
                if not (rect.left - 3 <= pt.x <= rect.right + 3 and rect.top - 3 <= pt.y <= rect.bottom + 3):
                    self.close()
                    return

        self._check_job = self.parent.after(40, self._check_click_outside)

    def render_to_pil(self):
        if not self._layout:
            self._build_layout(compact_ratio=1.0)
        w = self.width
        h = self._cur_h
        s = self.scale

        # 2x supersampled crisp dark glass background card
        ss = 2
        hi = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        ImageDraw.Draw(hi).rounded_rectangle(
            [4, 4, w * ss - 5, h * ss - 5],
            radius=self.corner_radius * ss,
            fill=(18, 21, 28, 242),
            outline=(255, 255, 255, 52),
            width=2,
        )
        img = hi.resize((w, h), Image.Resampling.LANCZOS)

        # Draw recessed rounded trays for expanded sections
        for tray_y1, tray_y2 in self._trays:
            alpha_rounded_rect(
                img,
                [10, tray_y1, w - 10, tray_y2],
                radius=int(8 * s),
                fill=(9, 11, 17, 130),
                outline=(255, 255, 255, 24),
                width=1,
            )

        # Draw all foreground items onto a clean RGBA overlay and alpha-composite
        ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(ov)

        for tray_y1, tray_y2 in self._trays:
            line_x = int(15 * s)
            draw.line(
                [(line_x, tray_y1 + int(6 * s)), (line_x, tray_y2 - int(6 * s))],
                fill=(96, 165, 250, 140),
                width=max(2, int(2 * s)),
            )

        font_size = max(11, int(13 * s))
        font_sub_size = max(10, int(12 * s))
        font_header_size = max(9, int(10 * s))
        fcache = getattr(CustomMenu, "_font_cache", None)
        if fcache is None:
            fcache = CustomMenu._font_cache = {}
        fkey = (font_size, font_sub_size, font_header_size)
        fonts = fcache.get(fkey)
        if fonts is None:
            try:
                fonts = (
                    ImageFont.truetype("segoeui.ttf", font_size),
                    ImageFont.truetype("segoeui.ttf", font_sub_size),
                    ImageFont.truetype("seguisb.ttf", font_size),
                    ImageFont.truetype("segoeuib.ttf", font_header_size),
                )
            except Exception:
                d = ImageFont.load_default()
                fonts = (d, d, d, d)
            if len(fcache) >= 8:
                fcache.clear()
            fcache[fkey] = fonts
        font, font_sub, font_sec, font_header = fonts

        for i, (y1, y2, row) in enumerate(self._layout):
            kind = row['kind']
            if kind == 'separator':
                sep_y = (y1 + y2) // 2
                if row.get('sub'):
                    draw.line([(22, sep_y), (w - 18, sep_y)], fill=(255, 255, 255, 24), width=1)
                else:
                    draw.line([(14, sep_y), (w - 14, sep_y)], fill=(255, 255, 255, 32), width=1)

            elif kind == 'header':
                header_y = (y1 + y2) // 2
                draw.text((16, header_y), row['label'].upper(), fill=(148, 163, 184, 220), font=font_header, anchor="lm")

            elif kind == 'section':
                sec = row['ref']
                is_expanded = bool(sec.get('expanded'))
                is_hovered = (i == self.hovered_row_idx)
                item_rect = [7, y1 + 1, w - 7, y2 - 1]

                if is_hovered:
                    alpha_rounded_rect(
                        img,
                        item_rect,
                        radius=int(8 * s),
                        fill=(59, 130, 246, 110),
                        outline=(96, 165, 250, 195),
                        width=1,
                    )
                elif is_expanded:
                    alpha_rounded_rect(
                        img,
                        item_rect,
                        radius=int(8 * s),
                        fill=(255, 255, 255, 14),
                        outline=(255, 255, 255, 28),
                        width=1,
                    )

                mid_y = (y1 + y2) // 2
                label_color = (255, 255, 255, 255) if (is_hovered or is_expanded) else (226, 232, 240, 245)
                draw.text((int(18 * s), mid_y), row['label'], fill=label_color, font=font_sec, anchor="lm")

                # Vector chevron indicator (> collapsed, v expanded)
                cx = w - int(22 * s)
                cy = mid_y
                # Vertical accordion chevrons: down = collapsed (unfolds below), up = expanded
                if is_expanded:
                    chev_color = (255, 255, 255, 255) if is_hovered else (96, 165, 250, 255)
                    pts = [(cx - 4 * s, cy + 2 * s), (cx, cy - 2.5 * s), (cx + 4 * s, cy + 2 * s)]
                else:
                    chev_color = (255, 255, 255, 255) if is_hovered else (148, 163, 184, 225)
                    pts = [(cx - 4 * s, cy - 2 * s), (cx, cy + 2.5 * s), (cx + 4 * s, cy - 2 * s)]
                draw.line(pts, fill=chev_color, width=max(2, int(1.8 * s)), joint="curve")

            else:
                is_sub = (kind == 'subitem')
                is_hovered = (i == self.hovered_row_idx)
                item_rect = [18, y1 + 1, w - 13, y2 - 1] if is_sub else [7, y1 + 1, w - 7, y2 - 1]

                if is_hovered:
                    if row.get('danger'):
                        fill_color = (220, 38, 38, 195)
                        border_color = (248, 113, 113, 210)
                    else:
                        fill_color = (59, 130, 246, 110)
                        border_color = (96, 165, 250, 195)
                    alpha_rounded_rect(
                        img,
                        item_rect,
                        radius=int((6 if is_sub else 8) * s),
                        fill=fill_color,
                        outline=border_color,
                        width=1,
                    )

                if row.get('danger'):
                    text_color = (255, 255, 255, 255) if is_hovered else (248, 113, 113, 250)
                else:
                    if is_hovered:
                        text_color = (255, 255, 255, 255)
                    elif is_sub:
                        text_color = (214, 222, 235, 240)
                    else:
                        text_color = (226, 232, 240, 245)

                mid_y = (y1 + y2) // 2
                checked = row.get('checked')
                base_lx = int(26 * s) if is_sub else int(18 * s)
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

                use_font = font_sub if is_sub else font
                draw.text((label_x, mid_y), row['label'], fill=text_color, font=use_font, anchor="lm")

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

    HOVER_EXPAND_MS = 160

    def _on_motion(self, event):
        new_hover = self._row_index_at(event.y)
        if new_hover != self.hovered_row_idx:
            self.hovered_row_idx = new_hover
            h = self.canvas.winfo_height()
            self._render(h)
        self._maybe_schedule_hover_expand(new_hover)

    def _on_leave(self, event):
        self._cancel_hover_expand()
        if self.hovered_row_idx != -1:
            self.hovered_row_idx = -1
            h = self.canvas.winfo_height()
            self._render(h)

    def _cancel_hover_expand(self):
        if self._hover_expand_job:
            try:
                self.parent.after_cancel(self._hover_expand_job)
            except Exception:
                pass
        self._hover_expand_job = None
        self._hover_expand_sec = None

    def _maybe_schedule_hover_expand(self, idx):
        sec = None
        if 0 <= idx < len(self._layout):
            row = self._layout[idx][2]
            if row['kind'] == 'section' and not row['ref'].get('expanded') and row['ref'].get('children'):
                sec = row['ref']
        if sec is None:
            self._cancel_hover_expand()
            return
        if self._hover_expand_sec is sec and self._hover_expand_job:
            return
        # Anti-oscillation: after an auto-expand reshuffles rows, require real cursor movement
        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)) and self._last_auto_expand_pt is not None:
            lx, ly = self._last_auto_expand_pt
            if abs(pt.x - lx) < 4 and abs(pt.y - ly) < 4:
                return
            self._last_auto_expand_pt = None
        self._cancel_hover_expand()
        self._hover_expand_sec = sec
        self._hover_expand_job = self.parent.after(self.HOVER_EXPAND_MS, self._run_hover_expand)

    def _run_hover_expand(self):
        sec = self._hover_expand_sec
        self._hover_expand_job = None
        self._hover_expand_sec = None
        if sec is None or sec.get('expanded') or not self.menu_window or not self.menu_window.winfo_exists():
            return
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return
        rel_x = pt.x - self._cur_x
        rel_y = pt.y - self._cur_y
        if not (0 <= rel_x <= self.width):
            return
        idx = self._row_index_at(rel_y)
        if idx < 0 or self._layout[idx][2].get('ref') is not sec:
            return
        self._set_section_expanded(sec, True, auto=True, cursor_pt=(pt.x, pt.y))

    def _set_section_expanded(self, sec, expanded, auto=False, cursor_pt=None):
        old = self._section_row_y(sec)
        pin_y = (self._cur_y + old[0]) if old else None
        sec['expanded'] = bool(expanded)
        if expanded and sec.get('exclusive', True):
            for other in self.items:
                if other is not sec and other.get('type') == 'section':
                    other['expanded'] = False
        if auto:
            self._last_auto_expand_time = time.time()
            self._last_auto_expand_sec = sec
            self._last_auto_expand_pt = cursor_pt
        self._relayout_and_render(pin_section=sec, pin_screen_y=pin_y)

    def _on_click(self, event):
        idx = self._row_index_at(event.y)
        if idx == -1 or not (0 <= idx < len(self._layout)):
            return

        _, _, row = self._layout[idx]
        if row['kind'] == 'section':
            sec = row['ref']
            self._cancel_hover_expand()
            # Section was just unfolded by hover: a click right after should keep it open, not collapse it
            if sec.get('expanded') and self._last_auto_expand_sec is sec and (time.time() - self._last_auto_expand_time) < 0.8:
                self.mouse_was_released = False
                return
            self._set_section_expanded(sec, not sec.get('expanded', False))
            # Guard against immediate outside-click close if collapsing moves window away from cursor
            self.mouse_was_released = False
            return

        cmd = row.get('command')
        if cmd:
            self.close()
            cmd()
        else:
            self.close()

    def close(self):
        CustomMenu._active_menus.discard(self)
        self._cancel_hover_expand()

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

        self.hovered_row_idx = -1
        CustomMenu.sync_active_count()
