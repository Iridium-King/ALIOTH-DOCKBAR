from collections import OrderedDict
import ctypes
import os
import sys
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
import uuid
from unittest.mock import patch
from types import SimpleNamespace

from PIL import Image, ImageDraw
from config import ConfigManager
from desktop_widgets import PwaWidget
from dock_window import DockWindow
import icon_extractor
from local_state import create_instance_mutex
import todo_store
from widget_runtime import WidgetRuntime
import window_utils


class Scheduler:
    def __init__(self):
        self.jobs = {}
        self.sequence = 0

    def after(self, delay, callback):
        self.sequence += 1
        self.jobs[self.sequence] = callback
        return self.sequence

    def after_cancel(self, job):
        self.jobs.pop(job, None)


class ScanTests(unittest.TestCase):
    def test_repeated_refresh_has_one_timer_and_no_closed_window_icons(self):
        dock = DockWindow.__new__(DockWindow)
        dock.root = Scheduler()
        dock.config_mgr = type('Config', (), {'get': lambda *_: True})()
        dock.items = []
        dock.pinned_items = [{'id': 'pinned'}]
        dock.icon_cache = {'pinned': Image.new('RGBA', (128, 128))}
        dock._scaled_icon_cache = OrderedDict()
        dock._dynamic_icon_cache = OrderedDict()
        dock._last_dynamic_sig = ()
        dock._last_running_sig = ()
        dock._rebuild_display_items = lambda: None
        dock.update_geometry = lambda: None
        with patch.object(icon_extractor, 'get_window_icon', side_effect=lambda *a, **k: Image.new('RGBA', (128, 128))):
            for index in range(50):
                dock._collect_open_windows = lambda i=index: (set(), [], [(i + 1, 'app.exe', str(i), '', str(i), '')])
                dock._scan_running_apps()
                self.assertEqual(len(dock.root.jobs), 1)
                self.assertEqual(len(dock.icon_cache), 2)
                self.assertEqual(len(dock._dynamic_icon_cache), 1)
                dyn_id = dock.dynamic_open_items[0]['id']
                dock._scaled_icon_cache[(dyn_id, 60)] = Image.new('RGBA', (60, 60))
            dock._collect_open_windows = lambda: (set(), [], [])
            dock._scan_running_apps()
        self.assertEqual(set(dock.icon_cache), {'pinned'})
        self.assertFalse(dock._dynamic_icon_cache)
        self.assertFalse(dock._scaled_icon_cache)

    def test_shell_com_initialization_is_balanced_on_failure(self):
        for initialized in (0, 1, -2147417850):
            with patch.object(icon_extractor.ole32, 'CoInitialize', return_value=initialized), \
                 patch.object(icon_extractor.ole32, 'CoUninitialize') as release, \
                 patch.object(icon_extractor.shell32, 'SHCreateItemFromParsingName', return_value=-1):
                self.assertIsNone(icon_extractor.extract_shell_image_factory('test'))
                self.assertEqual(release.call_count, int(initialized in (0, 1)))


class ImageResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.config = ConfigManager(os.path.join(self.temp.name, 'config.json'))
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_pwa_cache_contains_only_display_sized_images_and_clears_on_hide(self):
        source = os.path.join(self.temp.name, 'large.png')
        Image.new('RGBA', (1024, 1024), 'blue').save(source)
        widget = PwaWidget(self.root, self.config)
        for index in range(40):
            icon = widget._get_pwa_icon({'id': str(index), 'icon_src': source}, 48)
            self.assertEqual(icon.size, (48, 48))
        self.assertLessEqual(len(widget._icon_cache), 32)
        widget.hide(animate=False)
        self.assertFalse(widget._icon_cache)
        self.assertIsNone(widget.photo_ref)

    def test_large_layered_surface_has_identical_pixels(self):
        top = tk.Toplevel(self.root)
        self.addCleanup(top.destroy)
        handle = window_utils.set_tool_window(top, layered=True)
        self.addCleanup(lambda: window_utils.release_layered_surface(handle))
        top.geometry('530x515+0+0')
        top.update_idletasks()
        image = Image.new('RGBA', (530, 515), (120, 40, 200, 80))
        draw = ImageDraw.Draw(image)
        for y in range(515):
            draw.line((0, y, 529, y), fill=(y % 256, 40, 200, (y * 3) % 256))
        self.assertTrue(window_utils.update_layered_pil(handle, 0, 0, image))
        entry = window_utils._surface_cache[handle]
        actual = ctypes.string_at(entry[5], 530 * 515 * 4)
        self.assertEqual(actual, image.convert('RGBa').tobytes('raw', 'BGRa'))

    def test_shared_runtime_imports_external_tasks_and_releases_windows(self):
        import time
        unique = 'Local\\AliothWidgetsTest_' + uuid.uuid4().hex
        with patch('widget_runtime.create_instance_mutex', side_effect=lambda _: create_instance_mutex(unique)):
            runtime = WidgetRuntime(self.root, self.config)
        self.addCleanup(runtime.close)
        self.assertTrue(runtime.active)
        self.assertIs(runtime.root, self.root)
        self.assertIs(runtime.config, self.config)
        added = todo_store.add_task('External change', config_path=self.config.config_path)
        deadline = time.monotonic() + .6
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        notebooks = runtime.manager.widgets['todo_list'].get_cfg('notebooks')
        self.assertTrue(any(task['id'] == added['task']['id'] for nb in notebooks for task in nb['tasks']))
        runtime.close()
        self.assertIsNone(runtime.manager)
        self.assertIsNone(runtime.job)
        self.assertIsNone(runtime.mutex)


