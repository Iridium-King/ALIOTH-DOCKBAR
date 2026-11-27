"""
Global Hotkey Manager for Alioth Dockbar.
Uses standard Win32 RegisterHotKey API with a dedicated hidden message window.
100% compliant with enterprise security: zero background powershell, zero global hooks.
"""
import ctypes
from ctypes import wintypes
import threading
import time
import win32gui
import win32con

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Modifiers
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# Virtual Keys
VK_SPACE = 0x20
VK_A = ord('A')
VK_F = ord('F')

class GlobalHotkeyManager:
    """
    Spawns a clean background thread running a native Win32 message loop
    to capture RegisterHotKey messages and dispatch callbacks safely to the UI queue.
    """

    def __init__(self, on_spotlight=None, on_launchpad=None):
        self.on_spotlight = on_spotlight
        self.on_launchpad = on_launchpad
        self._thread = None
        self._hwnd = None
        self._running = False
        self._thread_id = None
        self._hotkey_ids = {}

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_hotkey_loop, daemon=True, name="AliothHotkeyListener")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._hwnd:
            try:
                user32.PostMessageW(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread_id:
            try:
                user32.PostThreadMessageW(self._thread_id, win32con.WM_QUIT, 0, 0)
            except Exception:
                pass

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_HOTKEY:
            hk_id = wparam
            if hk_id == 1001:  # Spotlight
                if callable(self.on_spotlight):
                    self.on_spotlight()
                return 0
            elif hk_id == 1002:  # Launchpad
                if callable(self.on_launchpad):
                    self.on_launchpad()
                return 0
        elif msg == win32con.WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run_hotkey_loop(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        
        # Register Window Class for hidden message receiver
        wnd_class = "AliothHotkeyReceiverWnd"
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wnd_proc
        wc.lpszClassName = wnd_class
        wc.hInstance = kernel32.GetModuleHandleW(None)
        
        try:
            atom = win32gui.RegisterClass(wc)
        except Exception:
            # Class already registered in previous run
            pass

        self._hwnd = win32gui.CreateWindowEx(
            0,
            wnd_class,
            "AliothHotkeyReceiver",
            0,
            0, 0, 0, 0,
            0, 0,
            kernel32.GetModuleHandleW(None),
            None
        )

        if not self._hwnd:
            print("[Hotkeys] Error creando ventana receptora de atajos.")
            return

        # Register Spotlight Hotkey
        # Primary: Win + Alt + Space (or Alt + Space fallback)
        spotlight_registered = False
        candidates_spotlight = [
            (MOD_WIN | MOD_ALT, VK_SPACE, "Win+Alt+Space"),
            (MOD_ALT, VK_SPACE, "Alt+Space"),
            (MOD_CONTROL | MOD_SHIFT, VK_SPACE, "Ctrl+Shift+Space")
        ]
        for mod, vk, name in candidates_spotlight:
            res = user32.RegisterHotKey(self._hwnd, 1001, mod | MOD_NOREPEAT, vk)
            if res:
                print(f"[Hotkeys] Atajo Spotlight activado: {name}")
                self._hotkey_ids[1001] = (mod, vk)
                spotlight_registered = True
                break

        # Register Launchpad Hotkey
        # Primary: Win + Alt + A (or Ctrl + Alt + Space)
        launchpad_registered = False
        candidates_launchpad = [
            (MOD_WIN | MOD_ALT, VK_A, "Win+Alt+A"),
            (MOD_CONTROL | MOD_ALT, VK_SPACE, "Ctrl+Alt+Space"),
            (MOD_WIN | MOD_SHIFT, VK_F, "Win+Shift+F")
        ]
        for mod, vk, name in candidates_launchpad:
            res = user32.RegisterHotKey(self._hwnd, 1002, mod | MOD_NOREPEAT, vk)
            if res:
                print(f"[Hotkeys] Atajo Launchpad activado: {name}")
                self._hotkey_ids[1002] = (mod, vk)
                launchpad_registered = True
                break

        # Win32 Message Loop
        msg = wintypes.MSG()
        while self._running:
            b_ret = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if b_ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # Cleanup Hotkeys
        for hk_id in list(self._hotkey_ids.keys()):
            user32.UnregisterHotKey(self._hwnd, hk_id)
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        print("[Hotkeys] Loop de atajos finalizado.")
