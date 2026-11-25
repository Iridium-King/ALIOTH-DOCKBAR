import os
import sys
import time
import json
import uuid
import copy
import winreg
import ctypes

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
STARTUP_LINK_NAME = "AliothDockbar.lnk"

DEFAULT_CONFIG = {
    "version": "1.0",
    "general": {
        "position": "bottom",               # "bottom", "top", "left", "right"
        "autohide": True,                    # Reveal when mouse touches edge
        "autohide_delay": 1.2,               # Seconds to wait before hiding/lowering
        "edge_trigger_thickness": 4,         # Pixels near edge to activate
        "start_with_windows": False,         # Startup shortcut
        "locked": False,                     # Lock icons from dragging/removing
        "show_open_windows": False,          # Show unpinned open apps on the right side of the dock
        "launcher_mode": "custom",           # "custom" (macOS-style app drawer) or "windows" (Win Key)
        "media_capsule_enabled": False,      # Centered media player capsule above Dockbar
        "media_capsule_style": "cristal",    # "cristal", "neon", "minimal", "oled", "acrilico", "editorial"
        "media_capsule_scale": 1.0,          # Capsule size scale (0.75 to 1.45)
        "media_capsule_width": 360,          # Base capsule width in px (260 to 560)
        "media_capsule_opacity": 0.90,       # Background transparency (0.15 to 1.0)
        "media_capsule_bg_color": "",        # Optional custom background hex
        "media_capsule_accent_color": "",    # Optional custom accent/button hex
        "media_capsule_font_family": "Segoe UI",
        "media_capsule_font_size": 11,       # Title font size (9 to 18 px)
        "media_capsule_title_color": "#f8fafc",
        "media_capsule_artist_color": "#94a3b8",
        "media_capsule_btn_pos": "right",    # "right", "left", "center", "split"
        "media_capsule_offset_y": 10         # Gap between Dockbar and capsule (4 to 48 px)
    },
    "appearance": {
        "edge_offset": 6,                    # Distance between dock and screen edge (0 to 60px)
        "base_icon_size": 48,                # Normal icon size in pixels (32 to 96)
        "max_zoom": 1.7,                     # Peak wave magnification factor (1.0 to 2.2)
        "wave_radius": 140,                  # Radius of wave influence around cursor
        "icon_spacing": 10,                  # Distance between items
        "dock_padding_x": 16,                # Horizontal padding inside dock
        "dock_padding_y": 8,                 # Vertical padding inside dock
        "dock_bg_scale": 1.0,                # Independent dock background size/height multiplier (0.40 to 1.50)
        "dock_bg_tilt": 0,                   # Independent 3D perspective tilt of dock background (0 to 40 deg)
        "corner_radius": 20,                 # Pill rounded corners
        "round_all_corners": True,           # Round bottom corners even at 0px edge offset
        "bg_color": "#181a20",               # Dock background color (Hex)
        "bg_opacity": 0.82,                  # Background opacity (0.1 to 1.0)
        "border_enabled": True,              # Enable Dockbar border
        "border_color": "#ffffff",           # Border color (Hex)
        "border_opacity": 0.24,              # Border opacity
        "border_width": 1,                   # Border thickness (1 to 6 px)
        "border_style": "solid",             # "solid", "dashed", "dotted", "double", "neon"
        "show_labels": True,                 # Floating tooltip label
        "label_bg": "#121318",
        "label_fg": "#ffffff",
        "label_font_family": "Segoe UI",
        "label_font_size": 11,
        "label_bold": False,
        "label_italic": False,
        "label_bg_opacity": 0.90,
        "folder_show_labels": True,
        "folder_label_font_family": "Segoe UI",
        "folder_label_font_size": 11,
        "folder_label_bold": False,
        "folder_label_italic": False,
        "folder_label_fg": "#dae2ee",
        "separator_color": "#ffffff",
        "separator_opacity": 0.35,
        "separator_width": 2,
        "separator_style": "solid",          # "solid", "dashed", "dotted", "double", "glow"
        "bounce_on_launch": True,            # Legacy compatibility switch
        "launch_effect": "gota_energia",     # "gota_energia", "gota_rebote", "pulso_neon", "levitacion", "pop_elastico", "rebote", "ninguno"
        "launch_effect_speed": 1.0,          # Speed multiplier (0.4x slow to 2.5x fast)
        "launch_effect_color": "#0078d4",    # Vibrant Outlook water/energy blue default
        "show_running_indicators": True,     # Open apps indicator underneath running apps
        "indicator_style": "dot",            # "dot", "pill", "line", "ring", "diamond"
        "indicator_color": "#60a5fa",
        "indicator_size": 6,                 # Base indicator size (3 to 12 px)
        "dock_battery_style": "ring",        # "ring", "bar", "pill", "minimal"
        "dock_clock_style": "wide_5_3",      # "wide_5_3", "digital", "analog", "neon", "minimal", "pill"
        "dock_clock_24h": True,
        "dock_clock_seconds": False,
        "dock_clock_show_date": True,
        "dock_clock_color": "#f8fafc"
    },
    "widgets": {
        "clock": {
            "enabled": True,
            "x": 24,
            "y": 48,
            "format_24h": True,
            "show_seconds": False,
            "clock_style": "moderno",
            "time_color": "#ffffff",
            "date_color": "#cbd5e1",
            "alignment": "left",
            "opacity": 0.88,
            "pinned_to_desktop": True
        },
        "system_monitor": {
            "enabled": True,
            "x": 24,
            "y": 170,
            "opacity": 0.88,
            "update_interval_sec": 2,
            "pinned_to_desktop": True
        },
        "battery": {
            "enabled": False,
            "x": 24,
            "y": 585,
            "battery_style": "anillo",       # "anillo", "barra", "neon", "pildora"
            "accent_color": "#38bdf8",
            "opacity": 0.88,
            "pinned_to_desktop": True
        },
        "desktop_minidock": {
            "enabled": False,
            "x": 24,
            "y": 360,
            "opacity": 0.88,
            "pinned_to_desktop": True
        },
        "todo_list": {
            "enabled": True,
            "x": 24,
            "y": 350,
            "opacity": 0.88,
            "pinned_to_desktop": True,
            "tasks": [
                {"id": "t1", "text": "Revisar pendientes del día", "done": False},
                {"id": "t2", "text": "Organizar accesos directos", "done": True}
            ]
        },
        "quick_access": {
            "enabled": False,
            "x": 300,
            "y": 60,
            "mode": "hover_expand",
            "opacity": 0.88,
            "pinned_to_desktop": True,
            "items": [
                {"id": "qa1", "type": "app", "name": "Explorador", "target": "explorer.exe", "icon_path": ""},
                {"id": "qa2", "type": "app", "name": "Bloc de Notas", "target": "notepad.exe", "icon_path": ""}
            ]
        },
        "pwa_apps": {
            "enabled": False,
            "x": 300,
            "y": 200,
            "opacity": 0.88,
            "pinned_to_desktop": True,
            "apps": [
                {"id": "pwa1", "name": "WhatsApp", "url": "https://web.whatsapp.com"},
                {"id": "pwa2", "name": "ChatGPT", "url": "https://chatgpt.com"},
                {"id": "pwa3", "name": "Gemini", "url": "https://gemini.google.com"}
            ]
        }
    },
    "active_theme": "",
    "saved_themes": {},
    "panel_bounds": {
        "width": 980,
        "height": 780,
        "x": None,
        "y": None
    },
    "items": []
}