class SharedDockTests(unittest.TestCase):
    def test_main_shutdown_leaves_no_tk_timers_or_widget_runtime(self):
        output, errors = sys.stdout, sys.stderr
        try:
            import main
        finally:
            sys.stdout, sys.stderr = output, errors
        import time
        import settings_server
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            config = ConfigManager(os.path.join(directory, 'config.json'))
            config.set('general', 'start_with_windows', False)
            created = []

            def make_dock(*args, **kwargs):
                dock = DockWindow(*args, **kwargs)
                created.append(dock)
                dock.root.after(600, dock.close)
                return dock

            unique = 'Local\\AliothMainTest_' + uuid.uuid4().hex
            with patch('config.ConfigManager', return_value=config), \
                 patch('dock_window.DockWindow', side_effect=make_dock), \
                 patch.object(DockWindow, '_collect_open_windows', return_value=(set(), [], [])), \
                 patch.object(settings_server, 'SERVER_PORT', 0), \
                 patch('widget_runtime.create_instance_mutex', side_effect=lambda _: create_instance_mutex(unique)):
                self.assertTrue(main.run_dock())
            self.assertFalse(created[0].root.tk.call('after', 'info'))
            self.assertIsNone(settings_server._server_instance)

    def test_shared_root_closes_widgets_and_loads_media_only_when_enabled(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            config = ConfigManager(os.path.join(directory, 'config.json'))
            config.set('general', 'media_capsule_enabled', False)

            class Media:
                def __init__(self, dock):
                    self.dock = dock
                    self.window = self.hwnd = None
                    self.enabled = False

                def sync_visibility(self):
                    self.enabled = self.dock.config_mgr.get('general', 'media_capsule_enabled', False)

                def hide(self):
                    self.enabled = False

                def sync_z_order(self, *_):
                    pass

            unique = 'Local\\AliothSharedRootTest_' + uuid.uuid4().hex
            with patch.object(DockWindow, '_collect_open_windows', return_value=(set(), [], [])), \
                 patch.dict(sys.modules, {'media_capsule': SimpleNamespace(MediaCapsule=Media)}), \
                 patch('widget_runtime.create_instance_mutex', side_effect=lambda _: create_instance_mutex(unique)):
                dock = DockWindow(config)
                try:
                    self.assertIsNone(dock.media_capsule)
                    runtime = WidgetRuntime(dock.root, config)
                    dock.on_before_close = runtime.close
                    self.assertIs(runtime.root, dock.root)
                    self.assertTrue(runtime.manager.widgets['todo_list'].window.winfo_exists())
                    config.set('general', 'media_capsule_enabled', True)
                    dock.update_geometry()
                    self.assertTrue(dock.media_capsule.enabled)
                    config.set('general', 'media_capsule_enabled', False)
                    dock.update_geometry()
                    self.assertFalse(dock.media_capsule.enabled)
                    handle = dock.hwnd
                    dock.close()
                    self.assertFalse(runtime.active)
                    self.assertNotIn(handle, window_utils._surface_cache)
                finally:
                    if not dock.user_wants_exit:
                        dock.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
