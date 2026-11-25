import os
import sys
import gc
import re
import time
import math
import queue
import subprocess
import struct
import uuid
import ctypes
from collections import OrderedDict
from ctypes import wintypes
import tkinter as tk
from tkinter import messagebox, simpledialog, colorchooser, filedialog
from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageFilter

import icon_extractor
from config import ConfigManager
from custom_menu import CustomMenu
from window_utils import set_tool_window, update_layered_pil, release_layered_surface, flatten_pil_for_colorkey, alpha_rounded_rect, alpha_ellipse, alpha_line, resolve_styled_font, find_lowest_app_above_desktop, get_battery_status

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32
comctl32 = ctypes.windll.comctl32
kernel32 = ctypes.windll.kernel32
ole32 = ctypes.windll.ole32

class _WIN_GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _WIN_GUID), ("pid", wintypes.DWORD)]

class _PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", wintypes.WORD),
        ("r1", wintypes.WORD),
        ("r2", wintypes.WORD),
        ("r3", wintypes.WORD),
        ("ptr", ctypes.c_void_p),
        ("extra", ctypes.c_void_p),
    ]

_IID_IPropertyStore = _WIN_GUID(
    0x886D8EEB, 0x8CF2, 0x4446,
    (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99),
)
_PKEY_AppUserModel_ID = _PROPERTYKEY(
    _WIN_GUID(
        0x9F4C2855, 0x9F79, 0x4B39,
        (ctypes.c_ubyte * 8)(0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3),
    ),
    5,
)

try:
    shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(_WIN_GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.HRESULT
    ole32.PropVariantClear.argtypes = [ctypes.POINTER(_PROPVARIANT)]
except Exception:
    pass


def _get_window_aumid(hwnd):
    """Reads Win32 System.AppUserModel.ID from a top-level HWND via SHGetPropertyStoreForWindow."""
    p_store = ctypes.c_void_p()
    try:
        hr = shell32.SHGetPropertyStoreForWindow(wintypes.HWND(hwnd), ctypes.byref(_IID_IPropertyStore), ctypes.byref(p_store))
        if hr != 0 or not p_store.value:
            return ""
        vtable = ctypes.cast(p_store.value, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_value = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))(vtable[5])
        release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])
        pv = _PROPVARIANT()
        out = ""
        try:
            hr2 = get_value(p_store.value, ctypes.byref(_PKEY_AppUserModel_ID), ctypes.byref(pv))
            if hr2 == 0 and pv.vt == 31 and pv.ptr:
                out = ctypes.wstring_at(pv.ptr) or ""
        finally:
            try:
                ole32.PropVariantClear(ctypes.byref(pv))
            except Exception:
                pass
            try:
                release(p_store.value)
            except Exception:
                pass
        return out.strip()
    except Exception:
        return ""

try:
    import pythoncom
    import win32clipboard
    import win32com.server.util
    HAS_PYWIN32_OLE = True
except Exception:
    HAS_PYWIN32_OLE = False

# Win32 Constants
WM_DROPFILES = 0x0233
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
HWND_TOP = 0
HWND_BOTTOM = 1
HWND_TOPMOST = ctypes.c_void_p(-1).value
HWND_NOTOPMOST = ctypes.c_void_p(-2).value
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

# OLE DROPEFFECT Constants (Standard oleidl.h)
DROPEFFECT_NONE = 0
DROPEFFECT_COPY = 1
DROPEFFECT_MOVE = 2
DROPEFFECT_LINK = 4
DROPEFFECT_SCROLL = 0x80000000

SUBCLASSPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    ctypes.c_size_t,
    ctypes.c_size_t
)

if HAS_PYWIN32_OLE:
    class DockOleDropTarget:
        """
        Crash-safe OLE IDropTarget for Windows 11 Explorer & Desktop drag-and-drop.
        IMPORTANT: Never call Win32 GUI/message APIs (SetWindowPos, UpdateLayeredWindow)
        or Tkinter methods (root.after, winfo_*) synchronously inside COM callbacks,
        because re-entering Tcl_DoOneEvent/Win32 during COM RPC corrupts Python 3.12's GIL
        state (0xc0000409). Mutate pure Python state & queue drops for the Tk mainloop.
        """
        _com_interfaces_ = [pythoncom.IID_IDropTarget]
        _public_methods_ = ['DragEnter', 'DragOver', 'DragLeave', 'Drop']

        def __init__(self, dock):
            self.dock = dock

        def DragEnter(self, data_obj, key_state, pt, effect):
            self.dock.is_revealed = True
            self.dock._need_topmost_reveal = True
            self.dock.last_leave_time = time.time()
            self.dock.is_dragging_item = True
            self.dock.mouse_inside_dock = True
            self.dock._drag_leave_ts = 0.0
            self.DragOver(key_state, pt, effect)
            return DROPEFFECT_COPY | DROPEFFECT_LINK

        def DragOver(self, key_state, pt, effect):
            try:
                cx = pt[0] - getattr(self.dock, "dock_x", 0)
                cy = pt[1] - getattr(self.dock, "dock_y", 0)
                now = time.time()
                self.dock.drag_cur_x = cx
                self.dock.drag_cur_y = cy
                self.dock.is_revealed = True
                self.dock.last_leave_time = now
                self.dock._last_drag_over_ts = now
                self.dock._drag_leave_ts = 0.0
                self.dock.is_dragging_item = True
                self.dock.mouse_inside_dock = True

                self.dock.drag_merge_mode = False
                self.dock.drag_target_idx = None

                rects = list(self.dock.item_rects)
                items = list(self.dock.items)
                hovered_item_idx = None
                for i, rect in enumerate(rects):
                    center_x = (rect[0] + rect[2]) / 2.0
                    if abs(cx - center_x) < (rect[2] - rect[0]) * 0.4:
                        if i < len(items) and items[i].get("type") in ("app", "group", "running_win"):
                            self.dock.drag_merge_mode = True
                            self.dock.drag_target_idx = i
                            hovered_item_idx = i
                            break

                # Spring-Loaded Hover Tracking (Hover-to-Activate window / flyout)
                if hovered_item_idx is not None:
                    if getattr(self.dock, "_spring_hover_idx", None) != hovered_item_idx:
                        self.dock._spring_hover_idx = hovered_item_idx
                        self.dock._spring_hover_start = now
                        self.dock._spring_hover_triggered = False
                    elif not getattr(self.dock, "_spring_hover_triggered", False):
                        if (now - getattr(self.dock, "_spring_hover_start", now)) >= 0.42:
                            self.dock._spring_hover_triggered = True
                            self.dock._pending_spring_activate_idx = hovered_item_idx
                else:
                    self.dock._spring_hover_idx = None
                    self.dock._spring_hover_start = 0.0
                    self.dock._spring_hover_triggered = False

                if not self.dock.drag_merge_mode:
                    ins_idx = len(items)
                    for i, rect in enumerate(rects):
                        mid = (rect[0] + rect[2]) / 2.0
                        if cx < mid:
                            ins_idx = i
                            break
                    self.dock.drag_target_idx = ins_idx

                self.dock._dirty = True
            except Exception:
                pass
            return DROPEFFECT_COPY | DROPEFFECT_LINK

        def DragLeave(self):
            # Defer drag-end check to Tk mainloop so brief alpha-gap flickers don't cancel drop
            self.dock._drag_leave_ts = time.time()
            self.dock._spring_hover_idx = None
            self.dock._spring_hover_start = 0.0
            self.dock._spring_hover_triggered = False
            self.dock._spring_active_idx = None
            self.dock._dirty = True

        def Drop(self, data_obj, key_state, pt, effect):
            try:
                cx = pt[0] - getattr(self.dock, "dock_x", 0)
                paths = self._extract_paths(data_obj)
                self.dock.is_dragging_item = False
                self.dock.drag_target_idx = None
                self.dock.drag_merge_mode = False
                self.dock._drag_leave_ts = 0.0
                self.dock.last_leave_time = time.time()
                self.dock._spring_hover_idx = None
                self.dock._spring_hover_start = 0.0
                self.dock._spring_hover_triggered = False
                self.dock._spring_active_idx = None
                self.dock._dirty = True
                if paths:
                    self.dock._drop_queue.put((paths, cx))
            except Exception as e:
                print(f"[OLE Drop] Error: {e}")
            return DROPEFFECT_COPY

        def _extract_paths(self, data_obj):
            paths = []
            # 1. Standard CF_HDROP (Windows Explorer / Start Menu file drop)
            try:
                etc = (win32clipboard.CF_HDROP, None, pythoncom.DVASPECT_CONTENT, -1, pythoncom.TYMED_HGLOBAL)
                stg = data_obj.GetData(etc)
                raw = stg.data
                if isinstance(raw, (bytes, bytearray)):
                    pFiles, pt_x, pt_y, fNC, fWide = struct.unpack_from('IIIIi', raw, 0)
                    file_data = raw[pFiles:]
                    if fWide:
                        text = file_data.decode('utf-16le', errors='ignore')
                    else:
                        text = file_data.decode('ansi', errors='ignore')
                    paths = [p.strip() for p in text.split('\x00') if p.strip()]
                elif isinstance(raw, int):
                    count = shell32.DragQueryFileW(raw, 0xFFFFFFFF, None, 0)
                    buf = ctypes.create_unicode_buffer(1024)
                    for i in range(count):
                        shell32.DragQueryFileW(raw, i, buf, 1024)
                        if buf.value:
                            paths.append(buf.value)
                if paths:
                    return paths
            except Exception as e:
                pass

            # 2. CF_UNICODETEXT / String fallback
            try:
                etc = (win32clipboard.CF_UNICODETEXT, None, pythoncom.DVASPECT_CONTENT, -1, pythoncom.TYMED_HGLOBAL)
                stg = data_obj.GetData(etc)
                raw = stg.data
                if isinstance(raw, (bytes, bytearray)):
                    text = raw.decode('utf-16le', errors='ignore')
                else:
                    text = str(raw)
                for line in text.splitlines():
                    clean = line.strip().strip('"').strip("'")
                    if os.path.exists(clean) or clean.lower().startswith("http"):
                        paths.append(clean)
                if paths:
                    return paths
            except Exception:
                pass

            return paths

# Setup 64-bit ctypes prototypes for safe Win32 API calls
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.UINT
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.SwitchToThisWindow.restype = None
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


try:
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD]
    kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
except Exception:
    pass


def _process_memory_mb():
    """Returns (private_mb, working_set_mb, peak_working_set_mb) for the current process."""
    try:
        c = _PROCESS_MEMORY_COUNTERS_EX()
        c.cb = ctypes.sizeof(c)
        if kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return (round(c.PrivateUsage / 1048576, 1), round(c.WorkingSetSize / 1048576, 1), round(c.PeakWorkingSetSize / 1048576, 1))
    except Exception:
        pass
    return (0.0, 0.0, 0.0)

shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
shell32.DragQueryFileW.restype = wintypes.UINT
shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
shell32.DragAcceptFiles.restype = None
shell32.DragQueryPoint.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]
shell32.DragQueryPoint.restype = wintypes.BOOL
shell32.DragFinish.argtypes = [ctypes.c_void_p]
shell32.DragFinish.restype = None
try:
    user32.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
    user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
except Exception:
    pass

comctl32.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
comctl32.DefSubclassProc.restype = ctypes.c_longlong
comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t]
comctl32.SetWindowSubclass.restype = wintypes.BOOL
comctl32.RemoveWindowSubclass.argtypes = [wintypes.HWND, SUBCLASSPROC, ctypes.c_size_t]
comctl32.RemoveWindowSubclass.restype = wintypes.BOOL

def hex_to_rgb(hex_str):
    hex_str = hex_str.lstrip("#")
    if len(hex_str) == 3:
        hex_str = "".join([c*2 for c in hex_str])
    if len(hex_str) >= 6:
        return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))
    return (25, 27, 33)

