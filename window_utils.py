import os
from collections import OrderedDict
import ctypes
from ctypes import wintypes
import tkinter as tk

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GA_ROOT = 2
GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]

# Proper 64-bit Win32 ctypes prototypes (prevents OverflowError on handles > 0x7FFFFFFF)
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID
]
user32.CreateWindowExW.restype = wintypes.HWND

user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND

user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG

user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG

user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p

user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.UINT
]
user32.SetWindowPos.restype = wintypes.BOOL

user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL

user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND

user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND

user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC

user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int

user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC,
    ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC

gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP

gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ

gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL

_shared_hidden_owner = None

def get_hidden_owner_hwnd():
    global _shared_hidden_owner
    if _shared_hidden_owner is None:
        try:
            _shared_hidden_owner = user32.CreateWindowExW(
                0, "Static", "AliothHiddenOwner", 0,
                0, 0, 0, 0, None, None, None, None
            )
        except Exception as e:
            print(f"[WindowUtils] Error creating hidden owner: {e}")
            _shared_hidden_owner = 0
    return _shared_hidden_owner

def set_tool_window(tk_win, owner_hwnd=None, layered=False, no_activate=False):
    """
    Guarantees that a Tkinter window NEVER appears in the Windows Taskbar
    or Alt+Tab switcher under Windows 10/11.
    Optionally enables WS_EX_LAYERED for true per-pixel alpha via UpdateLayeredWindow,
    and WS_EX_NOACTIVATE so clicks on floating dock surfaces don't steal foreground focus.
    """
    try:
        tk_win.update_idletasks()
        inner_hwnd = tk_win.winfo_id()
        top_hwnd = user32.GetAncestor(inner_hwnd, GA_ROOT) or inner_hwnd

        target_owner = owner_hwnd if owner_hwnd else get_hidden_owner_hwnd()
        if target_owner and top_hwnd != target_owner:
            user32.SetWindowLongPtrW(top_hwnd, GWLP_HWNDPARENT, target_owner)

        style = user32.GetWindowLongW(top_hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        if layered:
            style |= WS_EX_LAYERED
        if no_activate:
            style |= WS_EX_NOACTIVATE
        user32.SetWindowLongW(top_hwnd, GWL_EXSTYLE, style)
        user32.SetWindowPos(
            top_hwnd, 0, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE
        )
        return top_hwnd
    except Exception as e:
        print(f"[WindowUtils] Error setting tool window: {e}")
        return None

_surface_cache = {}  # top_hwnd -> (w, h, hdc_mem, hbm, old_bm, bits_ptr)

def release_layered_surface(top_hwnd):
    """Frees cached GDI DIBSection / MemDC for a destroyed layered window."""
    entry = _surface_cache.pop(top_hwnd, None)
    if entry:
        _, _, hdc_mem, hbm, old_bm, _ = entry
        try:
            gdi32.SelectObject(hdc_mem, old_bm)
            gdi32.DeleteObject(hbm)
            gdi32.DeleteDC(hdc_mem)
        except Exception:
            pass

def update_layered_pil(top_hwnd, x, y, pil_rgba):
    """
    Renders a PIL RGBA Image directly to a WS_EX_LAYERED HWND with true 32-bit
    per-pixel alpha compositing (premultiplied BGRA) using Win32 UpdateLayeredWindow.
    Caches DIBSection + MemDC per (hwnd, w, h) and uses uint16 premultiplication for 60+ FPS.
    """
    if not top_hwnd or pil_rgba is None:
        return False
    try:
        if pil_rgba.mode != "RGBA":
            pil_rgba = pil_rgba.convert("RGBA")
        w, h = pil_rgba.size
        if w <= 0 or h <= 0:
            return False

        # Ensure WS_EX_LAYERED is set (only check once per cached surface)
        entry = _surface_cache.get(top_hwnd)
        hdc_screen = user32.GetDC(0)
        if not hdc_screen:
            return False

        try:
            if entry is None or entry[0] != w or entry[1] != h:
                if entry is not None:
                    release_layered_surface(top_hwnd)
                style = user32.GetWindowLongW(top_hwnd, GWL_EXSTYLE)
                if not (style & WS_EX_LAYERED):
                    user32.SetWindowLongW(top_hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
                hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
                bmi = BITMAPINFOHEADER(
                    ctypes.sizeof(BITMAPINFOHEADER),
                    w, -h, 1, 32, 0, 0, 0, 0, 0, 0
                )
                bits = ctypes.c_void_p()
                hbm = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
                if not hbm:
                    gdi32.DeleteDC(hdc_mem)
                    return False
                old_bm = gdi32.SelectObject(hdc_mem, hbm)
                entry = (w, h, hdc_mem, hbm, old_bm, bits)
                _surface_cache[top_hwnd] = entry
            else:
                _, _, hdc_mem, _, _, bits = entry

            # Fast native C premultiplication & RGBA->BGRa swizzle directly in Pillow (zero NumPy overhead)
            if w * h > 262144:
                for top in range(0, h, 64):
                    block = pil_rgba.crop((0, top, w, min(h, top + 64)))
                    data = block.convert('RGBa').tobytes('raw', 'BGRa')
                    ctypes.memmove(bits.value + top * w * 4, data, len(data))
            else:
                data = pil_rgba.convert('RGBa').tobytes('raw', 'BGRa')
                ctypes.memmove(bits, data, w * h * 4)

            pt_dst = wintypes.POINT(int(x), int(y))
            sz = wintypes.SIZE(int(w), int(h))
            pt_src = wintypes.POINT(0, 0)
            bf = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

            ok = user32.UpdateLayeredWindow(
                top_hwnd, hdc_screen,
                ctypes.byref(pt_dst), ctypes.byref(sz),
                hdc_mem, ctypes.byref(pt_src),
                0, ctypes.byref(bf), ULW_ALPHA
            )
            if not ok:
                # Auto-recover from ERROR_INVALID_PARAMETER (87) caused when Tkinter or
                # SW_HIDE switches a layered window out of UpdateLayeredWindow mode.
                cur_ex = user32.GetWindowLongW(top_hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(top_hwnd, GWL_EXSTYLE, cur_ex & ~WS_EX_LAYERED)
                user32.SetWindowLongW(top_hwnd, GWL_EXSTYLE, cur_ex | WS_EX_LAYERED)
                ok = user32.UpdateLayeredWindow(
                    top_hwnd, hdc_screen,
                    ctypes.byref(pt_dst), ctypes.byref(sz),
                    hdc_mem, ctypes.byref(pt_src),
                    0, ctypes.byref(bf), ULW_ALPHA
                )
            return bool(ok)
        finally:
            user32.ReleaseDC(0, hdc_screen)
    except Exception as e:
        print(f"[WindowUtils] UpdateLayeredWindow error: {e}")
        return False


def cancel_window_animation(window):
    job = getattr(window, '_motion_job', None)
    if job:
        try:
            window.after_cancel(job)
        except tk.TclError:
            pass
    window._motion_job = None


def animate_window(window, showing=True, on_done=None, layered_hwnd=None):
    """Short, cancellable transition; reuses the existing layered surface."""
    import time
    cancel_window_animation(window)
    if not window.winfo_exists():
        return
    started = time.monotonic()
    x, y = window.winfo_x(), window.winfo_y()
    enabled = wintypes.BOOL(True)
    user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)

    def step():
        window._motion_job = None
        if not window.winfo_exists():
            return
        progress = min(1.0, (time.monotonic() - started) / .13) if enabled.value else 1.0
        eased = 1 - (1 - progress) ** 3
        opacity = eased if showing else 1 - eased
        entry = _surface_cache.get(layered_hwnd)
        if entry:
            w, h, dc, *_ = entry
            point = wintypes.POINT(x, y + int((1 - opacity) * 6))
            size = wintypes.SIZE(w, h)
            source = wintypes.POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, int(opacity * 255), AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(layered_hwnd, None, ctypes.byref(point), ctypes.byref(size), dc, ctypes.byref(source), 0, ctypes.byref(blend), ULW_ALPHA)
        elif not layered_hwnd:
            window.attributes('-alpha', opacity)
        if progress < 1:
            window._motion_job = window.after(25, step)
        elif on_done:
            on_done()
    step()


def enable_native_motion(window):
    window.update_idletasks()
    hwnd = user32.GetAncestor(window.winfo_id(), GA_ROOT) or window.winfo_id()
    disabled = wintypes.BOOL(False)
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 3, ctypes.byref(disabled), ctypes.sizeof(disabled))
    except OSError:
        pass
    animate_window(window)


def flatten_pil_for_colorkey(pil_rgba, key_rgb=(1, 2, 3), alpha_threshold=50):
    """Pure-Pillow colorkey flatten fallback when UpdateLayeredWindow is unavailable."""
    from PIL import Image
    if pil_rgba.mode != "RGBA":
        pil_rgba = pil_rgba.convert("RGBA")
    bg = Image.new("RGB", pil_rgba.size, key_rgb)
    mask = pil_rgba.getchannel("A").point(lambda a: 255 if a >= alpha_threshold else 0)
    out = Image.composite(pil_rgba.convert("RGB"), bg, mask)
    return out.convert("RGBA")


def alpha_rounded_rect(base_img, box, radius=8, fill=None, outline=None, width=1):
    """True Porter-Duff alpha-composited rounded rectangle on an RGBA Pillow image (never punches alpha holes)."""
    from PIL import Image, ImageDraw
    x1, y1, x2, y2 = [int(round(v)) for v in box]
    pad = max(2, int(width) + 1)
    bx1 = max(0, x1 - pad)
    by1 = max(0, y1 - pad)
    bx2 = min(base_img.width, x2 + pad + 1)
    by2 = min(base_img.height, y2 + pad + 1)
    pw, ph = bx2 - bx1, by2 - by1
    if pw <= 0 or ph <= 0:
        return
    patch = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    pd = ImageDraw.Draw(patch)
    pd.rounded_rectangle(
        [x1 - bx1, y1 - by1, x2 - bx1, y2 - by1],
        radius=radius,
        fill=fill,
        outline=outline,
        width=width
    )
    base_img.alpha_composite(patch, (bx1, by1))


def alpha_ellipse(base_img, box, fill=None, outline=None, width=1):
    """True Porter-Duff alpha-composited ellipse on an RGBA Pillow image."""
    from PIL import Image, ImageDraw
    x1, y1, x2, y2 = [int(round(v)) for v in box]
    pad = max(2, int(width) + 1)
    bx1 = max(0, x1 - pad)
    by1 = max(0, y1 - pad)
    bx2 = min(base_img.width, x2 + pad + 1)
    by2 = min(base_img.height, y2 + pad + 1)
    pw, ph = bx2 - bx1, by2 - by1
    if pw <= 0 or ph <= 0:
        return
    patch = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    pd = ImageDraw.Draw(patch)
    pd.ellipse([x1 - bx1, y1 - by1, x2 - bx1, y2 - by1], fill=fill, outline=outline, width=width)
    base_img.alpha_composite(patch, (bx1, by1))


def alpha_line(base_img, pts, fill, width=1):
    """True Porter-Duff alpha-composited line on an RGBA Pillow image."""
    from PIL import Image, ImageDraw
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    pad = max(2, int(width) + 2)
    bx1 = max(0, int(min(xs)) - pad)
    by1 = max(0, int(min(ys)) - pad)
    bx2 = min(base_img.width, int(max(xs)) + pad + 1)
    by2 = min(base_img.height, int(max(ys)) + pad + 1)
    pw, ph = bx2 - bx1, by2 - by1
    if pw <= 0 or ph <= 0:
        return
    patch = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    pd = ImageDraw.Draw(patch)
    shifted = [(p[0] - bx1, p[1] - by1) for p in pts]
    pd.line(shifted, fill=fill, width=width)
    base_img.alpha_composite(patch, (bx1, by1))


# Shared bounded cache for styled TTF fonts (Regular / Bold / Italic / Bold-Italic)
_FONT_VARIANTS = {
    "Segoe UI": ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    "Calibri": ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    "Arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "Consolas": ("consola.ttf", "consolab.ttf", "consolai.ttf", "consolaz.ttf"),
    "Georgia": ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    "Trebuchet MS": ("trebuc.ttf", "trebucbd.ttf", "trebuci.ttf", "trebucbi.ttf"),
    "Verdana": ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf"),
    "Tahoma": ("tahoma.ttf", "tahomabd.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
}
_shared_font_cache = OrderedDict()


def resolve_styled_font(family="Segoe UI", size=12, bold=False, italic=False):
    """Returns a cached PIL ImageFont matching family, pixel size, bold, and italic."""
    from PIL import ImageFont
    sz = max(7, int(round(size)))
    fam = (family or "Segoe UI").strip()
    key = (fam, sz, bool(bold), bool(italic))
    cached = _shared_font_cache.get(key)
    if cached is not None:
        _shared_font_cache.move_to_end(key)
        return cached

    variants = _FONT_VARIANTS.get(fam) or _FONT_VARIANTS["Segoe UI"]
    idx = (3 if (bold and italic) else (1 if bold else (2 if italic else 0)))
    candidates = [variants[idx], variants[1 if bold else 0], "segoeuib.ttf" if bold else "segoeui.ttf"]

    font_obj = None
    for ttf in candidates:
        try:
            font_obj = ImageFont.truetype(ttf, sz)
            break
        except Exception:
            continue
    if font_obj is None:
        font_obj = ImageFont.load_default()

    while len(_shared_font_cache) >= 32:
        _shared_font_cache.popitem(last=False)
    _shared_font_cache[key] = font_obj
    return font_obj


_IGNORE_APP_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow", "TaskListThumbnailWnd", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "Xaml_WindowedPopupClass",
    "EdgeUiInputTopWndClass", "MultitaskingViewFrame", "ForegroundStaging",
}


def find_lowest_app_above_desktop(exclude_hwnds=()):
    """
    Walks Win32 Z-order from TOP to BOTTOM (GetTopWindow -> GW_HWNDNEXT) to determine whether
    any visible normal application window is currently in front of the Windows Desktop shell
    (Progman / WorkerW).

    Why top-to-bottom:
    When the user triggers 'Mostrar escritorio' (Taskbar corner, Win+D, or ToggleDesktop()),
    Windows 11 raises a full-screen WorkerW / Progman sheet ABOVE non-minimized normal windows
    (so IsIconic remains False on those windows!). Scanning top-to-bottom hits that raised
    WorkerW/Progman sheet FIRST and stops immediately, returning (0, desk_hwnd) so both the
    Dockbar and Desktop Widgets stay visible on the desktop instead of hiding behind WorkerW!
    When normal app windows sit in front of the desktop, returns (lowest_visible_app_hwnd, desk_hwnd).
    """
    excl = {int(h) for h in (exclude_hwnds or ()) if h}
    if _shared_hidden_owner:
        excl.add(int(_shared_hidden_owner))

    dwmapi = ctypes.windll.dwmapi
    sw = max(800, user32.GetSystemMetrics(0))
    sh = max(600, user32.GetSystemMetrics(1))

    lowest_app = 0
    desk_cover = 0
    cur = user32.GetTopWindow(0)
    steps = 0
    cbuf = ctypes.create_unicode_buffer(64)
    rect = wintypes.RECT()

    while cur and steps < 700:
        steps += 1
        cur_i = int(cur)
        nxt = user32.GetWindow(cur, 2)  # GW_HWNDNEXT (walk downwards)

        if cur_i in excl or not user32.IsWindowVisible(cur) or user32.IsIconic(cur):
            cur = nxt
            continue

        user32.GetClassNameW(cur, cbuf, 64)
        cls = cbuf.value

        # 1. Check if we reached the visible Desktop cover (Progman or raised Show-Desktop WorkerW)
        if cls in ("Progman", "WorkerW"):
            cloaked = ctypes.c_int(0)
            try:
                dwmapi.DwmGetWindowAttribute(cur, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            except Exception:
                cloaked.value = 0
            if not cloaked.value:
                has_defview = bool(user32.FindWindowExW(cur, 0, "SHELLDLL_DefView", None))
                is_fullscreen = False
                if not has_defview and user32.GetWindowRect(cur, ctypes.byref(rect)):
                    is_fullscreen = ((rect.right - rect.left) >= int(sw * 0.75) and (rect.bottom - rect.top) >= int(sh * 0.75))
                if has_defview or is_fullscreen:
                    desk_cover = cur_i
                    break
            cur = nxt
            continue

        # 2. Check if cur is a normal visible non-topmost application window above the desktop
        ex = user32.GetWindowLongW(cur, GWL_EXSTYLE)
        if (ex & WS_EX_TOOLWINDOW) or (ex & 0x00000008):
            cur = nxt
            continue
        if cls in _IGNORE_APP_CLASSES:
            cur = nxt
            continue
        if user32.GetWindowTextLengthW(cur) <= 0:
            cur = nxt
            continue
        cloaked = ctypes.c_int(0)
        try:
            dwmapi.DwmGetWindowAttribute(cur, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if cloaked.value:
                cur = nxt
                continue
        except Exception:
            pass
        if not user32.GetWindowRect(cur, ctypes.byref(rect)):
            cur = nxt
            continue
        if (rect.right - rect.left) <= 40 or (rect.bottom - rect.top) <= 40:
            cur = nxt
            continue

        lowest_app = cur_i
        cur = nxt

    return lowest_app, desk_cover


def is_hwnd_behind(hwnd, ref_hwnd):
    """Returns True if hwnd sits strictly BELOW ref_hwnd in Win32 Z-order (e.g. behind raised WorkerW)."""
    if not hwnd or not ref_hwnd or int(hwnd) == int(ref_hwnd):
        return False
    cur = user32.GetWindow(int(ref_hwnd), 2)  # GW_HWNDNEXT (walking downward from ref_hwnd)
    steps = 0
    while cur and steps < 600:
        if int(cur) == int(hwnd):
            return True
        cur = user32.GetWindow(cur, 2)
        steps += 1
    return False


_WALLPAPER_CACHE = {"path": "", "mtime": 0.0, "size": (0, 0), "img": None}


def get_blurred_desktop_backdrop(x, y, w, h, screen_w, screen_h, blur_radius=16):
    """
    Returns an RGBA crop of the user's real Windows wallpaper at screen rect (x, y, w, h)
    with heavy Gaussian blur for genuine optical frosted-glass graph tracks (zero flicker, tiny RAM).
    """
    from PIL import Image, ImageOps, ImageFilter
    try:
        buf = ctypes.create_unicode_buffer(520)
        ok = user32.SystemParametersInfoW(0x0073, 520, buf, 0)  # SPI_GETDESKWALLPAPER
        wp_path = buf.value.strip() if ok else ""
        if wp_path and os.path.exists(wp_path):
            mt = os.path.getmtime(wp_path)
            sz = (max(320, int(screen_w)), max(240, int(screen_h)))
            if (
                _WALLPAPER_CACHE["img"] is None
                or _WALLPAPER_CACHE["path"] != wp_path
                or _WALLPAPER_CACHE["mtime"] != mt
                or _WALLPAPER_CACHE["size"] != sz
            ):
                with Image.open(wp_path) as raw:
                    # Downscale 1/2 for super-fast heavy optical blur + minimal RAM (<2 MB)
                    scale = min(.5, 960 / sz[0], 540 / sz[1])
                    half_sz = (max(1, int(sz[0] * scale)), max(1, int(sz[1] * scale)))
                    raw.draft("RGB", half_sz)
                    fitted = ImageOps.fit(raw, half_sz, method=Image.Resampling.BILINEAR).convert("RGB")
                    radius = max(1, max(6, int(blur_radius * .65)) * scale / .5)
                    blurred_wp = fitted.filter(ImageFilter.GaussianBlur(radius=radius))
                _WALLPAPER_CACHE["path"] = wp_path
                _WALLPAPER_CACHE["mtime"] = mt
                _WALLPAPER_CACHE["size"] = sz
                _WALLPAPER_CACHE["img"] = blurred_wp

            wp_half = _WALLPAPER_CACHE["img"]
            if wp_half is not None:
                sx, sy = wp_half.width / sz[0], wp_half.height / sz[1]
                hx1 = max(0, min(wp_half.width - 2, int(x * sx)))
                hy1 = max(0, min(wp_half.height - 2, int(y * sy)))
                hx2 = max(hx1 + 2, min(wp_half.width, int((x + w + 1) * sx)))
                hy2 = max(hy1 + 2, min(wp_half.height, int((y + h + 1) * sy)))
                crop = wp_half.crop((hx1, hy1, hx2, hy2)).resize((max(1, int(w)), max(1, int(h))), Image.Resampling.BILINEAR)
                return crop.convert("RGBA")
    except Exception:
        pass
    return None


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def get_battery_status():
    """Returns live Windows battery percentage, AC charging state, and availability."""
    s = _SYSTEM_POWER_STATUS()
    try:
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            pct = int(s.BatteryLifePercent)
            if pct < 0:
                pct += 256
            has_battery = (pct != 255) and not (s.BatteryFlag & 128)
            if pct > 100:
                pct = 100
            charging = (s.ACLineStatus == 1) or bool(s.BatteryFlag & 8)
            life_sec = int(s.BatteryLifeTime)
            if life_sec == 0xFFFFFFFF or life_sec < 0:
                life_sec = -1
            return {
                "has_battery": has_battery,
                "percent": pct if has_battery else 100,
                "charging": bool(charging),
                "saver": bool(s.SystemStatusFlag & 1),
                "life_minutes": (life_sec // 60) if life_sec > 0 else None,
            }
    except Exception:
        pass
    return {"has_battery": False, "percent": 100, "charging": True, "saver": False, "life_minutes": None}


def resolve_shortcut_target(path):
    """Resolves a .lnk file target via native IShellLinkW COM (never WScript.Shell), or returns cleaned path."""
    raw = os.path.expandvars((path or "").strip().strip('"')).strip()
    if not raw:
        return ""
    if raw.lower().endswith(".lnk") and os.path.exists(raw):
        try:
            import pythoncom
            from win32com.shell import shell
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass
            sl = pythoncom.CoCreateInstance(
                shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink
            )
            pf = sl.QueryInterface(pythoncom.IID_IPersistFile)
            pf.Load(os.path.abspath(raw))
            tgt, _ = sl.GetPath(0)
            cleaned_tgt = os.path.expandvars((tgt or "").strip().strip('"')).strip()
            if cleaned_tgt and os.path.exists(cleaned_tgt):
                return cleaned_tgt
        except Exception:
            pass
    return raw


def open_file_location(path):
    """Opens Windows Explorer selecting the resolved file or folder via SHOpenFolderAndSelectItems."""
    import subprocess
    raw = os.path.expandvars((path or "").strip().strip('"')).strip()
    if not raw:
        return False
    resolved = resolve_shortcut_target(raw)
    target = resolved if (resolved and os.path.exists(resolved)) else (raw if os.path.exists(raw) else "")
    if not target:
        return False
    abs_p = os.path.abspath(target)
    try:
        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
        ole32.CoInitialize(None)
        try:
            shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
            shell32.ILCreateFromPathW.restype = ctypes.c_void_p
            shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
            shell32.SHOpenFolderAndSelectItems.restype = ctypes.HRESULT
            shell32.ILFree.argtypes = [ctypes.c_void_p]
            pidl = shell32.ILCreateFromPathW(abs_p)
            if pidl:
                try:
                    hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
                    if hr == 0:
                        return True
                finally:
                    shell32.ILFree(pidl)
        finally:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass
    except Exception:
        pass
    try:
        subprocess.Popen(["explorer.exe", f"/select,{abs_p}"])
        return True
    except Exception:
        return False


def copy_files_to_clipboard(paths):
    """Copies one or more existing file/folder paths onto the Windows clipboard as CF_HDROP for pasting."""
    import struct
    if isinstance(paths, (str, os.PathLike)):
        paths = [str(paths)]
    valid = []
    for p in paths or []:
        rp = resolve_shortcut_target(str(p))
        use_p = rp if (rp and os.path.exists(rp)) else os.path.expandvars(str(p or "").strip().strip('"')).strip()
        if use_p and os.path.exists(use_p):
            valid.append(os.path.abspath(use_p))
    if not valid:
        return False
    payload = struct.pack("<IiiII", 20, 0, 0, 0, 1) + ("\0".join(valid) + "\0\0").encode("utf-16-le")
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, payload)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


def copy_text_to_clipboard(text):
    """Copies Unicode text cleanly onto the Windows clipboard."""
    if text is None:
        return False
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, str(text))
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False


def share_path_via_email(path, display_name=""):
    """Shares a URL or local file via Outlook COM attachment (when available) or mailto: URI."""
    import urllib.parse
    raw = os.path.expandvars((path or "").strip().strip('"')).strip()
    if not raw:
        return False
    low = raw.lower()
    if low.startswith(("http://", "https://")):
        subj = urllib.parse.quote(display_name or "Enlace compartido")
        body = urllib.parse.quote(raw)
        try:
            os.startfile(f"mailto:?subject={subj}&body={body}")
            return True
        except Exception:
            return False

    resolved = resolve_shortcut_target(raw)
    target = resolved if (resolved and os.path.exists(resolved)) else (raw if os.path.exists(raw) else raw)
    if target and os.path.isfile(target):
        try:
            import pythoncom
            import win32com.client
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass
            outlook = win32com.client.Dispatch("Outlook.Application")
            mail = outlook.CreateItem(0)
            mail.Subject = display_name or os.path.basename(target)
            mail.Attachments.Add(os.path.abspath(target))
            mail.Display(False)
            return True
        except Exception:
            pass
    try:
        subj = urllib.parse.quote(display_name or os.path.basename(target) or "Archivo compartido")
        body = urllib.parse.quote(os.path.abspath(target) if target else raw)
        os.startfile(f"mailto:?subject={subj}&body={body}")
        return True
    except Exception:
        return False


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def show_file_properties(path):
    """Opens the native Windows Properties dialog for the given file/folder/shortcut."""
    raw = os.path.expandvars((path or "").strip().strip('"')).strip()
    if not raw:
        return False
    resolved = resolve_shortcut_target(raw)
    target = resolved if (resolved and os.path.exists(resolved)) else (raw if os.path.exists(raw) else "")
    if not target:
        return False
    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
        shell32.ShellExecuteExW.restype = wintypes.BOOL
        sei = _SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
        sei.fMask = 0x0000000C  # SEE_MASK_INVOKEIDLIST
        sei.lpVerb = "properties"
        sei.lpFile = os.path.abspath(target)
        sei.nShow = 5
        return bool(shell32.ShellExecuteExW(ctypes.byref(sei)))
    except Exception:
        return False


def create_desktop_shortcut(target, name="", args=""):
    """
    Creates a native Windows .lnk shortcut on the user's Desktop using COM IShellLink
    (zero WScript.Shell, zero PowerShell). Returns the created .lnk path or "".
    """
    raw = os.path.expandvars((target or "").strip().strip('"')).strip()
    if not raw:
        return ""
    try:
        import re
        import pythoncom
        from win32com.shell import shell, shellcon

        pythoncom.CoInitialize()
        desktop_dir = shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0)
        if not desktop_dir or not os.path.isdir(desktop_dir):
            return ""

        base = (name or "").strip()
        if not base:
            base = os.path.splitext(os.path.basename(raw.rstrip("\\/")))[0] or "Acceso directo"
        safe_base = re.sub(r'[<>:"/\\|?*]+', "_", base).strip(" .") or "Acceso directo"

        lnk_path = os.path.join(desktop_dir, f"{safe_base}.lnk")
        idx = 2
        while os.path.exists(lnk_path):
            lnk_path = os.path.join(desktop_dir, f"{safe_base} ({idx}).lnk")
            idx += 1

        link = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink,
            None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IShellLink,
        )
        link.SetPath(raw)
        if args:
            link.SetArguments(str(args))
        work_dir = os.path.dirname(raw) if os.path.isfile(raw) else (raw if os.path.isdir(raw) else "")
        if work_dir and os.path.isdir(work_dir):
            link.SetWorkingDirectory(work_dir)
        link.SetDescription(base)

        pf = link.QueryInterface(pythoncom.IID_IPersistFile)
        pf.Save(lnk_path, 0)
        return lnk_path
    except Exception as e:
        print(f"[WindowUtils] create_desktop_shortcut error: {e}")
        return ""


