"""
Centered floating Media Player Control Capsule displayed neatly over the Dockbar.
Reads live active player metadata (Spotify, YouTube/Chrome/Edge, Apple Music, VLC, etc.)
via a single persistent lightweight Windows Runtime SMTC session stream, and executes
zero-latency media controls via Win32 VK_MEDIA_* keys.
Fully customizable: size/scale, width, opacity, custom colors, font family/size/colors,
button position (right/left/center/split), visual style, and vertical gap over Dockbar.
"""
import ctypes
import json
import subprocess
import threading
import time
import tkinter as tk
from PIL import Image, ImageDraw, ImageTk

from window_utils import (
    alpha_ellipse,
    alpha_line,
    alpha_rounded_rect,
    flatten_pil_for_colorkey,
    release_layered_surface,
    resolve_styled_font,
    set_tool_window,
    update_layered_pil,
)

user32 = ctypes.windll.user32

HWND_TOPMOST = ctypes.c_void_p(-1).value
HWND_NOTOPMOST = ctypes.c_void_p(-2).value
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002

_SMTC_PS_SCRIPT = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -match 'IAsyncOperation'
})[0]
function AwaitOp($op, [Type]$resType) {
    $net = $asTask.MakeGenericMethod($resType)
    $t = $net.Invoke($null, @($op))
    $t.Wait(1500) | Out-Null
    return $t.Result
}
$mgr = AwaitOp ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
if ($null -eq $mgr) { exit 1 }
while ($true) {
    try {
        $sess = $mgr.GetCurrentSession()
        if ($null -eq $sess) {
            $all = $mgr.GetSessions()
            if ($all -and $all.Count -gt 0) { $sess = $all[0] }
        }
        if ($null -eq $sess) {
            [Console]::Out.WriteLine('{"active":false}')
        } else {
            $pb = $sess.GetPlaybackInfo()
            $status = [string]$pb.PlaybackStatus
            $props = AwaitOp ($sess.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsMediaProperties])
            $out = @{
                active = $true
                title = [string]$props.Title
                artist = [string]$props.Artist
                album = [string]$props.AlbumTitle
                source = [string]$sess.SourceAppUserModelId
                playing = ($status -eq 'Playing')
                status = $status
            } | ConvertTo-Json -Compress
            [Console]::Out.WriteLine($out)
        }
    } catch {
        [Console]::Out.WriteLine('{"active":false}')
    }
    Start-Sleep -Milliseconds 950
}
"""


def _send_media_vk(vk_code):
    try:
        user32.keybd_event(vk_code, 0, 0, 0)
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def _hex_to_rgb(hex_str, default=(18, 22, 32)):
    try:
        h = str(hex_str or "").strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        pass
    return default


class MediaCapsule:
    """Floating centered, fully customizable media control pill anchored over the Dockbar."""

    def __init__(self, dock_window):
        self.dock = dock_window
        self.root = dock_window.root
        self.config_mgr = dock_window.config_mgr

        self.window = None
        self.canvas = None
        self.top_hwnd = None
        self.hwnd = None
        self.transparent_key = "#010203"
        self.photo_ref = None

        self.cur_w = 360
        self.cur_h = 48
        self.win_x = 0
        self.win_y = 0
        self.btn_boxes = []  # [(x1, y1, x2, y2, action), ...]
        self.hovered_btn = None
        self._hover_inside = False
        self._last_render_sig = None

        self.media_state = {
            "active": False,
            "title": "Sin reproducción activa",
            "artist": "Listo para controlar música/video",
            "playing": False,
            "source": "",
        }
        self._smtc_proc = None
        self._smtc_thread = None
        self._stop_flag = False
        self._sync_job = None

    def is_enabled(self):
        return bool(self.config_mgr.get("general", "media_capsule_enabled", False))

    def _compute_dims(self):
        scale = max(0.75, min(1.50, float(self.config_mgr.get("general", "media_capsule_scale", 1.0) or 1.0)))
        base_w = max(240, min(600, int(self.config_mgr.get("general", "media_capsule_width", 360) or 360)))
        btn_pos = (self.config_mgr.get("general", "media_capsule_btn_pos", "right") or "right").lower()
        base_h = 68 if btn_pos == "center" else 48
        return max(220, int(base_w * scale)), max(38, int(base_h * scale)), scale, btn_pos

    def sync_visibility(self):
        if self.is_enabled():
            if not self.window or not self.window.winfo_exists():
                self.show()
            else:
                self.reposition()
                self._render(force=True)
        else:
            self.hide()

    def show(self):
        if self.window and self.window.winfo_exists():
            return
        self._stop_flag = False
        w, h, _, _ = self._compute_dims()
        self.cur_w, self.cur_h = w, h

        self.window = tk.Toplevel(self.root)
        self.window.overrideredirect(True)
        self.window.config(bg=self.transparent_key)
        self.top_hwnd = set_tool_window(self.window, owner_hwnd=None, layered=True, no_activate=True)
        self.hwnd = self.top_hwnd

        self.canvas = tk.Canvas(
            self.window,
            width=w,
            height=h,
            bg=self.transparent_key,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Button-3>", self._on_right_click)

        self._start_smtc_reader()
        self.reposition()
        self._render(force=True)
        self._schedule_tick()

    def hide(self):
        self._stop_flag = True
        if self._sync_job:
            try:
                self.root.after_cancel(self._sync_job)
            except Exception:
                pass
            self._sync_job = None
        if self._smtc_proc is not None:
            try:
                self._smtc_proc.terminate()
            except Exception:
                pass
            self._smtc_proc = None
        if self.top_hwnd:
            release_layered_surface(self.top_hwnd)
            self.top_hwnd = None
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None
        self.hwnd = None
        self._last_render_sig = None

    def _start_smtc_reader(self):
        if self._smtc_thread and self._smtc_thread.is_alive():
            return

        def _worker():
            while not self._stop_flag:
                try:
                    self._smtc_proc = subprocess.Popen(
                        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", _SMTC_PS_SCRIPT],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        encoding="utf-8",
                        errors="ignore",
                        creationflags=0x08000000,
                    )
                    for raw_line in iter(self._smtc_proc.stdout.readline, ""):
                        if self._stop_flag:
                            break
                        ln = (raw_line or "").strip()
                        if not ln.startswith("{"):
                            continue
                        try:
                            info = json.loads(ln)
                            self.media_state = {
                                "active": bool(info.get("active")),
                                "title": (info.get("title") or "").strip() or "Sin reproducción activa",
                                "artist": (info.get("artist") or "").strip() or (info.get("source") or "").split("!")[0] or "Reproductor",
                                "playing": bool(info.get("playing")),
                                "source": info.get("source") or "",
                            }
                        except Exception:
                            pass
                except Exception:
                    pass
                if self._stop_flag:
                    break
                time.sleep(2.5)

        self._smtc_thread = threading.Thread(target=_worker, daemon=True)
        self._smtc_thread.start()

    def _schedule_tick(self):
        if self._stop_flag or not self.window or not self.window.winfo_exists():
            return
        self.reposition()
        self._render()
        self._sync_job = self.root.after(450, self._schedule_tick)

    def reposition(self):
        if not self.window or not self.window.winfo_exists():
            return
        w, h, _, _ = self._compute_dims()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        pos = self.config_mgr.get("general", "position", "bottom")
        gap_y = max(4, min(60, int(self.config_mgr.get("general", "media_capsule_offset_y", 10) or 10)))
        dx = getattr(self.dock, "dock_x", (sw - 600) // 2)
        dy = getattr(self.dock, "dock_y", sh - 90)
        dw = getattr(self.dock, "dock_w", 600)
        pb = getattr(self.dock, "pill_bounds", (0, 0, dw, 64))

        pill_cx = dx + (pb[0] + pb[2]) // 2
        nx = max(8, min(sw - w - 8, int(pill_cx - w // 2)))
        if pos == "top":
            ny = min(sh - h - 8, int(dy + pb[3] + gap_y))
        else:
            ny = max(8, int(dy + pb[1] - h - gap_y))

        if nx != self.win_x or ny != self.win_y or w != self.cur_w or h != self.cur_h:
            self.win_x, self.win_y = nx, ny
            self.cur_w, self.cur_h = w, h
            self.window.geometry(f"{w}x{h}+{nx}+{ny}")
            if self.canvas:
                self.canvas.config(width=w, height=h)

    def sync_z_order(self, revealed, anchor=0):
        if not self.hwnd or not user32.IsWindow(self.hwnd):
            return
        try:
            if revealed or self._hover_inside or not anchor:
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            else:
                user32.SetWindowPos(self.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                user32.SetWindowPos(self.hwnd, int(anchor), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        except Exception:
            pass

    def _on_motion(self, event):
        self._hover_inside = True
        old = self.hovered_btn
        self.hovered_btn = None
        for x1, y1, x2, y2, act in self.btn_boxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.hovered_btn = act
                break
        if old != self.hovered_btn:
            self._render(force=True)

    def _on_leave(self, _event=None):
        self._hover_inside = False
        if self.hovered_btn is not None:
            self.hovered_btn = None
            self._render(force=True)

    def _on_click(self, event):
        for x1, y1, x2, y2, act in self.btn_boxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                if act == "prev":
                    _send_media_vk(VK_MEDIA_PREV_TRACK)
                elif act == "play_pause":
                    _send_media_vk(VK_MEDIA_PLAY_PAUSE)
                    self.media_state["playing"] = not self.media_state.get("playing", False)
                elif act == "next":
                    _send_media_vk(VK_MEDIA_NEXT_TRACK)
                self._render(force=True)
                return

    def _on_right_click(self, event):
        from custom_menu import CustomMenu
        menu = CustomMenu(self.window)
        cur_st = self.config_mgr.get("general", "media_capsule_style", "cristal")
        cur_bp = self.config_mgr.get("general", "media_capsule_btn_pos", "right")
        sec_st = menu.add_section("Estilo de Cápsula", expanded=False)
        for sid, lbl in (
            ("cristal", "Cristal Oscuro"),
            ("neon", "Neón Eléctrico"),
            ("minimal", "Minimal Suave"),
            ("oled", "Negro OLED"),
            ("acrilico", "Acrílico Azul"),
            ("editorial", "Editorial Dorado"),
        ):
            sec_st.add_command(lbl, lambda k=sid: (self.config_mgr.set("general", "media_capsule_style", k), self._render(force=True)), checked=(cur_st == sid))

        sec_bp = menu.add_section("Posición de Botones", expanded=False)
        for bpid, bplbl in (
            ("right", "Controles a la Derecha"),
            ("left", "Controles a la Izquierda"),
            ("center", "Centrados Abajo"),
            ("split", "Divididos (Izq / Der)"),
        ):
            sec_bp.add_command(bplbl, lambda k=bpid: (self.config_mgr.set("general", "media_capsule_btn_pos", k), self.reposition(), self._render(force=True)), checked=(cur_bp == bpid))

        menu.add_separator()
        if getattr(self.dock, "on_open_settings", None):
            menu.add_command("Personalizar Cápsula en Ajustes...", self.dock.on_open_settings)
        menu.add_command("Ocultar cápsula de reproductor", lambda: (self.config_mgr.set("general", "media_capsule_enabled", False), self.hide()), danger=True)
        menu.show(event.x_root, event.y_root)

    def _draw_ctrl_btn(self, composite, draw, act, bcx, bcy, scale, acc_rgb, playing):
        r = max(10, int((14 if act == "play_pause" else 12) * scale))
        box = (int(bcx - r), int(bcy - r), int(bcx + r), int(bcy + r))
        self.btn_boxes.append((*box, act))
        is_hov = (self.hovered_btn == act)
        if act == "play_pause":
            fill_b = (*acc_rgb, 235 if is_hov else 185)
            alpha_ellipse(composite, list(box), fill=fill_b, outline=(255, 255, 255, 200 if is_hov else 115), width=1)
            p = max(4, int(5 * scale))
            if playing:
                bw = max(2, int(3 * scale))
                draw.rectangle([bcx - p + 1, bcy - p, bcx - p + 1 + bw, bcy + p], fill=(255, 255, 255, 255))
                draw.rectangle([bcx + p - 1 - bw, bcy - p, bcx + p - 1, bcy + p], fill=(255, 255, 255, 255))
            else:
                draw.polygon([(bcx - int(3 * scale), bcy - p - 1), (bcx - int(3 * scale), bcy + p + 1), (bcx + int(6 * scale), bcy)], fill=(255, 255, 255, 255))
        else:
            if is_hov:
                alpha_ellipse(composite, list(box), fill=(255, 255, 255, 38))
            col_i = (255, 255, 255, 255) if is_hov else (203, 213, 225, 235)
            p = max(4, int(5 * scale))
            if act == "prev":
                draw.line([(bcx - p, bcy - p), (bcx - p, bcy + p)], fill=col_i, width=max(2, int(2 * scale)))
                draw.polygon([(bcx + p, bcy - p), (bcx + p, bcy + p), (bcx - p + 2, bcy)], fill=col_i)
            else:
                draw.polygon([(bcx - p, bcy - p), (bcx - p, bcy + p), (bcx + p - 2, bcy)], fill=col_i)
                draw.line([(bcx + p, bcy - p), (bcx + p, bcy + p)], fill=col_i, width=max(2, int(2 * scale)))

    def _draw_eq_badge(self, composite, draw, cx_ic, cy_ic, scale, acc_rgb, playing):
        r = max(9, int(11 * scale))
        alpha_ellipse(composite, [cx_ic - r, cy_ic - r, cx_ic + r, cy_ic + r], fill=(*acc_rgb, 52), outline=(*acc_rgb, 195), width=1)
        heights = (9, 13, 7) if playing else (5, 8, 5)
        step = max(3, int(4 * scale))
        for idx_b, bh in enumerate(heights):
            bh_s = max(4, int(bh * scale))
            bx = cx_ic - step + idx_b * step
            draw.line([(bx, cy_ic + bh_s // 2), (bx, cy_ic - bh_s // 2)], fill=(*acc_rgb, 255) if playing else (203, 213, 225, 215), width=max(2, int(2 * scale)))

    def _render(self, force=False):
        if not self.window or not self.canvas:
            return
        w, h, scale, btn_pos = self._compute_dims()
        style = (self.config_mgr.get("general", "media_capsule_style", "cristal") or "cristal").lower()
        opacity = max(0.15, min(1.0, float(self.config_mgr.get("general", "media_capsule_opacity", 0.90) or 0.90)))
        bg_custom = (self.config_mgr.get("general", "media_capsule_bg_color", "") or "").strip()
        acc_custom = (self.config_mgr.get("general", "media_capsule_accent_color", "") or "").strip()
        font_fam = self.config_mgr.get("general", "media_capsule_font_family", "Segoe UI") or "Segoe UI"
        font_sz = max(9, min(18, int(self.config_mgr.get("general", "media_capsule_font_size", 11) or 11)))
        title_hex = self.config_mgr.get("general", "media_capsule_title_color", "#f8fafc") or "#f8fafc"
        artist_hex = self.config_mgr.get("general", "media_capsule_artist_color", "#94a3b8") or "#94a3b8"

        st = self.media_state
        sig = (
            w, h, style, round(opacity, 2), bg_custom, acc_custom, font_fam, font_sz,
            title_hex, artist_hex, btn_pos, st["active"], st["title"], st["artist"], st["playing"], self.hovered_btn,
        )
        if not force and sig == self._last_render_sig:
            return
        self._last_render_sig = sig

        composite = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(composite, "RGBA")

        styles = {
            "cristal": ((18, 22, 32), (255, 255, 255, 58), (0, 120, 212)),
            "neon": ((9, 13, 24), (34, 211, 238, 220), (34, 211, 238)),
            "minimal": ((16, 19, 27), (255, 255, 255, 34), (226, 232, 240)),
            "oled": ((4, 5, 8), (168, 85, 247, 175), (168, 85, 247)),
            "acrilico": ((17, 27, 48), (96, 165, 250, 135), (96, 165, 250)),
            "editorial": ((26, 22, 16), (245, 197, 66, 165), (245, 197, 66)),
        }
        base_bg_rgb, out_rgba, base_acc_rgb = styles.get(style, styles["cristal"])
        bg_rgb = _hex_to_rgb(bg_custom, base_bg_rgb) if bg_custom else base_bg_rgb
        acc_rgb = _hex_to_rgb(acc_custom, base_acc_rgb) if acc_custom else base_acc_rgb
        if acc_custom and style in ("neon", "oled", "editorial"):
            out_rgba = (*acc_rgb, out_rgba[3])
        bg_rgba = (*bg_rgb, max(20, int(255 * opacity)))
        rad = min(28, (h - 4) // 2) if btn_pos == "center" else (h - 4) // 2
        draw.rounded_rectangle([2, 2, w - 3, h - 3], radius=rad, fill=bg_rgba, outline=out_rgba, width=2 if style == "neon" else 1)

        f_title = resolve_styled_font(font_fam, max(9, int(font_sz * scale)), bold=True)
        f_sub = resolve_styled_font(font_fam, max(8, int((font_sz - 1.5) * scale)), bold=False)
        title_rgb = _hex_to_rgb(title_hex, (248, 250, 252))
        artist_rgb = _hex_to_rgb(artist_hex, (148, 163, 184))

        def _clip(text, font, max_w):
            out = text
            while out and draw.textlength(out, font=font) > max(24, max_w):
                out = out[:-1]
            return (out.rstrip() + "…") if out != text else out

        self.btn_boxes = []
        btn_gap = int(32 * scale)

        if btn_pos == "left":
            b1 = int(26 * scale)
            self._draw_ctrl_btn(composite, draw, "prev", b1, h // 2, scale, acc_rgb, st["playing"])
            self._draw_ctrl_btn(composite, draw, "play_pause", b1 + btn_gap, h // 2, scale, acc_rgb, st["playing"])
            self._draw_ctrl_btn(composite, draw, "next", b1 + btn_gap * 2, h // 2, scale, acc_rgb, st["playing"])
            eq_x = w - int(24 * scale)
            self._draw_eq_badge(composite, draw, eq_x, h // 2, scale, acc_rgb, st["playing"])
            txt_x = b1 + btn_gap * 2 + int(22 * scale)
            max_tw = eq_x - int(18 * scale) - txt_x
            draw.text((txt_x, h // 2 - int(7 * scale)), _clip(st["title"], f_title, max_tw), fill=(*title_rgb, 255), font=f_title, anchor="lm")
            draw.text((txt_x, h // 2 + int(8 * scale)), _clip(st["artist"], f_sub, max_tw), fill=(*artist_rgb, 235), font=f_sub, anchor="lm")

        elif btn_pos == "center":
            top_y = int(15 * scale)
            max_tw = w - int(36 * scale)
            draw.text((w / 2.0, top_y), _clip(st["title"], f_title, max_tw), fill=(*title_rgb, 255), font=f_title, anchor="mm")
            draw.text((w / 2.0, top_y + int(13 * scale)), _clip(st["artist"], f_sub, max_tw), fill=(*artist_rgb, 235), font=f_sub, anchor="mm")
            by = h - int(18 * scale)
            self._draw_ctrl_btn(composite, draw, "prev", w // 2 - int(38 * scale), by, scale * 0.92, acc_rgb, st["playing"])
            self._draw_ctrl_btn(composite, draw, "play_pause", w // 2, by, scale * 0.92, acc_rgb, st["playing"])
            self._draw_ctrl_btn(composite, draw, "next", w // 2 + int(38 * scale), by, scale * 0.92, acc_rgb, st["playing"])

        elif btn_pos == "split":
            lx = int(24 * scale)
            rx = w - int(24 * scale)
            px = w - int(56 * scale)
            self._draw_ctrl_btn(composite, draw, "prev", lx, h // 2, scale, acc_rgb, st["playing"])
            self._draw_ctrl_btn(composite, draw, "play_pause", px, h // 2, scale, acc_rgb, st["playing"])
            self._draw_ctrl_btn(composite, draw, "next", rx, h // 2, scale, acc_rgb, st["playing"])
            txt_x1 = lx + int(20 * scale)
            txt_x2 = px - int(20 * scale)
            tcx = (txt_x1 + txt_x2) / 2.0
            max_tw = txt_x2 - txt_x1
            draw.text((tcx, h // 2 - int(7 * scale)), _clip(st["title"], f_title, max_tw), fill=(*title_rgb, 255), font=f_title, anchor="mm")
            draw.text((tcx, h // 2 + int(8 * scale)), _clip(st["artist"], f_sub, max_tw), fill=(*artist_rgb, 235), font=f_sub, anchor="mm")

        else:
            # Default: "right"
            cx_ic = int(24 * scale)
            self._draw_eq_badge(composite, draw, cx_ic, h // 2, scale, acc_rgb, st["playing"])
            b3 = w - int(26 * scale)
            b2 = b3 - btn_gap
            b1 = b2 - btn_gap
            self._draw_ctrl_btn(composite, draw, "prev", b1, h // 2, scale, acc_rgb, st["playing"])
            self._draw_ctrl_btn(composite, draw, "play_pause", b2, h // 2, scale, acc_rgb, st["playing"])
            self._draw_ctrl_btn(composite, draw, "next", b3, h // 2, scale, acc_rgb, st["playing"])
            txt_x = cx_ic + int(20 * scale)
            max_tw = b1 - int(18 * scale) - txt_x
            draw.text((txt_x, h // 2 - int(7 * scale)), _clip(st["title"], f_title, max_tw), fill=(*title_rgb, 255), font=f_title, anchor="lm")
            draw.text((txt_x, h // 2 + int(8 * scale)), _clip(st["artist"], f_sub, max_tw), fill=(*artist_rgb, 235), font=f_sub, anchor="lm")

        self.window.update_idletasks()
        hwnd = self.top_hwnd or user32.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
        if not update_layered_pil(hwnd, self.win_x, self.win_y, composite):
            final_img = flatten_pil_for_colorkey(composite, (1, 2, 3))
            self.photo_ref = ImageTk.PhotoImage(final_img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo_ref)