class DockWindow:
    def __init__(self, config_manager, on_open_settings=None):
        self.config_mgr = config_manager
        self.on_open_settings = on_open_settings

        self.root = tk.Tk()
        self.root.title("Alioth Dockbar")
        self.root.overrideredirect(True)
        self.transparent_key = "#010203"
        self.root.config(bg=self.transparent_key)

        # Make layered toolwindow (true per-pixel alpha + never appears in taskbar or Alt+Tab)
        self.root.update_idletasks()
        self.hwnd = int(self.root.wm_frame(), 16) if self.root.wm_frame() else self.root.winfo_id()
        self._set_tool_window()

        # Canvas
        self.canvas = tk.Canvas(
            self.root,
            bg=self.transparent_key,
            highlightthickness=0,
            bd=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # State
        self.items = []
        self.icon_cache = {}          # item_id -> PIL.Image (128x128)
        self._scaled_icon_cache = OrderedDict()  # (item_id, size) -> resized PIL.Image (LRU <= 90)
        self._pill_cache = OrderedDict()         # key -> 2x supersampled glass pill PIL.Image (LRU <= 8)
        self.current_sizes = []       # Animated item sizes
        self.target_sizes = []        # Target item sizes
        self.item_rects = []          # Bounding boxes [(x1, y1, x2, y2), ...]
        self.hovered_index = None
        self.mouse_inside_dock = False
        self.mouse_canvas_pos = (-1, -1)
        self.current_photo = None     # Fallback PhotoImage reference
        self._has_rendered = False
        self._dirty = True
        self._last_hover_rendered = None
        self._last_anim_time = time.perf_counter()
        self._font_cache = {}

        # Crash-safe OLE drag-drop queue & deferred flags
        self._drop_queue = queue.SimpleQueue()
        self._need_topmost_reveal = False
        self._drag_leave_ts = 0.0
        self._last_drag_over_ts = 0.0

        # Auto-hide / Edge reveal state
        self.is_revealed = True
        self.last_leave_time = time.time()
        self.edge_check_job = None
        self.anim_job = None
        self.user_wants_exit = False

        # Flyout, Group hover & Live Window Preview state
        self.active_flyout = None
        self._hover_group_timer = None
        self._hover_group_idx = None
        self.flyout_leave_time = 0
        self.active_preview = None
        self._hover_preview_timer = None
        self._hover_preview_idx = None
        self.preview_leave_time = 0

        # Drag item within dock (reorder / group) & long-click guard
        self.press_x = 0
        self.press_y = 0
        self.press_time = 0.0
        self.pressed_idx = None
        self.is_dragging_item = False
        self.drag_target_idx = None
        self.drag_merge_mode = False

        # Drag and drop subclass proc reference to prevent GC
        self._subclass_proc_ref = None

        # Effects and Indicators
        self.bouncing_items = {}       # item_id -> start_time
        self.running_exes = set()      # set of running non-shell executable basenames
        self.running_windows = []      # list of (exe_base, title_lower, cls_lower)
        self._last_running_sig = ()
        self.pinned_items = []
        self.dynamic_open_items = []
        self._dynamic_icon_cache = OrderedDict()
        self._last_dynamic_sig = ()
        self._pwa_name_cache = {}
        self.active_launcher = None
        self._bat_cache = None
        self._bat_cache_ts = 0.0
        self._bat_icon_cache = None
        self._last_bat_draw_ts = 0.0
        self._clock_icon_cache = None
        self._last_clock_tick = ""
        try:
            from media_capsule import MediaCapsule
            self.media_capsule = MediaCapsule(self)
        except Exception as _mc_ex:
            print(f"[Dock] MediaCapsule init note: {_mc_ex}")
            self.media_capsule = None

        # Click-outside immediate autohide & Show Desktop Z-order tracking
        self._prev_btn_down = False
        self._btn_down_pt = (0, 0)
        self._prev_fg_hwnd = 0
        self._desktop_hwnd = 0
        self._was_on_desktop = False

        # Setup Drag and Drop
        self._setup_drag_and_drop()

        # Bind events
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Enter>", self._on_mouse_enter)
        self.canvas.bind("<Leave>", self._on_mouse_leave)
        self.canvas.bind("<Button-1>", self._on_button_press)
        self.canvas.bind("<B1-Motion>", self._on_button_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_button_release)
        self.canvas.bind("<Button-2>", self._on_middle_click)
        self.canvas.bind("<Button-3>", self._on_right_click)

        # Initial load
        self.reload_items()
        self.update_geometry()
        if self.media_capsule:
            try:
                self.media_capsule.sync_visibility()
            except Exception:
                pass

        # Start loops
        self._start_animation_loop()
        self._start_edge_sensor_loop()
        self._scan_running_apps()
        self.root.after(1500, self._warm_launcher_bg)
        self.root.after(4500, lambda: self._idle_memory_trim(force=True))

    def _warm_launcher_bg(self):
        try:
            import app_launcher
            app_launcher.warm_launcher_cache()
        except Exception:
            pass

    def _add_app_from_launcher(self, app):
        if not app:
            return
        new_item = {
            "type": "app",
            "name": (app.get("name") or "App").strip(),
            "target": (app.get("target") or "").strip(),
            "args": (app.get("args") or "").strip(),
            "icon_path": "",
        }
        if not new_item["target"]:
            return
        self.config_mgr.add_item(new_item)
        self.reload_items()
        self.update_geometry()
        self._render_composite_dock()

    @staticmethod
    def _is_browser_pwa_window(w):
        """Returns True if w is a standalone Chrome/Edge/Brave PWA or App-mode window (not a normal browser window)."""
        aum = (w.get("aumid_l") or "").strip()
        cls_l = w.get("cls_l", "")
        title_l = w.get("title_l", "")
        if title_l.startswith("configuración - alioth dockbar"):
            return False
        if "_crx_" in aum or aum.startswith(("apps.", "chrome._crx_", "msedge._crx_", "microsoft.msedge._crx_")):
            return True
        if cls_l.startswith("chrome_widgetwin") and w.get("exe_base") in ("chrome.exe", "msedge.exe", "brave.exe"):
            browser_suffixes = (" - google chrome", " - microsoft edge", " - microsoft\u200b edge", " - brave", " - chromium")
            if not any(title_l.endswith(sfx) for sfx in browser_suffixes):
                return True
        return False

    def _collect_open_windows(self):
        """Enumerates visible non-cloaked top-level app windows (including PWAs, UWP apps, and folder apps) via pure Win32."""
        running = set()
        win_records = []
        open_windows = []
        my_pid = os.getpid()
        show_open = bool(self.config_mgr.get("general", "show_open_windows", False))
        dwmapi = ctypes.windll.dwmapi

        ignore_classes = {
            "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
            "Windows.UI.Core.CoreWindow", "TaskListThumbnailWnd", "NotifyIconOverflowWindow",
            "TopLevelWindowForOverflowXamlIsland", "Xaml_WindowedPopupClass",
            "ForegroundStaging", "EdgeUiInputTopWndClass", "MultitaskingViewFrame"
        }
        ignore_exes = {
            "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
            "textinputhost.exe", "lockapp.exe", "explorer.exe"
        }

        def enum_proc(hwnd, lParam):
            if not user32.IsWindowVisible(hwnd) or hwnd == self.hwnd:
                return True
            title_len = user32.GetWindowTextLengthW(hwnd)
            if title_len <= 0:
                return True
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex_style & WS_EX_TOOLWINDOW:
                return True
            cls_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls_buf, 64)
            cls_name = cls_buf.value
            if cls_name in ignore_classes:
                return True
            # Skip owned popup/tool dialogs unless explicitly marked WS_EX_APPWINDOW
            if user32.GetWindow(hwnd, 4) and not (ex_style & 0x00040000):
                return True
            cloaked = ctypes.c_int(0)
            try:
                dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
                if cloaked.value:
                    return True
            except Exception:
                pass

            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value or pid.value == my_pid:
                return True

            exe_path = ""
            hProc = kernel32.OpenProcess(0x1000, False, pid.value)
            if hProc:
                try:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if kernel32.QueryFullProcessImageNameW(hProc, 0, buf, ctypes.byref(size)):
                        exe_path = buf.value
                finally:
                    kernel32.CloseHandle(hProc)

            if not exe_path:
                return True

            exe_base = os.path.basename(exe_path).lower()
            tbuf = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(hwnd, tbuf, title_len + 1)
            title = tbuf.value.strip()
            if title.startswith("Configuración - Alioth Dockbar"):
                return True
            cls_l = cls_name.lower()

            # Enumerate inactive/active Windows 11 File Explorer tabs (capped at 20)
            tabs_l = []
            if exe_base == "explorer.exe" and cls_name == "CabinetWClass":
                child = 0
                for _ in range(20):
                    child = user32.FindWindowExW(hwnd, child, "ShellTabWindowClass", None)
                    if not child:
                        break
                    clen = user32.GetWindowTextLengthW(child)
                    if clen > 0:
                        cb = ctypes.create_unicode_buffer(clen + 1)
                        user32.GetWindowTextW(child, cb, clen + 1)
                        ctitle = cb.value.strip().lower()
                        if ctitle:
                            tabs_l.append(ctitle)

            aumid_raw = ""
            if cls_l.startswith("chrome_widgetwin") or exe_base in (
                "chrome.exe", "msedge.exe", "brave.exe", "applicationframehost.exe",
                "whatsapp.root.exe", "whatsapp.exe", "ms-teams.exe", "claude.exe", "codex.exe", "electron.exe"
            ):
                aumid_raw = _get_window_aumid(hwnd)
            aumid_l = aumid_raw.lower()

            win_records.append({
                "hwnd": int(hwnd),
                "pid": int(pid.value),
                "exe_base": exe_base,
                "exe_path": exe_path,
                "title": title or os.path.splitext(exe_base)[0],
                "title_l": title.lower(),
                "cls_l": cls_l,
                "aumid": aumid_raw,
                "aumid_l": aumid_l,
                "iconic": bool(user32.IsIconic(hwnd)),
                "tabs_l": tabs_l,
            })

            if exe_base not in ("explorer.exe", "applicationframehost.exe", "chrome_proxy.exe", "msedge_proxy.exe"):
                running.add(exe_base)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_proc), 0)

        if show_open and win_records:
            # Exclude ONLY windows already represented directly as top-level icons on the main dockbar
            # (apps inside folders and unpinned PWAs ARE shown in the open-apps section!)
            prev_wins = self.running_windows
            self.running_windows = win_records
            top_pinned_hwnds = set()
            folder_child_by_hwnd = {}
            try:
                for pit in self.pinned_items:
                    ptype = pit.get("type")
                    if ptype == "app":
                        mw, _ = self.get_matching_windows(pit)
                        for w in mw:
                            top_pinned_hwnds.add(w["hwnd"])
                    elif ptype == "group":
                        for sub in pit.get("items", pit.get("children", [])):
                            mw, _ = self.get_matching_windows(sub)
                            for w in mw:
                                folder_child_by_hwnd.setdefault(w["hwnd"], sub)
            finally:
                self.running_windows = prev_wins

            seen_keys = set()
            browser_suffixes = (" - Google Chrome", " - Microsoft Edge", " - Microsoft\u200b Edge", " - Brave", " - Chromium")
            for w in win_records:
                if len(open_windows) >= 14:
                    break
                if w["hwnd"] in top_pinned_hwnds:
                    continue
                exe_b = w["exe_base"]
                if exe_b in ignore_exes:
                    continue
                is_pwa = self._is_browser_pwa_window(w)
                if exe_b == "applicationframehost.exe" and not w["aumid_l"] and not w["title"]:
                    continue

                matched_sub = folder_child_by_hwnd.get(w["hwnd"])
                if matched_sub and matched_sub.get("id"):
                    dyn_key = f"sub:{matched_sub['id']}"
                elif is_pwa or exe_b == "applicationframehost.exe":
                    dyn_key = f"pwa:{w['aumid_l'] or w['title_l']}"
                else:
                    dyn_key = f"exe:{w['exe_path'].lower()}"

                if dyn_key in seen_keys:
                    continue
                seen_keys.add(dyn_key)

                if matched_sub and matched_sub.get("name"):
                    short_name = matched_sub["name"]
                    pref_icon = matched_sub.get("icon_path", "")
                else:
                    pref_icon = ""
                    disp_title = w["title"] or os.path.splitext(os.path.basename(w["exe_path"]))[0]
                    for sfx in browser_suffixes:
                        if disp_title.endswith(sfx) and len(disp_title) > len(sfx):
                            disp_title = disp_title[:-len(sfx)].strip()
                    if not is_pwa and " - " in disp_title and exe_b not in ("whatsapp.root.exe", "whatsapp.exe"):
                        tail = disp_title.split(" - ")[-1].strip()
                        if len(tail) >= 3:
                            disp_title = tail
                    short_name = (disp_title[:25] + "…") if len(disp_title) > 27 else disp_title
                open_windows.append((w["hwnd"], w["exe_path"], short_name, w.get("aumid", ""), dyn_key, pref_icon))

        return running, win_records, open_windows

    def _refresh_open_windows_now(self):
        try:
            running, win_records, _ = self._collect_open_windows()
            self.running_exes = running
            self.running_windows = win_records
        except Exception:
            pass

    def _scan_running_apps(self):
        try:
            show_open = bool(self.config_mgr.get("general", "show_open_windows", False))
            running, win_records, open_windows = self._collect_open_windows()
            self.running_exes = running
            self.running_windows = win_records

            new_sig = tuple((dkey, name, pic) for _, _, name, _, dkey, pic in open_windows) if show_open else ()
            new_dot_sig = tuple(self._is_item_running(it) for it in self.items)
            if new_sig != self._last_dynamic_sig:
                self._last_dynamic_sig = new_sig
                self._last_running_sig = new_dot_sig
                dyn_list = []
                for hwnd, exe_path, name, aumid_raw, dyn_key, pref_icon in open_windows:
                    dyn_id = f"dyn_{abs(hash(dyn_key)) & 0xFFFFFF:x}"
                    it = {
                        "id": dyn_id,
                        "type": "running_win",
                        "name": name,
                        "target": exe_path,
                        "aumid": aumid_raw,
                        "dyn_key": dyn_key,
                        "icon_path": pref_icon,
                        "hwnd": hwnd,
                        "is_dynamic": True,
                    }
                    cache_lookup = f"{dyn_key}|{pref_icon}"
                    cached_ic = self._dynamic_icon_cache.get(cache_lookup)
                    if cached_ic is None:
                        while len(self._dynamic_icon_cache) >= 32:
                            self._dynamic_icon_cache.popitem(last=False)
                        if pref_icon and os.path.exists(pref_icon):
                            cached_ic = icon_extractor.get_or_create_icon(it, size=128)
                        else:
                            cached_ic = icon_extractor.get_window_icon(
                                hwnd, exe_path=exe_path, aumid=aumid_raw, name=name, size=128
                            )
                        self._dynamic_icon_cache[cache_lookup] = cached_ic
                    else:
                        self._dynamic_icon_cache.move_to_end(cache_lookup)
                    self.icon_cache[dyn_id] = cached_ic
                    dyn_list.append(it)
                self.dynamic_open_items = dyn_list
                self._rebuild_display_items()
                self.update_geometry()
            elif new_dot_sig != self._last_running_sig:
                self._last_running_sig = new_dot_sig
                self._dirty = True
            elif show_open:
                # Refresh live HWND handles even when same open items remain active
                hwnd_map = {dkey: hw for hw, _, _, _, dkey, _ in open_windows}
                for d_it in self.dynamic_open_items:
                    hw = hwnd_map.get(d_it.get("dyn_key", ""))
                    if hw:
                        d_it["hwnd"] = hw
        except Exception:
            pass
        if not getattr(self, "user_wants_exit", False):
            self.root.after(1500, self._scan_running_apps)

    def _get_pwa_manifest_tokens(self, target, args):
        """Extracts original PWA title tokens from Chrome/Edge Web Applications\\_crx_<id>\\*.ico."""
        key = ((target or "").lower(), (args or "").lower())
        if not hasattr(self, "_pwa_name_cache"):
            self._pwa_name_cache = {}
        cached = self._pwa_name_cache.get(key)
        if cached is not None:
            return cached
        tokens = []
        try:
            _ico_path, stem = icon_extractor.find_browser_pwa_ico(target, args)
            stem_l = (stem or "").strip().lower()
            if len(stem_l) >= 3:
                tokens.append(stem_l)
                for sep in (" · ", " - ", " | "):
                    if sep in stem_l:
                        for part in stem_l.split(sep):
                            part = part.strip()
                            if len(part) >= 3 and part not in tokens:
                                tokens.append(part)
        except Exception:
            pass
        self._pwa_name_cache[key] = tokens
        return tokens

    def get_matching_windows(self, it):
        """
        Returns (matched_window_dicts, can_kill) for an app, folder, script, group, or running_win.
        can_kill is True whenever at least one open window matches; terminate_item_processes safely
        isolates shared-process hosts (PWAs, Explorer, multi-workbook Office) so only that window ends.
        """
        wins = getattr(self, "running_windows", ())
        it_type = it.get("type", "app")

        if it_type == "running_win":
            hwnd_it = it.get("hwnd")
            aumid_it = (it.get("aumid") or "").strip().lower()
            if aumid_it:
                matches = [w for w in wins if w.get("aumid_l") == aumid_it]
                if not matches and hwnd_it:
                    matches = [w for w in wins if w["hwnd"] == hwnd_it]
            else:
                matches = [w for w in wins if w["hwnd"] == hwnd_it]
                if not matches:
                    t_exe = os.path.basename((it.get("target") or "").strip()).lower()
                    if t_exe:
                        matches = [w for w in wins if w["exe_base"] == t_exe and not self._is_browser_pwa_window(w)]
            return matches, bool(matches)

        if it_type == "group":
            seen = set()
            matches = []
            for sub in it.get("items", it.get("children", [])):
                swins, _ = self.get_matching_windows(sub)
                for w in swins:
                    if w["hwnd"] not in seen:
                        seen.add(w["hwnd"])
                        matches.append(w)
            return matches, bool(matches)

        if it_type != "app":
            return [], False

        target = (it.get("target") or "").strip()
        target_exe = os.path.basename(target).lower()
        target_ext = os.path.splitext(target_exe)[1]
        args_l = (it.get("args") or "").lower()
        name_raw = (it.get("name") or "").strip().lower()
        clean_name = re.sub(r"\s*\([^)]*\)\s*", " ", name_raw).strip()

        is_shell_app = "shell:appsfolder\\" in args_l
        is_browser_proxy = (
            target_exe in ("chrome_proxy.exe", "msedge_proxy.exe")
            or "--app-id=" in args_l
            or "--app=" in args_l
        )

        # Case A: Pinned filesystem folder (matches File Explorer CabinetWClass title or Windows 11 tab title)
        is_folder_path = (
            target
            and target_exe != "explorer.exe"
            and not is_shell_app
            and not target.lower().startswith(("http://", "https://", "shell:"))
            and not args_l
            and ((target_ext == "" and ("\\" in target or "/" in target)) or os.path.isdir(target))
        )
        if is_folder_path:
            folder_base = os.path.basename(os.path.normpath(target)).lower()
            if not folder_base:
                return [], False
            matches = []
            for w in wins:
                if w["exe_base"] == "explorer.exe" and w["cls_l"] == "cabinetwclass":
                    t = w["title_l"]
                    candidates = [t] + w.get("tabs_l", [])
                    if any(
                        c == folder_base
                        or c.startswith(folder_base + " - ")
                        or c.endswith("\\" + folder_base)
                        for c in candidates
                    ):
                        matches.append(w)
            return matches, bool(matches)

        # Case B: UWP / Store / Shell:AppsFolder or standalone Edge/Chrome PWA proxy
        if is_shell_app or is_browser_proxy:
            m_app_id = re.search(r"--app-id=([^\s\"']+)", args_l)
            if m_app_id:
                app_id = m_app_id.group(1).strip()
                if app_id:
                    m = [w for w in wins if app_id in w.get("aumid_l", "")]
                    if m:
                        return m, True

            if is_shell_app:
                m_aumid = re.search(r"shell:appsfolder\\([^\"]+)", args_l)
                if m_aumid:
                    expected_aumid = m_aumid.group(1).strip()
                    pkg_prefix = expected_aumid.split("!", 1)[0] if "!" in expected_aumid else expected_aumid
                    m = [
                        w for w in wins
                        if w.get("aumid_l", "") == expected_aumid
                        or (pkg_prefix and len(pkg_prefix) >= 6 and pkg_prefix in w.get("aumid_l", ""))
                    ]
                    if m:
                        return m, True

            if "whatsapp" in args_l:
                m = [
                    w for w in wins
                    if w["exe_base"] in ("whatsapp.exe", "whatsapp.root.exe")
                    or (w["exe_base"] == "applicationframehost.exe" and "whatsapp" in w["title_l"])
                ]
                return m, bool(m)
            if "msteams" in args_l:
                m = [
                    w for w in wins
                    if w["exe_base"] in ("ms-teams.exe", "teams.exe")
                    or (w["exe_base"] == "applicationframehost.exe" and "teams" in w["title_l"])
                ]
                return m, bool(m)
            if "anthropicclaude" in args_l:
                m = [
                    w for w in wins
                    if w["exe_base"] == "claude.exe"
                    or (w["exe_base"] in ("claude.exe", "electron.exe", "applicationframehost.exe") and "claude" in w["title_l"])
                ]
                return m, bool(m)
            if "openai.codex" in args_l:
                m = [
                    w for w in wins
                    if w["exe_base"] == "codex.exe"
                    or (w["exe_base"] in ("codex.exe", "electron.exe", "applicationframehost.exe") and "codex" in w["title_l"])
                ]
                return m, bool(m)

            browser_tab_suffixes = (" - google chrome", " - microsoft edge", " - microsoft\u200b edge", " - chromium")
            tokens = []
            for tok in self._get_pwa_manifest_tokens(target, it.get("args") or ""):
                if tok not in tokens:
                    tokens.append(tok)
            if len(clean_name) >= 3 and clean_name not in tokens:
                tokens.append(clean_name)
                for sep in (" - ", " · ", " | "):
                    if sep in clean_name:
                        for part in clean_name.split(sep):
                            part = part.strip()
                            if len(part) >= 4 and part not in tokens:
                                tokens.append(part)
            for domain_kw in ("chatgpt", "powerapps", "keep.google", "claude", "gemini"):
                if domain_kw in args_l and domain_kw not in tokens:
                    tokens.append(domain_kw)
            if not tokens:
                return [], False

            m = []
            for w in wins:
                aum = w.get("aumid_l", "")
                is_standalone_pwa = (
                    aum.startswith(("chrome._crx_", "msedge._crx_"))
                    or (w["cls_l"].startswith("chrome_widgetwin") and not any(w["title_l"].endswith(sfx) for sfx in browser_tab_suffixes))
                    or w["exe_base"] == "applicationframehost.exe"
                )
                if is_standalone_pwa and any(tok in w["title_l"] for tok in tokens):
                    m.append(w)
            return m, bool(m)

        # Case C: Plain File Explorer shortcut
        if target_exe == "explorer.exe":
            m = [w for w in wins if w["exe_base"] == "explorer.exe" and w["cls_l"] == "cabinetwclass"]
            return m, bool(m)

        # Case D: Scripts (.bat, .cmd, .ps1, .vbs)
        if target_ext in (".bat", ".cmd", ".ps1", ".vbs"):
            stem = os.path.splitext(target_exe)[0]
            parent_dir = os.path.basename(os.path.dirname(target)).lower()
            kws = []
            if stem not in ("iniciar", "start", "run", "main", "launch") and len(stem) >= 4:
                kws.append(stem)
            if len(clean_name) >= 4 and clean_name not in kws:
                kws.append(clean_name)
            if parent_dir and len(parent_dir) >= 4 and parent_dir not in kws:
                kws.append(parent_dir)
            script_hosts = ("cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "conhost.exe", "python.exe", "pythonw.exe")
            m = [w for w in wins if w["exe_base"] in script_hosts and any(k in w["title_l"] for k in kws)]
            return m, bool(m)

        # Case E: Documents (.xlsm, .xlsx, .docx, .pdf, etc.)
        if target_ext in (".xlsm", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".pdf", ".txt", ".csv"):
            doc_stem = os.path.splitext(target_exe)[0]
            if len(doc_stem) < 4:
                return [], False
            m = [w for w in wins if doc_stem in w["title_l"]]
            return m, bool(m)

        # Case F: Standard desktop executable (exclude standalone PWAs when matching plain browser .exe)
        if not target_exe:
            return [], False
        if target_exe in ("chrome.exe", "msedge.exe", "brave.exe"):
            m = [w for w in wins if w["exe_base"] == target_exe and not self._is_browser_pwa_window(w)]
        else:
            m = [w for w in wins if w["exe_base"] == target_exe]
        return m, bool(m)

    def _is_item_running(self, it):
        """Accurately determines whether a dock item is open."""
        matches, _ = self.get_matching_windows(it)
        return bool(matches)

    def _rebuild_display_items(self):
        base_size = float(self.config_mgr.get("appearance", "base_icon_size", 48))
        combined = list(self.pinned_items)
        if self.config_mgr.get("general", "show_open_windows", False) and self.dynamic_open_items:
            combined.append({"id": "__dyn_sep__", "type": "separator", "is_dynamic": True})
            combined.extend(self.dynamic_open_items)
        self.items = combined
        self.current_sizes = [base_size] * len(self.items)
        self.target_sizes = [base_size] * len(self.items)
        self._dirty = True

    def _activate_window(self, hwnd, tuck_after=True):
        try:
            if not hwnd or not user32.IsWindow(hwnd):
                return
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            else:
                user32.ShowWindow(hwnd, 5)  # SW_SHOW

            fg = user32.GetForegroundWindow()
            my_tid = kernel32.GetCurrentThreadId()
            fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            attached = False
            if fg_tid and fg_tid != my_tid:
                try:
                    attached = bool(user32.AttachThreadInput(my_tid, fg_tid, True))
                except Exception:
                    attached = False
            try:
                user32.BringWindowToTop(hwnd)
                ok = user32.SetForegroundWindow(hwnd)
                if not ok:
                    user32.SwitchToThisWindow(hwnd, True)
            finally:
                if attached:
                    try:
                        user32.AttachThreadInput(my_tid, fg_tid, False)
                    except Exception:
                        pass

            if tuck_after and self.config_mgr.get("general", "autohide", True):
                self.root.after(60, lambda: self._tuck_behind_windows(reason="activate"))
        except Exception as e:
            print(f"[Dock] Error activating window: {e}")

    def close_item_windows(self, item):
        """Posts WM_CLOSE gracefully to all open top-level windows matching the given dock item."""
        self._refresh_open_windows_now()
        wins, _ = self.get_matching_windows(item)
        seen = set()
        for w in wins:
            hw = w.get("hwnd")
            if hw and hw not in seen and user32.IsWindow(hw):
                seen.add(hw)
                try:
                    user32.PostMessageW(hw, 0x0010, 0, 0)  # WM_CLOSE
                except Exception:
                    pass
        if seen and not getattr(self, "user_wants_exit", False):
            self.root.after(280, self._scan_running_apps)
            self.root.after(700, self._scan_running_apps)

    @staticmethod
    def _force_end_window_only(hwnd):
        """Force-closes a single HWND via Win32 EndTask/SC_CLOSE without terminating shared host processes."""
        try:
            if not hwnd or not user32.IsWindow(hwnd):
                return
            user32.PostMessageW(wintypes.HWND(hwnd), 0x0112, 0xF060, 0)  # WM_SYSCOMMAND / SC_CLOSE
            user32.PostMessageW(wintypes.HWND(hwnd), 0x0010, 0, 0)       # WM_CLOSE
            try:
                user32.EndTask.argtypes = [wintypes.HWND, wintypes.BOOL, wintypes.BOOL]
                user32.EndTask.restype = wintypes.BOOL
                user32.EndTask(wintypes.HWND(hwnd), False, True)
            except Exception:
                pass
        except Exception:
            pass

    def terminate_item_processes(self, item):
        """
        Ends task for the requested item while strictly isolating shared host processes:
        - If 2 Chrome/Edge PWAs or multiple workbooks share a PID, ONLY the targeted window(s) end.
        - If UWP ApplicationFrameHost wraps an app, resolves and terminates the real child CoreWindow PID.
        - If a standalone app owns its PID exclusively, terminates that PID immediately.
        """
        self._refresh_open_windows_now()
        protected_exes = {
            "explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe",
            "svchost.exe", "sihost.exe", "shellexperiencehost.exe"
        }
        shared_browser_hosts = {"chrome.exe", "msedge.exe", "brave.exe"}
        my_pid = os.getpid()
        sub_items = (
            item.get("items", item.get("children", []))
            if item.get("type") == "group"
            else [item]
        )

        matched_wins = []
        matched_hwnds = set()
        for sub in sub_items:
            wins, _ = self.get_matching_windows(sub)
            for w in wins:
                hw = w.get("hwnd")
                if hw and hw not in matched_hwnds:
                    matched_hwnds.add(hw)
                    matched_wins.append(w)

        all_wins = getattr(self, "running_windows", ())
        pids_to_terminate = set()
        hwnds_to_end_only = set()

        for w in matched_wins:
            hw = w.get("hwnd")
            pid = w.get("pid", 0)
            exe_b = w.get("exe_base", "")
            if not hw or pid == my_pid:
                continue

            # 1. UWP host: find isolated child CoreWindow process inside ApplicationFrameHost
            if exe_b == "applicationframehost.exe":
                child_pids = set()
                try:
                    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                    def _child_cb(ch_hwnd, _lp):
                        cpid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(ch_hwnd, ctypes.byref(cpid))
                        if cpid.value and cpid.value not in (pid, my_pid):
                            child_pids.add(int(cpid.value))
                        return True
                    user32.EnumChildWindows(wintypes.HWND(hw), WNDENUMPROC(_child_cb), 0)
                except Exception:
                    pass
                if child_pids:
                    pids_to_terminate.update(child_pids)
                hwnds_to_end_only.add(hw)
                continue

            # 2. Protected shells, shared browsers/PWAs, or PIDs owning ANY other unmatched window -> end ONLY this HWND!
            pid_has_other_windows = any(
                ow.get("pid") == pid and ow.get("hwnd") not in matched_hwnds
                for ow in all_wins
            )
            if exe_b in protected_exes or exe_b in shared_browser_hosts or pid_has_other_windows:
                hwnds_to_end_only.add(hw)
            else:
                hwnds_to_end_only.add(hw)
                if pid:
                    pids_to_terminate.add(pid)

        for hw in hwnds_to_end_only:
            self._force_end_window_only(hw)

        for pid in pids_to_terminate:
            try:
                hProc = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
                if hProc:
                    try:
                        kernel32.TerminateProcess(hProc, 1)
                    finally:
                        kernel32.CloseHandle(hProc)
            except Exception:
                pass

        if (pids_to_terminate or hwnds_to_end_only) and not getattr(self, "user_wants_exit", False):
            self.root.after(220, self._scan_running_apps)
            self.root.after(620, self._scan_running_apps)

    def _pin_running_app(self, item):
        exe_path = (item.get("target") or "").strip()
        aumid = (item.get("aumid") or "").strip()
        if not exe_path and not aumid:
            return

        new_target = exe_path
        new_args = ""
        new_icon_path = ""
        disp_name = (item.get("name") or "").strip()

        aumid_l = aumid.lower()
        if "_crx_" in aumid_l and exe_path:
            raw_id = aumid_l.split("_crx_", 1)[1].split("!", 1)[0].strip("._- ")
            if raw_id:
                new_args = f"--app-id={raw_id}"
                ico_p, stem = icon_extractor.find_browser_pwa_ico(exe_path, new_args)
                if ico_p:
                    new_icon_path = icon_extractor.import_custom_icon(ico_p) or ico_p
                if not disp_name and stem:
                    disp_name = stem
        elif "!" in aumid:
            new_target = "explorer.exe"
            new_args = f"shell:AppsFolder\\{aumid}"

        if not new_args and exe_path:
            res = icon_extractor.resolve_target_and_icon(exe_path)
            new_target = res["target"]
            new_args = res.get("args", "")
            new_icon_path = res["icon_path"]
            if not disp_name:
                disp_name = res["name"]

        if not new_icon_path:
            live_ic = self.icon_cache.get(item.get("id"))
            if live_ic is not None:
                try:
                    icon_extractor.ensure_cache_dir()
                    key_h = icon_extractor.hashlib.md5(f"{new_target}|{new_args}|{aumid}".encode("utf-8", "ignore")).hexdigest()[:14]
                    out_png = os.path.join(icon_extractor.CACHE_DIR, f"v2_pin_{key_h}.png")
                    live_ic.save(out_png, "PNG")
                    new_icon_path = out_png
                except Exception:
                    pass

        new_item = {
            "type": "app",
            "name": disp_name or "Aplicación",
            "target": new_target,
            "args": new_args,
            "icon_path": new_icon_path,
            "run_as_admin": False,
        }
        self.config_mgr.add_item(new_item)
        self._last_dynamic_sig = None
        self.reload_items()
        self.update_geometry()

    def _close_running_window(self, hwnd):
        try:
            if hwnd and user32.IsWindow(hwnd):
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        except Exception:
            pass

    def _set_tool_window(self):
        try:
            top = set_tool_window(self.root, layered=True, no_activate=True)
            if top:
                self.hwnd = top
                print(f"[Dock] Layered tool window set successfully on HWND {hex(top)}")
        except Exception as e:
            print(f"[Dock] Error setting toolwindow style: {e}")

    def _setup_drag_and_drop(self):
        try:
            canvas_hwnd = self.canvas.winfo_id()
            ole_ok = False

            if HAS_PYWIN32_OLE:
                try:
                    pythoncom.OleInitialize()
                    self._ole_drop_target = win32com.server.util.wrap(DockOleDropTarget(self), pythoncom.IID_IDropTarget)
                    pythoncom.RegisterDragDrop(self.hwnd, self._ole_drop_target)
                    if canvas_hwnd and canvas_hwnd != self.hwnd:
                        try:
                            pythoncom.RegisterDragDrop(canvas_hwnd, self._ole_drop_target)
                        except Exception:
                            pass
                    ole_ok = True
                    print("[Dock] OLE Drag & Drop target registered successfully.")
                except Exception as e:
                    print(f"[Dock] Note on OLE Drag & Drop: {e}")

            for h in (self.hwnd, canvas_hwnd):
                try:
                    user32.ChangeWindowMessageFilterEx(h, WM_DROPFILES, 1, None)
                    user32.ChangeWindowMessageFilterEx(h, 0x0049, 1, None) # WM_COPYGLOBALDATA
                    user32.ChangeWindowMessageFilterEx(h, 0x004A, 1, None) # WM_COPYDATA
                except Exception:
                    pass

            # Only attach legacy WM_DROPFILES subclass when OLE RegisterDragDrop is unavailable
            if not ole_ok:
                for h in (self.hwnd, canvas_hwnd):
                    shell32.DragAcceptFiles(h, True)

                def wnd_proc(hwnd, msg, wparam, lparam, id_subclass, ref_data):
                    if msg == WM_DROPFILES:
                        hDrop = wparam
                        count = shell32.DragQueryFileW(hDrop, 0xFFFFFFFF, None, 0)
                        dropped_paths = []
                        buf = ctypes.create_unicode_buffer(1024)
                        for i in range(count):
                            shell32.DragQueryFileW(hDrop, i, buf, 1024)
                            if buf.value:
                                dropped_paths.append(buf.value)
                        pt = wintypes.POINT()
                        shell32.DragQueryPoint(hDrop, ctypes.byref(pt))
                        shell32.DragFinish(hDrop)
                        if dropped_paths:
                            self._drop_queue.put((dropped_paths, pt.x))
                        return 0
                    return comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)

                self._subclass_proc_ref = SUBCLASSPROC(wnd_proc)
                comctl32.SetWindowSubclass(self.hwnd, self._subclass_proc_ref, 101, 0)
                comctl32.SetWindowSubclass(canvas_hwnd, self._subclass_proc_ref, 102, 0)
        except Exception as e:
            print(f"[Dock] Error setting up Drag & Drop: {e}")

    def _handle_dropped_files(self, paths, drop_x):
        if self.config_mgr.get("general", "locked", False):
            messagebox.showinfo("Dock Bloqueado", "El Dock está bloqueado para evitar modificaciones. Desbloquéalo desde el menú contextual.", parent=self.root)
            return

        # Check if dropped directly onto an existing group or app item (auto-grouping)
        target_group_idx = None
        target_app_idx = None
        for i, rect in enumerate(self.item_rects):
            if i < len(self.items):
                item_cx = (rect[0] + rect[2]) / 2.0
                item_w = rect[2] - rect[0]
                if abs(drop_x - item_cx) < (item_w * 0.45):
                    if self.items[i].get("type") == "group":
                        target_group_idx = i
                        break
                    elif self.items[i].get("type") == "app":
                        target_app_idx = i
                        break

        if target_group_idx is not None:
            group_item = self.items[target_group_idx]
            items_list = group_item.setdefault("items", group_item.get("children", []))
            for path in paths:
                if not os.path.exists(path) and not path.lower().startswith("http"):
                    continue
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
                items_list.append(new_child)
            self.config_mgr.set_items(self.items)
            self.reload_items()
            self._render_composite_dock()
            return

        if target_app_idx is not None:
            existing_app = self.items[target_app_idx]
            app_target = existing_app.get("target", "")
            
            # If dropped items are document/media files and target_app has a valid executable, perform "Abrir con..."
            exec_exts = ('.exe', '.lnk', '.bat', '.cmd', '.url')
            is_all_docs = all(not p.lower().endswith(exec_exts) for p in paths if os.path.exists(p))
            if is_all_docs and app_target and (os.path.exists(app_target) or app_target.lower().endswith(".exe")):
                try:
                    args = " ".join(f'"{p}"' for p in paths)
                    shell32.ShellExecuteW(None, "open", app_target, args, None, 1)
                    return
                except Exception as ex:
                    print(f"[Dock] Error launching app with dropped file: {ex}")

            new_items_list = [existing_app]
            for path in paths:
                if not os.path.exists(path) and not path.lower().startswith("http"):
                    continue
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
                new_items_list.append(new_child)
            new_group = {
                "id": str(uuid.uuid4()),
                "type": "group",
                "name": f"Grupo ({existing_app.get('name', 'Apps')})",
                "items": new_items_list
            }
            self.items[target_app_idx] = new_group
            self.config_mgr.set_items(self.items)
            self.reload_items()
            self.update_geometry()
            return

        # Determine drop index
        insert_idx = len(self.items)
        for i, rect in enumerate(self.item_rects):
            if drop_x < (rect[0] + rect[2]) / 2:
                insert_idx = i
                break

        for path in paths:
            if not os.path.exists(path) and not path.lower().startswith("http"):
                continue
            item_data = icon_extractor.resolve_target_and_icon(path)
            new_item = {
                "type": "app",
                "name": item_data["name"],
                "target": item_data["target"],
                "args": item_data.get("args", ""),
                "icon_path": item_data["icon_path"],
                "run_as_admin": False
            }
            self.config_mgr.add_item(new_item, insert_idx)
            insert_idx += 1

        self.reload_items()
        self.update_geometry()

    def reload_items(self):
        """Loads pinned items from config, caches icons (including system actions), and merges dynamic open apps."""
        self.pinned_items = self.config_mgr.get_items()
        base_size = self.config_mgr.get("appearance", "base_icon_size", 48)

        # Clear and reload icon cache (upgrade legacy pre-v2 cached icons cleanly)
        self.icon_cache.clear()
        self._scaled_icon_cache.clear()
        self._pill_cache.clear()
        self._bat_icon_cache = None
        self._clock_icon_cache = None
        for it in self.pinned_items:
            it_id = it.get("id")
            if it.get("type") in ("app", "group", "action"):
                ic_p = it.get("icon_path", "")
                if ic_p:
                    bname = os.path.basename(ic_p)
                    if ("cache" in ic_p.lower() and "icons" in ic_p.lower()) and not (bname.startswith("v2_") or bname.startswith("custom_")):
                        it["icon_path"] = ""
                self.icon_cache[it_id] = icon_extractor.get_or_create_icon(it, size=128)

        # Re-attach dynamic icons if show_open_windows is active
        if self.config_mgr.get("general", "show_open_windows", False):
            for d_it in self.dynamic_open_items:
                ek = f"{d_it.get('dyn_key', '')}|{d_it.get('icon_path', '')}"
                if ek in self._dynamic_icon_cache:
                    self.icon_cache[d_it["id"]] = self._dynamic_icon_cache[ek]
        else:
            self.dynamic_open_items = []
            self._last_dynamic_sig = ()

        self._rebuild_display_items()
        gc.collect()

    def update_geometry(self):
        """Calculates dock window position and dimension based on screen bounds."""
        pos = self.config_mgr.get("general", "position", "bottom")
        base_size = self.config_mgr.get("appearance", "base_icon_size", 48)
        max_zoom = self.config_mgr.get("appearance", "max_zoom", 1.7)
        max_icon_h = int(base_size * max_zoom)
        pad_x = self.config_mgr.get("appearance", "dock_padding_x", 16)
        pad_y = self.config_mgr.get("appearance", "dock_padding_y", 8)
        spacing = self.config_mgr.get("appearance", "icon_spacing", 10)

        # Estimate tight max width with wave expansion (smaller buffer = faster 60+ FPS blits)
        num_items = max(1, len(self.items))
        lbl_font_sz = int(self.config_mgr.get("appearance", "label_font_size", 12))
        label_clearance = max(78, lbl_font_sz + 52) if self.config_mgr.get("appearance", "show_labels", True) else 58
        
        dock_h = max_icon_h + (pad_y * 2) + label_clearance
        wave_extra = base_size * max(0.2, max_zoom - 1.0) * min(num_items, 3.6)
        dock_w = int((base_size * num_items) + wave_extra + (pad_x * 2) + (spacing * max(0, num_items - 1)) + 96)
        
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        dock_w = min(dock_w, sw - 20)
        x = (sw - dock_w) // 2

        edge_offset = int(self.config_mgr.get("appearance", "edge_offset", 6))

        if pos == "bottom":
            y = sh - dock_h - edge_offset
        elif pos == "top":
            y = 32 + edge_offset  # Clearance for top taskbar
        elif pos == "left":
            x = edge_offset
            y = (sh - dock_h) // 2
        elif pos == "right":
            x = sw - dock_w - edge_offset
            y = (sh - dock_h) // 2
        else:
            y = sh - dock_h - edge_offset

        self.dock_w = dock_w
        self.dock_h = dock_h
        self.dock_x = x
        self.dock_y = y

        self.root.geometry(f"{dock_w}x{dock_h}+{x}+{y}")
        self.canvas.config(width=dock_w, height=dock_h)

        try:
            if self.is_revealed:
                user32.SetWindowPos(
                    self.hwnd, HWND_TOPMOST,
                    int(x), int(y), int(dock_w), int(dock_h),
                    SWP_SHOWWINDOW | SWP_NOACTIVATE
                )
            else:
                SWP_NOZORDER = 0x0004
                user32.SetWindowPos(
                    self.hwnd, 0,
                    int(x), int(y), int(dock_w), int(dock_h),
                    SWP_NOZORDER | SWP_SHOWWINDOW | SWP_NOACTIVATE
                )
            if not self.is_revealed:
                self._place_above_desktop_below_apps()
        except Exception as e:
            print(f"[Dock] SetWindowPos error: {e}")
        self._dirty = True
        if getattr(self, "media_capsule", None):
            try:
                self.media_capsule.sync_visibility()
            except Exception:
                pass

    def live_update(self):
        """Immediately updates items and geometry in real-time."""
        if not self.config_mgr.get("general", "autohide", True):
            self.is_revealed = True
        self.reload_items()
        self.update_geometry()
        if self.active_flyout and self.active_flyout.window and self.active_flyout.window.winfo_exists():
            try:
                self.active_flyout.refresh_style()
            except Exception:
                pass
        self._dirty = True
        self._render_composite_dock()
        gc.collect()

    def _get_launch_effect_name(self):
        app_cfg = self.config_mgr.data.get("appearance", {})
        if "launch_effect" in app_cfg:
            eff = str(app_cfg.get("launch_effect") or "gota_energia").strip().lower()
        else:
            eff = "gota_energia" if app_cfg.get("bounce_on_launch", True) else "ninguno"
        valid = {"gota_energia", "rebote", "gota_rebote", "pulso_neon", "levitacion", "pop_elastico", "ninguno"}
        return eff if eff in valid else "gota_energia"

    def _trigger_launch_effect(self, item_id, force=False):
        if not item_id:
            return
        eff = self._get_launch_effect_name()
        if eff == "ninguno":
            if force:
                eff = "gota_energia"
            else:
                return
        self.bouncing_items[item_id] = (time.time(), eff)
        self._dirty = True

    def preview_launch_effect(self):
        """Reveals the dockbar briefly and triggers the configured launch effect on a central icon."""
        self.is_revealed = True
        self.last_leave_time = time.time() + 1.6
        try:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
        except Exception:
            pass
        candidates = [it for it in self.items if it.get("type") in ("app", "group", "action", "running_win") and it.get("id")]
        if candidates:
            target_it = candidates[len(candidates) // 2]
            self._trigger_launch_effect(target_it["id"], force=True)

    def force_reveal(self):
        """Immediately brings the dockbar to the front (e.g. second-instance trigger)."""
        self.is_revealed = True
        self.last_leave_time = time.time()
        try:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        except Exception:
            pass
        if self.on_open_settings:
            self.on_open_settings()

    def _get_font(self, ttf_name, size):
        key = (ttf_name, int(size))
        f = self._font_cache.get(key)
        if f is None:
            try:
                f = ImageFont.truetype(ttf_name, int(size))
            except Exception:
                try:
                    f = ImageFont.truetype("segoeui.ttf", int(size))
                except Exception:
                    f = ImageFont.load_default()
            self._font_cache[key] = f
        return f

    # -------------------------------------------------------------
    # Mouse & Animation Wave Mathematics
    # -------------------------------------------------------------
    def _on_mouse_enter(self, event):
        self.mouse_inside_dock = True
        self.is_revealed = True
        self._dirty = True
        try:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
        except Exception:
            pass

    def _on_mouse_leave(self, event):
        if getattr(self, "is_dragging_item", False):
            return
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        try:
            # Only ignore <Leave> if Win32 pixel-accurate hit-testing confirms cursor is STILL on a visible dock pixel
            hit_hwnd = user32.WindowFromPoint(pt)
            hit_root = user32.GetAncestor(hit_hwnd, 2) if hit_hwnd else 0
            if hit_root == self.hwnd:
                return
        except Exception:
            pass
        self.mouse_inside_dock = False
        self.hovered_index = None
        self.mouse_canvas_pos = (-1, -1)
        self.last_leave_time = time.time()
        self._cancel_hover_group_timer()
        self._cancel_hover_preview_timer()
        # Reset target sizes to base size
        base_size = float(self.config_mgr.get("appearance", "base_icon_size", 48))
        self.target_sizes = [base_size] * len(self.items)
        self._dirty = True

    def _on_mouse_move(self, event):
        self.mouse_inside_dock = True
        self.mouse_canvas_pos = (event.x, event.y)
        self._calculate_wave(event.x)
        self._check_hover_flyouts()

    def _check_hover_flyouts(self):
        if getattr(self, "is_dragging_item", False) or CustomMenu.has_active_menu():
            self._cancel_hover_group_timer()
            self._cancel_hover_preview_timer()
            return

        if self.hovered_index is not None and 0 <= self.hovered_index < len(self.items):
            item = self.items[self.hovered_index]
            it_type = item.get("type")
            if it_type == "group":
                self._cancel_hover_preview_timer()
                self._close_active_preview()
                if self._hover_group_idx != self.hovered_index:
                    self._hover_group_idx = self.hovered_index
                    if self._hover_group_timer:
                        try:
                            self.root.after_cancel(self._hover_group_timer)
                        except Exception:
                            pass
                    # Auto-open group after 220ms hover debounce
                    self._hover_group_timer = self.root.after(220, lambda idx=self.hovered_index: self._trigger_hover_group(idx))
                return
            elif it_type in ("app", "running_win"):
                self._cancel_hover_group_timer()
                if self._hover_preview_idx != self.hovered_index:
                    self._hover_preview_idx = self.hovered_index
                    if self._hover_preview_timer:
                        try:
                            self.root.after_cancel(self._hover_preview_timer)
                        except Exception:
                            pass
                    # Show Windows 11-style live window preview after 280ms hover debounce
                    self._hover_preview_timer = self.root.after(280, lambda idx=self.hovered_index: self._trigger_hover_preview(idx))
                return

        self._cancel_hover_group_timer()
        self._cancel_hover_preview_timer()

    def _cancel_hover_group_timer(self):
        if self._hover_group_timer:
            try:
                self.root.after_cancel(self._hover_group_timer)
            except Exception:
                pass
            self._hover_group_timer = None
        self._hover_group_idx = None

    def _cancel_hover_preview_timer(self):
        if self._hover_preview_timer:
            try:
                self.root.after_cancel(self._hover_preview_timer)
            except Exception:
                pass
            self._hover_preview_timer = None
        self._hover_preview_idx = None

    def _close_active_preview(self):
        if self.active_preview:
            try:
                self.active_preview.close()
            except Exception:
                pass
            self.active_preview = None
            self.preview_leave_time = 0

    def _trigger_hover_group(self, idx):
        self._hover_group_timer = None
        if not (0 <= idx < len(self.items)) or not self.item_rects or idx >= len(self.item_rects):
            return
        if not self.mouse_inside_dock or self.hovered_index != idx:
            return
        item = self.items[idx]
        if item.get("type") != "group":
            return
        self._close_active_preview()
        if self.active_flyout and self.active_flyout.window and self.active_flyout.window.winfo_exists():
            if self.active_flyout.group_item.get("id") == item.get("id"):
                return
            self.active_flyout.close()
        self._open_group_flyout(item, self.item_rects[idx])

    def _trigger_hover_preview(self, idx):
        self._hover_preview_timer = None
        if CustomMenu.has_active_menu() or getattr(self, "is_dragging_item", False):
            return
        if not (0 <= idx < len(self.items)) or not self.item_rects or idx >= len(self.item_rects):
            return
        if not self.mouse_inside_dock or self.hovered_index != idx:
            return
        item = self.items[idx]
        if item.get("type") not in ("app", "running_win"):
            return
        self._refresh_open_windows_now()
        wins, _ = self.get_matching_windows(item)
        if not wins:
            self._close_active_preview()
            return
        if self.active_preview and self.active_preview.window and self.active_preview.window.winfo_exists():
            same_item = (self.active_preview.app_item.get("id") == item.get("id"))
            same_hwnds = ([w.get("hwnd") for w in self.active_preview.windows] == [w.get("hwnd") for w in wins[: self.active_preview.MAX_WINDOWS]])
            if same_item and same_hwnds:
                return
        self._open_window_preview(item, self.item_rects[idx], wins=wins)

    def _open_window_preview(self, item, item_rect, wins=None):
        try:
            self._close_active_preview()
            if self.active_flyout and self.active_flyout.window and self.active_flyout.window.winfo_exists():
                try:
                    self.active_flyout.close()
                except Exception:
                    pass
                self.active_flyout = None
            if wins is None:
                self._refresh_open_windows_now()
                wins, _ = self.get_matching_windows(item)
            if not wins:
                return

            from window_preview import WindowPreviewFlyout
            pos = self.config_mgr.get("general", "position", "bottom")
            can_new = (item.get("type") == "app")

            def _refresh_fn(it=item):
                self._refresh_open_windows_now()
                return self.get_matching_windows(it)[0]

            def _on_prev_close():
                self.active_preview = None
                self.preview_leave_time = 0

            self.active_preview = WindowPreviewFlyout(
                self.root,
                item,
                wins,
                item_rect,
                dock_pos=pos,
                app_icon_pil=self.icon_cache.get(item.get("id")),
                on_select_window=lambda hw: self._activate_window(hw),
                on_close_window=lambda hw: self._close_running_window(hw),
                on_new_instance=(lambda it=item: self._launch_item(it)) if can_new else None,
                on_close_all=lambda it=item: self.close_item_windows(it),
                on_close=_on_prev_close,
                refresh_windows_fn=_refresh_fn,
            )
            self._dirty = True
        except Exception as e:
            print(f"[Dock] Error opening window preview: {e}")

    def _calculate_wave(self, cursor_x):
        base_size = float(self.config_mgr.get("appearance", "base_icon_size", 48))
        max_zoom = float(self.config_mgr.get("appearance", "max_zoom", 1.7))
        wave_radius = float(self.config_mgr.get("appearance", "wave_radius", 140))
        spacing = float(self.config_mgr.get("appearance", "icon_spacing", 10))
        n = len(self.items)
        if n == 0:
            self.target_sizes = []
            self.hovered_index = None
            return

        if len(self.target_sizes) != n:
            self.target_sizes = [base_size] * n

        # Use unzoomed rest icon centers to eliminate wave feedback wobble/jitter
        win_w = getattr(self, "dock_w", 600)
        rest_total_w = n * base_size + max(0, n - 1) * spacing
        rest_start_x = (win_w - rest_total_w) / 2.0

        closest_idx = None
        min_dist = float('inf')

        for i in range(n):
            rest_cx = rest_start_x + i * (base_size + spacing) + base_size * 0.5
            dist = abs(cursor_x - rest_cx)
            if dist < wave_radius:
                u = dist / wave_radius
                factor = 0.5 * (1.0 + math.cos(math.pi * u))
                self.target_sizes[i] = base_size * (1.0 + (max_zoom - 1.0) * factor)
            else:
                self.target_sizes[i] = base_size

            # Use live rendered rect (when available) for exact hover/click hit accuracy
            hit_cx = ((self.item_rects[i][0] + self.item_rects[i][2]) * 0.5) if i < len(self.item_rects) else rest_cx
            hit_dist = abs(cursor_x - hit_cx)
            if hit_dist < min_dist:
                min_dist = hit_dist
                closest_idx = i

        self.hovered_index = closest_idx if min_dist < (base_size * max_zoom * 0.65) else None

    def _start_animation_loop(self):
        """Runs smooth dt-interpolated 60+ FPS animation loop and safely drains OLE drops."""
        self._last_anim_time = time.perf_counter()
        self._tick_animation()

    def _tick_animation(self):
        tick_ms = 55
        try:
            # 1. Apply deferred topmost reveal from COM drag callbacks safely on Tk thread
            if getattr(self, "_need_topmost_reveal", False):
                self._need_topmost_reveal = False
                self.is_revealed = True
                try:
                    user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
                except Exception:
                    pass

            # 2. Drain queued external OLE/WM_DROPFILES drops safely on Tk thread
            while not self._drop_queue.empty():
                try:
                    paths, drop_x = self._drop_queue.get_nowait()
                    if paths:
                        self._handle_dropped_files(paths, drop_x)
                except Exception as drop_ex:
                    print(f"[Dock] Drop handle error: {drop_ex}")

            # 3. Process deferred DragLeave cleanly (avoids flicker over alpha gaps)
            leave_ts = getattr(self, "_drag_leave_ts", 0.0)
            if leave_ts > 0.0 and (time.time() - leave_ts) >= 0.09:
                self._drag_leave_ts = 0.0
                if (time.time() - getattr(self, "_last_drag_over_ts", 0.0)) >= 0.08:
                    pt = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    dock_rect = wintypes.RECT()
                    user32.GetWindowRect(self.hwnd, ctypes.byref(dock_rect))
                    inside = (dock_rect.left - 12 <= pt.x <= dock_rect.right + 12) and (dock_rect.top - 16 <= pt.y <= dock_rect.bottom + 12)
                    if not inside:
                        self.is_dragging_item = False
                        self.drag_target_idx = None
                        self.drag_merge_mode = False
                        self._dirty = True

            # 4. Keep size vectors defensively synced to self.items
            base_size = float(self.config_mgr.get("appearance", "base_icon_size", 48))
            n_items = len(self.items)
            if len(self.current_sizes) != n_items:
                self.current_sizes = [base_size] * n_items
                self._dirty = True
            if len(self.target_sizes) != n_items:
                self.target_sizes = [base_size] * n_items
                self._dirty = True

            # 5. Frame-rate-independent smooth exponential interpolation
            now_perf = time.perf_counter()
            dt = min(0.05, max(0.004, now_perf - getattr(self, "_last_anim_time", now_perf)))
            self._last_anim_time = now_perf
            alpha_ease = 1.0 - math.exp(-22.0 * dt)

            needs_redraw = False
            for i in range(n_items):
                diff = self.target_sizes[i] - self.current_sizes[i]
                if abs(diff) > 0.2:
                    self.current_sizes[i] += diff * alpha_ease
                    needs_redraw = True
                elif self.current_sizes[i] != self.target_sizes[i]:
                    self.current_sizes[i] = self.target_sizes[i]
                    needs_redraw = True

            is_bouncing = bool(getattr(self, "bouncing_items", {}))
            hover_changed = (self.hovered_index != getattr(self, "_last_hover_rendered", None))
            is_dragging = getattr(self, "is_dragging_item", False)

            if needs_redraw or is_bouncing or is_dragging or hover_changed or getattr(self, "_dirty", False) or not getattr(self, "_has_rendered", False):
                self._dirty = False
                self._last_hover_rendered = self.hovered_index
                self._render_composite_dock()

            # 10ms (~100 FPS budget) during fluid launch animations, 12ms motion/hover, 55ms idle
            tick_ms = 10 if is_bouncing else (12 if (needs_redraw or is_dragging or self.mouse_inside_dock) else 55)
        except Exception as ex:
            print(f"[Dock] Animation tick error: {ex}")
        finally:
            if not getattr(self, "user_wants_exit", False):
                try:
                    self.anim_job = self.root.after(tick_ms, self._tick_animation)
                except Exception:
                    pass

    # -------------------------------------------------------------
    # Off-screen Pillow Rendering (Zero Flicker, True Alpha Pill)
    # -------------------------------------------------------------
    @staticmethod
    def _rounded_rect_perimeter_pts(x1, y1, x2, y2, r, step=4.0):
        """Samples evenly-spaced points along a rounded rectangle perimeter for dashed/dotted borders."""
        r = max(2, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
        segs = []
        def _add_line(ax, ay, bx, by):
            d = math.hypot(bx - ax, by - ay)
            n = max(1, int(d / step))
            for k in range(n):
                t = k / n
                segs.append((ax + (bx - ax) * t, ay + (by - ay) * t))
        def _add_arc(cx, cy, start_deg, end_deg):
            arc_len = abs(end_deg - start_deg) * math.pi / 180.0 * r
            n = max(3, int(arc_len / step))
            for k in range(n):
                ang = math.radians(start_deg + (end_deg - start_deg) * (k / n))
                segs.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        _add_line(x1 + r, y1, x2 - r, y1)
        _add_arc(x2 - r, y1 + r, -90, 0)
        _add_line(x2, y1 + r, x2, y2 - r)
        _add_arc(x2 - r, y2 - r, 0, 90)
        _add_line(x2 - r, y2, x1 + r, y2)
        _add_arc(x1 + r, y2 - r, 90, 180)
        _add_line(x1, y2 - r, x1, y1 + r)
        _add_arc(x1 + r, y1 + r, 180, 270)
        return segs

    def _get_battery_cached(self):
        now = time.monotonic()
        if self._bat_cache is None or (now - getattr(self, "_bat_cache_ts", 0.0)) >= 4.0:
            self._bat_cache = get_battery_status()
            self._bat_cache_ts = now
        return self._bat_cache

    def _get_battery_icon_img(self, size, bat_info, style):
        pct = int(bat_info.get("percent", 100)) if bat_info.get("has_battery") else 100
        charging = bool(bat_info.get("charging", False))
        key = (int(size), pct, charging, str(style))
        if self._bat_icon_cache and self._bat_icon_cache[0] == key:
            return self._bat_icon_cache[1]
        s = max(24, int(size))
        hi_s = s * 2
        hi = Image.new("RGBA", (hi_s, hi_s), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hi, "RGBA")
        if pct <= 15 and not charging:
            acc = (239, 68, 68)
        elif pct <= 30 and not charging:
            acc = (245, 158, 11)
        elif charging:
            acc = (56, 189, 248)
        else:
            acc = (34, 197, 94)

        cx = cy = hi_s // 2
        pad = int(hi_s * 0.12)
        hd.rounded_rectangle([pad - 2, pad - 2, hi_s - pad + 2, hi_s - pad + 2], radius=int(hi_s * 0.24), fill=(15, 23, 42, 215), outline=(255, 255, 255, 55), width=2)
        st = (style or "ring").lower()
        lbl = f"{pct}%" if bat_info.get("has_battery") else "AC"
        if st == "bar":
            bx1, bx2 = int(hi_s * 0.18), int(hi_s * 0.78)
            by1, by2 = int(hi_s * 0.34), int(hi_s * 0.66)
            hd.rounded_rectangle([bx1, by1, bx2, by2], radius=6, fill=(255, 255, 255, 26), outline=(226, 232, 240, 210), width=3)
            hd.rounded_rectangle([bx2 + 2, by1 + (by2 - by1) // 4, bx2 + 8, by2 - (by2 - by1) // 4], radius=2, fill=(226, 232, 240, 210))
            fw = max(4, int((bx2 - bx1 - 10) * (pct / 100.0)))
            hd.rounded_rectangle([bx1 + 5, by1 + 5, bx1 + 5 + fw, by2 - 5], radius=3, fill=(*acc, 240))
            f_pct = self._get_font("segoeuib.ttf", max(14, int(hi_s * 0.17)))
            hd.text((cx, int(hi_s * 0.79)), lbl, fill=(248, 250, 252, 255), font=f_pct, anchor="mm")
        elif st == "pill":
            hd.rounded_rectangle([int(hi_s * 0.15), int(hi_s * 0.28), int(hi_s * 0.85), int(hi_s * 0.72)], radius=int(hi_s * 0.22), fill=(*acc, 55), outline=(*acc, 230), width=4)
            f_pct = self._get_font("segoeuib.ttf", max(16, int(hi_s * 0.22)))
            hd.text((cx, cy), lbl, fill=(248, 250, 252, 255), font=f_pct, anchor="mm")
        elif st == "minimal":
            f_pct = self._get_font("segoeuib.ttf", max(18, int(hi_s * 0.26)))
            hd.text((cx, cy - int(hi_s * 0.05)), lbl, fill=(*acc, 255), font=f_pct, anchor="mm")
            bar_w = int(hi_s * 0.54)
            bx1 = cx - bar_w // 2
            hd.rounded_rectangle([bx1, int(hi_s * 0.70), bx1 + bar_w, int(hi_s * 0.77)], radius=3, fill=(255, 255, 255, 45))
            hd.rounded_rectangle([bx1, int(hi_s * 0.70), bx1 + max(4, int(bar_w * (pct / 100.0))), int(hi_s * 0.77)], radius=3, fill=(*acc, 245))
        else:
            # "ring" gauge (default)
            r_in = int(hi_s * 0.28)
            th = max(5, int(hi_s * 0.07))
            hd.arc([cx - r_in, cy - r_in, cx + r_in, cy + r_in], start=0, end=360, fill=(255, 255, 255, 38), width=th)
            span = int(360 * (pct / 100.0))
            if span > 0:
                hd.arc([cx - r_in, cy - r_in, cx + r_in, cy + r_in], start=-90, end=-90 + span, fill=(*acc, 250), width=th)
            f_pct = self._get_font("segoeuib.ttf", max(14, int(hi_s * 0.19)))
            hd.text((cx, cy), f"{pct}", fill=(248, 250, 252, 255), font=f_pct, anchor="mm")

        if charging:
            bx = int(hi_s * 0.80)
            by = int(hi_s * 0.22)
            hd.polygon([(bx + 2, by - 7), (bx - 5, by + 1), (bx + 1, by + 1), (bx - 2, by + 8), (bx + 6, by - 1), (bx, by - 1)], fill=(250, 204, 21, 255))

        img = hi.resize((s, s), Image.Resampling.LANCZOS)
        self._bat_icon_cache = (key, img)
        return img

    def _get_clock_icon_img(self, size):
        now_lt = time.localtime()
        style = (self.config_mgr.get("appearance", "dock_clock_style", "digital") or "digital").lower()
        use_24h = bool(self.config_mgr.get("appearance", "dock_clock_24h", True))
        show_sec = bool(self.config_mgr.get("appearance", "dock_clock_seconds", False))
        show_date = bool(self.config_mgr.get("appearance", "dock_clock_show_date", True))
        col_hex = self.config_mgr.get("appearance", "dock_clock_color", "#f8fafc") or "#f8fafc"
        acc_rgb = hex_to_rgb(col_hex)

        hr = now_lt.tm_hour
        if not use_24h:
            hr = hr % 12 or 12
        t_str = f"{hr:02d}:{now_lt.tm_min:02d}"
        s_str = f":{now_lt.tm_sec:02d}" if show_sec else ""
        dias = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
        d_str = f"{dias[now_lt.tm_wday]} {now_lt.tm_mday}" if show_date else ""
        key = (int(size), style, t_str, s_str if (show_sec or style == "analog") else "", d_str, col_hex)
        if self._clock_icon_cache and self._clock_icon_cache[0] == key:
            return self._clock_icon_cache[1]

        s = max(24, int(size))
        hi_s = s * 2
        hi = Image.new("RGBA", (hi_s, hi_s), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hi, "RGBA")
        cx = cy = hi_s // 2
        pad = int(hi_s * 0.08)

        if style == "analog":
            r = hi_s // 2 - pad
            hd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(15, 23, 42, 230), outline=(*acc_rgb, 195), width=4)
            for k_h in range(12):
                ang = math.radians(k_h * 30 - 90)
                r1 = r - (12 if k_h % 3 == 0 else 7)
                r2 = r - 3
                hd.line([(cx + math.cos(ang) * r1, cy + math.sin(ang) * r1), (cx + math.cos(ang) * r2, cy + math.sin(ang) * r2)], fill=(226, 232, 240, 210 if k_h % 3 == 0 else 120), width=3 if k_h % 3 == 0 else 2)
            # Hour, minute, optional second hand
            h_ang = math.radians(((now_lt.tm_hour % 12) + now_lt.tm_min / 60.0) * 30.0 - 90.0)
            m_ang = math.radians((now_lt.tm_min + now_lt.tm_sec / 60.0) * 6.0 - 90.0)
            hd.line([(cx, cy), (cx + math.cos(h_ang) * (r * 0.50), cy + math.sin(h_ang) * (r * 0.50))], fill=(*acc_rgb, 255), width=6)
            hd.line([(cx, cy), (cx + math.cos(m_ang) * (r * 0.72), cy + math.sin(m_ang) * (r * 0.72))], fill=(248, 250, 252, 250), width=4)
            if show_sec:
                sec_ang = math.radians(now_lt.tm_sec * 6.0 - 90.0)
                hd.line([(cx, cy), (cx + math.cos(sec_ang) * (r * 0.78), cy + math.sin(sec_ang) * (r * 0.78))], fill=(56, 189, 248, 255), width=2)
            hd.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(56, 189, 248, 255))
        elif style == "neon":
            hd.rounded_rectangle([pad, pad, hi_s - pad, hi_s - pad], radius=int(hi_s * 0.24), fill=(9, 13, 24, 240), outline=(56, 189, 248, 225), width=4)
            f_t = self._get_font("segoeuib.ttf", max(14, int(hi_s * (0.23 if not show_sec else 0.19))))
            hd.text((cx, cy - (int(hi_s * 0.10) if d_str else 0)), t_str + s_str, fill=(56, 189, 248, 255), font=f_t, anchor="mm")
            if d_str:
                f_d = self._get_font("segoeui.ttf", max(11, int(hi_s * 0.14)))
                hd.text((cx, cy + int(hi_s * 0.19)), d_str, fill=(*acc_rgb, 225), font=f_d, anchor="mm")
        elif style == "minimal":
            f_t = self._get_font("segoeuib.ttf", max(15, int(hi_s * (0.27 if not show_sec else 0.21))))
            hd.text((cx, cy - (int(hi_s * 0.11) if d_str else 0)), t_str + s_str, fill=(*acc_rgb, 255), font=f_t, anchor="mm")
            if d_str:
                f_d = self._get_font("segoeui.ttf", max(11, int(hi_s * 0.15)))
                hd.text((cx, cy + int(hi_s * 0.20)), d_str, fill=(203, 213, 225, 225), font=f_d, anchor="mm")
        elif style == "pill":
            hd.rounded_rectangle([pad, int(hi_s * 0.16), hi_s - pad, int(hi_s * 0.84)], radius=int(hi_s * 0.32), fill=(0, 120, 212, 72), outline=(56, 189, 248, 210), width=3)
            f_t = self._get_font("segoeuib.ttf", max(14, int(hi_s * (0.22 if not show_sec else 0.18))))
            hd.text((cx, cy - (int(hi_s * 0.09) if d_str else 0)), t_str + s_str, fill=(*acc_rgb, 255), font=f_t, anchor="mm")
            if d_str:
                f_d = self._get_font("segoeui.ttf", max(10, int(hi_s * 0.13)))
                hd.text((cx, cy + int(hi_s * 0.16)), d_str, fill=(224, 242, 254, 235), font=f_d, anchor="mm")
        else:
            # "digital" crisp frosted glass tile
            hd.rounded_rectangle([pad, pad, hi_s - pad, hi_s - pad], radius=int(hi_s * 0.24), fill=(15, 23, 42, 225), outline=(255, 255, 255, 65), width=2)
            f_t = self._get_font("segoeuib.ttf", max(15, int(hi_s * (0.24 if not show_sec else 0.19))))
            hd.text((cx, cy - (int(hi_s * 0.10) if d_str else 0)), t_str + s_str, fill=(*acc_rgb, 255), font=f_t, anchor="mm")
            if d_str:
                f_d = self._get_font("segoeui.ttf", max(11, int(hi_s * 0.14)))
                hd.text((cx, cy + int(hi_s * 0.19)), d_str, fill=(148, 163, 184, 240), font=f_d, anchor="mm")

        img = hi.resize((s, s), Image.Resampling.LANCZOS)
        self._clock_icon_cache = (key, img)
        return img

    @staticmethod
    def _draw_liquid_droplet(composite, cx, cy, rx, ry, base_rgb, hi_rgb, alpha):
        """Draws a thick, plump, smoothly rounded glossy water-energy teardrop with soft luminous aura."""
        if alpha <= 6 or rx < 2 or ry < 2:
            return
        pad = max(8, int(max(rx, ry) * 0.65))
        bw = int((rx + pad) * 2)
        bh = int((ry + pad) * 2)
        ss = 2
        patch = Image.new("RGBA", (bw * ss, bh * ss), (0, 0, 0, 0))
        pd = ImageDraw.Draw(patch, "RGBA")
        pcx, pcy = bw * ss / 2.0, bh * ss / 2.0
        rxs, rys = rx * ss, ry * ss
        er, eg, eb = base_rgb
        hr, hg, hb = hi_rgb

        # Soft energy halo behind teardrop
        pd.ellipse([pcx - rxs * 1.45, pcy - rys * 1.15, pcx + rxs * 1.45, pcy + rys * 1.25], fill=(er, eg, eb, int(alpha * 0.34)))
        # Rounded bottom bulb + smooth organic top tear cap (zero sharp jagged vertices!)
        bulb_cy = pcy + rys * 0.24
        pd.ellipse([pcx - rxs, bulb_cy - rxs, pcx + rxs, bulb_cy + rxs], fill=(er, eg, eb, int(alpha * 0.94)))
        # Smoothly tapered rounded neck using stacked overlapping circles -> super fluid organic teardrop
        for step_k in range(1, 7):
            frac = step_k / 6.0
            nr = max(2.0, rxs * (1.0 - frac * 0.78) ** 0.85)
            ny = bulb_cy - rys * 1.18 * frac
            pd.ellipse([pcx - nr, ny - nr, pcx + nr, ny + nr], fill=(er, eg, eb, int(alpha * 0.94)))
        # Inner bright electric-cyan energy core
        pd.ellipse([pcx - rxs * 0.68, bulb_cy - rxs * 0.68, pcx + rxs * 0.68, bulb_cy + rxs * 0.68], fill=(hr, hg, 255, int(alpha * 0.78)))
        # Glossy white curved specular crescent highlight
        sp_r = max(2.0, rxs * 0.30)
        pd.ellipse([pcx - rxs * 0.45 - sp_r, bulb_cy - rxs * 0.22 - sp_r, pcx - rxs * 0.45 + sp_r, bulb_cy - rxs * 0.22 + sp_r], fill=(245, 252, 255, min(255, int(alpha * 0.96))))

        soft = patch.filter(ImageFilter.GaussianBlur(radius=1.2))
        patch = Image.alpha_composite(soft, patch)
        small = patch.resize((bw, bh), Image.Resampling.LANCZOS)
        composite.alpha_composite(small, (int(cx - bw // 2), int(cy - bh // 2)))

    @staticmethod
    def _draw_rounded_orb(composite, cx, cy, r, base_rgb, hi_rgb, alpha):
        """Draws a glossy rounded water-energy bead with soft cyan-blue aura & white specular dot."""
        if alpha <= 6 or r < 1.5:
            return
        er, eg, eb = base_rgb
        hr, hg, hb = hi_rgb
        ri = max(2, int(round(r)))
        alpha_ellipse(composite, [cx - ri - 3, cy - ri - 3, cx + ri + 3, cy + ri + 3], fill=(er, eg, eb, int(alpha * 0.42)))
        alpha_ellipse(composite, [cx - ri, cy - ri, cx + ri, cy + ri], fill=(er, eg, eb, int(alpha * 0.92)), outline=(hr, hg, 255, min(255, int(alpha))), width=1)
        inner_r = max(1, int(ri * 0.52))
        alpha_ellipse(composite, [cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r], fill=(hr, hg, 255, int(alpha * 0.85)))
        sp = max(1, int(ri * 0.32))
        alpha_ellipse(composite, [cx - int(ri * 0.35) - sp, cy - int(ri * 0.35) - sp, cx - int(ri * 0.35) + sp, cy - int(ri * 0.35) + sp], fill=(250, 254, 255, min(255, int(alpha * 0.95))))

    def _render_composite_dock(self):
        win_w = getattr(self, "dock_w", self.canvas.winfo_width())
        win_h = getattr(self, "dock_h", self.canvas.winfo_height())
        if win_w <= 1 or win_h <= 1:
            return

        base_size = self.config_mgr.get("appearance", "base_icon_size", 48)
        spacing = self.config_mgr.get("appearance", "icon_spacing", 10)
        pad_x = self.config_mgr.get("appearance", "dock_padding_x", 16)
        pad_y = self.config_mgr.get("appearance", "dock_padding_y", 8)
        corner_radius = self.config_mgr.get("appearance", "corner_radius", 20)
        bg_hex = self.config_mgr.get("appearance", "bg_color", "#181a20")
        bg_opacity = float(self.config_mgr.get("appearance", "bg_opacity", 0.82))
        border_enabled = bool(self.config_mgr.get("appearance", "border_enabled", True))
        border_hex = self.config_mgr.get("appearance", "border_color", "#ffffff")
        border_opacity = float(self.config_mgr.get("appearance", "border_opacity", 0.18))
        border_w = max(1, min(6, int(self.config_mgr.get("appearance", "border_width", 1) or 1)))
        border_style = (self.config_mgr.get("appearance", "border_style", "solid") or "solid").lower()
        pos = self.config_mgr.get("general", "position", "bottom")
        edge_offset = int(self.config_mgr.get("appearance", "edge_offset", 6))

        # Create 32-bit transparent RGBA surface (RGBA draw mode avoids punching alpha cutouts)
        composite = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(composite, "RGBA")

        # Calculate width of all items
        num_items = len(self.items)
        if num_items == 0:
            # Empty dock notice
            return

        total_content_w = sum(self.current_sizes) + (spacing * (num_items - 1))
        dock_pill_w = int(total_content_w + (pad_x * 2))
        
        # Pill height remains STRICTLY CONSTANT based on base icon size!
        # The background pill never grows or bounces on hover.
        dock_pill_h = int(base_size + (pad_y * 2))

        # Center pill horizontally
        pill_x1 = (win_w - dock_pill_w) // 2
        pill_x2 = pill_x1 + dock_pill_w
        
        # Position pill vertically
        label_reserve = 30 if self.config_mgr.get("appearance", "show_labels", True) else 4
        if pos == "bottom":
            pill_y2 = win_h
            pill_y1 = pill_y2 - dock_pill_h
        else: # top or lateral
            pill_y1 = label_reserve
            pill_y2 = pill_y1 + dock_pill_h

        # Hit-testable dropzone halo when dragging (prevents UpdateLayeredWindow alpha==0 DragLeave drops)
        if getattr(self, "is_dragging_item", False) or getattr(self, "_external_drag_active", False):
            dz_x1 = max(0, pill_x1 - 24)
            dz_y1 = max(2, pill_y1 - 18)
            dz_x2 = min(win_w, pill_x2 + 24)
            dz_y2 = min(win_h, pill_y2 + 4)
            draw.rounded_rectangle(
                [dz_x1, dz_y1, dz_x2, dz_y2],
                radius=corner_radius + 6,
                fill=(59, 130, 246, 12),
                outline=(96, 165, 250, 150),
                width=2
            )

        # Draw 2x Supersampled Antialiased macOS Glass Rounded Pill (cached)
        bg_rgb = hex_to_rgb(bg_hex)
        bg_rgba = (*bg_rgb, max(4, int(255 * bg_opacity)))
        border_rgb = hex_to_rgb(border_hex)
        border_rgba = (*border_rgb, int(255 * border_opacity))
        round_all = bool(self.config_mgr.get("appearance", "round_all_corners", True))
        self.pill_bounds = (pill_x1, pill_y1, pill_x2, pill_y2)

        pill_w_int = max(4, pill_x2 - pill_x1)
        pill_h_int = max(4, pill_y2 - pill_y1)
        flat_mode = "all"
        if not round_all and edge_offset == 0:
            if pos == "bottom":
                flat_mode = "flat_bottom"
            elif pos == "top":
                flat_mode = "flat_top"

        pill_key = (pill_w_int, pill_h_int, corner_radius, bg_rgba, border_enabled, border_rgba, border_w, border_style, flat_mode)
        pill_img = self._pill_cache.get(pill_key)
        if pill_img is None:
            while len(self._pill_cache) >= 8:
                self._pill_cache.popitem(last=False)
            ss = 2
            hi_w, hi_h = pill_w_int * ss, pill_h_int * ss
            hi_r = min(corner_radius * ss, hi_h // 2, hi_w // 2)
            hi = Image.new("RGBA", (hi_w, hi_h), (0, 0, 0, 0))
            hd = ImageDraw.Draw(hi)
            if flat_mode == "flat_bottom":
                rect_pts = [0, 0, hi_w - 1, hi_h + hi_r]
            elif flat_mode == "flat_top":
                rect_pts = [0, -hi_r, hi_w - 1, hi_h - 1]
            else:
                rect_pts = [0, 0, hi_w - 1, hi_h - 1]
            hd.rounded_rectangle(rect_pts, radius=hi_r, fill=bg_rgba)
            if border_enabled and border_rgba[3] > 2:
                bw_hi = border_w * ss
                pad_b = max(1, bw_hi // 2)
                b_pts = [rect_pts[0] + pad_b, rect_pts[1] + pad_b, rect_pts[2] - pad_b, rect_pts[3] - pad_b]
                if border_style == "neon":
                    glow_a = max(15, int(border_rgba[3] * 0.35))
                    hd.rounded_rectangle(b_pts, radius=max(4, hi_r - pad_b), outline=(border_rgb[0], border_rgb[1], border_rgb[2], glow_a), width=bw_hi * 3)
                    hd.rounded_rectangle(b_pts, radius=max(4, hi_r - pad_b), outline=border_rgba, width=bw_hi)
                elif border_style == "double":
                    w1 = max(2, bw_hi // 2)
                    hd.rounded_rectangle(b_pts, radius=max(4, hi_r - pad_b), outline=border_rgba, width=w1)
                    ins = bw_hi + ss * 3
                    in_pts = [b_pts[0] + ins, b_pts[1] + ins, b_pts[2] - ins, b_pts[3] - ins]
                    if in_pts[2] > in_pts[0] + 20 and in_pts[3] > in_pts[1] + 20:
                        hd.rounded_rectangle(in_pts, radius=max(4, hi_r - pad_b - ins), outline=border_rgba, width=w1)
                elif border_style in ("dashed", "dotted"):
                    pts = self._rounded_rect_perimeter_pts(b_pts[0], b_pts[1], b_pts[2], b_pts[3], max(4, hi_r - pad_b), step=4.0)
                    if border_style == "dashed":
                        dash_on, dash_off = 5, 4
                        cyc = dash_on + dash_off
                        for idx_p in range(len(pts)):
                            if (idx_p % cyc) < dash_on and idx_p + 1 < len(pts):
                                hd.line([pts[idx_p], pts[idx_p + 1]], fill=border_rgba, width=bw_hi)
                    else:
                        step_d = max(3, int(bw_hi * 1.45))
                        rad_d = max(2, bw_hi // 2)
                        for idx_p in range(0, len(pts), step_d):
                            px_d, py_d = pts[idx_p]
                            hd.ellipse([px_d - rad_d, py_d - rad_d, px_d + rad_d, py_d + rad_d], fill=border_rgba)
                else:
                    hd.rounded_rectangle(b_pts, radius=max(4, hi_r - pad_b), outline=border_rgba, width=bw_hi)
            pill_img = hi.resize((pill_w_int, pill_h_int), Image.Resampling.LANCZOS)
            self._pill_cache[pill_key] = pill_img
        else:
            self._pill_cache.move_to_end(pill_key)

        composite.alpha_composite(pill_img, (pill_x1, pill_y1))

        # Draw Items (Icons and Separators)
        self.item_rects = []
        cur_x = pill_x1 + pad_x
        eff_speed = max(0.35, min(3.0, float(self.config_mgr.get("appearance", "launch_effect_speed", 1.0) or 1.0)))
        eff_dur = 1.38 / eff_speed
        eff_rgb = hex_to_rgb(self.config_mgr.get("appearance", "launch_effect_color", "#0078d4"))
        er, eg, eb = eff_rgb
        er_hi, eg_hi, eb_hi = min(255, er + 55), min(255, eg + 115), min(255, eb + 45)
        now_eff = time.time()

        for i, it in enumerate(self.items):
            size = int(self.current_sizes[i]) if i < len(self.current_sizes) else int(base_size)
            it_type = it.get("type", "app")
            it_id = it.get("id")

            # On bottom dock, align items to bottom baseline so only the icon expands UPWARDS
            if pos == "bottom":
                baseline_bottom = pill_y2 - pad_y
                item_y = baseline_bottom - size
            elif pos == "top":
                baseline_top = pill_y1 + pad_y
                item_y = baseline_top
            else:
                item_y = pill_y1 + (dock_pill_h - size) // 2

            # Launch Effects (Water/Energy Droplet, Classic Bounce, Neon Pulse, Levitation, Elastic Pop)
            active_eff = None
            p_eff = 0.0
            decay = 0.0
            eff_scale = 1.0
            bounce_dy = 0.0
            b_entry = getattr(self, "bouncing_items", {}).get(it_id)
            if b_entry is not None:
                if isinstance(b_entry, tuple):
                    b_start, b_eff = b_entry
                else:
                    b_start, b_eff = float(b_entry), self._get_launch_effect_name()
                p_eff = (now_eff - b_start) / max(0.12, eff_dur)
                if 0.0 <= p_eff < 1.0 and b_eff != "ninguno":
                    active_eff = b_eff
                    decay = max(0.0, 1.0 - p_eff)
                    if b_eff == "rebote":
                        bounce_dy = -abs(math.sin(p_eff * math.pi * 3.0)) * (base_size * 0.44) * (decay ** 1.15)
                        eff_scale = 1.0 + 0.08 * math.cos(p_eff * math.pi * 6.0) * decay
                    elif b_eff == "gota_energia":
                        # Phase 1 drop impact at p=0.26 -> gentle elastic liquid cushion dip + rise
                        if p_eff >= 0.22:
                            sp_p = (p_eff - 0.22) / 0.78
                            bounce_dy = math.sin(sp_p * math.pi * 2.0) * (base_size * 0.10) * ((1.0 - sp_p) ** 1.2)
                            eff_scale = 1.0 + 0.09 * math.sin(sp_p * math.pi * 2.5) * (1.0 - sp_p)
                    elif b_eff == "gota_rebote":
                        if p_eff >= 0.20:
                            sp_p = (p_eff - 0.20) / 0.80
                            bounce_dy = -abs(math.sin(sp_p * math.pi * 3.0)) * (base_size * 0.42) * ((1.0 - sp_p) ** 1.1)
                            eff_scale = 1.0 + 0.08 * math.sin(sp_p * math.pi * 3.0) * (1.0 - sp_p)
                    elif b_eff == "pulso_neon":
                        eff_scale = 1.0 + 0.16 * math.sin(p_eff * math.pi * 2.0) * decay
                        bounce_dy = -math.sin(p_eff * math.pi) * (base_size * 0.14)
                    elif b_eff == "levitacion":
                        bounce_dy = -math.sin(p_eff * math.pi) * (base_size * 0.36)
                    elif b_eff == "pop_elastico":
                        eff_scale = 1.0 + 0.24 * math.sin(p_eff * math.pi * 2.2) * (decay ** 1.1)
                        bounce_dy = -math.sin(p_eff * math.pi) * (base_size * 0.16) * decay
                else:
                    self.bouncing_items.pop(it_id, None)

            if pos == "top":
                item_y = min(win_h - size - 2, item_y - bounce_dy)
            else:
                item_y = max(2, item_y + bounce_dy)

            if it_type == "separator":
                sep_hex = self.config_mgr.get("appearance", "separator_color", "#ffffff") or "#ffffff"
                sep_rgb = hex_to_rgb(sep_hex)
                sep_op = float(self.config_mgr.get("appearance", "separator_opacity", 0.28) if self.config_mgr.get("appearance", "separator_opacity", None) is not None else 0.28)
                sep_rgba = (*sep_rgb, max(12, min(255, int(255 * sep_op))))
                sep_w = max(1, min(6, int(self.config_mgr.get("appearance", "separator_width", 2) or 2)))
                sep_st = (self.config_mgr.get("appearance", "separator_style", "solid") or "solid").lower()
                sep_h = int(dock_pill_h * 0.58)
                sy1 = pill_y1 + (dock_pill_h - sep_h) // 2
                sy2 = sy1 + sep_h
                sx = cur_x + size // 2
                if sep_st == "dashed":
                    y_c = sy1
                    while y_c < sy2:
                        y_e = min(sy2, y_c + 6)
                        alpha_line(composite, [(sx, y_c), (sx, y_e)], fill=sep_rgba, width=sep_w)
                        y_c += 10
                elif sep_st == "dotted":
                    y_c = sy1 + 2
                    rad_s = max(1, sep_w // 2 + 1)
                    while y_c <= sy2 - 2:
                        alpha_ellipse(composite, [sx - rad_s, y_c - rad_s, sx + rad_s, y_c + rad_s], fill=sep_rgba)
                        y_c += max(6, rad_s * 3)
                elif sep_st == "double":
                    off = max(2, sep_w)
                    alpha_line(composite, [(sx - off, sy1), (sx - off, sy2)], fill=sep_rgba, width=max(1, sep_w // 2 + 1))
                    alpha_line(composite, [(sx + off, sy1), (sx + off, sy2)], fill=sep_rgba, width=max(1, sep_w // 2 + 1))
                elif sep_st == "glow":
                    alpha_line(composite, [(sx, sy1), (sx, sy2)], fill=(*sep_rgb, max(18, int(sep_rgba[3] * 0.35))), width=sep_w + 6)
                    alpha_line(composite, [(sx, sy1), (sx, sy2)], fill=sep_rgba, width=sep_w)
                else:
                    alpha_line(composite, [(sx, sy1), (sx, sy2)], fill=sep_rgba, width=sep_w)
                self.item_rects.append((cur_x, pill_y1, cur_x + size, pill_y2))
            else:
                icx = int(cur_x + size * 0.5)
                icy = int(item_y + size * 0.5)
                floor_y = int(pill_y2 - 5 if pos == "bottom" else pill_y1 + 5)

                # Under-icon fluid water-energy splash waves / rounded orbital aurora / volumetric beam
                if active_eff in ("gota_energia", "gota_rebote"):
                    # Phase 2 starts smoothly right as the thick falling droplet impacts (p >= 0.20)
                    if p_eff >= 0.20:
                        sp_t = min(1.0, (p_eff - 0.20) / 0.80)
                        sp_ease = 1.0 - (1.0 - sp_t) ** 3
                        sp_dec = (1.0 - sp_t) ** 1.15
                        # 1) Smooth layered radial water-energy bloom pool beneath icon
                        for bloom_m, bloom_a in ((1.05, 42), (0.78, 74), (0.54, 115)):
                            br = int(size * (0.42 + 0.32 * math.sin(sp_t * math.pi)) * bloom_m)
                            alpha_ellipse(composite, [icx - br, icy - br, icx + br, icy + br], fill=(er, eg, eb, int(bloom_a * sp_dec)))
                        # 2) Three ultra-fluid 3D concentric liquid-energy waves across dock floor
                        for rw_i, dly in enumerate((0.0, 0.14, 0.29)):
                            if sp_t >= dly:
                                lp = min(1.0, (sp_t - dly) / max(0.1, 1.0 - dly))
                                l_ease = 1.0 - (1.0 - lp) ** 2.6
                                ld = (1.0 - lp) ** 1.05
                                rx_f = int(size * (0.32 + 1.52 * l_ease))
                                ry_f = max(6, int(rx_f * 0.27))
                                ra_glow = int((145 - rw_i * 22) * ld)
                                ra_mid = int((238 - rw_i * 28) * ld)
                                ra_hi = int((255 - rw_i * 30) * ld)
                                if ra_mid > 8:
                                    # Soft wide outer energy glow + lush Outlook-blue body + crisp cyan-white rounded crest
                                    alpha_ellipse(composite, [icx - rx_f, floor_y - ry_f, icx + rx_f, floor_y + ry_f], outline=(er, eg, eb, ra_glow), width=10 if rw_i == 0 else 7)
                                    alpha_ellipse(composite, [icx - rx_f, floor_y - ry_f, icx + rx_f, floor_y + ry_f], fill=(er, eg, eb, int(46 * ld)) if rw_i == 0 else None, outline=(er, eg, eb, ra_mid), width=5 if rw_i == 0 else 4)
                                    alpha_ellipse(composite, [icx - rx_f + 2, floor_y - ry_f + 1, icx + rx_f - 2, floor_y + ry_f - 1], outline=(er_hi, eg_hi, 255, ra_hi), width=2)
                        # 3) Organic rounded back splash cushion (overlapping rounded liquid domes, zero sharp spikes)
                        if sp_t < 0.78:
                            dome_p = sp_t / 0.78
                            dome_rise = math.sin(dome_p * math.pi)
                            dome_a = int(200 * (1.0 - dome_p * 0.72))
                            for lobe_x, lobe_r_m, lobe_h_m in ((-0.46, 0.24, 0.36), (-0.24, 0.28, 0.46), (0.0, 0.30, 0.42), (0.24, 0.28, 0.46), (0.46, 0.24, 0.36)):
                                lx = int(icx + lobe_x * size * (0.8 + 0.55 * dome_p))
                                ly = int(floor_y - size * lobe_h_m * dome_rise)
                                lr = max(4, int(size * lobe_r_m * (0.65 + 0.45 * dome_rise)))
                                alpha_ellipse(composite, [lx - lr, ly - lr, lx + lr, ly + lr], fill=(er, eg, eb, int(dome_a * 0.58)), outline=(er_hi, eg_hi, 255, int(dome_a * 0.92)), width=2)
                elif active_eff == "pulso_neon":
                    pulse = math.sin(p_eff * math.pi)
                    e_out = 1.0 - (1.0 - p_eff) ** 2.4
                    for r_m, a_m in ((0.96, 46), (0.72, 82), (0.50, 120)):
                        rr = int(size * (r_m + 0.26 * pulse))
                        alpha_ellipse(composite, [icx - rr, icy - rr, icx + rr, icy + rr], fill=(er, eg, eb, int(a_m * decay)))
                    ang_s = int((e_out * 480) % 360)
                    r_orb = int(size * (0.58 + 0.28 * e_out))
                    draw.arc([icx - r_orb, icy - r_orb, icx + r_orb, icy + r_orb], start=ang_s, end=ang_s + 135, fill=(er, eg, eb, int(190 * decay)), width=7)
                    draw.arc([icx - r_orb, icy - r_orb, icx + r_orb, icy + r_orb], start=ang_s + 6, end=ang_s + 128, fill=(er_hi, eg_hi, 255, int(245 * decay)), width=3)
                    draw.arc([icx - r_orb, icy - r_orb, icx + r_orb, icy + r_orb], start=ang_s + 180, end=ang_s + 315, fill=(er, eg, eb, int(170 * decay)), width=6)
                    draw.arc([icx - r_orb, icy - r_orb, icx + r_orb, icy + r_orb], start=ang_s + 186, end=ang_s + 308, fill=(255, 255, 255, int(225 * decay)), width=2)
                elif active_eff == "levitacion":
                    lift_s = math.sin(p_eff * math.pi)
                    pad_rx = int(size * (0.52 + 0.22 * lift_s))
                    pad_ry = max(5, int(size * 0.15))
                    beam_w = max(9, int(size * 0.36 * lift_s))
                    top_b, bot_b = min(icy, floor_y), max(icy, floor_y)
                    if bot_b - top_b > 4:
                        alpha_rounded_rect(composite, [icx - beam_w - 4, top_b, icx + beam_w + 4, bot_b], radius=beam_w + 4, fill=(er, eg, eb, int(42 * lift_s)))
                        alpha_rounded_rect(composite, [icx - beam_w, top_b, icx + beam_w, bot_b], radius=beam_w, fill=(er_hi, eg_hi, 255, int(68 * lift_s)))
                        for ring_off in (0.0, 0.5):
                            ry_l = int(bot_b - (bot_b - top_b) * ((p_eff * 1.8 + ring_off) % 1.0))
                            alpha_ellipse(composite, [icx - beam_w - 5, ry_l - 4, icx + beam_w + 5, ry_l + 4], outline=(er_hi, eg_hi, 255, int(215 * lift_s)), width=3)
                    alpha_ellipse(composite, [icx - pad_rx, floor_y - pad_ry, icx + pad_rx, floor_y + pad_ry], fill=(er, eg, eb, int(155 * lift_s)), outline=(er_hi, eg_hi, 255, int(235 * lift_s)), width=3)
                elif active_eff == "pop_elastico":
                    e_out = 1.0 - (1.0 - p_eff) ** 2.6
                    r_fl = int(size * (0.52 + 0.36 * e_out))
                    alpha_ellipse(composite, [icx - r_fl, icy - r_fl, icx + r_fl, icy + r_fl], fill=(er, eg, eb, int(92 * decay)))
                    r_sw = int(size * (0.36 + 1.05 * e_out))
                    alpha_ellipse(composite, [icx - r_sw, icy - r_sw, icx + r_sw, icy + r_sw], outline=(er, eg, eb, int(195 * decay)), width=6)
                    alpha_ellipse(composite, [icx - r_sw + 2, icy - r_sw + 2, icx + r_sw - 2, icy + r_sw - 2], outline=(er_hi, eg_hi, 255, int(235 * decay)), width=2)
                elif active_eff == "rebote":
                    sh_rx = max(7, int(size * (0.42 - 0.16 * min(1.0, abs(bounce_dy) / max(1.0, base_size * 0.4)))))
                    alpha_ellipse(composite, [icx - sh_rx, floor_y - 4, icx + sh_rx, floor_y + 4], fill=(0, 0, 0, int(125 * decay)))

                # App / Group / Action / Battery / Clock icon rendering
                if it_type == "battery":
                    bat_info = self._get_battery_cached()
                    bat_st = self.config_mgr.get("appearance", "dock_battery_style", "ring")
                    icon_img = self._get_battery_icon_img(size, bat_info, bat_st)
                    draw_sz = max(12, int(round(size * eff_scale))) if abs(eff_scale - 1.0) > 0.01 else size
                    dx_ic = int(icx - draw_sz * 0.5) if draw_sz != size else int(cur_x)
                    dy_ic = max(1, int(icy - draw_sz * 0.5)) if draw_sz != size else int(item_y)
                    if draw_sz != icon_img.width:
                        icon_img = icon_img.resize((draw_sz, draw_sz), Image.Resampling.LANCZOS)
                    composite.alpha_composite(icon_img, (dx_ic, dy_ic))
                elif it_type == "clock":
                    icon_img = self._get_clock_icon_img(size)
                    draw_sz = max(12, int(round(size * eff_scale))) if abs(eff_scale - 1.0) > 0.01 else size
                    dx_ic = int(icx - draw_sz * 0.5) if draw_sz != size else int(cur_x)
                    dy_ic = max(1, int(icy - draw_sz * 0.5)) if draw_sz != size else int(item_y)
                    if draw_sz != icon_img.width:
                        icon_img = icon_img.resize((draw_sz, draw_sz), Image.Resampling.LANCZOS)
                    composite.alpha_composite(icon_img, (dx_ic, dy_ic))
                else:
                    icon_img = self.icon_cache.get(it_id)
                    if icon_img:
                        draw_sz = max(12, int(round(size * eff_scale))) if abs(eff_scale - 1.0) > 0.01 else size
                        sk = (it_id, draw_sz)
                        resized = self._scaled_icon_cache.get(sk)
                        if resized is None:
                            while len(self._scaled_icon_cache) >= 48:
                                self._scaled_icon_cache.popitem(last=False)
                            resized = icon_img.resize((draw_sz, draw_sz), Image.Resampling.LANCZOS)
                            self._scaled_icon_cache[sk] = resized
                        else:
                            self._scaled_icon_cache.move_to_end(sk)
                        dx_ic = int(icx - draw_sz * 0.5) if draw_sz != size else int(cur_x)
                        dy_ic = max(1, int(icy - draw_sz * 0.5)) if draw_sz != size else int(item_y)
                        composite.alpha_composite(resized, (dx_ic, dy_ic))

                # Over-icon Phase 1 thick falling water-energy drop + Phase 2 rounded liquid crown & beads
                if active_eff in ("gota_energia", "gota_rebote"):
                    # Phase 1 (0.00 -> 0.28): Plump thick glossy water-energy teardrop plunging smoothly onto icon!
                    if p_eff < 0.29:
                        dp1 = min(1.0, p_eff / 0.26)
                        fall_ease = dp1 ** 2.1
                        start_y = max(12, int(item_y - size * 0.95))
                        end_y = int(icy + size * 0.06)
                        drop_cy = int(start_y + (end_y - start_y) * fall_ease)
                        # Drop grows thick & slightly stretches vertically as it accelerates, flattens right on impact
                        squash = 1.0 + 0.18 * dp1 if dp1 < 0.85 else max(0.65, 1.18 - (dp1 - 0.85) * 3.0)
                        drop_rx = max(7, int(size * 0.23 / math.sqrt(squash)))
                        drop_ry = max(9, int(size * 0.32 * squash))
                        fade1 = 250 if dp1 < 0.88 else int(250 * max(0.0, (0.29 - p_eff) / 0.06))
                        self._draw_liquid_droplet(composite, icx, drop_cy, drop_rx, drop_ry, (er, eg, eb), (er_hi, eg_hi, eb_hi), fade1)
                        # Soft trailing energy bead following the main thick droplet
                        if dp1 > 0.12 and dp1 < 0.86:
                            tr_y = max(6, drop_cy - int(drop_ry * 1.45))
                            self._draw_rounded_orb(composite, icx, tr_y, max(3, drop_rx * 0.42), (er, eg, eb), (er_hi, eg_hi, eb_hi), int(fade1 * 0.72))

                    # Phase 2 (0.20 -> 1.00): Rounded fluid water-energy splash crown + smooth glossy beads!
                    if p_eff >= 0.20:
                        sp_t = min(1.0, (p_eff - 0.20) / 0.80)
                        sp_ease = 1.0 - (1.0 - sp_t) ** 2.4
                        sp_dec = (1.0 - sp_t) ** 1.15
                        # Thick smooth curved liquid-energy splash arc embracing lower rim of icon
                        cr_rx = int(size * (0.56 + 0.34 * sp_ease))
                        cr_ry = int(size * (0.24 + 0.20 * math.sin(sp_t * math.pi)))
                        crown_a = int(245 * sp_dec)
                        if crown_a > 8:
                            draw.arc([icx - cr_rx, floor_y - cr_ry * 2, icx + cr_rx, floor_y + 4], start=10, end=170, fill=(er, eg, eb, int(crown_a * 0.78)), width=9)
                            draw.arc([icx - cr_rx + 2, floor_y - cr_ry * 2 + 2, icx + cr_rx - 2, floor_y + 2], start=14, end=166, fill=(er_hi, eg_hi, 255, crown_a), width=4)
                            draw.arc([icx - cr_rx + 4, floor_y - cr_ry * 2 + 4, icx + cr_rx - 4, floor_y], start=24, end=156, fill=(245, 253, 255, int(crown_a * 0.85)), width=2)
                        # 10 smooth glossy rounded water-energy beads & mini-droplets arcing fluidly outward
                        orb_specs = [
                            (-156, 1.05, 5.2, 0.00), (-140, 1.28, 6.8, 0.02), (-120, 1.46, 7.6, 0.00),
                            (-102, 1.38, 6.4, 0.04), (-78, 1.40, 6.4, 0.03), (-60, 1.48, 7.6, 0.00),
                            (-40, 1.28, 6.8, 0.02), (-24, 1.05, 5.2, 0.00), (-130, 0.92, 4.4, 0.10), (-50, 0.92, 4.4, 0.10),
                        ]
                        for ang_deg, spd_m, o_rad, o_dly in orb_specs:
                            if sp_t < o_dly:
                                continue
                            op = min(1.0, (sp_t - o_dly) / max(0.15, 0.92 - o_dly))
                            ang_r = math.radians(ang_deg)
                            dist_o = size * spd_m * (1.0 - (1.0 - op) ** 2.0)
                            grav_o = (size * 0.72) * (op ** 2.0)
                            ox = int(icx + math.cos(ang_r) * dist_o)
                            oy = max(6, int(icy + math.sin(ang_r) * dist_o + grav_o))
                            oa = int(250 * (1.0 - op ** 1.45))
                            cur_r = o_rad * (1.0 - op * 0.36)
                            self._draw_rounded_orb(composite, ox, oy, cur_r, (er, eg, eb), (er_hi, eg_hi, eb_hi), oa)
                elif active_eff == "pop_elastico":
                    e_out = 1.0 - (1.0 - p_eff) ** 2.2
                    for k_ang in range(10):
                        ang = (k_ang * math.pi / 5.0) + (p_eff * 0.38)
                        dist_p = size * (0.42 + 0.76 * e_out)
                        px = int(icx + math.cos(ang) * dist_p)
                        py = max(5, int(icy + math.sin(ang) * dist_p))
                        self._draw_rounded_orb(composite, px, py, max(2.5, 5.5 * decay), (er, eg, eb), (er_hi, eg_hi, eb_hi), int(245 * decay))

                # Running app indicator (customizable type, color & size)
                if self.config_mgr.get("appearance", "show_running_indicators", True):
                    is_open_dot = self._is_item_running(it)
                    if is_open_dot:
                        ind_st = (self.config_mgr.get("appearance", "indicator_style", "dot") or "dot").lower()
                        ind_rgb = hex_to_rgb(self.config_mgr.get("appearance", "indicator_color", "#60a5fa") or "#60a5fa")
                        ind_sz = max(3, min(12, int(self.config_mgr.get("appearance", "indicator_size", 4) or 4)))
                        dot_cx = int(cur_x + size // 2)
                        dot_cy = int(pill_y2 - 5 if pos == "bottom" else pill_y1 + 5)
                        if ind_st == "pill":
                            hw = max(6, int(ind_sz * 1.8))
                            hh = max(2, ind_sz // 2)
                            draw.rounded_rectangle([dot_cx - hw, dot_cy - hh, dot_cx + hw, dot_cy + hh], radius=hh, fill=(*ind_rgb, 245), outline=(255, 255, 255, 175), width=1)
                        elif ind_st == "line":
                            hw = max(10, int(size * 0.24 + ind_sz * 1.4))
                            hh = max(1, ind_sz // 3 + 1)
                            alpha_line(composite, [(dot_cx - hw, dot_cy), (dot_cx + hw, dot_cy)], fill=(*ind_rgb, 95), width=hh * 3)
                            draw.rounded_rectangle([dot_cx - hw, dot_cy - hh, dot_cx + hw, dot_cy + hh], radius=hh, fill=(*ind_rgb, 255))
                        elif ind_st == "ring":
                            r = max(3, ind_sz // 2 + 1)
                            draw.ellipse([dot_cx - r, dot_cy - r, dot_cx + r, dot_cy + r], outline=(*ind_rgb, 255), width=2)
                        elif ind_st == "diamond":
                            r = max(3, ind_sz // 2 + 1)
                            draw.polygon([(dot_cx, dot_cy - r), (dot_cx + r, dot_cy), (dot_cx, dot_cy + r), (dot_cx - r, dot_cy)], fill=(*ind_rgb, 245), outline=(255, 255, 255, 190))
                        else:
                            r = max(2, ind_sz // 2 + 1)
                            draw.ellipse([dot_cx - r, dot_cy - r, dot_cx + r, dot_cy + r], fill=(*ind_rgb, 245), outline=(255, 255, 255, 185))

                # Notification badge (if any)
                badge_val = it.get("badge")
                if badge_val:
                    b_str = str(badge_val).strip()
                    bx2 = int(cur_x + size + 2)
                    by1 = int(item_y - 2)
                    bw = max(16, len(b_str) * 8 + 6)
                    bh = 16
                    draw.rounded_rectangle([bx2 - bw, by1, bx2, by1 + bh], radius=8, fill=(239, 68, 68, 255), outline=(255, 255, 255, 220), width=1)
                    f_bdg = self._get_font("segoeui.ttf", 9)
                    draw.text((bx2 - bw + 4, by1 + 1), b_str[:3], fill=(255, 255, 255, 255), font=f_bdg)

                self.item_rects.append((cur_x, item_y, cur_x + size, item_y + size))

            cur_x += size + spacing

        # Draw Drag & Drop Indicators (Reorder insertion line or Group Merge aura)
        if getattr(self, "is_dragging_item", False):
            # Draw semi-transparent floating ghost under cursor
            if self.pressed_idx is not None and 0 <= self.pressed_idx < len(self.items):
                drag_it = self.items[self.pressed_idx]
                ghost_img = self.icon_cache.get(drag_it.get("id"))
                if ghost_img and hasattr(self, "drag_cur_x") and hasattr(self, "drag_cur_y"):
                    g_size = int(base_size * 1.15)
                    resized_ghost = ghost_img.resize((g_size, g_size), Image.Resampling.LANCZOS)
                    # 75% opacity
                    alpha = resized_ghost.split()[3].point(lambda p: int(p * 0.75))
                    resized_ghost.putalpha(alpha)
                    gx = int(self.drag_cur_x - g_size // 2)
                    gy = int(self.drag_cur_y - g_size // 2)
                    composite.alpha_composite(resized_ghost, (gx, gy))
            elif hasattr(self, "drag_cur_x") and hasattr(self, "drag_cur_y") and self.drag_cur_x >= 0:
                # External file being dragged from Windows Explorer
                g_size = int(base_size * 1.0)
                gx = int(self.drag_cur_x - g_size // 2)
                gy = int(self.drag_cur_y - g_size // 2)
                draw.rounded_rectangle([gx, gy, gx + g_size, gy + g_size], radius=10, fill=(30, 41, 59, 225), outline=(96, 165, 250, 245), width=2)
                f_drag = self._get_font("segoeuib.ttf", max(14, int(g_size * 0.48)))
                draw.text((gx + g_size / 2.0, gy + g_size / 2.0), "+", fill=(255, 255, 255, 255), font=f_drag, anchor="mm")

            if self.drag_target_idx is not None:
                if getattr(self, "drag_merge_mode", False):
                    if 0 <= self.drag_target_idx < len(self.item_rects):
                        tr = self.item_rects[self.drag_target_idx]
                        pad_m = 4
                        is_spring = (getattr(self, "_spring_active_idx", None) == self.drag_target_idx)
                        fill_color = (16, 185, 129, 95) if is_spring else (59, 130, 246, 90)
                        outline_color = (52, 211, 153, 240) if is_spring else (96, 165, 250, 240)
                        alpha_rounded_rect(
                            composite,
                            [tr[0] - pad_m, tr[1] - pad_m, tr[2] + pad_m, tr[3] + pad_m],
                            radius=12,
                            fill=fill_color,
                            outline=outline_color,
                            width=2
                        )
                        f_badge = self._get_font("segoeui.ttf", 9)
                        badge_text = "Ventana Activa (Soltar en app)" if is_spring else "Compartir / Agrupar"
                        draw.text((tr[0] - 8, max(2, tr[1] - 16)), badge_text, fill=(240, 253, 250, 255) if is_spring else (191, 219, 254, 255), font=f_badge)
                else:
                    if self.drag_target_idx < len(self.item_rects):
                        line_x = self.item_rects[self.drag_target_idx][0] - (spacing // 2)
                    elif self.item_rects:
                        line_x = self.item_rects[-1][2] + (spacing // 2)
                    else:
                        line_x = pill_x1 + pad_x
                    draw.line([(line_x, pill_y1 + 4), (line_x, pill_y2 - 4)], fill=(59, 130, 246, 255), width=3)

        # Draw Tooltip Label for Hovered Item (suppressed while a group flyout or window preview is open)
        flyout_open = bool(self.active_flyout and self.active_flyout.window and self.active_flyout.window.winfo_exists())
        preview_open = bool(self.active_preview and self.active_preview.window and self.active_preview.window.winfo_exists())
        if not flyout_open and not preview_open and self.config_mgr.get("appearance", "show_labels", True) and self.hovered_index is not None and not getattr(self, "is_dragging_item", False):
            if 0 <= self.hovered_index < len(self.items):
                it = self.items[self.hovered_index]
                if it.get("type") == "battery":
                    bi = self._get_battery_cached()
                    pct = bi.get("percent", 100)
                    chg = " (Cargando)" if bi.get("charging") else ""
                    name = f"Batería: {pct}%{chg}" if bi.get("has_battery") else "Corriente AC"
                    self._draw_tooltip_label(draw, name, self.item_rects[self.hovered_index], pill_y1, pill_y2, win_w, pos)
                elif it.get("type") == "clock":
                    lt = time.localtime()
                    dias = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
                    meses = ("Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")
                    name = f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}  •  {dias[lt.tm_wday]} {lt.tm_mday} {meses[lt.tm_mon - 1]}"
                    self._draw_tooltip_label(draw, name, self.item_rects[self.hovered_index], pill_y1, pill_y2, win_w, pos)
                elif it.get("type") in ("app", "group", "action", "running_win"):
                    name = it.get("name", "")
                    if name:
                        self._draw_tooltip_label(draw, name, self.item_rects[self.hovered_index], pill_y1, pill_y2, win_w, pos)

        # True 32-bit Per-Pixel Alpha rendering via Win32 UpdateLayeredWindow:
        # Background pill is independently translucent while icons/text remain 100% opaque & razor sharp!
        dock_x = getattr(self, "dock_x", 0)
        dock_y = getattr(self, "dock_y", 0)
        self._has_rendered = True
        if not update_layered_pil(self.hwnd, dock_x, dock_y, composite):
            key_rgb = hex_to_rgb(self.transparent_key)
            final_img = flatten_pil_for_colorkey(composite, key_rgb)
            self.current_photo = ImageTk.PhotoImage(final_img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self.current_photo)

    def _draw_tooltip_label(self, draw, text, item_rect, pill_y1, pill_y2, win_w, pos):
        font_name = self.config_mgr.get("appearance", "label_font_family", "Segoe UI")
        font_size = int(self.config_mgr.get("appearance", "label_font_size", 11))
        label_bold = bool(self.config_mgr.get("appearance", "label_bold", False))
        label_italic = bool(self.config_mgr.get("appearance", "label_italic", False))
        bg_hex = self.config_mgr.get("appearance", "label_bg", "#121318")
        fg_hex = self.config_mgr.get("appearance", "label_fg", "#ffffff")
        bg_opacity = float(self.config_mgr.get("appearance", "label_bg_opacity", 0.90))

        font = resolve_styled_font(font_name, font_size, bold=label_bold, italic=label_italic)

        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        lbl_pad_x = max(10, int(font_size * 0.9))
        lbl_pad_y = max(5, int(font_size * 0.45))
        lbl_w = text_w + (lbl_pad_x * 2)
        lbl_h = text_h + (lbl_pad_y * 2)

        center_x = (item_rect[0] + item_rect[2]) // 2
        lbl_x1 = max(4, min(win_w - lbl_w - 4, center_x - (lbl_w // 2)))
        lbl_x2 = lbl_x1 + lbl_w

        if pos == "bottom":
            lbl_y2 = min(pill_y1, item_rect[1]) - 6
            lbl_y1 = max(2, lbl_y2 - lbl_h)
            lbl_y2 = lbl_y1 + lbl_h
        else:
            lbl_y1 = pill_y2 + 6
            lbl_y2 = lbl_y1 + lbl_h

        bg_rgb = hex_to_rgb(bg_hex)
        fg_rgb = hex_to_rgb(fg_hex)
        bg_rgba = (*bg_rgb, int(255 * bg_opacity))

        # Draw dark rounded badge
        draw.rounded_rectangle(
            [lbl_x1, lbl_y1, lbl_x2, lbl_y2],
            radius=max(6, int(lbl_h * 0.3)),
            fill=bg_rgba,
            outline=(255, 255, 255, 55),
            width=1
        )
        # Geometrically centered horizontally and vertically inside badge
        lbl_cx = (lbl_x1 + lbl_x2) / 2.0
        lbl_cy = (lbl_y1 + lbl_y2) / 2.0
        draw.text((lbl_cx, lbl_cy), text, fill=(*fg_rgb, 255), font=font, anchor="mm")

    # -------------------------------------------------------------
    # Screen Edge Sensor & Maximized Window Behavior
    # -------------------------------------------------------------
    def _start_edge_sensor_loop(self):
        self._check_edge_sensor()

    def _is_dock_overlapped(self):
        """Checks if the active foreground window intersects the dock pill bounds."""
        try:
            fg = user32.GetForegroundWindow()
            if not fg or fg == self.hwnd or not user32.IsWindowVisible(fg):
                return False
            cls_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(fg, cls_buf, 64)
            # Ignore desktop shells / taskbar when checking overlap
            if cls_buf.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
                return False
            fg_rect = wintypes.RECT()
            dock_rect = wintypes.RECT()
            user32.GetWindowRect(fg, ctypes.byref(fg_rect))
            user32.GetWindowRect(self.hwnd, ctypes.byref(dock_rect))
            pb = getattr(self, "pill_bounds", None)
            if pb:
                px1 = dock_rect.left + pb[0]
                py1 = dock_rect.top + pb[1]
                px2 = dock_rect.left + pb[2]
                py2 = dock_rect.top + pb[3]
            else:
                px1, py1, px2, py2 = dock_rect.left, dock_rect.top, dock_rect.right, dock_rect.bottom
            return not (fg_rect.right <= px1 or fg_rect.left >= px2 or fg_rect.bottom <= py1 or fg_rect.top >= py2)
        except Exception:
            return False

    def _find_desktop_hwnd(self):
        """Locates the Windows Desktop host HWND (Progman or WorkerW owning SHELLDLL_DefView)."""
        cached = getattr(self, "_desktop_hwnd", 0)
        if cached and user32.IsWindow(cached) and user32.FindWindowExW(cached, 0, "SHELLDLL_DefView", None):
            return cached
        progman = user32.FindWindowW("Progman", None)
        if progman and user32.FindWindowExW(progman, 0, "SHELLDLL_DefView", None):
            self._desktop_hwnd = progman
            return progman
        found = [0]
        try:
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _cb(hwnd, _lp):
                if user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None):
                    found[0] = hwnd
                    return False
                return True
            user32.EnumWindows(WNDENUMPROC(_cb), 0)
        except Exception:
            pass
        self._desktop_hwnd = found[0] or progman or 0
        return self._desktop_hwnd

    def _is_normal_app_window(self, hwnd):
        """Returns True if hwnd is a visible, non-minimized, non-cloaked, non-shell normal application window."""
        if not hwnd or hwnd == self.hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return False
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        # Skip toolwindows and always-on-top overlays
        if (ex & WS_EX_TOOLWINDOW) or (ex & 0x00000008):
            return False
        ignore_cls = {
            "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
            "Windows.UI.Core.CoreWindow", "TaskListThumbnailWnd", "NotifyIconOverflowWindow",
            "TopLevelWindowForOverflowXamlIsland", "Xaml_WindowedPopupClass",
            "EdgeUiInputTopWndClass", "MultitaskingViewFrame", "ForegroundStaging"
        }
        cbuf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cbuf, 64)
        if cbuf.value in ignore_cls:
            return False
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return False
        cloaked = ctypes.c_int(0)
        try:
            ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if cloaked.value:
                return False
        except Exception:
            pass
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return False
        if (r.right - r.left) <= 40 or (r.bottom - r.top) <= 40:
            return False
        return True

    def _inspect_desktop_zorder(self):
        """
        Checks whether the Desktop shell (Progman/WorkerW owning SHELLDLL_DefView) has been
        raised ABOVE self.hwnd (e.g. during Windows Show Desktop / Win+D / taskbar corner).
        Walks strictly via GetWindow(GW_HWNDPREV) from self.hwnd so it never relies on
        EnumWindows ordering quirks on Windows 11.
        Returns (desktop_hwnd, dock_is_behind_desktop).
        """
        desk = self._find_desktop_hwnd()
        behind_desk = False
        if desk and self.hwnd:
            cur = user32.GetWindow(self.hwnd, 3)  # GW_HWNDPREV (walk upwards)
            steps = 0
            while cur and steps < 600:
                if cur == desk:
                    behind_desk = True
                    break
                cur = user32.GetWindow(cur, 3)
                steps += 1
        self._last_zorder_info = (desk, behind_desk)
        return desk, behind_desk

    def get_debug_status(self):
        """Returns lightweight live diagnostics for /api/dock_status."""
        desk, behind_desk = getattr(self, "_last_zorder_info", (0, False))
        fg = user32.GetForegroundWindow()
        fg_title = ""
        try:
            b = ctypes.create_unicode_buffer(128)
            user32.GetWindowTextW(fg, b, 128)
            fg_title = b.value
        except Exception:
            pass
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) if self.hwnd else 0
        above_fg = bool(fg and fg != self.hwnd and self._is_window_above(self.hwnd, fg))
        dots = [it.get("name") for it in self.items if self._is_item_running(it)]
        wins_sample = [
            f"{w['exe_base']}:{w['cls_l']}:{w['title'][:25]}" + (f"[{w['aumid_l'][:28]}]" if w.get("aumid_l") else "")
            for w in getattr(self, "running_windows", [])[:12]
        ]
        flyout_open = bool(self.active_flyout and self.active_flyout.window and self.active_flyout.window.winfo_exists())
        preview_open = bool(self.active_preview and self.active_preview.window and self.active_preview.window.winfo_exists())
        return {
            "hwnd": hex(self.hwnd) if self.hwnd else None,
            "is_revealed": bool(self.is_revealed),
            "topmost": bool(ex & 0x00000008),
            "noactivate": bool(ex & 0x08000000),
            "mouse_inside_dock": bool(self.mouse_inside_dock),
            "autohide": bool(self.config_mgr.get("general", "autohide", True)),
            "desk": hex(desk) if desk else None,
            "behind_desk": bool(behind_desk),
            "apps_above_desk": bool(self._find_lowest_app_above_desktop()),
            "above_fg": above_fg,
            "menu_active_count": CustomMenu.sync_active_count(),
            "flyout_open": flyout_open,
            "preview_open": preview_open,
            "is_dragging_item": bool(getattr(self, "is_dragging_item", False)),
            "mem_private_mb": _process_memory_mb()[0],
            "mem_working_set_mb": _process_memory_mb()[1],
            "mem_peak_working_set_mb": _process_memory_mb()[2],
            "scaled_icon_cache": len(self._scaled_icon_cache),
            "idle_trimmed": bool(getattr(self, "_idle_trimmed", False)),
            "last_hide_reason": getattr(self, "_last_hide_reason", ""),
            "fg_hwnd": hex(fg) if fg else None,
            "fg_title": fg_title,
            "running_dots": dots,
            "running_windows": wins_sample,
        }

    def _find_lowest_app_above_desktop(self):
        """
        Uses top-to-bottom Win32 Z-order scan so raised Windows 11 'Mostrar escritorio' WorkerW
        sheets (Taskbar corner, Dockbar button, or Win+D) are detected immediately even when
        normal app windows remain non-minimized behind WorkerW.
        """
        lowest_app, desk_cover = find_lowest_app_above_desktop(exclude_hwnds=(self.hwnd,))
        if desk_cover:
            self._desktop_hwnd = desk_cover
        return lowest_app

    def _place_above_desktop_below_apps(self):
        """
        Places self.hwnd strictly BELOW all visible normal application windows when any exist above
        the desktop, AND keeps self.hwnd visible above DWM's raised WorkerW sheet when NO normal app
        windows are above the desktop (e.g. Taskbar 'Mostrar escritorio' / Win+D).
        """
        if not self.hwnd or not user32.IsWindow(self.hwnd):
            return
        try:
            anchor = self._find_lowest_app_above_desktop()
            if anchor:
                user32.SetWindowPos(self.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                user32.SetWindowPos(self.hwnd, anchor, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            else:
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            if getattr(self, "media_capsule", None):
                self.media_capsule.sync_z_order(self.is_revealed, anchor or 0)
        except Exception:
            pass

    def _ensure_visible_on_desktop(self):
        """
        Keeps the dockbar visible right after Show Desktop (Win+D / taskbar corner / Dock button)
        above WorkerW/Progman while staying strictly BELOW any open normal app windows!
        """
        try:
            if not self.hwnd or not user32.IsWindow(self.hwnd):
                return
            was_visible = bool(user32.IsWindowVisible(self.hwnd))
            if user32.IsIconic(self.hwnd):
                user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
                was_visible = False
            desk, behind_desk = self._inspect_desktop_zorder()
            if behind_desk or not was_visible:
                if not was_visible:
                    user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
                    self._dirty = True
                if self.is_revealed:
                    user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                    if getattr(self, "media_capsule", None):
                        self.media_capsule.sync_z_order(True, 0)
                else:
                    self._place_above_desktop_below_apps()
        except Exception:
            pass

    def _is_window_above(self, hwnd_a, hwnd_b):
        """Returns True if hwnd_a sits above hwnd_b in Win32 Z-order."""
        cur = user32.GetWindow(hwnd_b, 3)  # GW_HWNDPREV
        steps = 0
        while cur and steps < 600:
            if cur == hwnd_a:
                return True
            cur = user32.GetWindow(cur, 3)
            steps += 1
        return False

    def _idle_memory_trim(self, force=False):
        """
        Releases transient animation caches and returns unused pages to Windows while the dock is
        idle. Rate-limited; never touches live state, so it is 100% safe on low-RAM machines.
        """
        if self.mouse_inside_dock or CustomMenu.has_active_menu() or getattr(self, "is_dragging_item", False):
            return
        now = time.monotonic()
        if not force and (now - getattr(self, "_last_mem_trim_ts", 0.0) < 35.0):
            return
        self._last_mem_trim_ts = now
        try:
            rest = int(float(self.config_mgr.get("appearance", "base_icon_size", 48)))
            for k in [k for k in self._scaled_icon_cache if k[1] != rest]:
                del self._scaled_icon_cache[k]
            while len(self._pill_cache) > 1:
                self._pill_cache.popitem(last=False)
            srv_mod = sys.modules.get("settings_server")
            app_search_mod = sys.modules.get("app_search")
            if app_search_mod is not None and hasattr(app_search_mod, "clear_caches"):
                if srv_mod is None or not getattr(srv_mod, "has_active_panel", lambda: False)():
                    app_search_mod.clear_caches()
            gc.collect()
            kernel32.SetProcessWorkingSetSize(kernel32.GetCurrentProcess(), ctypes.c_size_t(-1).value, ctypes.c_size_t(-1).value)
        except Exception as e:
            print(f"[Dock] Idle memory trim note: {e}")

    def _tuck_behind_windows(self, reason=""):
        """
        Tucks the dockbar strictly behind ALL open normal app windows while staying above the
        Desktop wallpaper (Progman/WorkerW) and resets hover state so idle CPU drops to ~0%.
        """
        self.is_revealed = False
        self._last_hide_reason = reason
        self._tucked_at = time.time()
        self._idle_trimmed = False
        self._cancel_hover_group_timer()
        self._cancel_hover_preview_timer()
        self._close_active_preview()
        if self.active_flyout and self.active_flyout.window and self.active_flyout.window.winfo_exists():
            try:
                self.active_flyout.close()
            except Exception:
                pass
            self.active_flyout = None
        if self.mouse_inside_dock or self.hovered_index is not None:
            self.mouse_inside_dock = False
            self.hovered_index = None
            self.mouse_canvas_pos = (-1, -1)
            base_size = float(self.config_mgr.get("appearance", "base_icon_size", 48))
            if any(abs(t - base_size) > 0.1 for t in self.target_sizes):
                self.target_sizes = [base_size] * len(self.items)
                self._dirty = True
        self._place_above_desktop_below_apps()

    def _check_edge_sensor(self):
        autohide = self.config_mgr.get("general", "autohide", True)
        pos = self.config_mgr.get("general", "position", "bottom")
        edge_thickness = self.config_mgr.get("general", "edge_trigger_thickness", 4)
        delay = float(self.config_mgr.get("general", "autohide_delay", 1.2))

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        # Check BOTH live state (0x8000) AND transition-since-last-poll (0x0001) so fast clicks are NEVER missed!
        st_l = user32.GetAsyncKeyState(0x01)
        st_r = user32.GetAsyncKeyState(0x02)
        btn_down_now = bool((st_l | st_r) & 0x8000)
        btn_just_pressed = bool((st_l | st_r) & 0x0001) or (btn_down_now and not self._prev_btn_down)
        if btn_down_now and not self._prev_btn_down:
            self._btn_down_pt = (pt.x, pt.y)
        self._prev_btn_down = btn_down_now

        is_ole_dragging = getattr(self, "is_dragging_item", False)
        drag_dist = math.hypot(pt.x - self._btn_down_pt[0], pt.y - self._btn_down_pt[1]) if btn_down_now else 0.0
        # Only expand edge drag trigger once the user has actually moved >= 18px while holding the mouse button
        # (prevents normal clicks near the bottom of an app window from expanding the 68px edge trigger!)
        external_drag = btn_down_now and (not is_ole_dragging) and (drag_dist >= 18.0)
        prev_ext = getattr(self, "_external_drag_active", False)
        self._external_drag_active = external_drag and self.is_revealed
        if self._external_drag_active != prev_ext:
            self._dirty = True

        # Process Spring-Loaded Activation triggered by OLE DragOver
        pending_spring_idx = getattr(self, "_pending_spring_activate_idx", None)
        if pending_spring_idx is not None:
            self._pending_spring_activate_idx = None
            if 0 <= pending_spring_idx < len(self.items):
                sp_item = self.items[pending_spring_idx]
                sp_type = sp_item.get("type", "app")
                if sp_type == "group":
                    if not self.active_flyout:
                        self._open_group_flyout(sp_item, self.item_rects[pending_spring_idx])
                elif sp_type in ("app", "running_win"):
                    self._refresh_open_windows_now()
                    wins, _ = self.get_matching_windows(sp_item)
                    if wins:
                        if len(wins) == 1:
                            self._activate_window(wins[0]["hwnd"], tuck_after=False)
                        else:
                            self._open_window_preview(sp_item, self.item_rects[pending_spring_idx], wins=wins)
                    self._spring_active_idx = pending_spring_idx
                    self._dirty = True

        at_edge = False
        trigger = edge_thickness + (68 if external_drag else 2)
        if pos == "bottom" and pt.y >= (sh - trigger):
            at_edge = True
        elif pos == "top" and pt.y <= (trigger + 32):
            at_edge = True
        elif pos == "left" and pt.x <= (trigger):
            at_edge = True
        elif pos == "right" and pt.x >= (sw - trigger):
            at_edge = True

        # Pixel-accurate hit-test against the ACTUAL visible layered dockbar pixels
        # (ignores the ~95px transparent air above the pill used for zoom/tooltips!).
        cursor_over_dock = False
        try:
            hit_hwnd = user32.WindowFromPoint(pt)
            hit_root = user32.GetAncestor(hit_hwnd, 2) if hit_hwnd else 0
            if hit_root == self.hwnd:
                cursor_over_dock = True
            elif external_drag and self.is_revealed:
                dock_rect = wintypes.RECT()
                user32.GetWindowRect(self.hwnd, ctypes.byref(dock_rect))
                pb = getattr(self, "pill_bounds", None)
                if pb:
                    px1, py1, px2, py2 = dock_rect.left + pb[0], dock_rect.top + pb[1], dock_rect.left + pb[2], dock_rect.top + pb[3]
                else:
                    px1, py1, px2, py2 = dock_rect.left, dock_rect.top, dock_rect.right, dock_rect.bottom
                if (px1 - 24 <= pt.x <= px2 + 24) and (py1 - 24 <= pt.y <= py2 + 10):
                    cursor_over_dock = True
        except Exception:
            pass

        # Self-heal stale mouse_inside_dock if Tkinter missed <Leave>
        if self.mouse_inside_dock and not cursor_over_dock and not is_ole_dragging:
            self.mouse_inside_dock = False
            self.hovered_index = None
            self.mouse_canvas_pos = (-1, -1)
            self.last_leave_time = time.time()
            self._cancel_hover_group_timer()
            self._cancel_hover_preview_timer()
            base_size = float(self.config_mgr.get("appearance", "base_icon_size", 48))
            self.target_sizes = [base_size] * len(self.items)
            self._dirty = True

        # Check if cursor is over active group flyout
        cursor_over_flyout = False
        if self.active_flyout and self.active_flyout.window and self.active_flyout.window.winfo_exists():
            try:
                hwnd = getattr(self.active_flyout, "top_hwnd", None) or user32.GetAncestor(self.active_flyout.window.winfo_id(), 2) or self.active_flyout.window.winfo_id()
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if (rect.left <= pt.x <= rect.right) and (rect.top <= pt.y <= rect.bottom):
                    cursor_over_flyout = True
            except Exception:
                pass

        # Check if cursor is over Media Capsule or Custom Launcher
        cursor_over_capsule = False
        mc = getattr(self, "media_capsule", None)
        if mc and mc.top_hwnd and user32.IsWindowVisible(mc.top_hwnd):
            try:
                crect = wintypes.RECT()
                user32.GetWindowRect(mc.top_hwnd, ctypes.byref(crect))
                if (crect.left - 4 <= pt.x <= crect.right + 4) and (crect.top - 4 <= pt.y <= crect.bottom + 4):
                    cursor_over_capsule = True
            except Exception:
                pass

        launcher_open = bool(self.active_launcher and self.active_launcher.window and self.active_launcher.window.winfo_exists())
        cursor_over_launcher = False
        if launcher_open:
            try:
                lhwnd = getattr(self.active_launcher, "top_hwnd", None) or self.active_launcher.window.winfo_id()
                lrect = wintypes.RECT()
                user32.GetWindowRect(lhwnd, ctypes.byref(lrect))
                if (lrect.left <= pt.x <= lrect.right) and (lrect.top <= pt.y <= lrect.bottom):
                    cursor_over_launcher = True
            except Exception:
                pass

        # Check if cursor is over active live window preview flyout
        cursor_over_preview = False
        if self.active_preview and self.active_preview.window and self.active_preview.window.winfo_exists():
            try:
                hwnd = getattr(self.active_preview, "top_hwnd", None) or user32.GetAncestor(self.active_preview.window.winfo_id(), 2) or self.active_preview.window.winfo_id()
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if (rect.left - 14 <= pt.x <= rect.right + 14) and (rect.top - 14 <= pt.y <= rect.bottom + 14):
                    cursor_over_preview = True
            except Exception:
                pass

        flyout_dragging = bool(self.active_flyout and getattr(self.active_flyout, "is_dragging", False))
        menu_open = CustomMenu.has_active_menu()
        cursor_over_menu = CustomMenu.is_point_inside_any(pt.x, pt.y, pad=8) if menu_open else False
        flyout_modal = bool(self.active_flyout and getattr(self.active_flyout, "modal_open", False))

        # Self-heal stuck dragging state if a drag ended without Drop/ButtonRelease-1
        if is_ole_dragging and not btn_down_now and (time.time() - getattr(self, "_last_drag_over_ts", 0.0)) > 0.35:
            self.is_dragging_item = False
            self.drag_target_idx = None
            self.drag_merge_mode = False
            self.pressed_idx = None
            self._dirty = True
            is_ole_dragging = False

        # Reveal ONLY on physical screen edge touch or genuine unoccluded visible-pixel hover
        if at_edge or cursor_over_dock:
            if not self.is_revealed:
                self.is_revealed = True
                try:
                    user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
                    if mc:
                        mc.sync_z_order(True, 0)
                except Exception:
                    pass
                self._dirty = True
            self.last_leave_time = time.time()
        elif cursor_over_flyout or cursor_over_preview or cursor_over_capsule or cursor_over_launcher or flyout_dragging or menu_open or cursor_over_menu or flyout_modal:
            if not self.is_revealed:
                self.is_revealed = True
                try:
                    user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
                    if mc:
                        mc.sync_z_order(True, 0)
                except Exception:
                    pass
                self._dirty = True
            self.last_leave_time = time.time()
            if cursor_over_flyout or flyout_dragging or menu_open or cursor_over_menu or flyout_modal:
                self.flyout_leave_time = 0
            if cursor_over_preview or menu_open or cursor_over_menu:
                self.preview_leave_time = 0
        else:
            pass

        # Requirement #11: Close Dockbar folders promptly whenever mouse leaves the folder (and its owner icon!)
        if self.active_flyout and self.active_flyout.window and self.active_flyout.window.winfo_exists():
            owner_id = self.active_flyout.group_item.get("id")
            hover_on_owner = (
                cursor_over_dock
                and self.hovered_index is not None
                and 0 <= self.hovered_index < len(self.items)
                and self.items[self.hovered_index].get("id") == owner_id
            )
            hover_other_icon = (
                cursor_over_dock
                and self.hovered_index is not None
                and not hover_on_owner
            )
            if cursor_over_flyout or flyout_dragging or menu_open or cursor_over_menu or flyout_modal or hover_on_owner:
                self.flyout_leave_time = 0
            else:
                if self.flyout_leave_time == 0:
                    self.flyout_leave_time = time.time()
                close_delay = 0.06 if hover_other_icon else 0.14
                if time.time() - self.flyout_leave_time >= close_delay:
                    try:
                        self.active_flyout.close()
                    except Exception:
                        pass
                    self.active_flyout = None
                    self.flyout_leave_time = 0

        if self.active_preview and self.active_preview.window and self.active_preview.window.winfo_exists():
            if not cursor_over_preview and not cursor_over_dock and not menu_open and not cursor_over_menu:
                if self.preview_leave_time == 0:
                    self.preview_leave_time = time.time()
                elif time.time() - self.preview_leave_time > 0.38:
                    self._close_active_preview()
            else:
                self.preview_leave_time = 0

        menu_open = CustomMenu.has_active_menu()
        flyout_open = bool(self.active_flyout and self.active_flyout.window and self.active_flyout.window.winfo_exists())
        preview_open = bool(self.active_preview and self.active_preview.window and self.active_preview.window.winfo_exists())

        # Instant click-outside detection (triggers immediately on mouse-DOWN outside dock/flyout/preview!)
        clicked_outside = (
            btn_just_pressed and not cursor_over_dock and not cursor_over_flyout
            and not cursor_over_preview and not cursor_over_capsule and not cursor_over_launcher
            and not flyout_dragging and not at_edge
            and not menu_open and not cursor_over_menu and not flyout_modal
        )

        fg_now = user32.GetForegroundWindow()
        fg_cls = ""
        if fg_now:
            try:
                _c = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(fg_now, _c, 64)
                fg_cls = _c.value
            except Exception:
                fg_cls = ""
        is_shell_fg = fg_cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")
        fg_changed_outside = (
            fg_now != 0 and fg_now != self.hwnd and fg_now != self._prev_fg_hwnd
            and not is_shell_fg
            and not cursor_over_dock and not cursor_over_flyout and not cursor_over_preview
            and not flyout_dragging and not at_edge
            and not menu_open and not cursor_over_menu and not flyout_modal
        )
        fg_switched_to_shell = (fg_now != self._prev_fg_hwnd and is_shell_fg)
        self._prev_fg_hwnd = fg_now

        if user32.IsIconic(self.hwnd):
            try:
                user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            except Exception:
                pass

        # Instant recovery when user clicks Taskbar 'Mostrar escritorio' or Win+D
        if fg_switched_to_shell and not self._find_lowest_app_above_desktop():
            try:
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                if mc:
                    mc.sync_z_order(self.is_revealed, 0)
            except Exception:
                pass

        # Periodically refresh Dockbar battery & clock items if present
        if any(it.get("type") == "battery" for it in self.items):
            if time.monotonic() - getattr(self, "_last_bat_draw_ts", 0.0) >= 4.0:
                self._last_bat_draw_ts = time.monotonic()
                self._dirty = True
        if any(it.get("type") == "clock" for it in self.items):
            lt_c = time.localtime()
            show_sec_c = bool(self.config_mgr.get("appearance", "dock_clock_seconds", False)) or (self.config_mgr.get("appearance", "dock_clock_style", "digital") == "analog")
            ck = f"{lt_c.tm_hour}:{lt_c.tm_min}:{lt_c.tm_sec}" if show_sec_c else f"{lt_c.tm_hour}:{lt_c.tm_min}"
            if ck != getattr(self, "_last_clock_tick", ""):
                self._last_clock_tick = ck
                self._dirty = True

        # 1. UNCONDITIONAL hide on click-outside, focus switch, or autohide delay
        if autohide and self.is_revealed and not menu_open and not cursor_over_menu and not flyout_modal and not cursor_over_dock and not cursor_over_flyout and not cursor_over_preview and not cursor_over_capsule and not cursor_over_launcher and not flyout_dragging and not at_edge and not is_ole_dragging:
            if clicked_outside or fg_changed_outside or (not btn_down_now and not flyout_open and not preview_open and not launcher_open and (time.time() - self.last_leave_time > delay)):
                self._tuck_behind_windows(reason="click" if clicked_outside else ("fg" if fg_changed_outside else "delay"))
        elif autohide and (not self.is_revealed):
            # Keep dock AND media capsule strictly behind apps when apps exist above desktop, OR above WorkerW during Show Desktop
            try:
                has_topmost = bool(user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & 0x00000008)
                anchor_app = self._find_lowest_app_above_desktop()
                has_apps = bool(anchor_app)
                if has_apps and has_topmost:
                    self._tuck_behind_windows(reason="heal_topmost")
                elif (not has_apps) and (not has_topmost):
                    self._place_above_desktop_below_apps()
                # Strict Media Capsule Z-order lockstep (never stays topmost when dock is tucked behind open apps!)
                if mc and mc.hwnd and user32.IsWindow(mc.hwnd) and not cursor_over_capsule:
                    mc_topmost = bool(user32.GetWindowLongW(mc.hwnd, GWL_EXSTYLE) & 0x00000008)
                    if has_apps and (mc_topmost or (fg_now and fg_now != self.hwnd and fg_now != mc.hwnd and not is_shell_fg and self._is_window_above(mc.hwnd, fg_now))):
                        mc.sync_z_order(False, fg_now if (fg_now and not is_shell_fg) else anchor_app)
                    elif (not has_apps) and (not mc_topmost):
                        mc.sync_z_order(False, 0)
            except Exception:
                pass

        # 2. Show Desktop recovery & foreground Z-order self-heal
        now_z = time.monotonic()
        z_interval = 0.16 if self.is_revealed else 0.35
        if now_z - getattr(self, "_last_z_scan_ts", 0.0) >= z_interval:
            self._last_z_scan_ts = now_z
            desk, behind_desk = self._inspect_desktop_zorder()
            vis_now = bool(user32.IsWindowVisible(self.hwnd))
            if behind_desk or not vis_now:
                try:
                    if not vis_now:
                        user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
                        self._dirty = True
                    if self.is_revealed:
                        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                        if mc:
                            mc.sync_z_order(True, 0)
                    else:
                        self._place_above_desktop_below_apps()
                except Exception:
                    pass
            elif autohide and (not self.is_revealed) and fg_now and fg_now != self.hwnd and not is_shell_fg:
                if mc and mc.hwnd and user32.IsWindow(mc.hwnd) and not cursor_over_capsule and self._is_window_above(mc.hwnd, fg_now):
                    mc.sync_z_order(False, fg_now)
                try:
                    if user32.IsWindowVisible(fg_now) and not user32.IsIconic(fg_now) and self._find_lowest_app_above_desktop():
                        fg_pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(fg_now, ctypes.byref(fg_pid))
                        if fg_pid.value and fg_pid.value != os.getpid():
                            if self._is_window_above(self.hwnd, fg_now):
                                self._place_above_desktop_below_apps()
                                if self._is_window_above(self.hwnd, fg_now):
                                    user32.SetWindowPos(self.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                                    user32.SetWindowPos(self.hwnd, fg_now, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                except Exception:
                    pass

        # 3. Low-RAM friendly idle trim: once per hide, after 12s fully idle (no menu/flyout/preview/drag)
        if (not self.is_revealed and not menu_open and not flyout_open and not preview_open
                and not is_ole_dragging and not btn_down_now and not getattr(self, "_idle_trimmed", True)
                and (time.time() - getattr(self, "_tucked_at", time.time())) >= 12.0):
            self._idle_trimmed = True
            self._idle_memory_trim()

        poll_ms = 25 if (self.is_revealed or at_edge or cursor_over_dock or cursor_over_flyout or cursor_over_preview or btn_down_now or is_ole_dragging) else 45
        self.edge_check_job = self.root.after(poll_ms, self._check_edge_sensor)

    # -------------------------------------------------------------
    # Actions: Drag & Drop (Reorder / Merge into Group), Launching, Context Menus
    # -------------------------------------------------------------
    def _close_html_panels(self, force=False, reason="dock_click"):
        try:
            import settings_server
            settings_server.close_settings_ui(force=force, reason=reason)
        except Exception:
            pass

    def _on_button_press(self, event):
        self._close_html_panels()
        self._cancel_hover_preview_timer()
        self._close_active_preview()
        self.press_x = event.x
        self.press_y = event.y
        self.press_time = time.monotonic()
        self.pressed_idx = self._get_item_index_at(event.x, event.y)
        self.is_dragging_item = False
        self.drag_target_idx = None
        self.drag_merge_mode = False

    def _on_middle_click(self, event):
        self._close_html_panels()
        self._cancel_hover_preview_timer()
        self._close_active_preview()
        idx = self._get_item_index_at(event.x, event.y)
        if idx is not None and 0 <= idx < len(self.items):
            item = self.items[idx]
            if item.get("type") == "app":
                self._launch_item(item)
            elif item.get("type") == "group":
                self._open_group_flyout(item, self.item_rects[idx])
            elif item.get("type") == "action":
                self._execute_system_action(item.get("action_type"))
            elif item.get("type") == "running_win":
                self._activate_window(item.get("hwnd"))

    def _on_button_drag(self, event):
        if self.config_mgr.get("general", "locked", False):
            return
        if self.pressed_idx is None or self.pressed_idx >= len(self.items):
            return

        dx = event.x - self.press_x
        dy = event.y - self.press_y
        if not self.is_dragging_item and (abs(dx) > 7 or abs(dy) > 7):
            pressed_it = self.items[self.pressed_idx]
            if pressed_it.get("type") != "separator" and not pressed_it.get("is_dynamic"):
                self.is_dragging_item = True

        if self.is_dragging_item:
            self.drag_cur_x = event.x
            self.drag_cur_y = event.y
            target_idx = None
            merge_mode = False

            n_pin = len(self.pinned_items)
            for i, rect in enumerate(self.item_rects[:n_pin]):
                center_x = (rect[0] + rect[2]) / 2.0
                item_w = rect[2] - rect[0]
                
                # If hovering directly over another pinned app or group (excluding itself)
                if abs(event.x - center_x) < (item_w * 0.42) and i != self.pressed_idx:
                    if i < n_pin and self.pinned_items[i].get("type") in ("app", "group"):
                        target_idx = i
                        merge_mode = True
                        break
                elif event.x < center_x:
                    target_idx = i
                    merge_mode = False
                    break

            if target_idx is None:
                target_idx = n_pin
                merge_mode = False

            self.drag_target_idx = target_idx
            self.drag_merge_mode = merge_mode
            self._render_composite_dock()

    def _on_button_release(self, event):
        if self.is_dragging_item:
            src_idx = self.pressed_idx
            tgt_idx = self.drag_target_idx
            is_merge = self.drag_merge_mode

            self.is_dragging_item = False
            self.pressed_idx = None
            self.drag_target_idx = None
            self.drag_merge_mode = False

            if is_merge and tgt_idx is not None and src_idx is not None and src_idx != tgt_idx:
                self.config_mgr.merge_into_group(src_idx, tgt_idx)
                self.reload_items()
                self.update_geometry()
            elif tgt_idx is not None and src_idx is not None:
                to_idx = tgt_idx
                if to_idx > src_idx:
                    to_idx -= 1
                if to_idx != src_idx:
                    self.config_mgr.move_item(src_idx, to_idx)
                    self.reload_items()
                    self.update_geometry()
            else:
                self._render_composite_dock()
        else:
            # Simple short click without dragging (long holds >= 0.42s or drag-off do NOT launch!)
            held = time.monotonic() - getattr(self, "press_time", 0.0)
            idx = self.pressed_idx
            self.pressed_idx = None
            rel_idx = self._get_item_index_at(event.x, event.y)
            if held >= 0.42 or rel_idx != idx:
                return
            if idx is not None and 0 <= idx < len(self.items):
                item = self.items[idx]
                it_type = item.get("type")
                if it_type == "app":
                    shift_held = bool(getattr(event, "state", 0) & 0x0001)
                    if shift_held:
                        self._launch_item(item)
                    else:
                        self._refresh_open_windows_now()
                        wins, _ = self.get_matching_windows(item)
                        if not wins:
                            self._launch_item(item)
                        elif len(wins) == 1:
                            fg = user32.GetForegroundWindow()
                            if wins[0]["hwnd"] == fg and not wins[0]["iconic"]:
                                self._open_window_preview(item, self.item_rects[idx], wins=wins)
                            else:
                                self._activate_window(wins[0]["hwnd"])
                        else:
                            self._open_window_preview(item, self.item_rects[idx], wins=wins)
                elif it_type == "group":
                    self._open_group_flyout(item, self.item_rects[idx])
                elif it_type == "action":
                    self._execute_system_action(item.get("action_type"), anchor_rect=self.item_rects[idx])
                elif it_type == "battery":
                    self._trigger_launch_effect(item.get("id"))
                    os.startfile("ms-settings:batterysaver")
                elif it_type == "clock":
                    self._trigger_launch_effect(item.get("id"))
                    os.startfile("ms-settings:dateandtime")
                elif it_type == "running_win":
                    self._activate_window(item.get("hwnd"))

    def _open_group_flyout(self, group_item, item_rect):
        try:
            self._cancel_hover_preview_timer()
            self._close_active_preview()
            if self.active_flyout and self.active_flyout.window and self.active_flyout.window.winfo_exists():
                try:
                    self.active_flyout.close()
                except Exception:
                    pass
                self.active_flyout = None

            from group_flyout import GroupFlyout
            def on_launch(sub_item):
                self._trigger_launch_effect(group_item.get("id"))
                self._launch_item(sub_item)
            
            def on_modified(action=None, group_id=None):
                if action == "ungroup" and group_id:
                    self.config_mgr.ungroup(group_id)
                else:
                    self.config_mgr.save()
                self.reload_items()
                self.update_geometry()
                self._render_composite_dock()

            def on_close():
                self.active_flyout = None
                self.flyout_leave_time = 0

            def on_pick_child_icon(group_id, child_id):
                if self.on_open_settings:
                    try:
                        self.on_open_settings(pick_icon_id=group_id, child_id=child_id)
                    except TypeError:
                        self.on_open_settings()

            def get_child_wins(sub_item):
                self._refresh_open_windows_now()
                return self.get_matching_windows(sub_item)

            pos = self.config_mgr.get("general", "position", "bottom")
            self.active_flyout = GroupFlyout(
                self.root,
                group_item,
                item_rect,
                dock_pos=pos,
                on_launch=on_launch,
                on_group_modified=on_modified,
                on_close=on_close,
                on_pick_child_icon=on_pick_child_icon,
                get_item_windows_fn=get_child_wins,
                on_activate_window=lambda hw: self._activate_window(hw),
                on_close_windows=lambda sub: self.close_item_windows(sub),
                on_kill_process=lambda sub: self.terminate_item_processes(sub),
                is_locked_fn=lambda: bool(self.config_mgr.get("general", "locked", False)),
                config_mgr=self.config_mgr,
            )
        except Exception as e:
            print(f"[Dock] Error opening group flyout: {e}")

    def _execute_system_action(self, action_type, anchor_rect=None):
        try:
            if action_type == "app_launcher":
                mode = self.config_mgr.get("general", "launcher_mode", "custom")
                if mode == "windows":
                    from app_launcher import open_windows_start_menu
                    open_windows_start_menu()
                else:
                    from app_launcher import CustomAppLauncher
                    if self.active_launcher and self.active_launcher.window and self.active_launcher.window.winfo_exists():
                        try:
                            self.active_launcher.close()
                        except Exception:
                            pass
                        self.active_launcher = None
                    else:
                        pos = self.config_mgr.get("general", "position", "bottom")
                        def _on_launch_done():
                            self.active_launcher = None
                        ax, ay = None, None
                        if anchor_rect:
                            dx = getattr(self, "dock_x", 0)
                            dy = getattr(self, "dock_y", 0)
                            ax = int(dx + (anchor_rect[0] + anchor_rect[2]) // 2)
                            pb = getattr(self, "pill_bounds", (0, 0, getattr(self, "dock_w", 600), getattr(self, "dock_h", 80)))
                            if pos == "bottom":
                                ay = int(dy + pb[1])
                            elif pos == "top":
                                ay = int(dy + pb[3])
                            elif pos == "left":
                                ax = int(dx + pb[2])
                                ay = int(dy + (anchor_rect[1] + anchor_rect[3]) // 2)
                            else:
                                ax = int(dx + pb[0])
                                ay = int(dy + (anchor_rect[1] + anchor_rect[3]) // 2)
                        self.active_launcher = CustomAppLauncher(
                            self.root,
                            dock_pos=pos,
                            anchor_x=ax,
                            anchor_y=ay,
                            on_close=_on_launch_done,
                            on_launch_feedback=lambda: None,
                            on_add_to_dock=self._add_app_from_launcher,
                        )
                return
            if action_type == "show_desktop":
                try:
                    import win32com.client
                    win32com.client.Dispatch("Shell.Application").ToggleDesktop()
                except Exception:
                    subprocess.Popen(["powershell", "-WindowStyle", "Hidden", "-Command", "(New-Object -ComObject Shell.Application).ToggleDesktop()"])
                # Re-assert Dockbar visibility on the Desktop right after Windows finishes Show Desktop
                self.root.after(90, self._ensure_visible_on_desktop)
                self.root.after(260, self._ensure_visible_on_desktop)
                self.root.after(520, self._ensure_visible_on_desktop)
            elif action_type == "lock_pc":
                ctypes.windll.user32.LockWorkStation()
            elif action_type == "taskmgr":
                subprocess.Popen(["taskmgr.exe"])
            elif action_type == "recycle_bin":
                subprocess.Popen(["explorer.exe", "::{645FF040-5081-101B-9F08-00AA002F954E}"])
            elif action_type == "snipping_tool":
                try:
                    os.startfile("ms-screenclip:")
                except Exception:
                    subprocess.Popen(["snippingtool.exe"])
            elif action_type == "settings":
                os.startfile("ms-settings:")
            elif action_type == "display_settings":
                os.startfile("ms-settings:display")
            elif action_type == "sound_settings":
                os.startfile("ms-settings:sound")
            elif action_type == "apps_features":
                os.startfile("ms-settings:appsfeatures")
            elif action_type == "control_panel":
                subprocess.Popen(["control.exe"])
            elif action_type == "network_settings":
                os.startfile("ms-settings:network")
            elif action_type == "bluetooth_settings":
                os.startfile("ms-settings:bluetooth")
            elif action_type == "shutdown_pc":
                subprocess.Popen(["shutdown.exe", "/s", "/t", "0"], creationflags=0x08000000)
            elif action_type == "restart_pc":
                subprocess.Popen(["shutdown.exe", "/r", "/t", "0"], creationflags=0x08000000)
            elif action_type == "logoff_pc":
                subprocess.Popen(["shutdown.exe", "/l"], creationflags=0x08000000)
            elif action_type == "dock_settings":
                self._open_settings()
            elif action_type == "dock_exit":
                self.close()
        except Exception as e:
            print(f"[Dock] Error executing action {action_type}: {e}")

    def _ungroup_item(self, item):
        self.config_mgr.ungroup(item.get("id"))
        self.reload_items()
        self.update_geometry()

    def _on_right_click(self, event):
        self._close_html_panels()
        self._cancel_hover_preview_timer()
        self._close_active_preview()
        self._refresh_open_windows_now()
        idx = self._get_item_index_at(event.x, event.y)
        menu = CustomMenu(self.root)

        if idx is not None and 0 <= idx < len(self.items):
            item = self.items[idx]
            if item.get("type") == "app":
                wins, can_kill = self.get_matching_windows(item)
                if wins:
                    if len(wins) == 1:
                        menu.add_command("Mostrar ventana abierta", lambda hw=wins[0]["hwnd"]: self._activate_window(hw))
                    else:
                        menu.add_command(f"Elegir ventana abierta ({len(wins)})...", lambda it=item, r=self.item_rects[idx]: self._open_window_preview(it, r))
                    menu.add_command(f"Abrir nueva ventana de {item.get('name')}", lambda: self._launch_item(item))
                    close_lbl = "Cerrar ventana" if len(wins) == 1 else f"Cerrar todas las ventanas ({len(wins)})"
                    menu.add_command(close_lbl, lambda it=item: self.close_item_windows(it), danger=not can_kill)
                    if can_kill:
                        menu.add_command("Finalizar tarea", lambda it=item: self.terminate_item_processes(it), danger=True)
                else:
                    menu.add_command(f"Abrir {item.get('name')}", lambda: self._launch_item(item))

                sec_item = menu.add_section("Editar y gestionar", expanded=False)
                sec_item.add_command("Cambiar icono...", lambda: self._pick_custom_icon(item))
                sec_item.add_command("Restaurar icono original", lambda it=item: self._restore_original_icon(it))
                sec_item.add_command("Cambiar nombre...", lambda: self._rename_item(item))
                sec_item.add_separator()
                sec_item.add_command("Ejecutar como administrador", lambda: self._launch_as_admin(item))
                sec_item.add_command("Abrir ubicación del archivo", lambda: self._open_file_location(item))
                sec_item.add_command("Insertar separador aquí", lambda: self._insert_separator(idx))
                sec_item.add_separator()
                sec_item.add_command("Quitar del Dock", lambda: self._remove_item(item), danger=True)
                menu.add_separator()
            elif item.get("type") == "group":
                wins, can_kill = self.get_matching_windows(item)
                menu.add_command(f"Abrir carpeta {item.get('name')}", lambda: self._open_group_flyout(item, self.item_rects[idx]))
                if wins:
                    menu.add_command(f"Cerrar ventanas abiertas ({len(wins)})", lambda it=item: self.close_item_windows(it), danger=not can_kill)
                    if can_kill:
                        menu.add_command("Finalizar tareas de la carpeta", lambda it=item: self.terminate_item_processes(it), danger=True)

                sec_grp = menu.add_section("Opciones de carpeta", expanded=False)
                sec_grp.add_command("Cambiar icono de carpeta...", lambda: self._pick_custom_icon(item))
                if item.get("icon_path"):
                    sec_grp.add_command("Restaurar icono original de carpeta", lambda it=item: self._restore_original_icon(it))
                sec_grp.add_command("Renombrar carpeta...", lambda: self._rename_item(item))
                sec_grp.add_command("Desagrupar elementos", lambda: self._ungroup_item(item))
                sec_grp.add_command("Insertar separador aquí", lambda: self._insert_separator(idx))
                sec_grp.add_separator()
                sec_grp.add_command("Quitar carpeta del Dock", lambda: self._remove_item(item), danger=True)
                menu.add_separator()
            elif item.get("type") == "action":
                menu.add_command(f"Ejecutar {item.get('name')}", lambda: self._execute_system_action(item.get("action_type")))

                sec_act = menu.add_section("Editar y gestionar", expanded=False)
                sec_act.add_command("Cambiar icono...", lambda: self._pick_custom_icon(item))
                if item.get("icon_path"):
                    sec_act.add_command("Restaurar icono original", lambda it=item: self._restore_original_icon(it))
                sec_act.add_command("Cambiar nombre...", lambda: self._rename_item(item))
                sec_act.add_command("Insertar separador aquí", lambda: self._insert_separator(idx))
                sec_act.add_separator()
                sec_act.add_command("Quitar del Dock", lambda: self._remove_item(item), danger=True)
                menu.add_separator()
            elif item.get("type") == "running_win":
                wins, can_kill = self.get_matching_windows(item)
                menu.add_header("Programa Abierto")
                menu.add_command(f"Mostrar {item.get('name')}", lambda hw=item.get("hwnd"): self._activate_window(hw))
                menu.add_command("Anclar permanentemente a la Dockbar", lambda it=item: self._pin_running_app(it))
                menu.add_command("Cerrar ventana", lambda hw=item.get("hwnd"): self._close_running_window(hw), danger=not can_kill)
                if can_kill:
                    menu.add_command("Finalizar tarea", lambda it=item: self.terminate_item_processes(it), danger=True)
                menu.add_separator()
            elif item.get("type") == "battery":
                menu.add_header("Widget de Batería en Dockbar")
                cur_bs = (self.config_mgr.get("appearance", "dock_battery_style", "ring") or "ring").lower()
                sec_bs = menu.add_section("Estilo de Batería en Dock", expanded=True)
                for bst_id, bst_lbl in [("ring", "Anillo Circular"), ("bar", "Barra Clásica"), ("pill", "Píldora"), ("minimal", "Minimalista")]:
                    sec_bs.add_command(bst_lbl, lambda s=bst_id: self._set_dock_battery_style(s), checked=(cur_bs == bst_id))
                menu.add_command("Configuración de Energía Windows...", lambda: os.startfile("ms-settings:batterysaver"))
                menu.add_command("Insertar separador aquí", lambda: self._insert_separator(idx))
                menu.add_command("Quitar batería del Dock", lambda: self._remove_item(item), danger=True)
                menu.add_separator()
            elif item.get("type") == "clock":
                menu.add_header("Widget de Reloj en Dockbar")
                cur_cs = (self.config_mgr.get("appearance", "dock_clock_style", "digital") or "digital").lower()
                sec_cs = menu.add_section("Estilo de Reloj en Dock", expanded=True)
                for cst_id, cst_lbl in [("digital", "Digital Cristal"), ("analog", "Analógico"), ("neon", "Neón"), ("minimal", "Minimalista"), ("pill", "Píldora Azul")]:
                    sec_cs.add_command(cst_lbl, lambda s=cst_id: self._set_dock_clock_opt("dock_clock_style", s), checked=(cur_cs == cst_id))
                menu.add_command("Formato 24 horas", lambda: self._set_dock_clock_opt("dock_clock_24h", not bool(self.config_mgr.get("appearance", "dock_clock_24h", True))), checked=bool(self.config_mgr.get("appearance", "dock_clock_24h", True)))
                menu.add_command("Mostrar segundero", lambda: self._set_dock_clock_opt("dock_clock_seconds", not bool(self.config_mgr.get("appearance", "dock_clock_seconds", False))), checked=bool(self.config_mgr.get("appearance", "dock_clock_seconds", False)))
                menu.add_command("Mostrar fecha", lambda: self._set_dock_clock_opt("dock_clock_show_date", not bool(self.config_mgr.get("appearance", "dock_clock_show_date", True))), checked=bool(self.config_mgr.get("appearance", "dock_clock_show_date", True)))
                menu.add_command("Fecha y Hora de Windows...", lambda: os.startfile("ms-settings:dateandtime"))
                menu.add_command("Insertar separador aquí", lambda: self._insert_separator(idx))
                menu.add_command("Quitar reloj del Dock", lambda: self._remove_item(item), danger=True)
                menu.add_separator()
            elif item.get("type") == "separator" and not item.get("is_dynamic"):
                menu.add_command("Quitar separador", lambda: self._remove_item(item), danger=True)
                menu.add_separator()

        # General Dock options (collapsible sections to keep menu compact)
        clicked_empty = not (idx is not None and 0 <= idx < len(self.items))
        sec_add = menu.add_section("Añadir a la Dockbar", expanded=clicked_empty)
        sec_add.add_command("Añadir programa o archivo...", self._add_item_manually)
        sec_add.add_command("Widget: Reloj y Fecha en Dockbar", self._add_clock_dock_item)
        sec_add.add_command("Widget: Porcentaje de Batería", self._add_battery_dock_item)
        sec_add.add_command("Botón: Lanzador de Apps (Inicio)", lambda: self._add_system_action("app_launcher", "Aplicaciones"))
        sec_add.add_command("Insertar separador al final", lambda: self._insert_separator(len(self.pinned_items)))
        sec_add.add_separator()
        sec_add.add_command("Botón: Ajustes del Dock", lambda: self._add_system_action("dock_settings", "Ajustes Dock"))
        sec_add.add_command("Botón: Detener Dockbar", lambda: self._add_system_action("dock_exit", "Detener Dock"))

        sec_win = menu.add_section("Acciones de Windows", expanded=False)
        sec_win.add_command("Mostrar Escritorio", lambda: self._add_system_action("show_desktop", "Mostrar Escritorio"))
        sec_win.add_command("Configuración de Pantalla", lambda: self._add_system_action("display_settings", "Pantalla"))
        sec_win.add_command("Panel de Control", lambda: self._add_system_action("control_panel", "Panel de Control"))
        sec_win.add_command("Desinstalar Aplicaciones", lambda: self._add_system_action("apps_features", "Desinstalar Apps"))
        sec_win.add_command("Sonido y Volumen", lambda: self._add_system_action("sound_settings", "Sonido"))
        sec_win.add_command("Red e Internet", lambda: self._add_system_action("network_settings", "Red"))
        sec_win.add_command("Papelera de Reciclaje", lambda: self._add_system_action("recycle_bin", "Papelera"))
        sec_win.add_command("Recortes", lambda: self._add_system_action("snipping_tool", "Recortes"))
        sec_win.add_command("Bloquear Equipo", lambda: self._add_system_action("lock_pc", "Bloquear Equipo"))
        sec_win.add_separator()
        sec_win.add_command("Cerrar Sesión", lambda: self._add_system_action("logoff_pc", "Cerrar Sesión"))
        sec_win.add_command("Reiniciar Equipo", lambda: self._add_system_action("restart_pc", "Reiniciar"))
        sec_win.add_command("Apagar Equipo", lambda: self._add_system_action("shutdown_pc", "Apagar"), danger=True)

        menu.add_separator()
        menu.add_command("Configuración de Alioth Dockbar...", self._open_settings)

        is_locked = self.config_mgr.get("general", "locked", False)
        menu.add_command("Bloquear iconos del Dock", self._toggle_lock, checked=is_locked)

        menu.add_separator()
        menu.add_command("Salir de Alioth Dockbar", self.close, danger=True)

        menu.show(event.x_root, event.y_root)

    def _get_item_index_at(self, x, y):
        for i, rect in enumerate(self.item_rects):
            if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                return i
        return None

    def _launch_item(self, item):
        target = item.get("target", "")
        args = item.get("args", "")
        if not target:
            return

        it_id = item.get("id")
        if it_id:
            self._trigger_launch_effect(it_id)

        try:
            if target.startswith("http://") or target.startswith("https://") or target.startswith("shell:"):
                os.startfile(target)
            elif target.lower() in ("explorer.exe", "explorer") and args and args.strip().lower().startswith("shell:"):
                subprocess.Popen(["explorer.exe", args.strip()])
            elif os.path.exists(target) or target.lower().endswith(".exe"):
                if args:
                    subprocess.Popen(f'"{target}" {args}', shell=True)
                else:
                    os.startfile(target)
            else:
                os.startfile(target)
        except Exception as e:
            messagebox.showerror("Error al abrir", f"No se pudo iniciar '{target}':\n{e}", parent=self.root)

    def _launch_as_admin(self, item):
        target = item.get("target", "")
        args = item.get("args", "")
        try:
            shell32.ShellExecuteW(None, "runas", target, args, None, 1)
        except Exception as e:
            messagebox.showerror("Error de elevación", f"No se pudo ejecutar como administrador:\n{e}")

    def _open_file_location(self, item):
        target = item.get("target", "")
        if target and os.path.exists(target):
            subprocess.Popen(f'explorer.exe /select,"{os.path.abspath(target)}"')

    def _add_system_action(self, action_type, name):
        new_act = {
            "type": "action",
            "name": name,
            "action_type": action_type
        }
        self.config_mgr.add_item(new_act)
        self.reload_items()
        self.update_geometry()

    def _add_battery_dock_item(self):
        new_bat = {"type": "battery", "name": "Batería"}
        self.config_mgr.add_item(new_bat)
        self.reload_items()
        self.update_geometry()

    def _set_dock_battery_style(self, st_id):
        self.config_mgr.set("appearance", "dock_battery_style", st_id)
        self._bat_icon_cache = None
        self._dirty = True

    def _add_clock_dock_item(self):
        new_clk = {"type": "clock", "name": "Reloj"}
        self.config_mgr.add_item(new_clk)
        self.reload_items()
        self.update_geometry()

    def _set_dock_clock_opt(self, key, val):
        self.config_mgr.set("appearance", key, val)
        self._clock_icon_cache = None
        self._dirty = True

    def _restore_original_icon(self, item):
        restored = icon_extractor.restore_original_icon(item)
        item["icon_path"] = restored
        if item.get("id"):
            self.config_mgr.update_item(item["id"], {"icon_path": restored})
        self.reload_items()
        self._render_composite_dock()

    def _pick_custom_icon(self, item):
        # Opens the visual Collections Picker modal (with Explorer fallback button inside!)
        item_id = item.get("id")
        if self.on_open_settings and item_id:
            try:
                self.on_open_settings(pick_icon_id=item_id)
                return
            except TypeError:
                pass
        # Fallback if no HTML settings callback available
        base_dir = os.path.dirname(os.path.abspath(__file__))
        initial_dir = os.path.join(base_dir, "assets", "icon_packs")
        if item.get("type") == "group":
            group_icons_dir = os.path.join(initial_dir, "Carpetas de Colores")
            if os.path.isdir(group_icons_dir):
                initial_dir = group_icons_dir
        path = filedialog.askopenfilename(
            initialdir=initial_dir,
            title="Seleccionar Icono o Imagen (PNG, ICO, DLL, EXE)",
            filetypes=[
                ("Todos los formatos de icono e imagen", "*.png;*.ico;*.dll;*.exe;*.jpg;*.jpeg;*.bmp"),
                ("Imágenes PNG", "*.png"),
                ("Iconos de Windows ICO", "*.ico"),
                ("Librerías y Ejecutables DLL/EXE", "*.dll;*.exe"),
                ("Todos los archivos", "*.*")
            ]
        )
        if path:
            cached_path = icon_extractor.import_custom_icon(path)
            if cached_path:
                item["icon_path"] = cached_path
                self.config_mgr.update_item(item["id"], {"icon_path": cached_path})
                self.reload_items()
                self._render_composite_dock()
            else:
                messagebox.showwarning("Formato no compatible", "No se pudo extraer el icono de este archivo.")

    def _rename_item(self, item):
        new_name = simpledialog.askstring("Cambiar Nombre", "Introduce la nueva etiqueta para este icono:", initialvalue=item.get("name", ""))
        if new_name is not None and new_name.strip():
            item["name"] = new_name.strip()
            self.config_mgr.update_item(item["id"], {"name": new_name.strip()})
            self._render_composite_dock()

    def _insert_separator(self, index):
        new_sep = {"type": "separator"}
        self.config_mgr.add_item(new_sep, index)
        self.reload_items()
        self.update_geometry()

    def _remove_item(self, item):
        self.config_mgr.remove_item(item.get("id"))
        self.reload_items()
        self.update_geometry()

    def _add_item_manually(self):
        path = filedialog.askopenfilename(
            title="Seleccionar Programa o Archivo",
            filetypes=[("Todos los archivos", "*.*")]
        )
        if path:
            self._handle_dropped_files([path], 9999)

    def _toggle_lock(self):
        locked = not self.config_mgr.get("general", "locked", False)
        self.config_mgr.set("general", "locked", locked)

    def _open_settings(self):
        if self.on_open_settings:
            self.on_open_settings()

    def close(self):
        self.user_wants_exit = True
        self._cancel_hover_group_timer()
        self._cancel_hover_preview_timer()
        self._close_active_preview()
        if getattr(self, "media_capsule", None):
            try:
                self.media_capsule.hide()
            except Exception:
                pass
        if getattr(self, "active_launcher", None):
            try:
                self.active_launcher.close()
            except Exception:
                pass
            self.active_launcher = None
        if self.active_flyout:
            try:
                self.active_flyout.close()
            except Exception:
                pass
            self.active_flyout = None
        self._close_html_panels(force=True, reason="dock_exit")
        if self.edge_check_job:
            try:
                self.root.after_cancel(self.edge_check_job)
            except Exception:
                pass
            self.edge_check_job = None
        if self.anim_job:
            try:
                self.root.after_cancel(self.anim_job)
            except Exception:
                pass
            self.anim_job = None
        try:
            if self._subclass_proc_ref:
                comctl32.RemoveWindowSubclass(self.hwnd, self._subclass_proc_ref, 101)
                try:
                    comctl32.RemoveWindowSubclass(self.canvas.winfo_id(), self._subclass_proc_ref, 102)
                except Exception:
                    pass
                self._subclass_proc_ref = None
        except Exception:
            pass
        try:
            if HAS_PYWIN32_OLE and hasattr(self, "_ole_drop_target"):
                try:
                    pythoncom.RevokeDragDrop(self.canvas.winfo_id())
                except Exception:
                    pass
                try:
                    pythoncom.RevokeDragDrop(self.hwnd)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
