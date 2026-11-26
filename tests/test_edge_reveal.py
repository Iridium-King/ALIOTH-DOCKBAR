import sys
import time
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dock_window as module
from config import ConfigManager


class EdgeRevealTests(unittest.TestCase):
    def make_dock(self, revealed=True, at_edge=True, covered=True):
        dock = module.DockWindow.__new__(module.DockWindow)
        dock.hwnd = 123
        dock.root = SimpleNamespace(winfo_screenwidth=lambda: 1920,
                                    winfo_screenheight=lambda: 1080, after=Mock(), attributes=Mock())
        dock.config_mgr = SimpleNamespace(get=lambda section, key, default=None: default)
        dock.is_revealed = revealed
        dock._prev_btn_down = False
        dock._btn_down_pt = (0, 0)
        dock.mouse_inside_dock = False
        dock.active_flyout = dock.active_preview = dock.active_launcher = None
        dock.media_capsule = None
        dock.items = []
        dock._prev_fg_hwnd = 456
        dock.last_leave_time = time.time()
        dock._idle_trimmed = True
        dock._inspect_desktop_zorder = Mock(return_value=(789, False))
        dock._is_window_above = Mock(return_value=covered)
        dock._find_lowest_app_above_desktop = Mock(return_value=456)
        dock._place_above_desktop_below_apps = Mock()
        dock._tuck_behind_windows = Mock()
        api = Mock()

        def cursor(point):
            point._obj.x, point._obj.y = 960, 1079 if at_edge else 500
            return True

        def window_class(hwnd, buffer, size):
            buffer.value = 'TestFullscreen'
            return len(buffer.value)

        api.GetCursorPos.side_effect = cursor
        api.GetClassNameW.side_effect = window_class
        api.GetAsyncKeyState.return_value = 0
        api.WindowFromPoint.return_value = 456
        api.GetAncestor.return_value = 456
        api.GetForegroundWindow.return_value = 456
        api.IsIconic.return_value = False
        api.IsWindowVisible.return_value = True
        api.GetWindowLongW.return_value = 0
        return dock, api

    def poll(self, dock, api):
        with patch.object(module, 'user32', api), \
             patch.object(module.CustomMenu, 'has_active_menu', return_value=False):
            dock._check_edge_sensor()

    def test_covered_revealed_dock_recovers_at_edge_without_activation(self):
        dock, api = self.make_dock()
        self.poll(dock, api)
        api.SetWindowPos.assert_called_once()
        args = api.SetWindowPos.call_args.args
        self.assertEqual(args[:2], (dock.hwnd, module.HWND_TOPMOST))
        self.assertTrue(args[-1] & module.SWP_NOACTIVATE)
        dock.root.attributes.assert_called_with('-topmost', True)
        dock._tuck_behind_windows.assert_not_called()

    def test_hidden_dock_reveals_at_edge(self):
        dock, api = self.make_dock(revealed=False, covered=False)
        self.poll(dock, api)
        self.assertTrue(dock.is_revealed)
        api.SetWindowPos.assert_called_once()

    def test_edge_reasserts_priority_without_relying_on_foreground_order(self):
        dock, api = self.make_dock(covered=False)
        self.poll(dock, api)
        api.SetWindowPos.assert_called_once()

    def test_edge_recovers_when_dock_is_still_foreground(self):
        dock, api = self.make_dock(covered=False)
        api.GetForegroundWindow.return_value = dock.hwnd
        self.poll(dock, api)
        api.SetWindowPos.assert_called_once()

    def test_edge_recovers_when_menu_is_open_elsewhere(self):
        dock, api = self.make_dock()
        with patch.object(module, 'user32', api), \
             patch.object(module.CustomMenu, 'has_active_menu', return_value=True), \
             patch.object(module.CustomMenu, 'is_point_inside_any', return_value=False):
            dock._check_edge_sensor()
        api.SetWindowPos.assert_called_once()

    def test_edge_does_not_cover_menu_under_pointer(self):
        dock, api = self.make_dock()
        with patch.object(module, 'user32', api), \
             patch.object(module.CustomMenu, 'has_active_menu', return_value=True), \
             patch.object(module.CustomMenu, 'is_point_inside_any', return_value=True):
            dock._check_edge_sensor()
        api.SetWindowPos.assert_not_called()

    def test_covered_dock_does_not_raise_away_from_edge(self):
        dock, api = self.make_dock(at_edge=False)
        self.poll(dock, api)
        api.SetWindowPos.assert_not_called()

    def test_recovery_uses_existing_scan_interval(self):
        dock, api = self.make_dock()
        dock._last_z_scan_ts = time.monotonic()
        self.poll(dock, api)
        api.SetWindowPos.assert_not_called()

    def test_autohide_still_tucks_after_leaving_edge(self):
        dock, api = self.make_dock(at_edge=False)
        dock.last_leave_time -= 10
        self.poll(dock, api)
        dock._tuck_behind_windows.assert_called_once_with(reason='delay')

    def test_desktop_placement_syncs_tk_without_activating(self):
        dock, api = self.make_dock(revealed=False, at_edge=False)
        dock._find_lowest_app_above_desktop.return_value = 0
        with patch.object(module, 'user32', api):
            module.DockWindow._place_above_desktop_below_apps(dock)
        dock.root.attributes.assert_called_with('-topmost', True)
        self.assertTrue(api.SetWindowPos.call_args.args[-1] & module.SWP_NOACTIVATE)
        self.assertFalse(dock.is_revealed)

    def test_desktop_recovery_syncs_tk_when_shell_covers_dock(self):
        dock, api = self.make_dock(at_edge=False)
        dock._inspect_desktop_zorder.return_value = (789, True)
        with patch.object(module, 'user32', api):
            dock._ensure_visible_on_desktop()
        dock.root.attributes.assert_called_with('-topmost', True)
        self.assertTrue(api.SetWindowPos.call_args.args[-1] & module.SWP_NOACTIVATE)

    def test_show_desktop_foreground_change_syncs_tk(self):
        dock, api = self.make_dock(revealed=False, at_edge=False)
        dock._prev_fg_hwnd = 999
        dock._find_lowest_app_above_desktop.return_value = 0
        api.GetClassNameW.side_effect = lambda hwnd, buffer, size: setattr(buffer, 'value', 'WorkerW')
        self.poll(dock, api)
        dock.root.attributes.assert_called_with('-topmost', True)

    def test_normal_app_placement_clears_tk_priority(self):
        dock, api = self.make_dock(revealed=False, at_edge=False)
        with patch.object(module, 'user32', api):
            module.DockWindow._place_above_desktop_below_apps(dock)
        dock.root.attributes.assert_called_with('-topmost', False)
        self.assertEqual(api.SetWindowPos.call_args.args[1], 456)


