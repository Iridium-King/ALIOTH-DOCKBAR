import os
import re
import shutil
import hashlib
import ctypes
from ctypes import wintypes
from PIL import Image, ImageDraw, ImageFont
import win32com.client
from window_utils import alpha_rounded_rect

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32

class ICONINFO(ctypes.Structure):
    _fields_ = [
        ('fIcon', wintypes.BOOL),
        ('xHotspot', wintypes.DWORD),
        ('yHotspot', wintypes.DWORD),
        ('hbmMask', wintypes.HBITMAP),
        ('hbmColor', wintypes.HBITMAP)
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wintypes.DWORD),
        ('biWidth', wintypes.LONG),
        ('biHeight', wintypes.LONG),
        ('biPlanes', wintypes.WORD),
        ('biBitCount', wintypes.WORD),
        ('biCompression', wintypes.DWORD),
        ('biSizeImage', wintypes.DWORD),
        ('biXPelsPerMeter', wintypes.LONG),
        ('biYPelsPerMeter', wintypes.LONG),
        ('biClrUsed', wintypes.DWORD),
        ('biClrImportant', wintypes.DWORD)
    ]

class SHFILEINFO(ctypes.Structure):
    _fields_ = [
        ('hIcon', wintypes.HICON),
        ('iIcon', ctypes.c_int),
        ('dwAttributes', wintypes.DWORD),
        ('szDisplayName', wintypes.WCHAR * 260),
        ('szTypeName', wintypes.WCHAR * 80)
    ]

ole32 = ctypes.windll.ole32

class BITMAP(ctypes.Structure):
    _fields_ = [
        ('bmType', ctypes.c_long),
        ('bmWidth', ctypes.c_long),
        ('bmHeight', ctypes.c_long),
        ('bmWidthBytes', ctypes.c_long),
        ('bmPlanes', wintypes.WORD),
        ('bmBitsPixel', wintypes.WORD),
        ('bmBits', ctypes.c_void_p),
    ]

class SIZE(ctypes.Structure):
    _fields_ = [('cx', ctypes.c_long), ('cy', ctypes.c_long)]

class GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', wintypes.DWORD),
        ('Data2', wintypes.WORD),
        ('Data3', wintypes.WORD),
        ('Data4', ctypes.c_byte * 8),
    ]

IID_IShellItemImageFactory = GUID(
    0xBCC18B79, 0xBA16, 0x442F,
    (ctypes.c_byte * 8)(0x80, 0xC4, 0x8A, 0x59, 0xC3, 0x0C, 0x46, 0x3B)
)

shell32.SHCreateItemFromParsingName.argtypes = [
    wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
]
shell32.SHCreateItemFromParsingName.restype = ctypes.HRESULT
gdi32.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
gdi32.GetObjectW.restype = ctypes.c_int

# Setup proper 64-bit ctypes prototypes
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT
]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
user32.DestroyIcon.argtypes = [wintypes.HICON]

from app_paths import app_dir as _app_dir

CACHE_DIR = os.path.join(_app_dir(), "cache", "icons")
os.makedirs(CACHE_DIR, exist_ok=True)

def _hbitmap_to_pil(hbm):
    """Converts a Win32 HBITMAP of any resolution to a PIL RGBA Image using real bitmap dimensions."""
    if not hbm:
        return None
    try:
        bm = BITMAP()
        if not gdi32.GetObjectW(hbm, ctypes.sizeof(BITMAP), ctypes.byref(bm)):
            return None
        w = int(bm.bmWidth)
        h = int(bm.bmHeight)
        if w <= 0 or h <= 0:
            return None

        hdc = user32.GetDC(0)
        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth = w
        bih.biHeight = -h  # Top-down bitmap
        bih.biPlanes = 1
        bih.biBitCount = 32
        bih.biCompression = 0

        buf = (ctypes.c_byte * (w * h * 4))()
        scanlines = gdi32.GetDIBits(hdc, hbm, 0, h, ctypes.byref(buf), ctypes.byref(bih), 0)
        user32.ReleaseDC(0, hdc)
        if scanlines == 0:
            return None

        img = Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1).copy()
        alpha_extrema = img.getextrema()[3]
        if alpha_extrema[0] == 0 and alpha_extrema[1] == 0:
            img = img.convert('RGB').convert('RGBA')
        return img
    except Exception as e:
        print(f"[IconExtractor] _hbitmap_to_pil error: {e}")
        return None

def hicon_to_pil(hicon, size=128):
    """Converts a Win32 HICON to a PIL RGBA Image without cropping high-DPI bitmaps."""
    if not hicon:
        return None
    try:
        ii = ICONINFO()
        if not user32.GetIconInfo(hicon, ctypes.byref(ii)):
            return None
        img = _hbitmap_to_pil(ii.hbmColor)
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)
        if img and size and img.size != (size, size):
            img = img.resize((size, size), Image.Resampling.LANCZOS)
        return img
    except Exception as e:
        print(f"[IconExtractor] Error converting HICON to PIL: {e}")
        return None

def extract_shell_image_factory(parsing_name, size=128):
    if not parsing_name:
        return None
    initialized = False
    factory = ctypes.c_void_p()
    bitmap = wintypes.HBITMAP()
    release = None
    try:
        try:
            initialized = ole32.CoInitialize(None) in (0, 1)
        except OSError:
            pass
        hr = shell32.SHCreateItemFromParsingName(str(parsing_name), None, ctypes.byref(IID_IShellItemImageFactory), ctypes.byref(factory))
        if hr != 0 or not factory.value:
            return None
        table = ctypes.cast(factory.value, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_image = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, SIZE, ctypes.c_int, ctypes.POINTER(wintypes.HBITMAP))(table[3])
        release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(table[2])
        if get_image(factory.value, SIZE(size, size), 0, ctypes.byref(bitmap)) != 0 or not bitmap.value:
            return None
        image = _hbitmap_to_pil(bitmap)
        return image.resize((size, size), Image.Resampling.LANCZOS) if image and image.size != (size, size) else image
    except Exception:
        return None
    finally:
        if bitmap.value:
            gdi32.DeleteObject(bitmap)
        if release:
            release(factory.value)
        if initialized:
            ole32.CoUninitialize()


