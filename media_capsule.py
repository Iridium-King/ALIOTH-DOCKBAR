"""
Centered floating Media Player Control Capsule displayed neatly over the Dockbar.
Reads live active player metadata (Spotify, YouTube/Chrome/Edge, Apple Music, VLC, etc.)
via a single persistent lightweight Windows Runtime SMTC session stream, and executes
zero-latency media controls via Win32 VK_MEDIA_* keys + SMTC command fallback.
"""
import ctypes
import json
import subprocess
import threading
import time
import tkinter as tk
from PIL import Image, ImageDraw

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


class MediaCapsule:
    """Floating centered media control pill anchored over the Dockbar."""

    WIDTH = 344
    HEIGHT = 46

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

        self.win_x = 0
        self.win_y = 0
        self.btn_boxes = []  # [(x1, y1, x2, y2, action), ...]
        self.hovered_btn = None
        self._hover_inside = False
        self._last_render_sig = None

        # Live media state updated by single persistent SMTC daemon thread
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

    def sync_visibility(self):
        if self.is_enabled():
            if not self.window or not self.window.winfo_exists():
                self.show()
            else:
                self.reposition()
                self._render()
        else:
            self.hide()

    def show(self):
        if self.window and self.window.winfo_exists():
            return
        self._stop_flag = False
        self.window = tk.Toplevel(self.root)
        self.window.overrideredirect(True)
        self.window.config(bg=self.transparent_key)
        self.top_hwnd = set_tool_window(self.window, owner_hwnd=None, layered=True, no_activate=True)
        self.hwnd = self.top_hwnd

        self.canvas = tk.Canvas(
            self.window,
            width=self.WIDTH,
            height=self.HEIGHT,
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
                        creationflags=0x08000000,  # CREATE_NO_WINDOW
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
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        pos = self.config_mgr.get("general", "position", "bottom")
        dx = getattr(self.dock, "dock_x", (sw - 600) // 2)
        dy = getattr(self.dock, "dock_y", sh - 90)
        dw = getattr(self.dock, "dock_w", 600)
        pb = getattr(self.dock, "pill_bounds", (0, 0, dw, 64))

        pill_cx = dx + (pb[0] + pb[2]) // 2
        nx = max(8, min(sw - self.WIDTH - 8, int(pill_cx - self.WIDTH // 2)))
        if pos == "top":
            ny = min(sh - self.HEIGHT - 8, int(dy + pb[3] + 8))
        else:
            ny = max(8, int(dy + pb[1] - self.HEIGHT - 8))

        if nx != self.win_x or ny != self.win_y:
            self.win_x, self.win_y = nx, ny
            self.window.geometry(f"{self.WIDTH}x{self.HEIGHT}+{nx}+{ny}")

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
        cur = self.config_mgr.get("general", "media_capsule_style", "cristal")
        menu.add_header("Estilo de Cápsula Multimedia")
        for sid, lbl in (
            ("cristal", "Cristal Oscuro"),
            ("neon", "Neón Eléctrico"),
            ("minimal", "Minimal Suave"),
            ("oled", "Negro OLED"),
            ("acrilico", "Acrílico Azul"),
        ):
            menu.add_command(lbl, lambda k=sid: (self.config_mgr.set("general", "media_capsule_style", k), self._render(force=True)), checked=(cur == sid))
        menu.add_separator()
        menu.add_command("Ocultar cápsula de reproductor", lambda: (self.config_mgr.set("general", "media_capsule_enabled", False), self.hide()), danger=True)
        menu.show(event.x_root, event.y_root)

    def _render(self, force=False):
        if not self.window or not self.canvas:
            return
        style = self.config_mgr.get("general", "media_capsule_style", "cristal") or "cristal"
        st = self.media_state
        sig = (style, st["active"], st["title"], st["artist"], st["playing"], self.hovered_btn)
        if not force and sig == self._last_render_sig:
            return
        self._last_render_sig = sig

        w, h = self.WIDTH, self.HEIGHT
        composite = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(composite, "RGBA")

        styles = {
            "cristal": ((18, 22, 32, 232), (255, 255, 255, 52), (56, 189, 248)),
            "neon": ((9, 13, 24, 244), (34, 211, 238, 210), (34, 211, 238)),
            "minimal": ((16, 19, 27, 175), (255, 255, 255, 28), (226, 232, 240)),
            "oled": ((4, 5, 8, 250), (168, 85, 247, 165), (168, 85, 247)),
            "acrilico": ((17, 27, 48, 235), (96, 165, 250, 120), (96, 165, 250)),
        }
        bg_rgba, out_rgba, acc_rgb = styles.get(style, styles["cristal"])
        draw.rounded_rectangle([2, 2, w - 3, h - 3], radius=(h - 4) // 2, fill=bg_rgba, outline=out_rgba, width=2 if style == "neon" else 1)

        # Left audio/disc indicator
        cx_ic, cy_ic = 23, h // 2
        alpha_ellipse(composite, [cx_ic - 11, cy_ic - 11, cx_ic + 11, cy_ic + 11], fill=(*acc_rgb, 48), outline=(*acc_rgb, 185), width=1)
        # Mini equalizer bars (outline/crisp)
        heights = (9, 13, 7) if st["playing"] else (5, 8, 5)
        for idx_b, bh in enumerate(heights):
            bx = cx_ic - 4 + idx_b * 4
            draw.line([(bx, cy_ic + bh // 2), (bx, cy_ic - bh // 2)], fill=(*acc_rgb, 255) if st["playing"] else (203, 213, 225, 210), width=2)

        # Track title + artist
        f_title = resolve_styled_font("Segoe UI", 11, bold=True)
        f_sub = resolve_styled_font("Segoe UI", 9.5, bold=False)
        txt_x = 42
        max_txt_w = w - 42 - 114
        title = st["title"]
        while title and draw.textlength(title, font=f_title) > max_txt_w:
            title = title[:-1]
        if title != st["title"]:
            title = title.rstrip() + "…"
        artist = st["artist"]
        while artist and draw.textlength(artist, font=f_sub) > max_txt_w:
            artist = artist[:-1]
        if artist != st["artist"]:
            artist = artist.rstrip() + "…"

        draw.text((txt_x, h // 2 - 7), title, fill=(248, 250, 252, 255), font=f_title, anchor="lm")
        draw.text((txt_x, h // 2 + 8), artist, fill=(148, 163, 184, 230), font=f_sub, anchor="lm")

        # Right playback controls: [Prev] [Play/Pause] [Next]
        self.btn_boxes = []
        btn_specs = [("prev", w - 98), ("play_pause", w - 66), ("next", w - 34)]
        for act, bcx in btn_specs:
            r = 14 if act == "play_pause" else 12
            bcy = h // 2
            box = (bcx - r, bcy - r, bcx + r, bcy + r)
            self.btn_boxes.append((*box, act))
            is_hov = (self.hovered_btn == act)
            if act == "play_pause":
                fill_b = (*acc_rgb, 230 if is_hov else 175)
                alpha_ellipse(composite, list(box), fill=fill_b, outline=(255, 255, 255, 190 if is_hov else 110), width=1)
                if st["playing"]:
                    draw.rectangle([bcx - 4, bcy - 5, bcx - 1, bcy + 5], fill=(255, 255, 255, 255))
                    draw.rectangle([bcx + 2, bcy - 5, bcx + 5, bcy + 5], fill=(255, 255, 255, 255))
                else:
                    draw.polygon([(bcx - 3, bcy - 6), (bcx - 3, bcy + 6), (bcx + 6, bcy)], fill=(255, 255, 255, 255))
            else:
                if is_hov:
                    alpha_ellipse(composite, list(box), fill=(255, 255, 255, 36))
                col_i = (255, 255, 255, 255) if is_hov else (203, 213, 225, 235)
                if act == "prev":
                    draw.line([(bcx - 5, bcy - 5), (bcx - 5, bcy + 5)], fill=col_i, width=2)
                    draw.polygon([(bcx + 5, bcy - 5), (bcx + 5, bcy + 5), (bcx - 3, bcy)], fill=col_i)
                else:
                    draw.polygon([(bcx - 5, bcy - 5), (bcx - 5, bcy + 5), (bcx + 3, bcy)], fill=col_i)
                    draw.line([(bcx + 5, bcy - 5), (bcx + 5, bcy + 5)], fill=col_i, width=2)

        self.window.update_idletasks()
        hwnd = self.top_hwnd or user32.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
        if not update_layered_pil(hwnd, self.win_x, self.win_y, composite):
            final_img = flatten_pil_for_colorkey(composite, (1, 2, 3))
            self.photo_ref = ImageTk.PhotoImage(final_img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo_ref)