class NativeEdgeRevealTests(unittest.TestCase):
    def test_minimized_dock_recovers_on_desktop_without_focus_change(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
             patch.object(module.DockWindow, '_collect_open_windows', return_value=(set(), [], [])):
            config = ConfigManager(str(Path(directory) / 'config.json'))
            config.set('general', 'media_capsule_enabled', False)
            dock = module.DockWindow(config)
            try:
                dock.root.update()
                for job in dock.root.tk.call('after', 'info'):
                    dock.root.after_cancel(job)
                dock.is_revealed = False
                dock.root.attributes('-topmost', False)
                module.user32.ShowWindow(dock.hwnd, 6)
                dock.root.update()
                self.assertTrue(module.user32.IsIconic(dock.hwnd))
                before = module.user32.GetForegroundWindow()
                with patch.object(dock, '_find_lowest_app_above_desktop', return_value=0):
                    dock._ensure_visible_on_desktop()
                self.assertFalse(module.user32.IsIconic(dock.hwnd))
                self.assertTrue(module.user32.IsWindowVisible(dock.hwnd))
                self.assertTrue(dock.root.attributes('-topmost'))
                self.assertTrue(module.user32.GetWindowLongW(dock.hwnd, module.GWL_EXSTYLE) & 8)
                self.assertEqual(module.user32.GetForegroundWindow(), before)
            finally:
                dock.close()

    def test_open_menu_does_not_undo_reveal_or_break_autohide(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
             patch.object(module.DockWindow, '_collect_open_windows', return_value=(set(), [], [])):
            config = ConfigManager(str(Path(directory) / 'config.json'))
            config.set('general', 'media_capsule_enabled', False)
            dock = module.DockWindow(config)
            cover = tk.Tk()
            menu = module.CustomMenu(dock.root)
            try:
                menu.add_command('Test', lambda: None)
                menu.show(100, 300)
                native_foreground = module.user32.GetForegroundWindow
                for fullscreen in (False, True):
                    with self.subTest(fullscreen=fullscreen):
                        cover.attributes('-fullscreen', fullscreen)
                        if not fullscreen:
                            cover.state('zoomed')
                        cover.update()
                        dock.root.update()
                        for job in dock.root.tk.call('after', 'info'):
                            dock.root.after_cancel(job)
                        handle = module.user32.GetAncestor(cover.winfo_id(), 2)
                        cover.attributes('-topmost', True)
                        module.user32.SetWindowPos(handle, module.HWND_TOPMOST, 0, 0, 0, 0,
                            module.SWP_NOMOVE | module.SWP_NOSIZE | module.SWP_NOACTIVATE)
                        self.assertTrue(dock._is_window_above(handle, dock.hwnd))

                        def cursor(point):
                            point._obj.x = dock.root.winfo_screenwidth() // 2
                            point._obj.y = dock.root.winfo_screenheight() - 1
                            return True

                        dock.is_revealed = True
                        dock._last_z_scan_ts = 0
                        before = native_foreground()
                        with patch.object(module.user32, 'GetCursorPos', side_effect=cursor), \
                             patch.object(module.user32, 'GetForegroundWindow', return_value=handle), \
                             patch.object(module.user32, 'GetAsyncKeyState', return_value=0):
                            dock._check_edge_sensor()
                        self.assertTrue(dock._is_window_above(dock.hwnd, handle))
                        self.assertTrue(module.user32.GetWindowLongW(dock.hwnd, module.GWL_EXSTYLE) & 8)
                        self.assertTrue(dock._is_window_above(menu.top_hwnd, dock.hwnd))
                        self.assertEqual(native_foreground(), before)
                        with patch.object(dock, '_find_lowest_app_above_desktop', return_value=handle):
                            dock._tuck_behind_windows(reason='test')
                        self.assertFalse(dock.root.attributes('-topmost'))
                        self.assertFalse(module.user32.GetWindowLongW(dock.hwnd, module.GWL_EXSTYLE) & 8)
                        self.assertTrue(dock._is_window_above(handle, dock.hwnd))
            finally:
                menu.close()
                cover.destroy()
                dock.close()


if __name__ == '__main__':
    unittest.main()
