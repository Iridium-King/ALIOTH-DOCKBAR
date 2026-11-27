import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import ConfigManager
from desktop_widgets import DesktopWidgetManager
from group_flyout import GroupFlyout
from settings_window import SettingsWindow
from window_utils import _surface_cache


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.manager = ConfigManager(os.path.join(self.temp.name, 'config.json'))
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.addCleanup(self.root.destroy)

    def pump(self, duration=.22):
        end = time.monotonic() + duration
        while time.monotonic() < end:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.errors, self.errors)

    def test_folder_open_and_close_releases_surface(self):
        folder = GroupFlyout(self.root, {'id': 'test', 'name': 'Test', 'items': []}, (10, 10, 60, 60))
        self.pump()
        handle = folder.top_hwnd
        self.assertTrue(folder.window.winfo_exists())
        folder.close()
        folder.close()
        self.pump()
        self.assertIsNone(folder.window)
        self.assertNotIn(handle, _surface_cache)

    def test_folder_icon_size_and_large_label_font_resize_flyout(self):
        self.manager.set('appearance', 'folder_icon_size', 84)
        self.manager.set('appearance', 'folder_label_font_size', 24)
        folder = GroupFlyout(
            self.root,
            {'id': 'grp', 'name': 'Apps', 'items': [{'id': 'c1', 'name': 'Documento Muy Largo', 'target': ''}]},
            (10, 10, 60, 60),
            config_mgr=self.manager,
        )
        self.addCleanup(folder.close)
        self.pump()
        self.assertEqual(folder._icon_sizes()[0], 84)
        self.assertEqual(folder._label_style()[2], 24)
        self.assertGreaterEqual(folder.cell_w, 84 + 48)
        self.assertGreaterEqual(folder.cell_h, 11 + 84 + 18)
        self.manager.set('appearance', 'folder_icon_size', 40)
        folder.refresh_style()
        self.pump()
        self.assertEqual(folder._icon_sizes()[0], 40)

    def test_widget_minimize_restore_and_close(self):
        widgets = DesktopWidgetManager(self.root, self.manager)
        self.addCleanup(widgets.close)
        self.pump()
        widget = widgets.widgets['todo_list']
        widget.show()
        self.pump()
        widget._toggle_minimized()
        self.pump(.4)
        self.assertTrue(widget.get_cfg('minimized'))
        widget._toggle_minimized()
        self.pump(.4)
        self.assertFalse(widget.get_cfg('minimized'))
        widget.hide()
        self.pump()
        self.assertIsNone(widget.window)
        widgets.close()
        self.assertFalse(self.errors)

    def test_settings_open_maximize_minimize_restore_close(self):
        panel = SettingsWindow(self.manager)
        self.pump()
        panel.root.state('zoomed')
        self.pump()
        self.assertEqual(panel.root.state(), 'zoomed')
        panel.root.iconify()
        self.pump()
        self.assertEqual(panel.root.state(), 'iconic')
        panel.root.deiconify()
        panel.root.state('normal')
        self.pump()
        self.assertTrue(panel.root.winfo_exists())
        panel._save_and_close()
        self.pump()
        self.assertFalse(panel.root.winfo_exists())

    def test_widget_hide_can_be_interrupted_by_show(self):
        widgets = DesktopWidgetManager(self.root, self.manager)
        self.addCleanup(widgets.close)
        widget = widgets.widgets['todo_list']
        widget.show()
        self.pump()
        widget.hide()
        self.pump(.04)
        widget.show()
        self.pump()
        self.assertTrue(widget.window.winfo_exists())
        self.assertFalse(widget._hiding)

    def test_unrelated_settings_do_not_redraw_todo(self):
        widgets = DesktopWidgetManager(self.root, self.manager)
        self.addCleanup(widgets.close)
        todo = widgets.widgets['todo_list']
        todo.show()
        self.pump()
        with patch.object(todo, '_redraw_now', wraps=todo._redraw_now) as redraw:
            self.manager.set_widget('clock', 'font_size', 20)
            widgets.live_update()
            self.assertEqual(redraw.call_count, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
