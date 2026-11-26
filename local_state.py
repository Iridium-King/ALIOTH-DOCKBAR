import copy
import ctypes
import threading
from ctypes import wintypes

_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel.CreateMutexW.restype = wintypes.HANDLE
_kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel.WaitForSingleObject.restype = wintypes.DWORD
_kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel.GetCurrentThreadId.restype = wintypes.DWORD
_user = ctypes.WinDLL('user32', use_last_error=True)
_user.GetThreadDesktop.argtypes = [wintypes.DWORD]
_user.GetThreadDesktop.restype = wintypes.HANDLE
_user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_thread_lock = threading.RLock()


def desktop_name():
    desktop = _user.GetThreadDesktop(_kernel.GetCurrentThreadId())
    buffer = ctypes.create_unicode_buffer(128)
    needed = wintypes.DWORD()
    if _user.GetUserObjectInformationW(desktop, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
        return ''.join(c if c.isalnum() else '_' for c in buffer.value) or 'Default'
    return 'Default'


def create_instance_mutex(name):
    handle = _kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        _kernel.CloseHandle(handle)
        return None
    return handle


def close_instance_mutex(handle):
    if handle:
        _kernel.CloseHandle(handle)


class InterProcessLock:
    def __init__(self, name='Local\\AliothTodoStore_Mutex', timeout=4000):
        self.name, self.timeout, self.handle = name, timeout, None

    def __enter__(self):
        if not _thread_lock.acquire(timeout=self.timeout / 1000):
            raise TimeoutError('Configuración ocupada; inténtalo de nuevo.')
        try:
            handle = _kernel.CreateMutexW(None, False, self.name)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            result = _kernel.WaitForSingleObject(handle, self.timeout)
            if result not in (0, 0x80):
                _kernel.CloseHandle(handle)
                raise TimeoutError('No se pudo bloquear la configuración.')
            self.handle = handle
            return self
        except BaseException:
            _thread_lock.release()
            raise

    def __exit__(self, *_):
        try:
            if self.handle:
                _kernel.ReleaseMutex(self.handle)
                _kernel.CloseHandle(self.handle)
                self.handle = None
        finally:
            _thread_lock.release()


def merge_changes(base, current, disk):
    """Apply local changes without replacing concurrent edits to other fields/IDs."""
    if current == base:
        return copy.deepcopy(disk)
    if all(isinstance(v, dict) for v in (base, current, disk)):
        result = copy.deepcopy(disk)
        for key in base.keys() - current.keys():
            result.pop(key, None)
        for key, value in current.items():
            if key not in base:
                result[key] = copy.deepcopy(value)
            elif key in disk:
                result[key] = merge_changes(base[key], value, disk[key])
            elif value != base[key]:
                result[key] = copy.deepcopy(value)
        return result
    if all(isinstance(v, list) for v in (base, current, disk)) and all(
        isinstance(item, dict) and item.get('id') for seq in (base, current, disk) for item in seq
    ):
        before = {item['id']: item for item in base}
        latest = {item['id']: item for item in disk}
        local_ids = {item['id'] for item in current}
        result = []
        for item in current:
            key = item['id']
            if key not in before:
                result.append(copy.deepcopy(latest.get(key, item)))
            elif key in latest:
                result.append(merge_changes(before[key], item, latest[key]))
        result.extend(copy.deepcopy(item) for item in disk if item['id'] not in before and item['id'] not in local_ids)
        return result
    return copy.deepcopy(current)
