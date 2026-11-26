import argparse
import ctypes
from ctypes import wintypes
import gc
import json
from pathlib import Path
import sys
import time
import tkinter as tk
import tracemalloc
from unittest.mock import patch
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import app_launcher
from dock_window import _process_memory_mb
import window_utils


def run(cycles=12, fallback=False, trace=False):
    if trace:
        tracemalloc.start(8)
    saved_catalog = app_launcher._CATALOG_CACHE, app_launcher._CATALOG_TS
    saved_thumbs = app_launcher._THUMB_CACHE.copy()
    root = tk.Tk()
    root.withdraw()
    refs, errors, samples = [], [], {}
    root.report_callback_exception = lambda *error: errors.append(str(error[1]))
    top_level = tk.Toplevel

    def hidden_window(*args, **kwargs):
        window = top_level(*args, **kwargs)
        window.withdraw()
        return window

    apps = [{'name': f'Test {i}', 'target': f'test-{i}.exe', 'args': '', 'icon_src': ''} for i in range(16)]
    app_launcher._CATALOG_CACHE = apps
    app_launcher._CATALOG_TS = time.monotonic()
    app_launcher._THUMB_CACHE.update({(app['name'], app['target']): Image.new('RGBA', (50, 50), (40, 90, 150, 255)) for app in apps})
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    gui = ctypes.windll.user32.GetGuiResources
    gui.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    def sample():
        gc.collect()
        private, working, _ = _process_memory_mb()
        return {
            'ram_mb': round(working, 2), 'private_mb': round(private, 2),
            'closed_panels_retained': sum(ref() is not None for ref in refs),
            'tk_images': len(root.tk.call('image', 'names')),
            'search_variables': len(root.tk.call('info', 'vars', 'PY_VAR*')),
            'surfaces': len(window_utils._surface_cache),
            'gdi_handles': gui(kernel.GetCurrentProcess(), 0),
            'user_handles': gui(kernel.GetCurrentProcess(), 1),
        }

    state = {'index': -1, 'panel': None}

    def create():
        state['panel'] = app_launcher.CustomAppLauncher(root)
        refs.append(weakref.ref(state['panel']))
        root.after(160, close)

    def close():
        state['panel'].close()
        root.after(200, settle)

    def settle():
        state['panel'] = None
        if state['index'] == -1:
            samples['warm'] = sample()
            if trace:
                state['trace_warm'] = tracemalloc.take_snapshot()
        state['index'] += 1
        if state['index'] < cycles:
            root.after(1, create)
        else:
            samples['after'] = sample()
            if trace:
                samples['python_allocations'] = [str(stat) for stat in tracemalloc.take_snapshot().compare_to(state['trace_warm'], 'lineno')[:10]]
            root.quit()

    patches = [patch.object(app_launcher.tk, 'Toplevel', new=hidden_window)]
    if fallback:
        patches.append(patch.object(app_launcher, 'update_layered_pil', new=lambda *_args, **_kwargs: False))
    for item in patches:
        item.start()
    try:
        root.after(1, create)
        watchdog = root.after(max(20000, (cycles + 1) * 1000), root.quit)
        root.mainloop()
        samples['errors'] = errors
        samples['cycles'] = cycles
        samples['fallback'] = fallback
        return samples
    finally:
        root.after_cancel(watchdog)
        for item in reversed(patches):
            item.stop()
        for ref in refs:
            panel = ref()
            variable = getattr(panel, 'search_var', None)
            if variable is not None:
                for mode, token in variable.trace_info():
                    variable.trace_remove(mode, token)
            if panel is not None:
                panel.photo_ref = None
        root.destroy()
        root = None
        app_launcher._CATALOG_CACHE, app_launcher._CATALOG_TS = saved_catalog
        app_launcher._THUMB_CACHE.clear()
        app_launcher._THUMB_CACHE.update(saved_thumbs)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cycles', type=int, default=12)
    parser.add_argument('--fallback', action='store_true')
    parser.add_argument('--trace', action='store_true')
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.cycles, arguments.fallback, arguments.trace)))
