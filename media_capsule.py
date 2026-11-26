"""
Centered floating Media Player Control Capsule displayed neatly over the Dockbar.
Reads live active player metadata & timeline (Spotify, YouTube/Chrome/Edge, Apple Music, VLC, etc.)
via a native pure-ctypes Windows Runtime (WinRT) SMTC client (zero PowerShell subprocesses),
executes zero-latency media + seek controls, provides master audio volume/mute via Core Audio COM,
auto-hides when idle, supports a close button, independent playback button sizing, and optionally
keeps audio active when source applications pause on focus loss.
"""
import ctypes
from ctypes import wintypes
import os
import queue
import threading
import time
import tkinter as tk
import uuid as _uuid
from PIL import Image, ImageDraw, ImageTk

from window_utils import (
    alpha_ellipse,
    alpha_rounded_rect,
    flatten_pil_for_colorkey,
    release_layered_surface,
    resolve_styled_font,
    set_tool_window,
    update_layered_pil,
)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
ole32 = ctypes.windll.ole32

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

HWND_TOPMOST = ctypes.c_void_p(-1).value
HWND_NOTOPMOST = ctypes.c_void_p(-2).value
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, guid_str):
        u = _uuid.UUID(guid_str)
        super().__init__(
            u.time_low,
            u.time_mid,
            u.time_hi_version,
            (ctypes.c_ubyte * 8)(*u.bytes[8:]),
        )


_CLSID_MMDeviceEnumerator = _GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMMDeviceEnumerator = _GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
_IID_IAudioEndpointVolume = _GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
_IID_IAudioSessionManager2 = _GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
_IID_IAudioSessionControl2 = _GUID("{bfb7ff88-7239-4fc9-8fa2-07c950be9c6d}")
_IID_IAudioMeterInformation = _GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")

ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(_GUID),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_GUID),
    ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long


