import os
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
            premul_bgra = pil_rgba.convert("RGBa").tobytes("raw", "BGRa")
            ctypes.memmove(bits, premul_bgra, w * h * 4)

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
            return bool(ok)
        finally:
            user32.ReleaseDC(0, hdc_screen)
    except Exception as e:
        print(f"[WindowUtils] UpdateLayeredWindow error: {e}")
        return False


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
_shared_font_cache = {}


def resolve_styled_font(family="Segoe UI", size=12, bold=False, italic=False):
    """Returns a cached PIL ImageFont matching family, pixel size, bold, and italic."""
    from PIL import ImageFont
    sz = max(7, int(round(size)))
    fam = (family or "Segoe UI").strip()
    key = (fam, sz, bool(bold), bool(italic))
    cached = _shared_font_cache.get(key)
    if cached is not None:
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

    if len(_shared_font_cache) >= 48:
        _shared_font_cache.clear()
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
                    half_sz = (max(160, sz[0] // 2), max(120, sz[1] // 2))
                    fitted = ImageOps.fit(raw.convert("RGB"), half_sz, method=Image.Resampling.BILINEAR)
                    blurred_wp = fitted.filter(ImageFilter.GaussianBlur(radius=max(6, int(blur_radius * 0.65))))
                _WALLPAPER_CACHE["path"] = wp_path
                _WALLPAPER_CACHE["mtime"] = mt
                _WALLPAPER_CACHE["size"] = sz
                _WALLPAPER_CACHE["img"] = blurred_wp

            wp_half = _WALLPAPER_CACHE["img"]
            if wp_half is not None:
                hx1 = max(0, min(wp_half.width - 2, int(x // 2)))
                hy1 = max(0, min(wp_half.height - 2, int(y // 2)))
                hx2 = max(hx1 + 2, min(wp_half.width, int((x + w + 1) // 2)))
                hy2 = max(hy1 + 2, min(wp_half.height, int((y + h + 1) // 2)))
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