def extract_hicon_from_file(file_path, icon_index=0, size=128):
    """Extracts HICON of requested size using PrivateExtractIconsW."""
    hicon = wintypes.HICON()
    pId = wintypes.UINT()
    ret = user32.PrivateExtractIconsW(
        file_path, icon_index, size, size,
        ctypes.byref(hicon), ctypes.byref(pId), 1, 0
    )
    if ret > 0 and hicon.value:
        return hicon
    return None

def extract_shell_icon(path, size=128):
    """Extracts crisp shell icon using IShellItemImageFactory first, SHGetFileInfo fallback."""
    img = extract_shell_image_factory(path, size=size)
    if img:
        return img
    sfi = SHFILEINFO()
    res = shell32.SHGetFileInfoW(path, 0, ctypes.byref(sfi), ctypes.sizeof(sfi), 0x000000100 | 0x000000000)
    if res and sfi.hIcon:
        img = hicon_to_pil(sfi.hIcon, size=size)
        user32.DestroyIcon(sfi.hIcon)
        return img
    return None

def create_fallback_icon(name, size=128):
    """Creates an attractive modern macOS-style rounded icon with the first letter."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Gradient-like background
    h = int(hashlib.md5(name.encode('utf-8')).hexdigest()[:6], 16)
    r = 50 + (h % 150)
    g = 70 + ((h >> 4) % 150)
    b = 90 + ((h >> 8) % 150)

    corner = int(size * 0.22)
    draw.rounded_rectangle([4, 4, size - 4, size - 4], radius=corner, fill=(r, g, b, 240), outline=(255, 255, 255, 70), width=2)

    # Initial letter
    letter = name[0].upper() if name else "?"
    try:
        font = ImageFont.truetype("arial.ttf", int(size * 0.48))
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), letter, font=font)
    w = bbox[2] - bbox[0]
    h_text = bbox[3] - bbox[1]
    draw.text(((size - w) / 2, (size - h_text) / 2 - 4), letter, fill=(255, 255, 255, 255), font=font)

    return img

def resolve_target_and_icon(path):
    """
    Given any dropped path (file, .lnk, folder, .url, or exe name):
    Returns (name, target, args, icon_cache_path)
    """
    if not os.path.exists(path):
        which_path = shutil.which(path)
        if which_path:
            path = which_path

    path = os.path.normpath(path)
    base_name = os.path.splitext(os.path.basename(path))[0]
    target = path
    args = ""
    icon_source = path
    icon_index = 0

    if path.lower().endswith('.lnk'):
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(path)
            if shortcut.TargetPath:
                target = shortcut.TargetPath
            if shortcut.Arguments:
                args = shortcut.Arguments
            if shortcut.IconLocation:
                parts = shortcut.IconLocation.split(',')
                if parts[0].strip():
                    icon_source = parts[0].strip()
                    if len(parts) > 1:
                        try:
                            icon_index = int(parts[1])
                        except Exception:
                            icon_index = 0
            else:
                icon_source = target
            if shortcut.Description:
                # Optionally use shortcut description if suitable
                pass
        except Exception as e:
            print(f"[IconExtractor] Error reading .lnk {path}: {e}")

    elif path.lower().endswith('.url'):
        # Internet shortcut file
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.startswith('URL='):
                        target = line.strip().split('=', 1)[1]
                    elif line.startswith('IconFile='):
                        icon_source = line.strip().split('=', 1)[1]
                    elif line.startswith('IconIndex='):
                        try:
                            icon_index = int(line.strip().split('=', 1)[1])
                        except Exception:
                            pass
        except Exception as e:
            print(f"[IconExtractor] Error reading .url {path}: {e}")

    # Extract icon and cache it (v2 cache avoids legacy 32px cropped icons)
    key = f"v2:{icon_source}:{icon_index}:{base_name}"
    cache_filename = f"v2_{hashlib.md5(key.encode('utf-8')).hexdigest()}.png"
    cached_path = os.path.join(CACHE_DIR, cache_filename)

    if not os.path.exists(cached_path):
        img = None
        if icon_source.startswith("shell:"):
            img = extract_shell_image_factory(icon_source, size=128)
        elif icon_source.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.ico')):
            try:
                img = Image.open(icon_source).convert('RGBA')
                img = img.resize((128, 128), Image.Resampling.LANCZOS)
            except Exception:
                pass

        # Try PrivateExtractIconsW for executables/DLLs
        if img is None and os.path.exists(icon_source) and icon_source.lower().endswith(('.exe', '.dll', '.ico')):
            hicon = extract_hicon_from_file(icon_source, icon_index=icon_index, size=128)
            if hicon:
                img = hicon_to_pil(hicon, size=128)
                user32.DestroyIcon(hicon)

        # High-res IShellItemImageFactory for files (.xlsm, .pdf, .docx), folders & shortcuts
        if img is None and os.path.exists(path):
            img = extract_shell_icon(path, size=128)

        # Fallback to custom letter badge
        if img is None:
            img = create_fallback_icon(base_name, size=128)

        try:
            img.save(cached_path, format="PNG")
        except Exception as e:
            print(f"[IconExtractor] Error saving cached icon: {e}")

    return {
        "name": base_name,
        "target": target,
        "args": args,
        "icon_path": cached_path
    }

def import_custom_icon(source_path, size=128):
    """
    Imports custom icon from PNG, ICO, DLL, EXE, JPG, BMP.
    Returns path to saved PNG in cache/icons/.
    """
    if not os.path.exists(source_path):
        return None

    ext = os.path.splitext(source_path)[1].lower()
    key = f"custom:{source_path}"
    cache_filename = f"custom_{hashlib.md5(key.encode('utf-8')).hexdigest()}.png"
    cached_path = os.path.join(CACHE_DIR, cache_filename)

    img = None
    if ext in ('.png', '.jpg', '.jpeg', '.bmp'):
        try:
            img = Image.open(source_path).convert('RGBA')
            img = img.resize((size, size), Image.Resampling.LANCZOS)
        except Exception as e:
            print(f"[IconExtractor] Error reading image {source_path}: {e}")

    elif ext == '.ico':
        try:
            ico_img = Image.open(source_path)
            best = None
            max_dim = 0
            for i in range(getattr(ico_img, "n_frames", 1)):
                ico_img.seek(i)
                w, h = ico_img.size
                if w * h > max_dim:
                    max_dim = w * h
                    best = ico_img.copy()
            if best:
                img = best.convert('RGBA').resize((size, size), Image.Resampling.LANCZOS)
        except Exception:
            pass

        if img is None:
            hicon = extract_hicon_from_file(source_path, icon_index=0, size=size)
            if hicon:
                img = hicon_to_pil(hicon, size=size)
                user32.DestroyIcon(hicon)

    elif ext in ('.dll', '.exe', '.icl'):
        hicon = extract_hicon_from_file(source_path, icon_index=0, size=size)
        if hicon:
            img = hicon_to_pil(hicon, size=size)
            user32.DestroyIcon(hicon)

    if img:
        try:
            img.save(cached_path, format="PNG")
            return cached_path
        except Exception as e:
            print(f"[IconExtractor] Error saving custom icon: {e}")
    return None

def generate_group_icon(group_item, size=128):
    """
    Creates an iOS/macOS style preview tile for a folder/group containing miniature icons.
    """
    custom_icon = group_item.get("icon_path", "")
    if custom_icon and os.path.exists(custom_icon):
        try:
            img = Image.open(custom_icon).convert("RGBA")
            if img.size != (size, size):
                img = img.resize((size, size), Image.Resampling.LANCZOS)
            return img
        except Exception:
            pass

    # Create 2x supersampled macOS/iOS frosted glass folder squircle tile
    ss = 2
    hi_sz = size * ss
    hi_tile = Image.new("RGBA", (hi_sz, hi_sz), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hi_tile, "RGBA")
    radius = int(hi_sz * 0.24)
    hd.rounded_rectangle(
        [8, 8, hi_sz - 9, hi_sz - 9],
        radius=radius,
        fill=(32, 39, 54, 232),
        outline=(255, 255, 255, 85),
        width=3
    )
    # Soft top inner glass reflection (alpha-blended so top half keeps full glass opacity!)
    alpha_rounded_rect(
        hi_tile,
        [14, 12, hi_sz - 15, hi_sz // 2],
        radius=max(6, radius - 6),
        fill=(255, 255, 255, 18)
    )
    base_tile = hi_tile.resize((size, size), Image.Resampling.LANCZOS)

    sub_items = group_item.get("children", group_item.get("items", []))
    if not sub_items:
        # Draw a crisp empty-folder vector silhouette inside the frosted glass tile
        s = size / 128.0
        ed = ImageDraw.Draw(base_tile, "RGBA")
        # Folder tab + body
        ed.rounded_rectangle(
            [int(26 * s), int(36 * s), int(62 * s), int(56 * s)],
            radius=max(3, int(6 * s)),
            fill=(96, 165, 250, 210),
        )
        ed.rounded_rectangle(
            [int(26 * s), int(46 * s), int(102 * s), int(94 * s)],
            radius=max(4, int(10 * s)),
            fill=(59, 130, 246, 225),
            outline=(191, 219, 254, 200),
            width=max(1, int(2 * s)),
        )
        return base_tile

    # 2x2 grid for up to 4 crisp mini icons
    inner_pad = int(size * 0.14)
    grid_gap = int(size * 0.08)
    sub_size = (size - (inner_pad * 2) - grid_gap) // 2

    positions = [
        (inner_pad, inner_pad),
        (inner_pad + sub_size + grid_gap, inner_pad),
        (inner_pad, inner_pad + sub_size + grid_gap),
        (inner_pad + sub_size + grid_gap, inner_pad + sub_size + grid_gap)
    ]

    from PIL import ImageChops
    for idx, sub_it in enumerate(sub_items[:4]):
        sub_img = get_or_create_icon(sub_it, size=sub_size)
        if sub_img:
            sub_rgba = sub_img.convert("RGBA")
            if sub_rgba.size != (sub_size, sub_size):
                sub_rgba = sub_rgba.resize((sub_size, sub_size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (sub_size, sub_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle([0, 0, sub_size - 1, sub_size - 1], radius=max(4, int(sub_size * 0.22)), fill=255)
            clipped = sub_rgba.copy()
            clipped.putalpha(ImageChops.darker(sub_rgba.getchannel("A"), mask))
            x, y = positions[idx]
            base_tile.alpha_composite(clipped, (x, y))

    return base_tile

def create_action_icon(action_type, size=128):
    """Draws a modern, crisp vector icon for built-in system actions."""
    import math
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    scale = size / 128.0

    pad = int(8 * scale)
    card_rect = [pad, pad, size - pad, size - pad]
    radius = int(24 * scale)

    if action_type == "show_desktop":
        # Deep blue background with desktop monitor
        draw.rounded_rectangle(card_rect, radius=radius, fill=(30, 58, 138, 240), outline=(96, 165, 250, 200), width=max(1, int(2 * scale)))
        mx1, my1 = int(28 * scale), int(30 * scale)
        mx2, my2 = int(100 * scale), int(78 * scale)
        draw.rounded_rectangle([mx1, my1, mx2, my2], radius=int(6 * scale), fill=(15, 23, 42, 255), outline=(147, 197, 253, 255), width=max(1, int(2 * scale)))
        draw.rounded_rectangle([mx1 + int(4*scale), my1 + int(4*scale), mx2 - int(4*scale), my2 - int(4*scale)], radius=int(3 * scale), fill=(56, 189, 248, 180))
        draw.rectangle([int(60 * scale), my2, int(68 * scale), int(90 * scale)], fill=(203, 213, 225, 255))
        draw.rounded_rectangle([int(46 * scale), int(90 * scale), int(82 * scale), int(96 * scale)], radius=int(3 * scale), fill=(226, 232, 240, 255))

    elif action_type == "lock_pc":
        draw.rounded_rectangle(card_rect, radius=radius, fill=(120, 53, 15, 240), outline=(251, 191, 36, 200), width=max(1, int(2 * scale)))
        cx, cy = int(64 * scale), int(52 * scale)
        r = int(18 * scale)
        w_sh = max(2, int(6 * scale))
        draw.arc([cx - r, cy - r, cx + r, cy + r], start=180, end=360, fill=(254, 243, 199, 255), width=w_sh)
        draw.line([(cx - r + w_sh//2, cy), (cx - r + w_sh//2, int(64 * scale))], fill=(254, 243, 199, 255), width=w_sh)
        draw.line([(cx + r - w_sh//2, cy), (cx + r - w_sh//2, int(64 * scale))], fill=(254, 243, 199, 255), width=w_sh)
        draw.rounded_rectangle([int(38 * scale), int(60 * scale), int(90 * scale), int(98 * scale)], radius=int(8 * scale), fill=(245, 158, 11, 255), outline=(254, 243, 199, 255), width=max(1, int(2 * scale)))
        draw.ellipse([int(60 * scale), int(70 * scale), int(68 * scale), int(78 * scale)], fill=(69, 26, 3, 255))
        draw.polygon([(int(62 * scale), int(76 * scale)), (int(66 * scale), int(76 * scale)), (int(67 * scale), int(88 * scale)), (int(61 * scale), int(88 * scale))], fill=(69, 26, 3, 255))

    elif action_type == "taskmgr":
        draw.rounded_rectangle(card_rect, radius=radius, fill=(15, 23, 42, 240), outline=(52, 211, 153, 200), width=max(1, int(2 * scale)))
        for gy in [42, 64, 86]:
            draw.line([(int(24 * scale), int(gy * scale)), (int(104 * scale), int(gy * scale))], fill=(30, 41, 59, 180), width=1)
        pts = [
            (int(24 * scale), int(68 * scale)),
            (int(42 * scale), int(68 * scale)),
            (int(52 * scale), int(36 * scale)),
            (int(62 * scale), int(94 * scale)),
            (int(72 * scale), int(54 * scale)),
            (int(84 * scale), int(74 * scale)),
            (int(94 * scale), int(68 * scale)),
            (int(104 * scale), int(68 * scale))
        ]
        draw.line(pts, fill=(52, 211, 153, 255), width=max(2, int(3.5 * scale)), joint="curve")

    elif action_type == "recycle_bin":
        draw.rounded_rectangle(card_rect, radius=radius, fill=(30, 41, 59, 240), outline=(148, 163, 184, 200), width=max(1, int(2 * scale)))
        draw.polygon([
            (int(42 * scale), int(48 * scale)),
            (int(86 * scale), int(48 * scale)),
            (int(80 * scale), int(98 * scale)),
            (int(48 * scale), int(98 * scale))
        ], fill=(71, 85, 105, 255), outline=(226, 232, 240, 255))
        draw.rounded_rectangle([int(36 * scale), int(40 * scale), int(92 * scale), int(48 * scale)], radius=int(3 * scale), fill=(148, 163, 184, 255))
        draw.rounded_rectangle([int(54 * scale), int(34 * scale), int(74 * scale), int(40 * scale)], radius=int(2 * scale), fill=(203, 213, 225, 255))
        draw.line([(int(54 * scale), int(56 * scale)), (int(56 * scale), int(90 * scale))], fill=(226, 232, 240, 200), width=max(1, int(2 * scale)))
        draw.line([(int(64 * scale), int(56 * scale)), (int(64 * scale), int(90 * scale))], fill=(226, 232, 240, 200), width=max(1, int(2 * scale)))
        draw.line([(int(74 * scale), int(56 * scale)), (int(72 * scale), int(90 * scale))], fill=(226, 232, 240, 200), width=max(1, int(2 * scale)))

    elif action_type == "snipping_tool":
        draw.rounded_rectangle(card_rect, radius=radius, fill=(88, 28, 135, 240), outline=(192, 132, 252, 200), width=max(1, int(2 * scale)))
        cx1, cy1, cx2, cy2 = int(34 * scale), int(34 * scale), int(94 * scale), int(94 * scale)
        draw.rectangle([cx1, cy1, cx2, cy2], outline=(233, 213, 255, 255), width=max(1, int(2 * scale)))
        cl = int(12 * scale)
        for x, y, dx, dy in [(cx1, cy1, 1, 1), (cx2, cy1, -1, 1), (cx1, cy2, 1, -1), (cx2, cy2, -1, -1)]:
            draw.line([(x, y), (x + dx * cl, y)], fill=(255, 255, 255, 255), width=max(2, int(3 * scale)))
            draw.line([(x, y), (x, y + dy * cl)], fill=(255, 255, 255, 255), width=max(2, int(3 * scale)))

    elif action_type in ("settings", "dock_settings"):
        # Rich purple/indigo dock card with mini colorful dock and gear
        draw.rounded_rectangle(card_rect, radius=radius, fill=(79, 70, 229, 240), outline=(165, 180, 252, 200), width=max(1, int(2 * scale)))
        cx, cy = int(64 * scale), int(52 * scale)
        r_out = int(22 * scale)
        r_in = int(10 * scale)
        draw.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], fill=(224, 231, 255, 255))
        for deg in range(0, 360, 45):
            rad = math.radians(deg)
            tx = cx + int((r_out + 4 * scale) * math.cos(rad))
            ty = cy + int((r_out + 4 * scale) * math.sin(rad))
            tw = int(4 * scale)
            draw.rectangle([tx - tw, ty - tw, tx + tw, ty + tw], fill=(224, 231, 255, 255))
        draw.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in], fill=(79, 70, 229, 255))
        # Mini dock pill at the bottom
        dp_w = int(72 * scale)
        dp_h = int(14 * scale)
        dp_x1 = int(64 * scale) - dp_w // 2
        dp_y1 = int(88 * scale)
        draw.rounded_rectangle([dp_x1, dp_y1, dp_x1 + dp_w, dp_y1 + dp_h], radius=int(6*scale), fill=(30, 27, 75, 240), outline=(199, 210, 254, 200), width=1)
        colors = [(239, 68, 68), (59, 130, 246), (16, 185, 129), (245, 158, 11), (168, 85, 247)]
        for i, col in enumerate(colors):
            dot_x = dp_x1 + int((12 + i * 12) * scale)
            dot_y = dp_y1 + int(7 * scale)
            draw.ellipse([dot_x - int(3*scale), dot_y - int(3*scale), dot_x + int(3*scale), dot_y + int(3*scale)], fill=col)

    elif action_type == "dock_exit":
        # Crimson red card with power symbol
        draw.rounded_rectangle(card_rect, radius=radius, fill=(159, 18, 57, 240), outline=(251, 113, 133, 200), width=max(1, int(2 * scale)))
        cx, cy = int(64 * scale), int(64 * scale)
        r = int(24 * scale)
        w_pw = max(2, int(4.5 * scale))
        draw.arc([cx - r, cy - r, cx + r, cy + r], start=130, end=410, fill=(255, 255, 255, 255), width=w_pw)
        draw.line([(cx, cy - r - int(4*scale)), (cx, cy - int(2*scale))], fill=(255, 255, 255, 255), width=w_pw)

    elif action_type == "display_settings":
        # Monitor with brightness/display accent
        draw.rounded_rectangle(card_rect, radius=radius, fill=(30, 58, 138, 240), outline=(96, 165, 250, 200), width=max(1, int(2 * scale)))
        mx1, my1 = int(26 * scale), int(28 * scale)
        mx2, my2 = int(102 * scale), int(78 * scale)
        draw.rounded_rectangle([mx1, my1, mx2, my2], radius=int(6 * scale), fill=(15, 23, 42, 255), outline=(147, 197, 253, 255), width=max(1, int(2 * scale)))
        draw.rounded_rectangle([mx1 + int(4*scale), my1 + int(4*scale), mx2 - int(4*scale), my2 - int(4*scale)], radius=int(3 * scale), fill=(30, 41, 59, 255))
        scx, scy = int(64 * scale), int(53 * scale)
        draw.ellipse([scx - int(8*scale), scy - int(8*scale), scx + int(8*scale), scy + int(8*scale)], fill=(251, 191, 36, 255))
        for deg in range(0, 360, 45):
            rad = math.radians(deg)
            rx1 = scx + int(11 * scale * math.cos(rad))
            ry1 = scy + int(11 * scale * math.sin(rad))
            rx2 = scx + int(15 * scale * math.cos(rad))
            ry2 = scy + int(15 * scale * math.sin(rad))
            draw.line([(rx1, ry1), (rx2, ry2)], fill=(251, 191, 36, 255), width=max(1, int(2 * scale)))
        draw.rectangle([int(60 * scale), my2, int(68 * scale), int(92 * scale)], fill=(203, 213, 225, 255))
        draw.rounded_rectangle([int(46 * scale), int(92 * scale), int(82 * scale), int(98 * scale)], radius=int(3 * scale), fill=(226, 232, 240, 255))

    elif action_type == "sound_settings":
        # Speaker with sound waves
        draw.rounded_rectangle(card_rect, radius=radius, fill=(15, 118, 110, 240), outline=(45, 212, 191, 200), width=max(1, int(2 * scale)))
        pts_spk = [
            (int(32 * scale), int(52 * scale)),
            (int(46 * scale), int(52 * scale)),
            (int(66 * scale), int(36 * scale)),
            (int(66 * scale), int(92 * scale)),
            (int(46 * scale), int(76 * scale)),
            (int(32 * scale), int(76 * scale))
        ]
        draw.polygon(pts_spk, fill=(240, 253, 250, 255), outline=(204, 251, 241, 255))
        draw.arc([int(58 * scale), int(48 * scale), int(80 * scale), int(80 * scale)], start=-60, end=60, fill=(45, 212, 191, 255), width=max(2, int(3 * scale)))
        draw.arc([int(62 * scale), int(38 * scale), int(94 * scale), int(90 * scale)], start=-60, end=60, fill=(94, 234, 212, 255), width=max(2, int(3 * scale)))
        draw.arc([int(66 * scale), int(28 * scale), int(108 * scale), int(100 * scale)], start=-60, end=60, fill=(153, 246, 228, 255), width=max(2, int(3 * scale)))

    elif action_type == "apps_features":
        # Apps / Uninstall card: 3D box with app tiles
        draw.rounded_rectangle(card_rect, radius=radius, fill=(67, 56, 202, 240), outline=(129, 140, 248, 200), width=max(1, int(2 * scale)))
        for row in range(2):
            for col in range(2):
                bx = int((36 + col * 32) * scale)
                by = int((36 + row * 32) * scale)
                bw = int(24 * scale)
                tile_fill = (199, 210, 254, 255) if (row + col) % 2 == 0 else (224, 231, 255, 255)
                draw.rounded_rectangle([bx, by, bx + bw, by + bw], radius=int(5 * scale), fill=tile_fill, outline=(255, 255, 255, 200), width=1)
                draw.ellipse([bx + int(8*scale), by + int(8*scale), bx + int(16*scale), by + int(16*scale)], fill=(99, 102, 241, 255))

    elif action_type == "control_panel":
        # Control Panel: Sliders & Controls
        draw.rounded_rectangle(card_rect, radius=radius, fill=(30, 41, 59, 240), outline=(148, 163, 184, 200), width=max(1, int(2 * scale)))
        slider_ys = [44, 64, 84]
        knob_xs = [50, 78, 62]
        for y, kx in zip(slider_ys, knob_xs):
            sy = int(y * scale)
            draw.line([(int(32 * scale), sy), (int(96 * scale), sy)], fill=(100, 116, 139, 255), width=max(2, int(3 * scale)))
            k_cx = int(kx * scale)
            draw.ellipse([k_cx - int(7*scale), sy - int(7*scale), k_cx + int(7*scale), sy + int(7*scale)], fill=(56, 189, 248, 255), outline=(240, 249, 255, 255), width=max(1, int(2*scale)))

    elif action_type == "network_settings":
        # Globe with latitude/longitude
        draw.rounded_rectangle(card_rect, radius=radius, fill=(29, 78, 216, 240), outline=(96, 165, 250, 200), width=max(1, int(2 * scale)))
        gcx, gcy = int(64 * scale), int(64 * scale)
        gr = int(32 * scale)
        draw.ellipse([gcx - gr, gcy - gr, gcx + gr, gcy + gr], fill=(30, 58, 138, 255), outline=(147, 197, 253, 255), width=max(2, int(2.5 * scale)))
        draw.line([(gcx - gr, gcy), (gcx + gr, gcy)], fill=(147, 197, 253, 255), width=max(1, int(1.5 * scale)))
        draw.line([(gcx, gcy - gr), (gcx, gcy + gr)], fill=(147, 197, 253, 255), width=max(1, int(1.5 * scale)))
        draw.arc([gcx - int(16*scale), gcy - gr, gcx + int(16*scale), gcy + gr], start=-90, end=90, fill=(147, 197, 253, 255), width=max(1, int(1.5 * scale)))
        draw.arc([gcx - int(16*scale), gcy - gr, gcx + int(16*scale), gcy + gr], start=90, end=270, fill=(147, 197, 253, 255), width=max(1, int(1.5 * scale)))

    elif action_type == "bluetooth_settings":
        # Bluetooth rune
        draw.rounded_rectangle(card_rect, radius=radius, fill=(30, 64, 175, 240), outline=(147, 197, 253, 200), width=max(1, int(2 * scale)))
        bx = int(64 * scale)
        draw.line([(bx, int(30 * scale)), (bx, int(98 * scale))], fill=(255, 255, 255, 255), width=max(2, int(3.5 * scale)))
        pts_bt = [
            (int(46 * scale), int(46 * scale)),
            (int(82 * scale), int(76 * scale)),
            (bx, int(98 * scale)),
            (bx, int(30 * scale)),
            (int(82 * scale), int(52 * scale)),
            (int(46 * scale), int(82 * scale))
        ]
        draw.line(pts_bt, fill=(255, 255, 255, 255), width=max(2, int(3 * scale)), joint="miter")

    elif action_type == "shutdown_pc":
        # Deep crimson power-off card
        draw.rounded_rectangle(card_rect, radius=radius, fill=(136, 19, 55, 245), outline=(251, 113, 133, 225), width=max(1, int(2.5 * scale)))
        cx, cy = int(64 * scale), int(66 * scale)
        r = int(25 * scale)
        w_pw = max(2, int(5 * scale))
        draw.arc([cx - r, cy - r, cx + r, cy + r], start=300, end=240, fill=(255, 241, 242, 255), width=w_pw)
        draw.line([(cx, int(30 * scale)), (cx, int(62 * scale))], fill=(255, 255, 255, 255), width=w_pw)

    elif action_type == "restart_pc":
        # Vibrant amber/orange restart circular arrow card
        draw.rounded_rectangle(card_rect, radius=radius, fill=(154, 52, 18, 245), outline=(251, 146, 60, 225), width=max(1, int(2.5 * scale)))
        cx, cy = int(64 * scale), int(64 * scale)
        r = int(25 * scale)
        w_ar = max(2, int(5 * scale))
        draw.arc([cx - r, cy - r, cx + r, cy + r], start=35, end=315, fill=(255, 247, 237, 255), width=w_ar)
        ax, ay = cx + int(r * math.cos(math.radians(315))), cy + int(r * math.sin(math.radians(315)))
        draw.polygon([
            (ax + int(8 * scale), ay - int(2 * scale)),
            (ax - int(8 * scale), ay - int(10 * scale)),
            (ax - int(5 * scale), ay + int(8 * scale)),
        ], fill=(255, 247, 237, 255))

    elif action_type == "logoff_pc":
        # Indigo/violet sign-out door + arrow card
        draw.rounded_rectangle(card_rect, radius=radius, fill=(76, 29, 149, 245), outline=(196, 181, 253, 225), width=max(1, int(2.5 * scale)))
        w_ln = max(2, int(4.5 * scale))
        dx1, dy1, dx2, dy2 = int(32 * scale), int(32 * scale), int(68 * scale), int(96 * scale)
        draw.rounded_rectangle([dx1, dy1, dx2, dy2], radius=int(6 * scale), outline=(237, 233, 254, 255), width=w_ln)
        draw.rectangle([int(56 * scale), int(44 * scale), int(74 * scale), int(84 * scale)], fill=(76, 29, 149, 255))
        my = int(64 * scale)
        draw.line([(int(52 * scale), my), (int(96 * scale), my)], fill=(255, 255, 255, 255), width=w_ln)
        draw.polygon([
            (int(100 * scale), my),
            (int(84 * scale), my - int(12 * scale)),
            (int(84 * scale), my + int(12 * scale)),
        ], fill=(255, 255, 255, 255))

    elif action_type == "app_launcher":
        # macOS Launchpad / App Drawer 3x3 colorful rounded tiles on dark slate
        draw.rounded_rectangle(card_rect, radius=radius, fill=(22, 28, 45, 248), outline=(96, 165, 250, 215), width=max(1, int(2.5 * scale)))
        tile_colors = [
            (239, 68, 68, 255), (59, 130, 246, 255), (16, 185, 129, 255),
            (245, 158, 11, 255), (168, 85, 247, 255), (6, 182, 212, 255),
            (236, 72, 153, 255), (99, 102, 241, 255), (52, 211, 153, 255),
        ]
        tw = max(4, int(18 * scale))
        gap = max(3, int(8 * scale))
        start = int(64 * scale) - (tw * 3 + gap * 2) // 2
        idx_t = 0
        for r in range(3):
            for c in range(3):
                tx1 = start + c * (tw + gap)
                ty1 = start + r * (tw + gap)
                draw.rounded_rectangle([tx1, ty1, tx1 + tw, ty1 + tw], radius=max(2, int(5 * scale)), fill=tile_colors[idx_t])
                idx_t += 1

    else:
        draw.rounded_rectangle(card_rect, radius=radius, fill=(30, 41, 59, 240), outline=(59, 130, 246, 200), width=max(1, int(2 * scale)))
        pts = [
            (int(68 * scale), int(28 * scale)),
            (int(44 * scale), int(68 * scale)),
            (int(62 * scale), int(68 * scale)),
            (int(58 * scale), int(100 * scale)),
            (int(84 * scale), int(56 * scale)),
            (int(68 * scale), int(56 * scale))
        ]
        draw.polygon(pts, fill=(250, 204, 21, 255))

    return img

def find_browser_pwa_ico(target, args):
    """Locates the installed Chrome/Edge PWA .ico file and display stem from --app-id=..."""
    args_str = args or ""
    m_id = re.search(r"--app-id=([^\s\"']+)", args_str, re.IGNORECASE)
    if not m_id:
        return None, ""
    app_id = m_id.group(1).strip()
    if not app_id:
        return None, ""

    prof_dir = "Default"
    m_prof = re.search(r'--profile-directory=(?:"([^"]+)"|([^\s]+))', args_str, re.IGNORECASE)
    if m_prof:
        prof_dir = (m_prof.group(1) or m_prof.group(2) or "Default").strip()

    local_app = os.environ.get("LOCALAPPDATA", "")
    chrome_root = os.path.join(local_app, r"Google\Chrome\User Data")
    edge_root = os.path.join(local_app, r"Microsoft\Edge\User Data")
    tgt_l = (target or "").lower()
    roots = [edge_root, chrome_root] if "edge" in tgt_l else [chrome_root, edge_root]

    for root in roots:
        if not os.path.isdir(root):
            continue
        prof_candidates = [prof_dir]
        try:
            for entry in os.listdir(root):
                if entry not in prof_candidates and os.path.isdir(os.path.join(root, entry, "Web Applications")):
                    prof_candidates.append(entry)
        except Exception:
            pass

        for prof in prof_candidates:
            crx_dir = os.path.join(root, prof, "Web Applications", f"_crx_{app_id}")
            if os.path.isdir(crx_dir):
                try:
                    for fname in os.listdir(crx_dir):
                        if fname.lower().endswith(".ico"):
                            full_ico = os.path.join(crx_dir, fname)
                            return full_ico, os.path.splitext(fname)[0].strip()
                except Exception:
                    pass
    return None, ""


def extract_ico_pil(ico_path, size=128):
    """Extracts the highest-resolution frame from an .ico file as a PIL RGBA image."""
    if not ico_path or not os.path.exists(ico_path):
        return None
    img = None
    try:
        ico_img = Image.open(ico_path)
        best = None
        max_dim = 0
        for i in range(getattr(ico_img, "n_frames", 1)):
            ico_img.seek(i)
            w, h = ico_img.size
            if w * h > max_dim:
                max_dim = w * h
                best = ico_img.copy()
        if best:
            img = best.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    except Exception:
        img = None

    if img is None:
        hicon = extract_hicon_from_file(ico_path, icon_index=0, size=size)
        if hicon:
            img = hicon_to_pil(hicon, size=size)
            user32.DestroyIcon(hicon)
    return img


def restore_original_icon(item, size=128):
    """
    Restores the native original icon path for an item (empty string for groups/actions
    so they revert to their default generated artwork; re-extracted cached PNG for apps).
    """
    if not item:
        return ""
    it_type = item.get("type", "app")
    if it_type in ("group", "action"):
        return ""

    target = (item.get("target") or "").strip()
    args = (item.get("args") or "").strip()
    name = (item.get("name") or "App").strip()
    if not target:
        return ""

    key = f"v2:orig:{target}:{args}"
    cache_filename = f"v2_orig_{hashlib.md5(key.encode('utf-8')).hexdigest()}.png"
    cached_path = os.path.join(CACHE_DIR, cache_filename)

    img = None
    # 1. UWP / Store / Shell:AppsFolder
    if target.lower() == "explorer.exe" and "shell:appsfolder\\" in args.lower():
        img = extract_shell_image_factory(args, size=size)
    elif target.lower().startswith("shell:"):
        img = extract_shell_image_factory(target, size=size)

    # 2. Chrome / Edge PWA (--app-id=...)
    if img is None and ("--app-id=" in args.lower() or os.path.basename(target).lower() in ("chrome_proxy.exe", "msedge_proxy.exe")):
        ico_path, _stem = find_browser_pwa_ico(target, args)
        if ico_path:
            img = extract_ico_pil(ico_path, size=size)

    # 3. Standard executable / DLL / ICO / file / folder / shortcut
    if img is None and target.lower().endswith((".exe", ".dll", ".ico")) and os.path.exists(target):
        hicon = extract_hicon_from_file(target, icon_index=0, size=size)
        if hicon:
            img = hicon_to_pil(hicon, size=size)
            user32.DestroyIcon(hicon)

    if img is None and os.path.exists(target):
        img = extract_shell_icon(target, size=size)

    if img is None:
        try:
            res = resolve_target_and_icon(target)
            rp = res.get("icon_path", "")
            if rp and os.path.exists(rp):
                return rp
        except Exception:
            pass
        img = create_fallback_icon(name, size=size)

    if img is not None:
        try:
            img.save(cached_path, format="PNG")
            return cached_path
        except Exception as e:
            print(f"[IconExtractor] Error saving restored original icon: {e}")
    return ""


def get_or_create_icon(item, size=128):
    """Retrieves a PIL Image for a given dock item dict."""
    it_type = item.get("type", "app")
    if it_type == "group":
        return generate_group_icon(item, size=size)

    icon_path = item.get("icon_path", "")
    if icon_path and os.path.exists(icon_path):
        try:
            with Image.open(icon_path) as raw_im:
                if raw_im.mode in ("P", "1"):
                    raw_im = raw_im.convert("RGBA")
                img = raw_im.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
            return img
        except Exception:
            pass

    if it_type == "action":
        return create_action_icon(item.get("action_type", ""), size=size)

    # Re-extract (also handles shell:AppsFolder\... and Chrome/Edge --app-id= PWAs)
    target = item.get("target", "")
    args = item.get("args", "")
    name = item.get("name", "App")
    if target.lower() == "explorer.exe" and "shell:appsfolder\\" in args.lower():
        im = extract_shell_image_factory(args.strip(), size=size)
        if im:
            return im
    if target and target.startswith("shell:"):
        im = extract_shell_image_factory(target, size=size)
        if im:
            return im
    if "--app-id=" in (args or "").lower():
        ico_path, _stem = find_browser_pwa_ico(target, args)
        if ico_path:
            im = extract_ico_pil(ico_path, size=size)
            if im:
                return im
    if target and (os.path.exists(target) or target.endswith(".exe")):
        res = resolve_target_and_icon(target)
        if res.get("icon_path") and os.path.exists(res["icon_path"]):
            item["icon_path"] = res["icon_path"]
            try:
                with Image.open(res["icon_path"]) as raw_im:
                    if raw_im.mode in ("P", "1"):
                        raw_im = raw_im.convert("RGBA")
                    return raw_im.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
            except Exception:
                pass

    return create_fallback_icon(name, size=size)


def get_window_icon(hwnd, exe_path="", aumid="", name="App", size=128):
    """
    Resolves the highest-fidelity icon for a live open window (including Chrome/Edge PWAs,
    UWP/Store ApplicationFrameHost windows, and custom HWND icons via WM_GETICON/GCLP_HICON).
    """
    aumid_s = (aumid or "").strip()
    aumid_l = aumid_s.lower()

    # 1. Chrome / Edge standalone PWA via _crx_<app_id> in AUMID
    if "_crx_" in aumid_l:
        app_id = aumid_l.split("_crx_", 1)[1].split("!", 1)[0].strip("._ ")
        if app_id:
            ico_path, _ = find_browser_pwa_ico(exe_path or "chrome.exe", f"--app-id={app_id}")
            if ico_path:
                im = extract_ico_pil(ico_path, size=size)
                if im is not None:
                    return im

    # 2. UWP / Store / Shell AUMID via IShellItemImageFactory
    if aumid_s and ("!" in aumid_s or aumid_l.startswith(("microsoft.", "windows.", "com.", "openai."))):
        im = extract_shell_image_factory(f"shell:AppsFolder\\{aumid_s}", size=size)
        if im is not None:
            return im

    # 3. Live Win32 HWND big icon (WM_GETICON ICON_BIG=1, ICON_SMALL2=2, or class HICON)
    if hwnd:
        try:
            u32 = ctypes.windll.user32
            WM_GETICON = 0x007F
            SMTO_ABORTIFHUNG = 0x0002
            res_ptr = ctypes.c_size_t(0)
            for icon_type in (1, 2, 0):
                if u32.SendMessageTimeoutW(wintypes.HWND(hwnd), WM_GETICON, icon_type, 0, SMTO_ABORTIFHUNG, 120, ctypes.byref(res_ptr)):
                    if res_ptr.value:
                        # CopyIcon prevents destroying the window's own HICON inside hicon_to_pil
                        hcopy = u32.CopyIcon(wintypes.HICON(res_ptr.value))
                        if hcopy:
                            im = hicon_to_pil(hcopy, size=size)
                            if im is not None:
                                return im
            get_cls_ptr = getattr(u32, "GetClassLongPtrW", u32.GetClassLongW)
            for gcl_idx in (-14, -34):  # GCLP_HICON, GCLP_HICONSM
                h_cls = get_cls_ptr(wintypes.HWND(hwnd), gcl_idx)
                if h_cls:
                    hcopy = u32.CopyIcon(wintypes.HICON(h_cls))
                    if hcopy:
                        im = hicon_to_pil(hcopy, size=size)
                        if im is not None:
                            return im
        except Exception:
            pass

    # 4. Standard executable / shell icon fallback
    return get_or_create_icon({"type": "app", "name": name, "target": exe_path, "icon_path": ""}, size=size)
