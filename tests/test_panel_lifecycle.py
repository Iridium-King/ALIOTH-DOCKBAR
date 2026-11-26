import gc
from contextlib import contextmanager
from pathlib import Path
import sys
import tkinter as tk
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from group_flyout import GroupFlyout
import custom_menu
import app_launcher
import icon_extractor
import panel_memory_probe
from window_preview import WindowPreviewFlyout


@contextmanager
def launcher_fixture():
    root = tk.Tk()
    root.withdraw()
    top_level = tk.Toplevel
    saved = app_launcher._CATALOG_CACHE, app_launcher._CATALOG_TS, app_launcher._THUMB_CACHE.copy()
    ready = []

    def hidden(*args, **kwargs):
        window = top_level(*args, **kwargs)
        window.withdraw()
        return window

    first = {'name': 'First', 'target': 'first.exe', 'icon_src': ''}
    app_launcher._CATALOG_CACHE = [first]
    app_launcher._THUMB_CACHE[('First', 'first.exe')] = Image.new('RGBA', (50, 50))
    launcher = None
    try:
        with patch.object(app_launcher.tk, 'Toplevel', new=hidden), \
             patch.object(app_launcher, 'warm_launcher_cache', new=lambda **kwargs: ready.append(kwargs['on_ready'])):
            launcher = app_launcher.CustomAppLauncher(root)
            yield root, launcher, ready
    finally:
        if launcher:
            launcher.close(animate=False)
        root.destroy()
        app_launcher._CATALOG_CACHE, app_launcher._CATALOG_TS = saved[:2]
        app_launcher._THUMB_CACHE.clear()
        app_launcher._THUMB_CACHE.update(saved[2])
        root = None
        gc.collect()


class PanelLifecycleTests(unittest.TestCase):
    def test_catalog_completion_from_worker_keeps_search_working(self):
        with launcher_fixture() as (root, launcher, ready):
            updated = {'name': 'Updated', 'target': 'updated.exe', 'icon_src': ''}
            app_launcher._CATALOG_CACHE = [updated]
            app_launcher._THUMB_CACHE[('Updated', 'updated.exe')] = Image.new('RGBA', (50, 50))
            worker = threading.Thread(target=ready[0])
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(launcher._ui_queue.empty())
            root.after_cancel(launcher._check_job)
            launcher._check_job = None
            launcher._poll_outside_click()
            self.assertEqual(launcher.all_apps, [updated])
            launcher.search_var.set('Updated')
            self.assertEqual(launcher.filtered_apps, [updated])
            launcher.search_var.set('not found')
            self.assertFalse(launcher.filtered_apps)

    def test_immediate_close_cancels_launcher_callbacks(self):
        with launcher_fixture() as (root, launcher, ready):
            launcher.close(animate=False)
            launcher.close(animate=False)
            self.assertFalse(root.tk.call('after', 'info'))
            self.assertIsNone(launcher.parent)
            self.assertIsNone(launcher.search_var)
            self.assertIsNone(launcher.photo_ref)
            ready[0]()
            self.assertFalse(root.tk.call('after', 'info'))

    def test_launcher_repeated_close_releases_tk_objects(self):
        result = panel_memory_probe.run(cycles=4, fallback=True)
        gc.collect()
        self.assertFalse(result['errors'])
        self.assertEqual(result['after']['closed_panels_retained'], 0)
        self.assertEqual(result['after']['tk_images'], result['warm']['tk_images'])
        self.assertEqual(result['after']['search_variables'], 0)
        self.assertEqual(result['after']['gdi_handles'], result['warm']['gdi_handles'])
        self.assertEqual(result['after']['surfaces'], 0)

    def test_folder_discards_original_without_changing_variants(self):
        image = Image.frombytes('RGBA', (128, 128), bytes(range(256)) * 256)
        folder = GroupFlyout.__new__(GroupFlyout)
        folder.group_item = {'items': [{'id': 'item'}]}
        folder.child_icons = {'item': image.copy()}
        folder._icon_variants = {}
        normal = folder._icon_variant('item', folder.ICON_SZ)
        hover = folder._icon_variant('item', folder.ICON_SZ_HOVER)
        self.assertNotIn('item', folder.child_icons)
        self.assertEqual(normal[0].tobytes(), image.resize((56, 56), Image.Resampling.LANCZOS).tobytes())
        self.assertEqual(hover[0].tobytes(), image.resize((62, 62), Image.Resampling.LANCZOS).tobytes())
        self.assertIs(folder._icon_variant('item', 56), normal)
        self.assertIs(folder._icon_variant('item', 62), hover)
        with patch.object(icon_extractor, 'get_or_create_icon', return_value=image.copy()) as load:
            extra = folder._icon_variant('item', 80)
        self.assertEqual(extra[0].size, (80, 80))
        self.assertEqual(load.call_count, 1)

    def test_menu_reuse_does_not_keep_fallback_bitmaps(self):
        root = tk.Tk()
        root.withdraw()
        top_level = tk.Toplevel

        def hidden(*args, **kwargs):
            window = top_level(*args, **kwargs)
            window.withdraw()
            return window

        menu = custom_menu.CustomMenu(root)
        menu.add_command('Test', lambda: None)
        try:
            initial_images = len(root.tk.call('image', 'names'))
            with patch.object(custom_menu.tk, 'Toplevel', new=hidden), \
                 patch.object(custom_menu, 'update_layered_pil', new=lambda *_: False):
                for _ in range(4):
                    menu.show(50, 50)
                    self.assertIsNotNone(menu.photo_ref)
                    menu.close()
                    self.assertIsNone(menu.canvas)
                    self.assertIsNone(menu.photo_ref)
                    self.assertEqual(len(root.tk.call('image', 'names')), initial_images)
            self.assertFalse(custom_menu.CustomMenu.has_active_menu())
        finally:
            menu.close()
            root.destroy()
            del menu, root
            gc.collect()

    def test_folder_and_preview_immediate_close_release_timers(self):
        root = tk.Tk()
        root.withdraw()
        top_level = tk.Toplevel

        def hidden(*args, **kwargs):
            window = top_level(*args, **kwargs)
            window.withdraw()
            return window

        folder = preview = None
        try:
            with patch.object(tk, 'Toplevel', new=hidden):
                folder = GroupFlyout(root, {'id': 'test', 'items': []}, (0, 0, 50, 50))
                folder._close_child_windows({})
                previous = folder._refresh_job
                folder._close_child_windows({})
                self.assertNotIn(previous, root.tk.call('after', 'info'))
                folder.close(animate=False)
                self.assertIsNone(folder._refresh_job)
                self.assertFalse(folder._font_cache)
                with patch.object(WindowPreviewFlyout, '_register_thumbnails', new=lambda self: None):
                    preview = WindowPreviewFlyout(root, {'name': 'Test'}, [{'hwnd': 0, 'title': 'Test'}], (0, 0, 50, 50))
                preview.close(animate=False)
                self.assertIsNone(preview.canvas)
                self.assertIsNone(preview.photo_ref)
                self.assertFalse(preview._font_cache)
                self.assertFalse(root.tk.call('after', 'info'))
        finally:
            if folder:
                folder.close(animate=False)
            if preview:
                preview.close(animate=False)
            root.destroy()
            del folder, preview, root
            gc.collect()


if __name__ == '__main__':
    unittest.main(verbosity=2)