def ask_styled_text(title, prompt, initialvalue="", parent=None, ok_text="Guardar", cancel_text="Cancelar"):
    """
    Modern monochrome dark-glass modal text prompt matching Alioth Dockbar's
    Launchpad / Spotlight / CustomMenu aesthetic (replaces legacy Tk simpledialog.askstring).
    Returns entered string or None if cancelled.
    """
    from PIL import Image, ImageDraw, ImageTk

    result = [None]
    created_root = False
    master = parent
    if master is None:
        master = getattr(tk, "_default_root", None)
    if master is None:
        master = tk.Tk()
        master.withdraw()
        created_root = True

    W, H = 430, 184
    KEY_HEX = "#010203"
    KEY_RGB = (1, 2, 3)
    CARD_RGBA = (29, 30, 34, 255)
    ENTRY_HEX = "#2b2d33"
    ENTRY_RGBA = (43, 45, 51, 255)

    dlg = tk.Toplevel(master)
    dlg.title(title or "")
    dlg.overrideredirect(True)
    dlg.attributes("-topmost", True)
    dlg.attributes("-transparentcolor", KEY_HEX)
    dlg.attributes("-alpha", 0.94)
    dlg.configure(bg=KEY_HEX)

    sw = dlg.winfo_screenwidth()
    sh = dlg.winfo_screenheight()
    px = (sw - W) // 2
    py = int((sh - H) * 0.42)
    dlg.geometry(f"{W}x{H}+{px}+{py}")

    top_hwnd = set_tool_window(dlg, owner_hwnd=None, layered=False, no_activate=False)

    canvas = tk.Canvas(dlg, width=W, height=H, bg=KEY_HEX, highlightthickness=0, bd=0)
    canvas.place(x=0, y=0, width=W, height=H)

    var = tk.StringVar(master=dlg, value=str(initialvalue or ""))
    entry = tk.Entry(
        dlg,
        textvariable=var,
        bg=ENTRY_HEX,
        fg="#f8fafc",
        insertbackground="#ffffff",
        selectbackground="#525866",
        selectforeground="#ffffff",
        bd=0,
        highlightthickness=0,
        relief=tk.FLAT,
        font=("Segoe UI", 12),
    )
    entry.place(x=34, y=82, width=W - 68, height=26)

    state = {"hov": None, "drag": None, "photo": None}
    btn_cancel = (W - 224, H - 50, W - 126, H - 18)
    btn_ok = (W - 116, H - 50, W - 22, H - 18)
    btn_x = (W - 38, 12, W - 12, 38)

    def _render():
        if not dlg.winfo_exists():
            return
        ss = 2
        hi = Image.new("RGBA", (W * ss, H * ss), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hi, "RGBA")
        # Outer dark-glass rounded card
        hd.rounded_rectangle(
            [2, 2, W * ss - 3, H * ss - 3],
            radius=36,
            fill=CARD_RGBA,
            outline=(255, 255, 255, 85),
            width=2,
        )
        # Header divider
        hd.line([(44, 90), ((W - 22) * ss, 90)], fill=(255, 255, 255, 22), width=2)
        # Rounded Search-style input pill with crisp WHITE border
        hd.rounded_rectangle(
            [44, 150, (W - 22) * ss, 232],
            radius=40,
            fill=ENTRY_RGBA,
            outline=(255, 255, 255, 225),
            width=3,
        )
        # Close (X) circular button
        cx2, cy2 = (W - 25) * ss, 25 * ss
        if state["hov"] == "x":
            hd.ellipse([cx2 - 22, cy2 - 22, cx2 + 22, cy2 + 22], fill=(64, 68, 78, 255))
        hd.line([(cx2 - 8, cy2 - 8), (cx2 + 8, cy2 + 8)], fill=(212, 214, 220, 255), width=3)
        hd.line([(cx2 + 8, cy2 - 8), (cx2 - 8, cy2 + 8)], fill=(212, 214, 220, 255), width=3)

        # Cancel pill button
        c1, c2, c3, c4 = [v * ss for v in btn_cancel]
        c_fill = (62, 65, 74, 255) if state["hov"] == "cancel" else (46, 48, 56, 255)
        hd.rounded_rectangle([c1, c2, c3, c4], radius=30, fill=c_fill, outline=(255, 255, 255, 55), width=2)

        # Primary OK pill button (crisp white)
        o1, o2, o3, o4 = [v * ss for v in btn_ok]
        o_fill = (255, 255, 255, 255) if state["hov"] == "ok" else (238, 240, 244, 255)
        hd.rounded_rectangle([o1, o2, o3, o4], radius=30, fill=o_fill)

        im = hi.resize((W, H), Image.Resampling.LANCZOS)
        d = ImageDraw.Draw(im, "RGBA")
        f_title = resolve_styled_font("Segoe UI", 12, bold=True)
        f_prompt = resolve_styled_font("Segoe UI", 10)
        f_btn = resolve_styled_font("Segoe UI", 10, bold=True)

        d.text((22, 25), str(title or ""), font=f_title, fill=(248, 250, 252, 255), anchor="lm")
        d.text((24, 60), str(prompt or ""), font=f_prompt, fill=(161, 165, 174, 255), anchor="lm")
        d.text(((btn_cancel[0] + btn_cancel[2]) // 2, (btn_cancel[1] + btn_cancel[3]) // 2), cancel_text, font=f_btn, fill=(228, 230, 236, 255), anchor="mm")
        d.text(((btn_ok[0] + btn_ok[2]) // 2, (btn_ok[1] + btn_ok[3]) // 2), ok_text, font=f_btn, fill=(18, 19, 23, 255), anchor="mm")

        flat = flatten_pil_for_colorkey(im, key_rgb=KEY_RGB, alpha_threshold=64)
        state["photo"] = ImageTk.PhotoImage(flat, master=dlg)
        canvas.delete("all")
        canvas.create_image(0, 0, image=state["photo"], anchor="nw")

    def _hit(x, y):
        for name, (x1, y1, x2, y2) in (("ok", btn_ok), ("cancel", btn_cancel), ("x", btn_x)):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return name
        return None

    def _confirm(_e=None):
        result[0] = var.get()
        try:
            dlg.destroy()
        except Exception:
            pass

    def _cancel(_e=None):
        result[0] = None
        try:
            dlg.destroy()
        except Exception:
            pass

    def _on_motion(e):
        h = _hit(e.x, e.y)
        if h != state["hov"]:
            state["hov"] = h
            canvas.configure(cursor="hand2" if h else "")
            _render()

    def _on_press(e):
        h = _hit(e.x, e.y)
        if h == "ok":
            _confirm()
        elif h in ("cancel", "x"):
            _cancel()
        elif e.y <= 46:
            state["drag"] = (e.x_root - dlg.winfo_x(), e.y_root - dlg.winfo_y())
        else:
            entry.focus_set()

    def _on_drag(e):
        if state["drag"]:
            dx, dy = state["drag"]
            dlg.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _on_release(_e):
        state["drag"] = None

    canvas.bind("<Motion>", _on_motion)
    canvas.bind("<Button-1>", _on_press)
    canvas.bind("<B1-Motion>", _on_drag)
    canvas.bind("<ButtonRelease-1>", _on_release)
    dlg.bind("<Escape>", _cancel)
    entry.bind("<Escape>", _cancel)
    entry.bind("<Return>", _confirm)

    _render()

    def _focus_now():
        if not dlg.winfo_exists():
            return
        try:
            kernel32 = ctypes.windll.kernel32
            fg = user32.GetForegroundWindow()
            cur_tid = kernel32.GetCurrentThreadId()
            fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            attached = bool(user32.AttachThreadInput(fg_tid, cur_tid, True)) if (fg_tid and fg_tid != cur_tid) else False
            try:
                hwnd = top_hwnd or user32.GetAncestor(dlg.winfo_id(), GA_ROOT) or dlg.winfo_id()
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(fg_tid, cur_tid, False)
        except Exception:
            pass
        try:
            dlg.lift()
            dlg.focus_force()
            entry.focus_set()
            entry.select_range(0, tk.END)
            entry.icursor(tk.END)
        except Exception:
            pass

    dlg.after(20, _focus_now)
    dlg.grab_set()
    dlg.wait_window()
    if created_root:
        try:
            master.destroy()
        except Exception:
            pass
    return result[0]