class _ConfigFileMutex:
    """Shared inter-process Win32 Mutex so Dockbar, Widgets, CLI & MCP never clobber config.json."""
    def __init__(self, name="Local\\AliothTodoStore_Mutex"):
        self.name = name
        self.handle = None

    def __enter__(self):
        try:
            k32 = ctypes.windll.kernel32
            h = k32.CreateMutexW(None, False, self.name)
            if h:
                k32.WaitForSingleObject(h, 4000)
                self.handle = h
        except Exception:
            self.handle = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.handle:
            try:
                k32 = ctypes.windll.kernel32
                k32.ReleaseMutex(self.handle)
                k32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


def _deep_merge_defaults(target, defaults):
    if not isinstance(target, dict) or not isinstance(defaults, dict):
        return target
    for k, v in defaults.items():
        if k not in target:
            target[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(target[k], dict):
            _deep_merge_defaults(target[k], v)
    return target


class ConfigManager:
    def __init__(self, config_path=DEFAULT_CONFIG_PATH):
        self.config_path = config_path
        self._dirty_sections = set()
        self._dirty_widgets = set()
        self.data = self.load()

    def load(self):
        if os.path.exists(self.config_path):
            with _ConfigFileMutex():
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        _deep_merge_defaults(data, DEFAULT_CONFIG)
                        return data
                except Exception as e:
                    print(f"[ConfigManager] Error reading config: {e}. Using defaults.")

        # Initial creation with default sample items
        initial = copy.deepcopy(DEFAULT_CONFIG)
        initial["items"] = self._get_initial_items()
        self.data = initial
        self._dirty_sections = set(initial.keys())
        self.save(initial)
        return initial

    def _get_initial_items(self):
        items = [
            {
                "id": str(uuid.uuid4()),
                "type": "app",
                "name": "Explorador de Archivos",
                "target": "explorer.exe",
                "args": "",
                "icon_path": "",
                "run_as_admin": False
            },
            {
                "id": str(uuid.uuid4()),
                "type": "app",
                "name": "Bloc de Notas",
                "target": "notepad.exe",
                "args": "",
                "icon_path": "",
                "run_as_admin": False
            },
            {
                "id": str(uuid.uuid4()),
                "type": "app",
                "name": "Calculadora",
                "target": "calc.exe",
                "args": "",
                "icon_path": "",
                "run_as_admin": False
            },
            {
                "id": str(uuid.uuid4()),
                "type": "separator"
            }
        ]
        return items

    def save(self, data=None):
        if data is not None:
            self.data = data
        tmp_path = f"{self.config_path}.{os.getpid()}.tmp"
        with _ConfigFileMutex():
            try:
                # Re-read disk state and merge only sections/widgets modified in this process,
                # so Dockbar and Widgets processes NEVER overwrite each other's settings or positions!
                merged = copy.deepcopy(self.data)
                if os.path.exists(self.config_path):
                    try:
                        with open(self.config_path, "r", encoding="utf-8") as rf:
                            disk_data = json.load(rf)
                        if isinstance(disk_data, dict):
                            dirty_secs = getattr(self, "_dirty_sections", set())
                            dirty_wids = getattr(self, "_dirty_widgets", set())
                            if dirty_secs or dirty_wids:
                                merged = copy.deepcopy(disk_data)
                                for sec in dirty_secs:
                                    if sec == "widgets" and dirty_wids:
                                        continue
                                    if sec in self.data:
                                        merged[sec] = copy.deepcopy(self.data[sec])
                                if dirty_wids:
                                    w_disk = merged.setdefault("widgets", {})
                                    w_mem = self.data.get("widgets", {})
                                    for wid in dirty_wids:
                                        if wid in w_mem:
                                            w_disk[wid] = copy.deepcopy(w_mem[wid])
                                self.data = copy.deepcopy(merged)
                    except Exception:
                        pass

                _deep_merge_defaults(merged, DEFAULT_CONFIG)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(merged, f, indent=4, ensure_ascii=False)
                    f.flush()
                last_err = None
                for attempt in range(6):
                    try:
                        os.replace(tmp_path, self.config_path)
                        last_err = None
                        break
                    except (PermissionError, OSError) as err:
                        last_err = err
                        time.sleep(0.08 * (attempt + 1))
                if last_err is not None:
                    raise last_err
                self._dirty_sections.clear()
                self._dirty_widgets.clear()
            except Exception as e:
                print(f"[ConfigManager] Error saving config: {e}")
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass

    def mark_dirty(self, section, widget_id=None):
        """Marks a config section or specific widget dirty so save() persists it cleanly."""
        if section == "widgets" and widget_id:
            self._dirty_widgets.add(widget_id)
        elif section:
            self._dirty_sections.add(section)

    def get(self, section, key, default=None):
        return self.data.get(section, {}).get(key, default)

    def set(self, section, key, value):
        if section not in self.data:
            self.data[section] = {}
        self.data[section][key] = value
        self._dirty_sections.add(section)
        self.save()

    def get_widget(self, name, key, default=None):
        return self.data.get("widgets", {}).get(name, {}).get(key, default)

    def set_widget(self, name, key, value):
        if "widgets" not in self.data:
            self.data["widgets"] = {}
        if name not in self.data["widgets"]:
            self.data["widgets"][name] = {}
        self.data["widgets"][name][key] = value
        self._dirty_widgets.add(name)
        self.save()

    def get_items(self):
        return list(self.data.get("items", []))

    def set_items(self, items):
        self.data["items"] = items
        self._dirty_sections.add("items")
        self.save()

    def add_item(self, item_dict, index=None):
        items = self.get_items()
        if "id" not in item_dict:
            item_dict["id"] = str(uuid.uuid4())
        if index is not None and 0 <= index <= len(items):
            items.insert(index, item_dict)
        else:
            items.append(item_dict)
        self.set_items(items)

    def remove_item(self, item_id):
        items = self.get_items()
        if isinstance(item_id, int):
            if 0 <= item_id < len(items):
                items.pop(item_id)
        else:
            items = [it for it in items if it.get("id") != item_id]
        self.set_items(items)

    def update_item(self, item_id, new_data):
        items = self.get_items()
        for i, it in enumerate(items):
            if it.get("id") == item_id:
                items[i].update(new_data)
                break
        self.set_items(items)

    def move_item(self, from_idx, to_idx):
        items = self.get_items()
        if 0 <= from_idx < len(items) and 0 <= to_idx < len(items):
            item = items.pop(from_idx)
            items.insert(to_idx, item)
            self.set_items(items)

    @staticmethod
    def _default_group_name(items_list, fallback_target_name="Apps"):
        power_actions = {"shutdown_pc", "restart_pc", "logoff_pc", "lock_pc"}
        if items_list and all(it.get("type") == "action" for it in items_list):
            if all(it.get("action_type") in power_actions for it in items_list):
                return "Energía"
            return "Acciones de Windows"
        return f"Grupo ({fallback_target_name})"

    def create_group(self, name="Nueva Carpeta", items_list=None, index=None):
        """Creates a new group/folder item on the dock and returns its id."""
        children = []
        for ch in (items_list or []):
            ch_copy = copy.deepcopy(ch)
            if "id" not in ch_copy or not ch_copy["id"]:
                ch_copy["id"] = str(uuid.uuid4())
            children.append(ch_copy)
        new_group = {
            "id": str(uuid.uuid4()),
            "type": "group",
            "name": (name or "Nueva Carpeta").strip() or "Nueva Carpeta",
            "icon_path": "",
            "items": children
        }
        self.add_item(new_group, index=index)
        return new_group["id"]

    def add_child_to_group(self, group_id, child_dict):
        """Appends a new app or system action child into an existing group."""
        items = self.get_items()
        for it in items:
            if it.get("id") == group_id and it.get("type") == "group":
                child = copy.deepcopy(child_dict)
                if "id" not in child or not child["id"]:
                    child["id"] = str(uuid.uuid4())
                grp_items = it.setdefault("items", it.get("children", []))
                grp_items.append(child)
                self.set_items(items)
                return child["id"]
        return None

    def move_item_to_group(self, source_ref, target_group_id):
        """Moves a top-level dock item (by index or id) into an existing group."""
        items = self.get_items()
        src_idx = None
        if isinstance(source_ref, int):
            if 0 <= source_ref < len(items):
                src_idx = source_ref
        else:
            for i, it in enumerate(items):
                if it.get("id") == source_ref:
                    src_idx = i
                    break
        tgt_idx = None
        for i, it in enumerate(items):
            if it.get("id") == target_group_id and it.get("type") == "group":
                tgt_idx = i
                break
        if src_idx is None or tgt_idx is None or src_idx == tgt_idx:
            return None
        return self.merge_into_group(src_idx, tgt_idx)

    def extract_from_group(self, group_id, child_ref):
        """Moves a single child item (by index or id) out of a group back to the main dock."""
        items = self.get_items()
        for g_idx, it in enumerate(items):
            if it.get("id") == group_id and it.get("type") == "group":
                children = it.setdefault("items", it.get("children", []))
                c_idx = None
                if isinstance(child_ref, int):
                    if 0 <= child_ref < len(children):
                        c_idx = child_ref
                else:
                    for i, ch in enumerate(children):
                        if ch.get("id") == child_ref:
                            c_idx = i
                            break
                if c_idx is None:
                    return None
                extracted = children.pop(c_idx)
                if not extracted.get("id"):
                    extracted["id"] = str(uuid.uuid4())
                if len(children) == 0:
                    items[g_idx] = extracted
                else:
                    items.insert(g_idx + 1, extracted)
                self.set_items(items)
                return extracted["id"]
        return None

    def merge_into_group(self, source_idx, target_idx, group_name="Nuevo Grupo"):
        """Merges two dock items (apps, system actions, or groups) into a group folder."""
        items = self.get_items()
        if source_idx == target_idx or not (0 <= source_idx < len(items)) or not (0 <= target_idx < len(items)):
            return None

        groupable_types = ("app", "action", "group")
        source_item = items[source_idx]
        target_item = items[target_idx]

        if source_item.get("type") not in groupable_types or target_item.get("type") not in groupable_types:
            return None

        # Remove source item first
        items.pop(source_idx)
        if source_idx < target_idx:
            target_idx -= 1

        target_item = items[target_idx]
        src_type = source_item.get("type")
        tgt_type = target_item.get("type")

        if tgt_type == "group":
            grp_items = target_item.setdefault("items", target_item.get("children", []))
            if src_type == "group":
                # Flatten source group children into target group instead of nesting groups
                for ch in source_item.get("items", source_item.get("children", [])):
                    grp_items.append(ch)
            else:
                grp_items.append(source_item)
            result_id = target_item.get("id")
        elif src_type == "group":
            # Moving a group onto an app/action adds that item into the group cleanly
            grp_items = source_item.setdefault("items", source_item.get("children", []))
            grp_items.insert(0, target_item)
            items[target_idx] = source_item
            result_id = source_item.get("id")
        else:
            # Create new group from both items (app+app, action+action, app+action, action+app)
            combined = [target_item, source_item]
            if group_name and group_name != "Nuevo Grupo":
                name = group_name
            else:
                name = self._default_group_name(combined, target_item.get("name", "Apps"))
            new_group = {
                "id": str(uuid.uuid4()),
                "type": "group",
                "name": name,
                "icon_path": "",
                "items": combined
            }
            items[target_idx] = new_group
            result_id = new_group["id"]

        self.set_items(items)
        return result_id

    def ungroup(self, group_id):
        """Unpacks all items from a group back to the main dock."""
        items = self.get_items()
        new_items = []
        for it in items:
            if it.get("id") == group_id and it.get("type") == "group":
                for child in it.get("items", []):
                    new_items.append(child)
            else:
                new_items.append(it)
        self.set_items(new_items)

    @staticmethod
    def _resolve_healthy_pythonw():
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        cur_pyw = os.path.join(exe_dir, "pythonw.exe")
        if os.path.exists(cur_pyw) and os.path.exists(os.path.join(exe_dir, "Lib", "encodings", "__init__.py")):
            return cur_pyw
        for py_dir in (
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python313"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python314"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311"),
            r"C:\Program Files\Python313",
            r"C:\Program Files\Python312",
        ):
            cand = os.path.join(py_dir, "pythonw.exe")
            if os.path.exists(cand) and os.path.exists(os.path.join(py_dir, "Lib", "encodings", "__init__.py")):
                return cand
        return cur_pyw if os.path.exists(cur_pyw) else "pyw.exe"

    @staticmethod
    def _ensure_startup_approved(subkey_name, entry_name):
        """Ensures Windows Task Manager StartupApproved does not have the entry disabled."""
        path = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\{subkey_name}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
                try:
                    val, reg_type = winreg.QueryValueEx(key, entry_name)
                    if isinstance(val, (bytes, bytearray)) and len(val) >= 1 and val[0] != 2:
                        winreg.SetValueEx(key, entry_name, 0, reg_type, b"\x02" + b"\x00" * 11)
                except FileNotFoundError:
                    pass
        except Exception:
            pass

    @staticmethod
    def _configure_user_logon_task(enable, pythonw="", pyw_path="", local_dir=""):
        """Best-effort per-user Task Scheduler logon trigger via COM (no admin required)."""
        try:
            import win32com.client
            service = win32com.client.Dispatch("Schedule.Service")
            service.Connect()
            root_folder = service.GetFolder("\\")
            task_name = "AliothDockbar"
            if not enable:
                try:
                    root_folder.DeleteTask(task_name, 0)
                except Exception:
                    pass
                return True

            task_def = service.NewTask(0)
            reg_info = task_def.RegistrationInfo
            reg_info.Description = "Alioth Dockbar autostart launcher"

            domain = os.environ.get("USERDOMAIN", "")
            user = os.environ.get("USERNAME", "")
            user_id = f"{domain}\\{user}" if (domain and user) else user

            principal = task_def.Principal
            if user_id:
                principal.UserId = user_id
            principal.LogonType = 3  # TASK_LOGON_INTERACTIVE_TOKEN
            principal.RunLevel = 0   # TASK_RUNLEVEL_LUA

            settings = task_def.Settings
            settings.Enabled = True
            settings.DisallowStartIfOnBatteries = False
            settings.StopIfGoingOnBatteries = False
            settings.AllowDemandStart = True
            settings.ExecutionTimeLimit = "PT5M"
            settings.MultipleInstances = 2  # TASK_INSTANCES_IGNORE_NEW

            trigger = task_def.Triggers.Create(9)  # TASK_TRIGGER_LOGON
            if user_id:
                trigger.UserId = user_id
            trigger.Enabled = True

            action = task_def.Actions.Create(0)  # TASK_ACTION_EXEC
            action.Path = pythonw
            action.Arguments = f'"{pyw_path}"'
            action.WorkingDirectory = local_dir

            # TASK_CREATE_OR_UPDATE = 6, TASK_LOGON_INTERACTIVE_TOKEN = 3
            root_folder.RegisterTaskDefinition(task_name, task_def, 6, None, None, 3)
            print("[Startup] Scheduled task AliothDockbar registered.")
            return True
        except Exception as e:
            print(f"[Startup] Scheduled task registration skipped: {e}")
            return False

    # Windows Startup Management (Without Admin, signed pythonw.exe local bootstrapper + Run key + Startup folder + Logon task)
    def update_startup_shortcut(self, enable=None):
        if enable is None:
            enable = self.get("general", "start_with_windows", False)

        startup_dir = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
        shortcut_path = os.path.join(startup_dir, STARTUP_LINK_NAME)
        local_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "AliothDockbar")
        pyw_path = os.path.join(local_dir, "autostart.pyw")
        old_vbs_path = os.path.join(local_dir, "autostart.vbs")
        run_key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        run_val_name = "AliothDockbar"

        if enable:
            dock_dir = os.path.dirname(os.path.abspath(__file__))
            main_py = os.path.join(dock_dir, "main.py")
            config_json = os.path.join(dock_dir, "config.json")
            pythonw = self._resolve_healthy_pythonw()

            try:
                os.makedirs(local_dir, exist_ok=True)
                if os.path.exists(old_vbs_path):
                    try:
                        os.remove(old_vbs_path)
                    except Exception:
                        pass

                pyw_script = f'''import os
import sys
import time
import ctypes
import subprocess
from ctypes import wintypes

LOCAL_DIR = {local_dir!r}
LOG_PATH = os.path.join(LOCAL_DIR, "autostart.log")
DOCK_DIR = {dock_dir!r}
MAIN_PY = {main_py!r}
CONFIG_JSON = {config_json!r}
PREF_PYW = {pythonw!r}

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_k32.OpenMutexW.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GetCurrentThreadId.restype = wintypes.DWORD
_u32.GetThreadDesktop.argtypes = [wintypes.DWORD]
_u32.GetThreadDesktop.restype = wintypes.HANDLE
_u32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]


def desktop_name():
    try:
        h_desk = _u32.GetThreadDesktop(_k32.GetCurrentThreadId())
        buf = ctypes.create_unicode_buffer(128)
        needed = wintypes.DWORD()
        if _u32.GetUserObjectInformationW(h_desk, 2, buf, 256, ctypes.byref(needed)):
            safe = "".join(c if c.isalnum() else "_" for c in buf.value)
            return safe or "Default"
    except Exception:
        pass
    return "Default"


def dock_running():
    h = _k32.OpenMutexW(0x00100000, False, "Local\\\\AliothDockbar_Mutex_" + desktop_name())
    if h:
        _k32.CloseHandle(h)
        return True
    return False


def write_log(msg):
    try:
        os.makedirs(LOCAL_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 131072:
            old_log = LOG_PATH + ".old"
            if os.path.exists(old_log):
                os.remove(old_log)
            os.replace(LOG_PATH, old_log)
        with open(LOG_PATH, "a", encoding="utf-8") as lf:
            lf.write(f"{{time.strftime('%Y-%m-%d %H:%M:%S')}} - {{msg}}\\n")
    except Exception:
        pass


def resolve_pythonw():
    if PREF_PYW and os.path.exists(PREF_PYW):
        return PREF_PYW
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    cur_pyw = os.path.join(exe_dir, "pythonw.exe")
    if os.path.exists(cur_pyw):
        return cur_pyw
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\\Programs\\Python\\Python313"),
        os.path.expandvars(r"%LOCALAPPDATA%\\Programs\\Python\\Python314"),
        os.path.expandvars(r"%LOCALAPPDATA%\\Programs\\Python\\Python312"),
        os.path.expandvars(r"%LOCALAPPDATA%\\Programs\\Python\\Python311"),
        r"C:\\Program Files\\Python313",
        r"C:\\Program Files\\Python312",
    ]
    for c in candidates:
        cand = os.path.join(c, "pythonw.exe")
        if os.path.exists(cand) and os.path.exists(os.path.join(c, "Lib", "encodings", "__init__.py")):
            return cand
    return sys.executable


def main():
    # Only ONE bootstrapper (Run key / Startup folder / logon task) may proceed; the rest exit instantly
    boot_lock = _k32.CreateMutexW(None, True, "Local\\\\AliothDockbar_Bootstrap")
    if boot_lock and ctypes.get_last_error() == 183:
        _k32.CloseHandle(boot_lock)
        return 0
    try:
        if dock_running():
            write_log("Dock already running; bootstrapper skipped")
            return 0
        return launch()
    finally:
        if boot_lock:
            _k32.CloseHandle(boot_lock)


def launch():
    write_log("Autostart bootstrapper started")
    ready = False
    for attempt in range(1, 46):
        if dock_running():
            write_log("Dock started meanwhile; bootstrapper skipped")
            return 0
        try:
            if os.path.exists(MAIN_PY) and os.path.exists(CONFIG_JSON):
                with open(MAIN_PY, "rb") as f1:
                    f1.read(16)
                with open(CONFIG_JSON, "rb") as f2:
                    f2.read(2)
                ready = True
                break
        except Exception as ex:
            write_log(f"Waiting for OneDrive/files attempt {{attempt}}: {{ex}}")
        else:
            write_log(f"Waiting for OneDrive/files attempt {{attempt}}")
        time.sleep(2.0)

    if not ready:
        write_log(f"ERROR: main.py/config.json unavailable after waiting: {{MAIN_PY}}")
        return 1

    pyw = resolve_pythonw()
    try:
        proc = subprocess.Popen(
            [pyw, MAIN_PY, "--autostart"],
            cwd=DOCK_DIR,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            close_fds=True,
        )
        write_log(f"Launched Alioth Dockbar PID={{proc.pid}} via {{pyw}}")
    except Exception as ex:
        write_log(f"ERROR launching Alioth Dockbar: {{ex}}")
        return 1
    # Hold the bootstrap lock until the dock owns its instance mutex, so concurrent launchers skip
    for _ in range(40):
        if dock_running() or proc.poll() is not None:
            break
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
                with open(pyw_path, "w", encoding="utf-8", newline="\n") as pf:
                    pf.write(pyw_script)
            except Exception as e:
                print(f"[Startup] Error creating local bootstrapper: {e}")

            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_key_path) as key:
                    winreg.SetValueEx(key, run_val_name, 0, winreg.REG_SZ, f'"{pythonw}" "{pyw_path}"')
                self._ensure_startup_approved("Run", run_val_name)
            except Exception as e:
                print(f"[Startup] Error updating HKCU Run key: {e}")

            try:
                import win32com.client
                shell = win32com.client.Dispatch("WScript.Shell")
                shortcut = shell.CreateShortcut(shortcut_path)
                shortcut.TargetPath = pythonw
                shortcut.Arguments = f'"{pyw_path}"'
                shortcut.WorkingDirectory = local_dir
                shortcut.Description = "Alioth Dockbar"
                shortcut.Save()
                self._ensure_startup_approved("StartupFolder", STARTUP_LINK_NAME)
                print(f"[Startup] Autostart configured via Run + {shortcut_path}")
            except Exception as e:
                print(f"[Startup] Error creating shortcut: {e}")

            self._configure_user_logon_task(True, pythonw=pythonw, pyw_path=pyw_path, local_dir=local_dir)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key_path, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, run_val_name)
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"[Startup] Error removing Run key: {e}")

            for p in (shortcut_path, pyw_path, old_vbs_path):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception as e:
                        print(f"[Startup] Error removing {p}: {e}")

            self._configure_user_logon_task(False)