class _SystemVolumeController:
    """Pure-ctypes Core Audio IAudioEndpointVolume controller (zero external dependencies)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._vol_ptr = None
        self._vol_vt = None
        self._com_init = False

    @staticmethod
    def _vtable(ptr):
        return ctypes.cast(
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )

    @staticmethod
    def _release_ptr(ptr):
        if ptr and getattr(ptr, "value", None):
            try:
                vt = _SystemVolumeController._vtable(ptr)
                rel = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])
                rel(ptr)
            except Exception:
                pass

    def _ensure_endpoint(self):
        if self._vol_ptr and self._vol_vt:
            return True
        try:
            if not self._com_init:
                ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
                self._com_init = True
            enum_ptr = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(_CLSID_MMDeviceEnumerator),
                None,
                23,
                ctypes.byref(_IID_IMMDeviceEnumerator),
                ctypes.byref(enum_ptr),
            )
            if hr < 0 or not enum_ptr.value:
                return False
            dev_ptr = ctypes.c_void_p()
            try:
                enum_vt = self._vtable(enum_ptr)
                get_def = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p),
                )(enum_vt[4])
                hr2 = get_def(enum_ptr, 0, 1, ctypes.byref(dev_ptr))  # eRender=0, eMultimedia=1
                if hr2 < 0 or not dev_ptr.value:
                    return False
                vol_ptr = ctypes.c_void_p()
                dev_vt = self._vtable(dev_ptr)
                activate = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.POINTER(_GUID),
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                )(dev_vt[3])
                hr3 = activate(dev_ptr, ctypes.byref(_IID_IAudioEndpointVolume), 23, None, ctypes.byref(vol_ptr))
                if hr3 < 0 or not vol_ptr.value:
                    return False
                self._vol_ptr = vol_ptr
                self._vol_vt = self._vtable(vol_ptr)
                return True
            finally:
                self._release_ptr(dev_ptr)
                self._release_ptr(enum_ptr)
        except Exception:
            self._vol_ptr = None
            self._vol_vt = None
            return False

    def release(self):
        with self._lock:
            self._release_ptr(self._vol_ptr)
            self._vol_ptr = None
            self._vol_vt = None

    def get_state(self):
        """Returns (volume_0_to_1, is_muted)."""
        with self._lock:
            for attempt in range(2):
                if not self._ensure_endpoint():
                    return 1.0, False
                try:
                    get_scalar = ctypes.WINFUNCTYPE(
                        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)
                    )(self._vol_vt[9])
                    get_mute = ctypes.WINFUNCTYPE(
                        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
                    )(self._vol_vt[15])
                    val = ctypes.c_float()
                    muted = ctypes.c_int()
                    hr1 = get_scalar(self._vol_ptr, ctypes.byref(val))
                    hr2 = get_mute(self._vol_ptr, ctypes.byref(muted))
                    if hr1 >= 0 and hr2 >= 0:
                        return max(0.0, min(1.0, float(val.value))), bool(muted.value)
                except Exception:
                    pass
                self._release_ptr(self._vol_ptr)
                self._vol_ptr = None
                self._vol_vt = None
                if attempt == 1:
                    break
            return 1.0, False

    def set_volume(self, level):
        level = max(0.0, min(1.0, float(level)))
        with self._lock:
            for attempt in range(2):
                if not self._ensure_endpoint():
                    return False
                try:
                    set_scalar = ctypes.WINFUNCTYPE(
                        ctypes.c_long, ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p
                    )(self._vol_vt[7])
                    hr = set_scalar(self._vol_ptr, ctypes.c_float(level), None)
                    if hr >= 0:
                        if level > 0.01:
                            set_mute = ctypes.WINFUNCTYPE(
                                ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p
                            )(self._vol_vt[14])
                            set_mute(self._vol_ptr, 0, None)
                        return True
                except Exception:
                    pass
                self._release_ptr(self._vol_ptr)
                self._vol_ptr = None
                self._vol_vt = None
                if attempt == 1:
                    break
            return False

    def step_volume(self, delta):
        cur, _ = self.get_state()
        nxt = max(0.0, min(1.0, cur + float(delta)))
        self.set_volume(nxt)
        return nxt

    def toggle_mute(self):
        _, muted = self.get_state()
        target = 0 if muted else 1
        with self._lock:
            for attempt in range(2):
                if not self._ensure_endpoint():
                    return muted
                try:
                    set_mute = ctypes.WINFUNCTYPE(
                        ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p
                    )(self._vol_vt[14])
                    hr = set_mute(self._vol_ptr, target, None)
                    if hr >= 0:
                        return bool(target)
                except Exception:
                    pass
                self._release_ptr(self._vol_ptr)
                self._vol_ptr = None
                self._vol_vt = None
                if attempt == 1:
                    break
        return muted


_combase = ctypes.windll.combase
_combase.RoInitialize.argtypes = [ctypes.c_int]
_combase.RoInitialize.restype = ctypes.c_long
_combase.RoUninitialize.argtypes = []
_combase.RoUninitialize.restype = None
_combase.WindowsCreateString.argtypes = [
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_void_p),
]
_combase.WindowsCreateString.restype = ctypes.c_long
_combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
_combase.WindowsDeleteString.restype = ctypes.c_long
_combase.WindowsGetStringRawBuffer.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
]
_combase.WindowsGetStringRawBuffer.restype = ctypes.c_wchar_p
_combase.RoGetActivationFactory.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(_GUID),
    ctypes.POINTER(ctypes.c_void_p),
]
_combase.RoGetActivationFactory.restype = ctypes.c_long

_IID_IAsyncInfo = _GUID("{00000036-0000-0000-C000-000000000046}")
_IID_SMTCManagerStatics = _GUID("{2050c4ee-11a0-57de-aed7-c97c70338245}")
_SMTC_CLASS_NAME = "Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager"
_SMTC_STATUS_MAP = {
    0: "Closed",
    1: "Opened",
    2: "Changing",
    3: "Stopped",
    4: "Playing",
    5: "Paused",
}
_FILETIME_EPOCH_OFFSET = 116444736000000000
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106


class _NativeSMTCClient:
    """Pure-ctypes WinRT SMTC client (zero PowerShell subprocesses)."""

    def __init__(self):
        self._mgr = None
        self._ro_inited = False

    @staticmethod
    def _vtable(ptr):
        return ctypes.cast(
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )

    @staticmethod
    def _release(ptr):
        if ptr and getattr(ptr, "value", None):
            try:
                vt = _NativeSMTCClient._vtable(ptr)
                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])(ptr)
            except Exception:
                pass

    @staticmethod
    def _qi(ptr, iid):
        if not ptr or not getattr(ptr, "value", None):
            return None
        out = ctypes.c_void_p()
        fn = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(_NativeSMTCClient._vtable(ptr)[0])
        hr = fn(ptr, ctypes.byref(iid), ctypes.byref(out))
        return out if hr >= 0 and out.value else None

    @staticmethod
    def _hstr_to_py(hs):
        if not hs or not getattr(hs, "value", None):
            return ""
        try:
            length = ctypes.c_uint32(0)
            s = _combase.WindowsGetStringRawBuffer(hs, ctypes.byref(length))
            return s or ""
        finally:
            _combase.WindowsDeleteString(hs)

    @staticmethod
    def _await_op(op_ptr, res_ctype, timeout=1.5, stop_check=None):
        if not op_ptr or not getattr(op_ptr, "value", None):
            return None
        info = _NativeSMTCClient._qi(op_ptr, _IID_IAsyncInfo)
        if not info:
            return None
        try:
            info_vt = _NativeSMTCClient._vtable(info)
            get_status = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
            )(info_vt[7])
            cancel_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(info_vt[9])
            close_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(info_vt[10])
            deadline = time.monotonic() + float(timeout)
            status = ctypes.c_int(0)
            while True:
                hr = get_status(info, ctypes.byref(status))
                if hr < 0:
                    return None
                if status.value != 0:
                    break
                if (stop_check and stop_check()) or time.monotonic() >= deadline:
                    try:
                        cancel_fn(info)
                        close_fn(info)
                    except Exception:
                        pass
                    return None
                time.sleep(0.01)
            if status.value != 1:
                try:
                    close_fn(info)
                except Exception:
                    pass
                return None
            res = res_ctype()
            get_res = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(res_ctype)
            )(_NativeSMTCClient._vtable(op_ptr)[8])
            hr = get_res(op_ptr, ctypes.byref(res))
            try:
                close_fn(info)
            except Exception:
                pass
            return res if hr >= 0 else None
        finally:
            _NativeSMTCClient._release(info)

    def open(self, timeout=2.0, stop_check=None):
        self.close()
        hr = _combase.RoInitialize(1)  # RO_INIT_MULTITHREADED
        if hr in (0, 1):
            self._ro_inited = True
        elif hr == _RPC_E_CHANGED_MODE:
            self._ro_inited = False
        elif hr < 0:
            return False

        hs = ctypes.c_void_p()
        factory = ctypes.c_void_p()
        op = ctypes.c_void_p()
        try:
            hr_s = _combase.WindowsCreateString(
                _SMTC_CLASS_NAME, len(_SMTC_CLASS_NAME), ctypes.byref(hs)
            )
            if hr_s < 0 or not hs.value:
                self.close()
                return False
            hr_f = _combase.RoGetActivationFactory(
                hs, ctypes.byref(_IID_SMTCManagerStatics), ctypes.byref(factory)
            )
            if hr_f < 0 or not factory.value:
                self.close()
                return False
            req_async = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            )(self._vtable(factory)[6])
            hr_op = req_async(factory, ctypes.byref(op))
            if hr_op < 0 or not op.value:
                self.close()
                return False
            mgr = self._await_op(op, ctypes.c_void_p, timeout=timeout, stop_check=stop_check)
            if not mgr or not mgr.value:
                self.close()
                return False
            self._mgr = mgr
            return True
        except Exception:
            self.close()
            return False
        finally:
            self._release(op)
            self._release(factory)
            if hs.value:
                _combase.WindowsDeleteString(hs)

    def close(self):
        if self._mgr is not None:
            self._release(self._mgr)
            self._mgr = None
        if self._ro_inited:
            try:
                _combase.RoUninitialize()
            except Exception:
                pass
            self._ro_inited = False

    def _get_session_source(self, sess):
        src_hs = ctypes.c_void_p()
        try:
            hr = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            )(self._vtable(sess)[6])(sess, ctypes.byref(src_hs))
            return self._hstr_to_py(src_hs) if hr >= 0 else ""
        except Exception:
            return ""

    def _get_session_playback_status(self, sess):
        pb = ctypes.c_void_p()
        try:
            hr = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            )(self._vtable(sess)[9])(sess, ctypes.byref(pb))
            if hr >= 0 and pb.value:
                st = ctypes.c_int(0)
                hr2 = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
                )(self._vtable(pb)[7])(pb, ctypes.byref(st))
                if hr2 >= 0:
                    return int(st.value)
        except Exception:
            pass
        finally:
            self._release(pb)
        return -1

    def _find_session(self, target_src=""):
        if not self._mgr or not self._mgr.value:
            return None
        mgr_vt = self._vtable(self._mgr)
        sessions_vec = ctypes.c_void_p()
        items = []
        selected = None
        try:
            hr = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            )(mgr_vt[7])(self._mgr, ctypes.byref(sessions_vec))
            if hr >= 0 and sessions_vec.value:
                vec_vt = self._vtable(sessions_vec)
                size = ctypes.c_uint32(0)
                if ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
                )(vec_vt[7])(sessions_vec, ctypes.byref(size)) >= 0:
                    get_at = ctypes.WINFUNCTYPE(
                        ctypes.c_long,
                        ctypes.c_void_p,
                        ctypes.c_uint32,
                        ctypes.POINTER(ctypes.c_void_p),
                    )(vec_vt[6])
                    for i in range(size.value):
                        s = ctypes.c_void_p()
                        if get_at(sessions_vec, i, ctypes.byref(s)) >= 0 and s.value:
                            items.append(s)

            if target_src and items:
                for s in items:
                    if self._get_session_source(s) == target_src:
                        selected = s
                        break
            if selected is None and items:
                for s in items:
                    if self._get_session_playback_status(s) == 4:  # Playing
                        selected = s
                        break
            if selected is None and items:
                for s in items:
                    if self._get_session_playback_status(s) == 5:  # Paused
                        selected = s
                        break
            if selected is None:
                cur = ctypes.c_void_p()
                hr_cur = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
                )(mgr_vt[6])(self._mgr, ctypes.byref(cur))
                if hr_cur >= 0 and cur.value:
                    selected = cur
                elif items:
                    for s in items:
                        if self._get_session_playback_status(s) != 0:  # not Closed
                            selected = s
                            break
                    if selected is None:
                        selected = items[0]
            return selected
        finally:
            for s in items:
                if s is not selected:
                    self._release(s)
            self._release(sessions_vec)

    def poll(self, stop_check=None):
        sess = self._find_session("")
        if not sess or not sess.value:
            return {"active": False}
        pb = ctypes.c_void_p()
        ctrl = ctypes.c_void_p()
        tl = ctypes.c_void_p()
        mp_op = ctypes.c_void_p()
        mp = None
        try:
            svt = self._vtable(sess)
            src = self._get_session_source(sess)

            status_val = 0
            can_seek = False
            if ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            )(svt[9])(sess, ctypes.byref(pb)) >= 0 and pb.value:
                pbvt = self._vtable(pb)
                st_c = ctypes.c_int(0)
                if ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
                )(pbvt[7])(pb, ctypes.byref(st_c)) >= 0:
                    status_val = int(st_c.value)
                if ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
                )(pbvt[6])(pb, ctypes.byref(ctrl)) >= 0 and ctrl.value:
                    cs_c = ctypes.c_ubyte(0)
                    if ctypes.WINFUNCTYPE(
                        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte)
                    )(self._vtable(ctrl)[20])(ctrl, ctypes.byref(cs_c)) >= 0:
                        can_seek = bool(cs_c.value)

            start_t = ctypes.c_int64(0)
            end_t = ctypes.c_int64(0)
            min_seek_t = ctypes.c_int64(0)
            max_seek_t = ctypes.c_int64(0)
            pos_t = ctypes.c_int64(0)
            upd_t = ctypes.c_int64(0)
            if ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            )(svt[8])(sess, ctypes.byref(tl)) >= 0 and tl.value:
                tlvt = self._vtable(tl)
                ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64))(tlvt[6])(tl, ctypes.byref(start_t))
                ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64))(tlvt[7])(tl, ctypes.byref(end_t))
                ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64))(tlvt[8])(tl, ctypes.byref(min_seek_t))
                ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64))(tlvt[9])(tl, ctypes.byref(max_seek_t))
                ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64))(tlvt[10])(tl, ctypes.byref(pos_t))
                ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64))(tlvt[11])(tl, ctypes.byref(upd_t))
                if end_t.value <= start_t.value and max_seek_t.value > min_seek_t.value:
                    start_t.value = min_seek_t.value
                    end_t.value = max_seek_t.value

            title = ""
            subtitle = ""
            artist = ""
            album = ""
            if ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            )(svt[7])(sess, ctypes.byref(mp_op)) >= 0 and mp_op.value:
                mp = self._await_op(mp_op, ctypes.c_void_p, timeout=1.5, stop_check=stop_check)
                if mp and mp.value:
                    mpvt = self._vtable(mp)
                    t_hs = ctypes.c_void_p()
                    s_hs = ctypes.c_void_p()
                    a_hs = ctypes.c_void_p()
                    al_hs = ctypes.c_void_p()
                    if ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(mpvt[6])(mp, ctypes.byref(t_hs)) >= 0:
                        title = self._hstr_to_py(t_hs)
                    if ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(mpvt[7])(mp, ctypes.byref(s_hs)) >= 0:
                        subtitle = self._hstr_to_py(s_hs)
                    if ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(mpvt[9])(mp, ctypes.byref(a_hs)) >= 0:
                        artist = self._hstr_to_py(a_hs)
                    if ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(mpvt[10])(mp, ctypes.byref(al_hs)) >= 0:
                        album = self._hstr_to_py(al_hs)
                    if not artist:
                        artist = album or subtitle

            status_str = _SMTC_STATUS_MAP.get(status_val, "Paused")
            last_upd = (
                (upd_t.value - _FILETIME_EPOCH_OFFSET) // 10000
                if upd_t.value > _FILETIME_EPOCH_OFFSET
                else 0
            )
            return {
                "active": True,
                "title": title,
                "artist": artist,
                "album": album or subtitle,
                "source": src,
                "playing": (status_str == "Playing"),
                "status": status_str,
                "pos": pos_t.value / 10_000_000.0,
                "start": start_t.value / 10_000_000.0,
                "end": end_t.value / 10_000_000.0,
                "last_upd": int(last_upd),
                "can_seek": can_seek,
            }
        except Exception:
            return {"active": False}
        finally:
            self._release(mp)
            self._release(mp_op)
            self._release(tl)
            self._release(ctrl)
            self._release(pb)
            self._release(sess)

    def execute_command(self, cmd_line, stop_check=None):
        parts = str(cmd_line or "").split(":", 1)
        verb = parts[0].strip().upper()
        arg = parts[1].strip() if len(parts) > 1 else ""
        target_src = ""
        if verb == "SEEK" and "|" in arg:
            arg, target_src = [x.strip() for x in arg.split("|", 1)]
        elif verb in ("PLAY", "PAUSE", "TOGGLE", "NEXT", "PREV"):
            target_src = arg
        sess = self._find_session(target_src)
        if not sess or not sess.value:
            return False
        op = ctypes.c_void_p()
        try:
            svt = self._vtable(sess)
            slot_map = {
                "PLAY": 10,
                "PAUSE": 11,
                "NEXT": 16,
                "PREV": 17,
                "TOGGLE": 20,
            }
            if verb == "SEEK" and arg != "":
                ticks = int(round(float(arg) * 10_000_000.0))
                hr = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.c_int64,
                    ctypes.POINTER(ctypes.c_void_p),
                )(svt[24])(sess, ctypes.c_int64(ticks), ctypes.byref(op))
            elif verb in slot_map:
                hr = ctypes.WINFUNCTYPE(
                    ctypes.c_long,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                )(svt[slot_map[verb]])(sess, ctypes.byref(op))
            else:
                return False
            if hr < 0 or not op.value:
                return False
            self._await_op(op, ctypes.c_ubyte, timeout=1.5, stop_check=stop_check)
            return True
        except Exception:
            return False
        finally:
            self._release(op)
            self._release(sess)


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


def _fmt_time(seconds):
    try:
        s = max(0, int(round(float(seconds))))
    except Exception:
        s = 0
    h = s // 3600
    m = (s % 3600) // 60
    rem = s % 60
    if h > 0:
        return f"{h}:{m:02d}:{rem:02d}"
    return f"{m}:{rem:02d}"


_APP_SOURCE_MAP = (
    ("photos", "Fotos", ("photos", "microsoft.photos")),
    ("microsoft.photos", "Fotos", ("photos", "microsoft.photos")),
    ("zunevideo", "Películas y TV", ("video.ui", "zune")),
    ("zunemusic", "Reproductor Multimedia", ("microsoft.media.player", "music.ui")),
    ("media.player", "Reproductor Multimedia", ("microsoft.media.player", "mediaplayer")),
    ("msedge", "Microsoft Edge", ("msedge",)),
    ("chrome", "Google Chrome", ("chrome",)),
    ("firefox", "Firefox", ("firefox",)),
    ("brave", "Brave", ("brave",)),
    ("opera", "Opera", ("opera",)),
    ("spotify", "Spotify", ("spotify",)),
    ("vlc", "VLC Media Player", ("vlc",)),
    ("applemusic", "Apple Music", ("applemusic",)),
    ("wmplayer", "Windows Media Player", ("wmplayer",)),
    ("ms-teams", "Microsoft Teams", ("ms-teams", "teams")),
    ("teams", "Microsoft Teams", ("ms-teams", "teams")),
    ("whatsapp", "WhatsApp", ("whatsapp",)),
)

_IGNORED_WASAPI_EXES = {
    "svchost.exe", "taskhostw.exe", "audiodg.exe", "shellexperiencehost.exe",
    "searchhost.exe", "explorer.exe", "sihost.exe", "runtimebroker.exe",
    "applicationframehost.exe", "startmenuexperiencehost.exe", "lockapp.exe",
    "systemsettings.exe", "textinputhost.exe", "widgets.exe",
}
_IGNORED_WIN_TITLES = {
    "program manager", "default ime", "msctfime ui",
    "alioth dockbar", "configuración - alioth dockbar",
}
_WIN_TITLE_SUFFIXES = (
    " - Fotos", " - Photos", " - VLC media player", " - Google Chrome",
    " - Microsoft\u200b Edge", " - Microsoft Edge", " - Reproductor multimedia",
    " - Media Player", " - Películas y TV", " - Movies & TV",
)

_win_title_cache = {"ts": 0.0, "key": "", "title": ""}


def _strip_player_suffix(win_title):
    t = (win_title or "").strip()
    for suf in _WIN_TITLE_SUFFIXES:
        if t.endswith(suf) and len(t) > len(suf):
            return t[: -len(suf)].strip()
    return t


def _get_window_title_for_pid_or_exe(target_pid=0, keywords=()):
    """Finds longest visible top-level window title matching target_pid first, or exe keywords."""
    pid_matches = []
    exe_matches = []
    kw_low = tuple(k.lower() for k in (keywords or ()) if k)
    t_pid = int(target_pid or 0)

    def _cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        ln = user32.GetWindowTextLengthW(hwnd)
        if ln <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        tbuf = ctypes.create_unicode_buffer(ln + 2)
        user32.GetWindowTextW(hwnd, tbuf, ln + 1)
        txt = (tbuf.value or "").strip()
        if not txt or txt.lower() in _IGNORED_WIN_TITLES:
            return True
        if t_pid and pid.value == t_pid:
            pid_matches.append(txt)
            return True
        if kw_low:
            hproc = kernel32.OpenProcess(0x1000, False, pid.value)
            if hproc:
                try:
                    pbuf = ctypes.create_unicode_buffer(260)
                    psz = wintypes.DWORD(260)
                    if kernel32.QueryFullProcessImageNameW(hproc, 0, pbuf, ctypes.byref(psz)):
                        exe_low = pbuf.value.rsplit("\\", 1)[-1].lower()
                        if any(k in exe_low for k in kw_low):
                            exe_matches.append(txt)
                finally:
                    kernel32.CloseHandle(hproc)
        return True

    try:
        user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    if pid_matches:
        return max(pid_matches, key=len)
    if exe_matches:
        return max(exe_matches, key=len)
    return ""


def _get_window_title_for_keywords(keywords):
    return _get_window_title_for_pid_or_exe(0, keywords)


def _friendly_app_name(source_or_exe):
    src_str = str(source_or_exe or "").strip()
    src_low = src_str.lower()
    for pat, fname, kws in _APP_SOURCE_MAP:
        if pat in src_low:
            return fname, kws
    base = src_str.split("!")[0].rsplit("\\", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    fname = base.capitalize() if base and base.islower() else (base or "Reproductor")
    kws = (base.lower(),) if base else ()
    return fname, kws


def _friendly_app_and_title(source_aumid, raw_title, raw_artist, playing):
    src_str = str(source_aumid or "").strip()
    src_low = src_str.lower()
    friendly, keywords = _friendly_app_name(src_str)

    title = (raw_title or "").strip()
    artist = (raw_artist or "").strip()
    if not title:
        now = time.time()
        cache_key = f"{src_low}"
        if cache_key == _win_title_cache["key"] and (now - _win_title_cache["ts"]) < 1.5:
            win_title = _win_title_cache["title"]
        else:
            win_title = _get_window_title_for_keywords(keywords)
            _win_title_cache.update({"ts": now, "key": cache_key, "title": win_title})
        win_title = _strip_player_suffix(win_title)
        if win_title:
            title = win_title
    if not title:
        title = friendly
    if not artist:
        artist = friendly if title != friendly else ("Reproduciendo" if playing else "En pausa")
    return title, artist


class _WASAPIAudioDetector:
    """Pure-ctypes Core Audio WASAPI fallback detector for non-SMTC media apps (Photos, Teams, etc.)."""

    def __init__(self):
        self._last_active = None
        self._com_init = False

    @staticmethod
    def _vtable(ptr):
        return ctypes.cast(
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )

    @staticmethod
    def _release(ptr):
        if ptr and getattr(ptr, "value", None):
            try:
                vt = _WASAPIAudioDetector._vtable(ptr)
                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])(ptr)
            except Exception:
                pass

    def poll(self):
        self_pid = os.getpid()
        enum_p = ctypes.c_void_p()
        dev_p = ctypes.c_void_p()
        mgr_p = ctypes.c_void_p()
        senum_p = ctypes.c_void_p()
        try:
            hr = ole32.CoCreateInstance(
                ctypes.byref(_CLSID_MMDeviceEnumerator),
                None,
                23,
                ctypes.byref(_IID_IMMDeviceEnumerator),
                ctypes.byref(enum_p),
            )
            if hr == -2147221008:  # CO_E_NOTINITIALIZED
                ole32.CoInitializeEx(None, 2)
                self._com_init = True
                hr = ole32.CoCreateInstance(
                    ctypes.byref(_CLSID_MMDeviceEnumerator),
                    None,
                    23,
                    ctypes.byref(_IID_IMMDeviceEnumerator),
                    ctypes.byref(enum_p),
                )
            if hr < 0 or not enum_p.value:
                return {"active": False}

            if ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
            )(self._vtable(enum_p)[4])(enum_p, 0, 1, ctypes.byref(dev_p)) < 0 or not dev_p.value:
                return {"active": False}

            if ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            )(self._vtable(dev_p)[3])(dev_p, ctypes.byref(_IID_IAudioSessionManager2), 23, None, ctypes.byref(mgr_p)) < 0 or not mgr_p.value:
                return {"active": False}

            if ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            )(self._vtable(mgr_p)[5])(mgr_p, ctypes.byref(senum_p)) < 0 or not senum_p.value:
                return {"active": False}

            cnt = ctypes.c_int(0)
            if ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
            )(self._vtable(senum_p)[3])(senum_p, ctypes.byref(cnt)) < 0:
                return {"active": False}

            get_sess = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
            )(self._vtable(senum_p)[4])

            active_candidates = []
            inactive_sessions = []

            for i in range(max(0, cnt.value)):
                ctl = ctypes.c_void_p()
                ctl2 = ctypes.c_void_p()
                meter = ctypes.c_void_p()
                try:
                    if get_sess(senum_p, i, ctypes.byref(ctl)) < 0 or not ctl.value:
                        continue
                    cvt = self._vtable(ctl)
                    st = ctypes.c_int(-1)
                    if ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))(cvt[3])(ctl, ctypes.byref(st)) < 0:
                        continue
                    if st.value not in (0, 1):  # 0=Inactive, 1=Active
                        continue

                    if ctypes.WINFUNCTYPE(
                        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
                    )(cvt[0])(ctl, ctypes.byref(_IID_IAudioSessionControl2), ctypes.byref(ctl2)) < 0 or not ctl2.value:
                        continue
                    c2vt = self._vtable(ctl2)
                    if ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(c2vt[15])(ctl2) == 0:  # IsSystemSoundsSession == S_OK
                        continue
                    pid = wintypes.DWORD(0)
                    ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))(c2vt[14])(ctl2, ctypes.byref(pid))
                    if not pid.value or pid.value == self_pid:
                        continue

                    exe_low = ""
                    hp = kernel32.OpenProcess(0x1000, False, pid.value)
                    if hp:
                        try:
                            pbuf = ctypes.create_unicode_buffer(260)
                            psz = wintypes.DWORD(260)
                            if kernel32.QueryFullProcessImageNameW(hp, 0, pbuf, ctypes.byref(psz)):
                                exe_low = pbuf.value.rsplit("\\", 1)[-1].lower()
                        finally:
                            kernel32.CloseHandle(hp)
                    if not exe_low or exe_low in _IGNORED_WASAPI_EXES:
                        continue

                    peak = ctypes.c_float(0.0)
                    if ctypes.WINFUNCTYPE(
                        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
                    )(cvt[0])(ctl, ctypes.byref(_IID_IAudioMeterInformation), ctypes.byref(meter)) >= 0 and meter.value:
                        ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_float))(self._vtable(meter)[3])(meter, ctypes.byref(peak))

                    entry = (float(peak.value), int(pid.value), exe_low)
                    if st.value == 1:
                        active_candidates.append(entry)
                    else:
                        inactive_sessions.append((int(pid.value), exe_low))
                finally:
                    self._release(meter)
                    self._release(ctl2)
                    self._release(ctl)

            now = time.time()
            if active_candidates:
                active_candidates.sort(key=lambda x: (1 if x[0] > 0.0005 else 0, x[0]), reverse=True)
                _, pid_v, exe_low = active_candidates[0]
                friendly_app, kws = _friendly_app_name(exe_low)
                stem = exe_low[:-4] if exe_low.endswith(".exe") else exe_low
                raw_win = _get_window_title_for_pid_or_exe(pid_v, kws or (stem,))
                win_title = _strip_player_suffix(raw_win)
                title = win_title or friendly_app
                artist = friendly_app if win_title and win_title != friendly_app else "Reproduciendo audio"
                self._last_active = {"exe": exe_low, "pid": pid_v, "title": title, "artist": friendly_app, "ts": now}
                return {
                    "active": True,
                    "title": title,
                    "artist": artist,
                    "source": f"wasapi:{exe_low}",
                    "playing": True,
                    "status": "Playing",
                    "pos": 0.0,
                    "start": 0.0,
                    "end": 0.0,
                    "last_upd": 0,
                    "can_seek": False,
                }

            if self._last_active and (now - self._last_active["ts"]) <= 20.0:
                last = self._last_active
                still_exists = any((p == last["pid"] or ex == last["exe"]) for p, ex in inactive_sessions)
                if still_exists:
                    title = last["title"]
                    friendly_app = last["artist"]
                    artist = friendly_app if title != friendly_app else "En pausa"
                    return {
                        "active": True,
                        "title": title,
                        "artist": artist,
                        "source": f"wasapi:{last['exe']}",
                        "playing": False,
                        "status": "Paused",
                        "pos": 0.0,
                        "start": 0.0,
                        "end": 0.0,
                        "last_upd": 0,
                        "can_seek": False,
                    }
                self._last_active = None

            return {"active": False}
        except Exception:
            return {"active": False}
        finally:
            self._release(senum_p)
            self._release(mgr_p)
            self._release(dev_p)
            self._release(enum_p)


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
        self.cur_h = 70
        self.win_x = 0
        self.win_y = 0
        self.btn_boxes = []  # [(x1, y1, x2, y2, action), ...]
        self.progress_box = None
        self.progress_track = None
        self.volume_box = None
        self.volume_track = None
        self.hovered_btn = None
        self._hover_inside = False
        self._drag_mode = None  # "seek" | "volume" | None
        self._drag_seek_ratio = None
        self._last_render_sig = None

        self._vol_ctrl = _SystemVolumeController()
        self._vol_level = 1.0
        self._vol_muted = False
        self._vol_last_poll = 0.0
        self._vol_overlay_until = 0.0

        self._win_visible = False
        self._manually_closed = False
        self._ever_played_this_session = False
        self._paused_since = 0.0
        self._inactive_since = 0.0

        # Focus-loss auto-resume tracking
        self._last_fg_hwnd = 0
        self._fg_changed_at = 0.0
        self._prev_playing = False
        self._last_playing_ts = 0.0
        self._user_paused_at = 0.0
        self._blur_resume_attempts = 0
        self._blur_resume_last_try = 0.0
        self._blur_target_source = ""

        self.media_state = {
            "active": False,
            "title": "Sin reproducción activa",
            "artist": "Listo para controlar música/video",
            "playing": False,
            "status": "Closed",
            "source": "",
            "pos": 0.0,
            "start": 0.0,
            "end": 0.0,
            "last_upd": 0,
            "can_seek": False,
            "received_at": 0.0,
        }
        self._smtc_cmd_queue = queue.Queue()
        self._smtc_wake_event = threading.Event()
        self._smtc_ready = False
        self._smtc_lock = threading.Lock()
        self._smtc_thread = None
        self._stop_flag = False
        self._sync_job = None

    def is_enabled(self):
        return bool(self.config_mgr.get("general", "media_capsule_enabled", False))

    @staticmethod
    def _calc_text_metrics(scale, font_sz):
        title_h = max(12, int(round(font_sz * scale * 1.28)))
        sub_h = max(10, int(round(max(8.0, font_sz - 1.5) * scale * 1.22)))
        text_gap = max(3, int(round(4 * scale)))
        return title_h, sub_h, text_gap, title_h + text_gap + sub_h

    def _compute_dims(self):
        scale = max(0.75, min(1.45, float(self.config_mgr.get("general", "media_capsule_scale", 1.0) or 1.0)))
        btn_scale = max(0.65, min(1.60, float(self.config_mgr.get("general", "media_capsule_btn_scale", 1.0) or 1.0)))
        base_w = max(260, min(600, int(self.config_mgr.get("general", "media_capsule_width", 360) or 360)))
        btn_pos = (self.config_mgr.get("general", "media_capsule_btn_pos", "right") or "right").lower()
        font_sz = max(9, min(18, int(self.config_mgr.get("general", "media_capsule_font_size", 11) or 11)))

        eff_bscale = scale * btn_scale
        r_play = max(10, int(14 * eff_bscale))
        bar_h = max(20, int(22 * scale))
        _, _, _, text_block_h = self._calc_text_metrics(scale, font_sz)

        if btn_pos == "center":
            pad_top = max(8, int(round(8 * scale)))
            pad_mid = max(6, int(round(7 * scale)))
            pad_bot = max(6, int(round(6 * scale)))
            r_play_c = max(10, int(14 * eff_bscale * 0.94))
            top_area = pad_top + text_block_h + pad_mid + (r_play_c * 2) + pad_bot
        else:
            top_area = max(int(44 * scale), r_play * 2 + int(14 * scale), text_block_h + int(14 * scale))

        w = max(240, int(base_w * scale))
        h = top_area + bar_h
        return w, h, scale, btn_scale, top_area, btn_pos

    def _set_window_visible(self, visible):
        if not self.top_hwnd or not user32.IsWindow(self.top_hwnd):
            self._win_visible = False
            return
        if bool(visible) == bool(self._win_visible):
            return
        self._win_visible = bool(visible)
        try:
            if visible:
                self.reposition()
                self._render(force=True)
                user32.ShowWindow(self.top_hwnd, SW_SHOWNOACTIVATE)
                revealed = bool(getattr(self.dock, "is_revealed", True))
                self.sync_z_order(revealed, 0)
            else:
                self._hover_inside = False
                self.hovered_btn = None
                self._drag_mode = None
                user32.ShowWindow(self.top_hwnd, SW_HIDE)
        except Exception:
            pass

    def _should_be_visible(self, now=None):
        if not self.is_enabled():
            return False
        now = now or time.time()
        auto_hide = bool(self.config_mgr.get("general", "media_capsule_auto_hide_idle", True))
        if self._manually_closed:
            return False
        if not auto_hide:
            return True
        st = self.media_state
        is_active = bool(st.get("active")) and st.get("status") not in ("Closed", "Stopped")
        if is_active and st.get("playing"):
            return True
        if is_active and self._ever_played_this_session:
            if self._hover_inside:
                return True
            if self._paused_since and (now - self._paused_since) < 90.0:
                return True
        return False

    def _update_idle_visibility(self, now=None):
        now = now or time.time()
        st = self.media_state
        is_active = bool(st.get("active")) and st.get("status") not in ("Closed", "Stopped")
        if is_active:
            self._inactive_since = 0.0
            if st.get("playing"):
                self._ever_played_this_session = True
                self._paused_since = 0.0
            else:
                if self._ever_played_this_session and not self._paused_since:
                    self._paused_since = now
        else:
            self._paused_since = 0.0
            if not self._inactive_since:
                self._inactive_since = now
            elif (now - self._inactive_since) >= 3.0:
                self._ever_played_this_session = False
                self._manually_closed = False

        want_vis = self._should_be_visible(now)
        if want_vis != self._win_visible:
            self._set_window_visible(want_vis)

    def sync_visibility(self):
        if self.is_enabled():
            self._manually_closed = False
            if not self.window or not self.window.winfo_exists():
                self.show()
            else:
                self._update_idle_visibility()
                if self._win_visible:
                    self.reposition()
                    self._render(force=True)
        else:
            self.hide()

    def show(self):
        if self.window and self.window.winfo_exists():
            self._update_idle_visibility()
            return
        self._stop_flag = False
        self._manually_closed = False
        w, h, _, _, _, _ = self._compute_dims()
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
        self.canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-3>", self._on_right_click)

        self._poll_volume(force=True)
        self._start_smtc_reader()
        self.reposition()

        if self._should_be_visible():
            self._win_visible = True
            self._render(force=True)
            try:
                user32.ShowWindow(self.top_hwnd, SW_SHOWNOACTIVATE)
            except Exception:
                pass
        else:
            self._win_visible = False
            try:
                user32.ShowWindow(self.top_hwnd, SW_HIDE)
            except Exception:
                pass
        self._schedule_tick()

    def hide(self):
        self._stop_flag = True
        self._smtc_ready = False
        self._win_visible = False
        if self._sync_job:
            try:
                self.root.after_cancel(self._sync_job)
            except Exception:
                pass
            self._sync_job = None
        self._smtc_wake_event.set()
        while True:
            try:
                self._smtc_cmd_queue.get_nowait()
            except queue.Empty:
                break
        self._vol_ctrl.release()
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
        self.canvas = None
        self.photo_ref = None
        self._last_render_sig = None

    def _send_smtc_cmd(self, cmd):
        with self._smtc_lock:
            if not (
                self._smtc_ready
                and not self._stop_flag
                and self._smtc_thread
                and self._smtc_thread.is_alive()
            ):
                return False
            try:
                self._smtc_cmd_queue.put_nowait(str(cmd))
                self._smtc_wake_event.set()
                return True
            except Exception:
                return False

    def _start_smtc_reader(self):
        if self._smtc_thread and self._smtc_thread.is_alive():
            return
        self._smtc_wake_event.clear()

        def _worker():
            client = _NativeSMTCClient()
            wasapi = _WASAPIAudioDetector()
            smtc_ok = False
            next_smtc_retry = 0.0
            force_poll = True
            last_poll = 0.0
            try:
                while not self._stop_flag:
                    now_mono = time.monotonic()
                    if not smtc_ok and now_mono >= next_smtc_retry:
                        smtc_ok = client.open(timeout=2.0, stop_check=lambda: self._stop_flag)
                        with self._smtc_lock:
                            self._smtc_ready = smtc_ok
                        if not smtc_ok:
                            next_smtc_retry = now_mono + 3.0

                    while not self._stop_flag:
                        try:
                            cmd = self._smtc_cmd_queue.get_nowait()
                        except queue.Empty:
                            break
                        if smtc_ok:
                            try:
                                client.execute_command(cmd, stop_check=lambda: self._stop_flag)
                            except Exception:
                                pass
                        force_poll = True

                    if self._stop_flag:
                        break

                    if force_poll or (now_mono - last_poll) >= 0.75:
                        force_poll = False
                        last_poll = now_mono
                        info = {"active": False}
                        if smtc_ok:
                            try:
                                info = client.poll(stop_check=lambda: self._stop_flag)
                            except Exception:
                                info = {"active": False}
                        if not info.get("active"):
                            w_info = wasapi.poll()
                            if w_info.get("active"):
                                info = w_info

                        active = bool(info.get("active"))
                        playing = bool(info.get("playing"))
                        src = info.get("source") or ""
                        now_ts = time.time()
                        if playing:
                            self._last_playing_ts = now_ts
                        if active:
                            if src.startswith("wasapi:"):
                                title = info.get("title") or "Reproductor"
                                artist = info.get("artist") or ("Reproduciendo audio" if playing else "En pausa")
                            else:
                                title, artist = _friendly_app_and_title(
                                    src, info.get("title"), info.get("artist") or info.get("album"), playing
                                )
                        else:
                            title = "Sin reproducción activa"
                            artist = "Listo para controlar música/video"
                        self.media_state = {
                            "active": active,
                            "title": title,
                            "artist": artist,
                            "playing": playing,
                            "status": info.get("status") or ("Playing" if playing else "Paused"),
                            "source": src,
                            "pos": float(info.get("pos") or 0.0),
                            "start": float(info.get("start") or 0.0),
                            "end": float(info.get("end") or 0.0),
                            "last_upd": int(info.get("last_upd") or 0),
                            "can_seek": bool(info.get("can_seek")),
                            "received_at": now_ts,
                        }

                    self._smtc_wake_event.wait(0.12)
                    self._smtc_wake_event.clear()
            finally:
                with self._smtc_lock:
                    self._smtc_ready = False
                client.close()

        self._smtc_thread = threading.Thread(target=_worker, daemon=True)
        self._smtc_thread.start()

    def _poll_volume(self, force=False):
        now = time.time()
        if not force and (now - self._vol_last_poll) < 0.75:
            return
        self._vol_last_poll = now
        if self._drag_mode == "volume":
            return
        vol, muted = self._vol_ctrl.get_state()
        self._vol_level = vol
        self._vol_muted = muted

    def _check_focus_loss_audio(self, now):
        try:
            fg = int(user32.GetForegroundWindow() or 0)
        except Exception:
            fg = 0
        if fg and fg != self._last_fg_hwnd and fg != self.top_hwnd:
            self._last_fg_hwnd = fg
            self._fg_changed_at = now
            self._blur_resume_attempts = 0
            if self.media_state.get("playing"):
                self._blur_target_source = self.media_state.get("source") or ""

        st = self.media_state
        playing_now = bool(st.get("playing"))
        keep_audio = bool(self.config_mgr.get("general", "media_capsule_keep_audio_on_blur", True))

        if keep_audio and st.get("active") and not playing_now:
            was_recently_playing = self._prev_playing or (now - self._last_playing_ts < 2.2)
            recent_fg_switch = (0.0 < (now - self._fg_changed_at) <= 2.8)
            user_did_pause = ((now - self._user_paused_at) <= 3.0)
            if (
                was_recently_playing
                and recent_fg_switch
                and not user_did_pause
                and self._blur_resume_attempts < 2
                and (now - self._blur_resume_last_try) >= 0.55
            ):
                src = self._blur_target_source or st.get("source") or ""
                sent = self._send_smtc_cmd(f"PLAY:{src}" if src else "PLAY")
                if not sent and self._blur_resume_attempts == 0:
                    _send_media_vk(VK_MEDIA_PLAY_PAUSE)
                self._blur_resume_attempts += 1
                self._blur_resume_last_try = now
                self._last_playing_ts = now
        elif playing_now:
            self._last_playing_ts = now
            self._blur_resume_attempts = 0

        self._prev_playing = playing_now

    def _get_live_progress(self):
        """Returns (current_seconds, duration_seconds, ratio_0_to_1)."""
        st = self.media_state
        start_s = float(st.get("start") or 0.0)
        end_s = float(st.get("end") or 0.0)
        dur = max(0.0, end_s - start_s)
        if self._drag_mode == "seek" and self._drag_seek_ratio is not None and dur > 0.0:
            ratio = max(0.0, min(1.0, self._drag_seek_ratio))
            return ratio * dur, dur, ratio

        raw_pos = max(0.0, float(st.get("pos") or 0.0) - start_s)
        if st.get("playing") and dur > 0.0:
            now_ts = time.time()
            last_upd_ms = int(st.get("last_upd") or 0)
            elapsed = 0.0
            if last_upd_ms > 0:
                delta = now_ts - (last_upd_ms / 1000.0)
                if 0.0 <= delta <= 86400.0:
                    elapsed = delta
            if elapsed == 0.0 and st.get("received_at"):
                elapsed = max(0.0, now_ts - float(st["received_at"]))
            raw_pos = min(dur, raw_pos + min(elapsed, 3600.0))
        elif dur > 0.0:
            raw_pos = min(dur, raw_pos)

        ratio = (raw_pos / dur) if dur > 0.0 else 0.0
        return raw_pos, dur, max(0.0, min(1.0, ratio))

    def _schedule_tick(self):
        if self._stop_flag or not self.window or not self.window.winfo_exists():
            return
        now = time.time()
        self._check_focus_loss_audio(now)
        self._update_idle_visibility(now)
        if self._win_visible:
            self._poll_volume(force=False)
            self.reposition()
            self._render()
            delay = 250 if self.media_state.get("playing") else 400
        else:
            delay = 450
        self._sync_job = self.root.after(delay, self._schedule_tick)

    def reposition(self):
        if not self.window or not self.window.winfo_exists():
            return
        w, h, _, _, _, _ = self._compute_dims()
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
            size_changed = (w != self.cur_w or h != self.cur_h)
            self.win_x, self.win_y = nx, ny
            self.cur_w, self.cur_h = w, h
            self.window.geometry(f"{w}x{h}+{nx}+{ny}")
            if self.canvas:
                self.canvas.config(width=w, height=h)
            if size_changed and self._win_visible:
                self._render(force=True)

    def _is_cursor_inside(self):
        if not self._win_visible or not self.hwnd or not user32.IsWindowVisible(self.hwnd):
            return False
        try:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            return (self.win_x - 2 <= pt.x <= self.win_x + self.cur_w + 2) and (self.win_y - 2 <= pt.y <= self.win_y + self.cur_h + 2)
        except Exception:
            return False

    def sync_z_order(self, revealed, anchor=0):
        if not self._win_visible or not self.hwnd or not user32.IsWindow(self.hwnd):
            return
        try:
            live_hover = self._is_cursor_inside()
            self._hover_inside = live_hover
            if revealed or live_hover or not anchor:
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            else:
                user32.SetWindowPos(self.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                user32.SetWindowPos(self.hwnd, int(anchor), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass

    def _hit_target(self, x, y):
        for x1, y1, x2, y2, act in self.btn_boxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return act
        if self.progress_box:
            px1, py1, px2, py2 = self.progress_box
            if px1 <= x <= px2 and py1 <= y <= py2:
                return "seek"
        if self.volume_box:
            vx1, vy1, vx2, vy2 = self.volume_box
            if vx1 <= x <= vx2 and vy1 <= y <= vy2:
                return "volume"
        return None

    def _ratio_for_track(self, track, x):
        if not track:
            return 0.0
        tx1, tx2 = track
        span = max(1.0, float(tx2 - tx1))
        return max(0.0, min(1.0, (float(x) - tx1) / span))

    def _on_motion(self, event):
        self._hover_inside = True
        if self._drag_mode:
            return
        old = self.hovered_btn
        self.hovered_btn = self._hit_target(event.x, event.y)
        if old != self.hovered_btn:
            self._render(force=True)

    def _on_leave(self, _event=None):
        self._hover_inside = False
        if not self._drag_mode and self.hovered_btn is not None:
            self.hovered_btn = None
            self._render(force=True)

    def _on_click(self, event):
        hit = self._hit_target(event.x, event.y)
        if hit == "close":
            auto_hide = bool(self.config_mgr.get("general", "media_capsule_auto_hide_idle", True))
            if auto_hide:
                self._manually_closed = True
                self._set_window_visible(False)
            else:
                self.config_mgr.set("general", "media_capsule_enabled", False)
                self.hide()
            return
        if hit == "seek":
            _, dur, _ = self._get_live_progress()
            if dur > 0.0:
                self._drag_mode = "seek"
                self._drag_seek_ratio = self._ratio_for_track(self.progress_track, event.x)
                self._render(force=True)
            return
        if hit == "volume":
            self._drag_mode = "volume"
            ratio = self._ratio_for_track(self.volume_track, event.x)
            self._vol_level = ratio
            self._vol_muted = False
            self._vol_overlay_until = time.time() + 1.4
            self._vol_ctrl.set_volume(ratio)
            self._render(force=True)
            return
        if hit == "mute":
            self._vol_muted = self._vol_ctrl.toggle_mute()
            self._vol_overlay_until = time.time() + 1.2
            self._render(force=True)
            return
        src = self.media_state.get("source") or ""
        is_wasapi = src.startswith("wasapi:")
        if hit == "prev":
            if is_wasapi or not self._send_smtc_cmd(f"PREV:{src}" if src else "PREV"):
                _send_media_vk(VK_MEDIA_PREV_TRACK)
            self._render(force=True)
        elif hit == "play_pause":
            was_playing = bool(self.media_state.get("playing"))
            if was_playing:
                self._user_paused_at = time.time()
                cmd = f"PAUSE:{src}" if src else "PAUSE"
            else:
                self._user_paused_at = 0.0
                self._last_playing_ts = time.time()
                cmd = f"PLAY:{src}" if src else "PLAY"
            if is_wasapi or not self._send_smtc_cmd(cmd):
                _send_media_vk(VK_MEDIA_PLAY_PAUSE)
            self.media_state["playing"] = not was_playing
            self._render(force=True)
        elif hit == "next":
            if is_wasapi or not self._send_smtc_cmd(f"NEXT:{src}" if src else "NEXT"):
                _send_media_vk(VK_MEDIA_NEXT_TRACK)
            self._render(force=True)

    def _on_drag_motion(self, event):
        if self._drag_mode == "seek":
            self._drag_seek_ratio = self._ratio_for_track(self.progress_track, event.x)
            self._render(force=True)
        elif self._drag_mode == "volume":
            ratio = self._ratio_for_track(self.volume_track, event.x)
            self._vol_level = ratio
            self._vol_muted = False
            self._vol_overlay_until = time.time() + 1.4
            self._vol_ctrl.set_volume(ratio)
            self._render(force=True)

    def _on_release(self, event):
        if self._drag_mode == "seek":
            ratio = self._ratio_for_track(self.progress_track, event.x)
            start_s = float(self.media_state.get("start") or 0.0)
            end_s = float(self.media_state.get("end") or 0.0)
            dur = max(0.0, end_s - start_s)
            if dur > 0.0:
                target_sec = start_s + ratio * dur
                self.media_state["pos"] = target_sec
                self.media_state["received_at"] = time.time()
                self.media_state["last_upd"] = 0
                src = self.media_state.get("source") or ""
                cmd = f"SEEK:{target_sec:.3f}|{src}" if src else f"SEEK:{target_sec:.3f}"
                self._send_smtc_cmd(cmd)
            self._drag_seek_ratio = None
        self._drag_mode = None
        self.hovered_btn = self._hit_target(event.x, event.y)
        self._render(force=True)

    def _on_mouse_wheel(self, event):
        step = 0.04 if event.delta > 0 else -0.04
        self._vol_level = self._vol_ctrl.step_volume(step)
        if self._vol_level > 0.01:
            self._vol_muted = False
        self._vol_overlay_until = time.time() + 1.4
        self._render(force=True)

    def _on_right_click(self, event):
        from custom_menu import CustomMenu
        menu = CustomMenu(self.window)
        cur_st = self.config_mgr.get("general", "media_capsule_style", "cristal")
        cur_bp = self.config_mgr.get("general", "media_capsule_btn_pos", "right")
        cur_bstyle = self.config_mgr.get("general", "media_capsule_btn_style", "clasico")
        cur_bs = float(self.config_mgr.get("general", "media_capsule_btn_scale", 1.0) or 1.0)
        auto_hide = bool(self.config_mgr.get("general", "media_capsule_auto_hide_idle", True))
        keep_blur = bool(self.config_mgr.get("general", "media_capsule_keep_audio_on_blur", True))

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

        sec_bst = menu.add_section("Estilo de Botones", expanded=False)
        for bsid, bslbl in (
            ("clasico", "Clásico Relleno"),
            ("outline", "Outlinear"),
            ("vidrio", "Cristal Glass"),
            ("cuadrado", "Cuadrado Redondeado"),
            ("neon", "Neón Glow"),
            ("minimal", "Minimal Sin Fondo"),
        ):
            sec_bst.add_command(bslbl, lambda k=bsid: (self.config_mgr.set("general", "media_capsule_btn_style", k), self._render(force=True)), checked=(cur_bstyle == bsid))

        sec_bs = menu.add_section("Tamaño de Botones", expanded=False)
        for bval, blbl in ((0.75, "Compacto (75%)"), (1.0, "Normal (100%)"), (1.25, "Grande (125%)"), (1.50, "Extra Grande (150%)")):
            sec_bs.add_command(blbl, lambda v=bval: (self.config_mgr.set("general", "media_capsule_btn_scale", v), self.reposition(), self._render(force=True)), checked=(abs(cur_bs - bval) < 0.06))

        menu.add_separator()
        menu.add_command(
            "Ocultar si no hay reproducción",
            lambda: (self.config_mgr.set("general", "media_capsule_auto_hide_idle", not auto_hide), self.sync_visibility()),
            checked=auto_hide,
        )
        menu.add_command(
            "Mantener audio al perder foco",
            lambda: self.config_mgr.set("general", "media_capsule_keep_audio_on_blur", not keep_blur),
            checked=keep_blur,
        )
        menu.add_separator()
        if getattr(self.dock, "on_open_settings", None):
            menu.add_command("Personalizar Cápsula en Ajustes...", self.dock.on_open_settings)
        menu.add_command("Ocultar cápsula de reproductor", lambda: (self.config_mgr.set("general", "media_capsule_enabled", False), self.hide()), danger=True)
        menu.show(event.x_root, event.y_root)

    def _draw_ctrl_btn(self, composite, draw, act, bcx, bcy, bscale, acc_rgb, playing, btn_style="clasico"):
        bst = (btn_style or "clasico").lower()
        r = max(9, int((14 if act == "play_pause" else 12) * bscale))
        box = (int(bcx - r), int(bcy - r), int(bcx + r), int(bcy + r))
        self.btn_boxes.append((*box, act))
        is_hov = (self.hovered_btn == act)
        is_play_btn = (act == "play_pause")

        # 1. Button background / border per btn_style
        if bst == "outline":
            ring_w = max(1, int(round(1.6 * bscale)))
            if is_play_btn:
                alpha_ellipse(
                    composite,
                    list(box),
                    fill=(*acc_rgb, 36 if is_hov else 12),
                    outline=(*acc_rgb, 255 if is_hov else 235),
                    width=ring_w,
                )
            else:
                alpha_ellipse(
                    composite,
                    list(box),
                    fill=(255, 255, 255, 22 if is_hov else 0),
                    outline=(255, 255, 255, 225 if is_hov else 135),
                    width=ring_w,
                )
        elif bst == "vidrio":
            if is_play_btn:
                alpha_ellipse(
                    composite,
                    list(box),
                    fill=(*acc_rgb, 118 if is_hov else 82),
                    outline=(255, 255, 255, 200 if is_hov else 135),
                    width=1,
                )
            else:
                alpha_ellipse(
                    composite,
                    list(box),
                    fill=(255, 255, 255, 50 if is_hov else 26),
                    outline=(255, 255, 255, 130 if is_hov else 72),
                    width=1,
                )
        elif bst == "cuadrado":
            sq_rad = max(5, int(r * 0.45))
            if is_play_btn:
                alpha_rounded_rect(
                    composite,
                    list(box),
                    radius=sq_rad,
                    fill=(*acc_rgb, 238 if is_hov else 192),
                    outline=(255, 255, 255, 205 if is_hov else 115),
                    width=1,
                )
            else:
                alpha_rounded_rect(
                    composite,
                    list(box),
                    radius=sq_rad,
                    fill=(255, 255, 255, 46 if is_hov else 22),
                    outline=(255, 255, 255, 105 if is_hov else 48),
                    width=1,
                )
        elif bst == "neon":
            glow_r = r + max(1, int(round(1.8 * bscale)))
            rim_w = max(1, int(round(1.6 * bscale)))
            glow_w = max(2, int(round(2.4 * bscale)))
            if is_play_btn:
                alpha_ellipse(
                    composite,
                    [int(bcx - glow_r), int(bcy - glow_r), int(bcx + glow_r), int(bcy + glow_r)],
                    fill=(0, 0, 0, 0),
                    outline=(*acc_rgb, 105 if is_hov else 60),
                    width=glow_w,
                )
                alpha_ellipse(
                    composite,
                    list(box),
                    fill=(8, 12, 22, 225),
                    outline=(*acc_rgb, 255),
                    width=rim_w,
                )
            else:
                alpha_ellipse(
                    composite,
                    list(box),
                    fill=(8, 12, 22, 205 if is_hov else 170),
                    outline=(*acc_rgb, 210 if is_hov else 130),
                    width=1,
                )
        elif bst == "minimal":
            if is_play_btn and is_hov:
                alpha_ellipse(composite, list(box), fill=(*acc_rgb, 36))
            elif (not is_play_btn) and is_hov:
                alpha_ellipse(composite, list(box), fill=(255, 255, 255, 26))
        else:
            # "clasico"
            if is_play_btn:
                fill_b = (*acc_rgb, 235 if is_hov else 185)
                alpha_ellipse(composite, list(box), fill=fill_b, outline=(255, 255, 255, 200 if is_hov else 115), width=1)
            elif is_hov:
                alpha_ellipse(composite, list(box), fill=(255, 255, 255, 38))

        # 2. Inner playback glyph per act + btn_style
        if is_play_btn:
            p = max(4, int((5.8 if bst == "minimal" else 5.0) * bscale))
            if bst == "neon":
                glyph_col = (255, 255, 255, 255) if is_hov else (*acc_rgb, 255)
            elif bst == "minimal":
                glyph_col = (255, 255, 255, 255) if is_hov else (*acc_rgb, 255)
            elif bst == "outline":
                glyph_col = (255, 255, 255, 255) if is_hov else (*acc_rgb, 245)
            else:
                glyph_col = (255, 255, 255, 255)

            if playing:
                bw = max(2, int(round(3.0 * bscale)))
                if bst == "outline":
                    lw_o = max(1, int(round(1.5 * bscale)))
                    draw.rectangle([bcx - p + 1, bcy - p, bcx - p + 1 + bw, bcy + p], outline=glyph_col, width=lw_o)
                    draw.rectangle([bcx + p - 1 - bw, bcy - p, bcx + p - 1, bcy + p], outline=glyph_col, width=lw_o)
                else:
                    draw.rectangle([bcx - p + 1, bcy - p, bcx - p + 1 + bw, bcy + p], fill=glyph_col)
                    draw.rectangle([bcx + p - 1 - bw, bcy - p, bcx + p - 1, bcy + p], fill=glyph_col)
            else:
                tri = [
                    (bcx - int(3 * bscale), bcy - p - 1),
                    (bcx - int(3 * bscale), bcy + p + 1),
                    (bcx + int(6 * bscale), bcy),
                ]
                if bst == "outline":
                    lw_o = max(1, int(round(1.6 * bscale)))
                    draw.line(tri + [tri[0]], fill=glyph_col, width=lw_o, joint="curve")
                else:
                    draw.polygon(tri, fill=glyph_col)
        else:
            if bst == "neon":
                col_i = (255, 255, 255, 255) if is_hov else (*acc_rgb, 215)
            else:
                col_i = (255, 255, 255, 255) if is_hov else (203, 213, 225, 235)
            p = max(4, int(5 * bscale))
            lw = max(2, int(2 * bscale))
            lw_o = max(1, int(round(1.5 * bscale)))
            if act == "prev":
                draw.line([(bcx - p, bcy - p), (bcx - p, bcy + p)], fill=col_i, width=lw)
                tri = [(bcx + p, bcy - p), (bcx + p, bcy + p), (bcx - p + 2, bcy)]
                if bst == "outline":
                    draw.line(tri + [tri[0]], fill=col_i, width=lw_o, joint="curve")
                else:
                    draw.polygon(tri, fill=col_i)
            else:
                tri = [(bcx - p, bcy - p), (bcx - p, bcy + p), (bcx + p - 2, bcy)]
                if bst == "outline":
                    draw.line(tri + [tri[0]], fill=col_i, width=lw_o, joint="curve")
                else:
                    draw.polygon(tri, fill=col_i)
                draw.line([(bcx + p, bcy - p), (bcx + p, bcy + p)], fill=col_i, width=lw)

    def _draw_close_btn(self, composite, draw, cx, cy, scale):
        r = max(7, int(8 * scale))
        self.btn_boxes.append((int(cx - r - 2), int(cy - r - 2), int(cx + r + 2), int(cy + r + 2), "close"))
        is_hov = (self.hovered_btn == "close")
        fill_c = (239, 68, 68, 225) if is_hov else (255, 255, 255, 22)
        out_c = (252, 165, 165, 220) if is_hov else (255, 255, 255, 45)
        alpha_ellipse(composite, [cx - r, cy - r, cx + r, cy + r], fill=fill_c, outline=out_c, width=1)
        d = max(3, int(3.2 * scale))
        x_col = (255, 255, 255, 255) if is_hov else (148, 163, 184, 225)
        draw.line([(cx - d, cy - d), (cx + d, cy + d)], fill=x_col, width=max(1, int(1.6 * scale)))
        draw.line([(cx - d, cy + d), (cx + d, cy - d)], fill=x_col, width=max(1, int(1.6 * scale)))

    def _draw_eq_badge(self, composite, draw, cx_ic, cy_ic, scale, acc_rgb, playing):
        r = max(9, int(11 * scale))
        alpha_ellipse(composite, [cx_ic - r, cy_ic - r, cx_ic + r, cy_ic + r], fill=(*acc_rgb, 52), outline=(*acc_rgb, 195), width=1)
        heights = (9, 13, 7) if playing else (5, 8, 5)
        step = max(3, int(4 * scale))
        for idx_b, bh in enumerate(heights):
            bh_s = max(4, int(bh * scale))
            bx = cx_ic - step + idx_b * step
            draw.line([(bx, cy_ic + bh_s // 2), (bx, cy_ic - bh_s // 2)], fill=(*acc_rgb, 255) if playing else (203, 213, 225, 215), width=max(2, int(2 * scale)))

    def _draw_speaker_btn(self, composite, draw, cx, cy, scale, acc_rgb):
        r = max(9, int(10 * scale))
        self.btn_boxes.append((int(cx - r), int(cy - r), int(cx + r), int(cy + r), "mute"))
        is_hov = (self.hovered_btn == "mute")
        if is_hov:
            alpha_ellipse(composite, [cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 32))
        col = (248, 113, 113, 245) if self._vol_muted else ((255, 255, 255, 255) if is_hov else (203, 213, 225, 230))
        u = max(1.0, scale)
        # Speaker cone body
        draw.rectangle([cx - int(5 * u), cy - int(2 * u), cx - int(3 * u), cy + int(2 * u)], fill=col)
        draw.polygon([
            (cx - int(3 * u), cy - int(2 * u)),
            (cx + int(1 * u), cy - int(5 * u)),
            (cx + int(1 * u), cy + int(5 * u)),
            (cx - int(3 * u), cy + int(2 * u)),
        ], fill=col)
        if self._vol_muted or self._vol_level <= 0.01:
            draw.line([(cx + int(3 * u), cy - int(2 * u)), (cx + int(7 * u), cy + int(2 * u))], fill=(248, 113, 113, 255), width=max(1, int(1.5 * u)))
            draw.line([(cx + int(3 * u), cy + int(2 * u)), (cx + int(7 * u), cy - int(2 * u))], fill=(248, 113, 113, 255), width=max(1, int(1.5 * u)))
        else:
            if self._vol_level > 0.10:
                draw.arc([cx - int(1 * u), cy - int(3 * u), cx + int(4 * u), cy + int(3 * u)], start=-55, end=55, fill=(*acc_rgb, 240), width=max(1, int(1.4 * u)))
            if self._vol_level > 0.50:
                draw.arc([cx - int(1 * u), cy - int(5 * u), cx + int(7 * u), cy + int(5 * u)], start=-55, end=55, fill=(*acc_rgb, 225), width=max(1, int(1.4 * u)))

    def _build_capsule_image(self):
        w, h, scale, btn_scale, top_area, btn_pos = self._compute_dims()
        style = (self.config_mgr.get("general", "media_capsule_style", "cristal") or "cristal").lower()
        btn_style = (self.config_mgr.get("general", "media_capsule_btn_style", "clasico") or "clasico").lower()
        opacity = max(0.15, min(1.0, float(self.config_mgr.get("general", "media_capsule_opacity", 0.90) or 0.90)))
        bg_custom = (self.config_mgr.get("general", "media_capsule_bg_color", "") or "").strip()
        acc_custom = (self.config_mgr.get("general", "media_capsule_accent_color", "") or "").strip()
        font_fam = self.config_mgr.get("general", "media_capsule_font_family", "Segoe UI") or "Segoe UI"
        font_sz = max(9, min(18, int(self.config_mgr.get("general", "media_capsule_font_size", 11) or 11)))
        title_hex = self.config_mgr.get("general", "media_capsule_title_color", "#f8fafc") or "#f8fafc"
        artist_hex = self.config_mgr.get("general", "media_capsule_artist_color", "#94a3b8") or "#94a3b8"

        st = self.media_state
        cur_sec, dur_sec, prog_ratio = self._get_live_progress()

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
        rad = min(22, int(20 * scale), (h - 4) // 2)
        # 2x supersampled background pill for silky smooth anti-aliased rounded corners & true alpha
        ss = 2
        bg_hi = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        d_hi = ImageDraw.Draw(bg_hi, "RGBA")
        d_hi.rounded_rectangle(
            [2 * ss, 2 * ss, (w - 3) * ss, (h - 3) * ss],
            radius=rad * ss,
            fill=bg_rgba,
            outline=out_rgba,
            width=(2 if style == "neon" else 1) * ss,
        )
        composite = bg_hi.resize((w, h), Image.LANCZOS)
        draw = ImageDraw.Draw(composite, "RGBA")

        f_title = resolve_styled_font(font_fam, max(9, int(font_sz * scale)), bold=True)
        f_sub = resolve_styled_font(font_fam, max(8, int((font_sz - 1.5) * scale)), bold=False)
        f_tiny = resolve_styled_font(font_fam, max(8, int(8.5 * scale)), bold=False)
        title_rgb = _hex_to_rgb(title_hex, (248, 250, 252))
        artist_rgb = _hex_to_rgb(artist_hex, (148, 163, 184))

        def _clip(text, font, max_w):
            out = text
            while out and draw.textlength(out, font=font) > max(24, max_w):
                out = out[:-1]
            return (out.rstrip() + "…") if out != text else out

        self.btn_boxes = []
        eff_bscale = scale * btn_scale
        r_play = max(10, int(14 * eff_bscale))
        r_side = max(9, int(12 * eff_bscale))
        btn_gap = r_play + r_side + max(4, int(5 * eff_bscale))

        title_h, sub_h, text_gap, text_block_h = self._calc_text_metrics(scale, font_sz)

        # Top-right Close button
        r_close = max(7, int(8 * scale))
        cx_close = w - int(14 * scale)
        cy_close = int(13 * scale)
        self._draw_close_btn(composite, draw, cx_close, cy_close, scale)

        top_cy = top_area // 2

        if btn_pos == "left":
            b1 = int(16 * scale) + r_side
            b2 = b1 + btn_gap
            b3 = b2 + btn_gap
            self._draw_ctrl_btn(composite, draw, "prev", b1, top_cy, eff_bscale, acc_rgb, st["playing"], btn_style)
            self._draw_ctrl_btn(composite, draw, "play_pause", b2, top_cy, eff_bscale, acc_rgb, st["playing"], btn_style)
            self._draw_ctrl_btn(composite, draw, "next", b3, top_cy, eff_bscale, acc_rgb, st["playing"], btn_style)
            eq_x = cx_close - r_close - max(14, int(16 * scale))
            self._draw_eq_badge(composite, draw, eq_x, top_cy, scale, acc_rgb, st["playing"])
            txt_x = b3 + r_side + int(10 * scale)
            max_tw = max(40, eq_x - int(16 * scale) - txt_x)
            title_y = top_cy - (sub_h + text_gap) / 2.0
            artist_y = top_cy + (title_h + text_gap) / 2.0
            draw.text((txt_x, title_y), _clip(st["title"], f_title, max_tw), fill=(*title_rgb, 255), font=f_title, anchor="lm")
            draw.text((txt_x, artist_y), _clip(st["artist"], f_sub, max_tw), fill=(*artist_rgb, 235), font=f_sub, anchor="lm")

        elif btn_pos == "center":
            pad_top = max(8, int(round(8 * scale)))
            pad_mid = max(6, int(round(7 * scale)))
            close_clear = (w - cx_close) + r_close + int(10 * scale)
            max_tw = max(60, w - 2 * close_clear)
            title_cy = pad_top + title_h / 2.0
            artist_cy = pad_top + title_h + text_gap + sub_h / 2.0
            draw.text((w / 2.0, title_cy), _clip(st["title"], f_title, max_tw), fill=(*title_rgb, 255), font=f_title, anchor="mm")
            draw.text((w / 2.0, artist_cy), _clip(st["artist"], f_sub, max_tw), fill=(*artist_rgb, 235), font=f_sub, anchor="mm")
            c_bscale = eff_bscale * 0.94
            r_play_c = max(10, int(14 * c_bscale))
            by = int(pad_top + text_block_h + pad_mid + r_play_c)
            gap_c = max(int(34 * scale), btn_gap + int(4 * scale))
            self._draw_ctrl_btn(composite, draw, "prev", w // 2 - gap_c, by, c_bscale, acc_rgb, st["playing"], btn_style)
            self._draw_ctrl_btn(composite, draw, "play_pause", w // 2, by, c_bscale, acc_rgb, st["playing"], btn_style)
            self._draw_ctrl_btn(composite, draw, "next", w // 2 + gap_c, by, c_bscale, acc_rgb, st["playing"], btn_style)

        elif btn_pos == "split":
            lx = int(16 * scale) + r_side
            rx = cx_close - r_close - max(6, int(6 * scale)) - r_side
            px = rx - btn_gap
            self._draw_ctrl_btn(composite, draw, "prev", lx, top_cy, eff_bscale, acc_rgb, st["playing"], btn_style)
            self._draw_ctrl_btn(composite, draw, "play_pause", px, top_cy, eff_bscale, acc_rgb, st["playing"], btn_style)
            self._draw_ctrl_btn(composite, draw, "next", rx, top_cy, eff_bscale, acc_rgb, st["playing"], btn_style)
            txt_x1 = lx + r_side + int(8 * scale)
            txt_x2 = px - r_play - int(8 * scale)
            tcx = (txt_x1 + txt_x2) / 2.0
            max_tw = max(40, txt_x2 - txt_x1)
            title_y = top_cy - (sub_h + text_gap) / 2.0
            artist_y = top_cy + (title_h + text_gap) / 2.0
            draw.text((tcx, title_y), _clip(st["title"], f_title, max_tw), fill=(*title_rgb, 255), font=f_title, anchor="mm")
            draw.text((tcx, artist_y), _clip(st["artist"], f_sub, max_tw), fill=(*artist_rgb, 235), font=f_sub, anchor="mm")

        else:
            # Default: "right"
            cx_ic = int(22 * scale)
            self._draw_eq_badge(composite, draw, cx_ic, top_cy, scale, acc_rgb, st["playing"])
            b3 = cx_close - r_close - max(6, int(6 * scale)) - r_side
            b2 = b3 - btn_gap
            b1 = b2 - btn_gap
            self._draw_ctrl_btn(composite, draw, "prev", b1, top_cy, eff_bscale, acc_rgb, st["playing"], btn_style)
            self._draw_ctrl_btn(composite, draw, "play_pause", b2, top_cy, eff_bscale, acc_rgb, st["playing"], btn_style)
            self._draw_ctrl_btn(composite, draw, "next", b3, top_cy, eff_bscale, acc_rgb, st["playing"], btn_style)
            txt_x = cx_ic + int(18 * scale)
            max_tw = max(40, b1 - r_side - int(8 * scale) - txt_x)
            title_y = top_cy - (sub_h + text_gap) / 2.0
            artist_y = top_cy + (title_h + text_gap) / 2.0
            draw.text((txt_x, title_y), _clip(st["title"], f_title, max_tw), fill=(*title_rgb, 255), font=f_title, anchor="lm")
            draw.text((txt_x, artist_y), _clip(st["artist"], f_sub, max_tw), fill=(*artist_rgb, 235), font=f_sub, anchor="lm")

        # Subtle hairline divider above bottom utility bar
        bar_pad = max(14, int(16 * scale))
        draw.line([(bar_pad, top_area), (w - bar_pad, top_area)], fill=(255, 255, 255, 22), width=1)

        # Bottom Utility Row: Interactive Progress Bar + Volume Control
        bar_y = h - max(10, int(11 * scale))
        pct_str = f"{int(round(self._vol_level * 100))}%"
        pct_w = max(int(26 * scale), int(draw.textlength("100%", font=f_tiny)) + 2)
        vol_track_w = max(34, min(64, int(w * 0.15)))
        vx2 = w - bar_pad - pct_w - int(4 * scale)
        vx1 = vx2 - vol_track_w
        spk_cx = vx1 - int(13 * scale)

        self._draw_speaker_btn(composite, draw, spk_cx, bar_y, scale, acc_rgb)
        self.volume_track = (vx1, vx2)
        self.volume_box = (vx1 - 4, bar_y - 10, vx2 + 4, bar_y + 10)

        vol_active = (self._drag_mode == "volume") or (self.hovered_btn == "volume") or (time.time() < self._vol_overlay_until)
        vth = max(4, int((5 if vol_active else 4) * scale))
        vy1, vy2 = bar_y - vth // 2, bar_y + vth // 2
        alpha_rounded_rect(composite, [vx1, vy1, vx2, vy2], radius=vth // 2, fill=(255, 255, 255, 34))
        v_ratio = 0.0 if self._vol_muted else max(0.0, min(1.0, self._vol_level))
        vfill_w = int((vx2 - vx1) * v_ratio)
        if vfill_w >= 2:
            vfill_col = (248, 113, 113, 210) if self._vol_muted else (*acc_rgb, 240 if vol_active else 210)
            alpha_rounded_rect(composite, [vx1, vy1, vx1 + vfill_w, vy2], radius=vth // 2, fill=vfill_col)
        v_knob_x = vx1 + vfill_w
        v_knob_r = max(3, int((4.5 if vol_active else 3.2) * scale))
        alpha_ellipse(
            composite,
            [v_knob_x - v_knob_r, bar_y - v_knob_r, v_knob_x + v_knob_r, bar_y + v_knob_r],
            fill=(255, 255, 255, 255 if vol_active else 220),
        )
        pct_alpha = 250 if vol_active else 175
        draw.text((w - bar_pad, bar_y), pct_str, fill=(*artist_rgb, pct_alpha), font=f_tiny, anchor="rm")

        # Left side: Time labels + Interactive Progress Bar
        t_cur_str = _fmt_time(cur_sec)
        is_live_no_dur = (dur_sec <= 0.0 and st.get("playing") and st.get("active"))
        if dur_sec > 0.0:
            t_dur_str = _fmt_time(dur_sec)
        elif is_live_no_dur:
            t_dur_str = "EN VIVO"
        else:
            t_dur_str = "0:00"

        cur_tw = int(draw.textlength(t_cur_str, font=f_tiny))
        dur_tw = int(draw.textlength(t_dur_str, font=f_tiny))
        draw.text((bar_pad, bar_y), t_cur_str, fill=(*artist_rgb, 220), font=f_tiny, anchor="lm")

        px1 = bar_pad + cur_tw + int(7 * scale)
        dur_rx = spk_cx - max(10, int(12 * scale)) - int(6 * scale)
        px2 = dur_rx - dur_tw - int(7 * scale)
        draw.text((dur_rx, bar_y), t_dur_str, fill=(*acc_rgb, 230) if is_live_no_dur else (*artist_rgb, 200), font=f_tiny, anchor="rm")

        if px2 > px1 + 14:
            self.progress_track = (px1, px2)
            self.progress_box = (px1 - 4, bar_y - 10, px2 + 4, bar_y + 10)
            seek_active = (self._drag_mode == "seek") or (self.hovered_btn == "seek")
            pth = max(4, int((5 if seek_active else 4) * scale))
            py1, py2 = bar_y - pth // 2, bar_y + pth // 2
            track_fill = (*acc_rgb, 60) if is_live_no_dur else (255, 255, 255, 34)
            alpha_rounded_rect(composite, [px1, py1, px2, py2], radius=pth // 2, fill=track_fill)
            pfill_w = int((px2 - px1) * prog_ratio)
            if pfill_w >= 2:
                alpha_rounded_rect(composite, [px1, py1, px1 + pfill_w, py2], radius=pth // 2, fill=(*acc_rgb, 248 if seek_active else 220))
            if dur_sec > 0.0 or seek_active:
                pkx = px1 + pfill_w
                pkr = max(3, int((4.8 if seek_active else 3.2) * scale))
                alpha_ellipse(
                    composite,
                    [pkx - pkr, bar_y - pkr, pkx + pkr, bar_y + pkr],
                    fill=(255, 255, 255, 255 if seek_active else 230),
                )
        else:
            self.progress_track = None
            self.progress_box = None

        return composite

    def _render(self, force=False):
        if not self.window or not self.canvas or not self._win_visible:
            return
        w, h, scale, btn_scale, _, btn_pos = self._compute_dims()
        style = (self.config_mgr.get("general", "media_capsule_style", "cristal") or "cristal").lower()
        btn_style = (self.config_mgr.get("general", "media_capsule_btn_style", "clasico") or "clasico").lower()
        opacity = max(0.15, min(1.0, float(self.config_mgr.get("general", "media_capsule_opacity", 0.90) or 0.90)))
        bg_custom = (self.config_mgr.get("general", "media_capsule_bg_color", "") or "").strip()
        acc_custom = (self.config_mgr.get("general", "media_capsule_accent_color", "") or "").strip()
        font_fam = self.config_mgr.get("general", "media_capsule_font_family", "Segoe UI") or "Segoe UI"
        font_sz = max(9, min(18, int(self.config_mgr.get("general", "media_capsule_font_size", 11) or 11)))
        title_hex = self.config_mgr.get("general", "media_capsule_title_color", "#f8fafc") or "#f8fafc"
        artist_hex = self.config_mgr.get("general", "media_capsule_artist_color", "#94a3b8") or "#94a3b8"

        st = self.media_state
        cur_sec, dur_sec, _ = self._get_live_progress()
        vol_ov = time.time() < self._vol_overlay_until
        sig = (
            w, h, style, btn_style, round(opacity, 2), round(btn_scale, 2), bg_custom, acc_custom,
            font_fam, font_sz, title_hex, artist_hex, btn_pos, st["active"], st["title"],
            st["artist"], st["playing"], int(cur_sec), int(dur_sec),
            round(self._vol_level, 2), self._vol_muted, vol_ov, self.hovered_btn, self._drag_mode,
        )
        if not force and sig == self._last_render_sig:
            return
        self._last_render_sig = sig

        composite = self._build_capsule_image()

        self.window.update_idletasks()
        hwnd = self.top_hwnd or user32.GetAncestor(self.window.winfo_id(), 2) or self.window.winfo_id()
        if not update_layered_pil(hwnd, self.win_x, self.win_y, composite):
            final_img = flatten_pil_for_colorkey(composite, (1, 2, 3))
            self.photo_ref = ImageTk.PhotoImage(final_img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo_ref)
