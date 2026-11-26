import os
import time

from local_state import create_instance_mutex, close_instance_mutex, desktop_name


def any_widget_enabled(config):
    return any(isinstance(value, dict) and value.get('enabled') for value in config.data.get('widgets', {}).values())


class WidgetRuntime:
    def __init__(self, root, config, on_open_settings=None, on_idle=None):
        self.root, self.config, self.on_idle = root, config, on_idle
        self.manager = None
        self.job = None
        self.mutex = create_instance_mutex('Local\\AliothWidgets_Mutex_' + desktop_name())
        self.active = bool(self.mutex)
        if not self.active:
            return
        try:
            import todo_store
            from desktop_widgets import DesktopWidgetManager
            self.inbox = os.path.join(os.path.dirname(config.config_path), 'todo_inbox.json')
            todo_store.ensure_inbox_file(self.inbox)
            self.next_retry = 0.0
            self.inbox_stamp = None
            self._ingest()
            config.load()
            self.manager = DesktopWidgetManager(root, config, on_open_settings=on_open_settings)
            self.config_stamp = self._stamp(config.config_path)
            self.empty_since = 0.0
            self.job = root.after(350, self._watch)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _stamp(path):
        try:
            stat = os.stat(path)
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _ingest(self):
        import todo_store
        if time.monotonic() < self.next_retry:
            return
        try:
            result = todo_store.ingest_todo_inbox(self.inbox, config_path=self.config.config_path)
            self.inbox_stamp = self._stamp(self.inbox)
            self.next_retry = 0.0
            if result.get('added'):
                print(f"[Widgets] Tareas importadas: {result['added']}")
        except (OSError, ValueError, TimeoutError) as exc:
            self.next_retry = time.monotonic() + 10
            print(f'[Widgets] Bandeja conservada para reintentar: {exc}')

    def refresh(self):
        if self.active and self.manager:
            self.manager.live_update()
            self.config_stamp = self._stamp(self.config.config_path)

    def _watch(self):
        self.job = None
        if not self.active:
            return
        try:
            self.manager.sync_z_order()
            if self._stamp(self.inbox) != self.inbox_stamp:
                self._ingest()
            stamp = self._stamp(self.config.config_path)
            if stamp != self.config_stamp:
                self.config.load()
                self.refresh()
            if not any_widget_enabled(self.config):
                if not self.empty_since:
                    self.empty_since = time.monotonic()
                elif time.monotonic() - self.empty_since >= 2:
                    self.close()
                    if self.on_idle:
                        self.on_idle()
                    return
            else:
                self.empty_since = 0.0
        except Exception as exc:
            print(f'[Widgets] Error de actualización: {exc}')
        if self.active:
            self.job = self.root.after(350, self._watch)

    def close(self):
        self.active = False
        if self.job:
            try:
                self.root.after_cancel(self.job)
            except Exception:
                pass
            self.job = None
        if self.manager:
            self.manager.close()
            self.manager = None
        close_instance_mutex(self.mutex)
        self.mutex = None
