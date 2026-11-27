import os
import sys
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, messagebox, colorchooser, filedialog, simpledialog
from PIL import Image, ImageTk

import icon_extractor
from app_paths import APP_VERSION, resolve_resource
from window_utils import animate_window, enable_native_motion

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

# Palette
BG_COLOR = "#121418"
CARD_BG = "#1a1d24"
BORDER_COLOR = "#2f333f"
TEXT_COLOR = "#f1f2f6"
TEXT_MUTED = "#9ba0b4"
ACCENT_COLOR = "#3b82f6"

class SettingsWindow:
    def __init__(self, config_manager, on_applied=None):
        self.config_mgr = config_manager
        self.on_applied = on_applied

        self.root = tk.Toplevel()
        self.root.title(f"Configuración de Alioth Dockbar v{APP_VERSION}")
        bounds = self.config_mgr.data.get("panel_bounds", {}) if isinstance(self.config_mgr.data.get("panel_bounds"), dict) else {}
        try:
            bw = max(640, int(bounds.get("width") or 780))
            bh = max(540, int(bounds.get("height") or 660))
        except Exception:
            bw, bh = 780, 660
        bx = bounds.get("x")
        by = bounds.get("y")
        if bx is not None and by is not None:
            self.root.geometry(f"{bw}x{bh}+{int(bx)}+{int(by)}")
        else:
            self.root.geometry(f"{bw}x{bh}")
        self.root.minsize(640, 540)
        self.root.config(bg=BG_COLOR)
        self.root.protocol("WM_DELETE_WINDOW", self._save_and_close)
        self.root.lift()
        self.root.focus_force()

        self._apply_win11_dark_and_rounded()

        # Custom ttk dark styling
        self._setup_dark_theme()

        self.notebook = ttk.Notebook(self.root, style="Dark.TNotebook")
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

        # Tabs
        self.tab_general = ttk.Frame(self.notebook, style="Dark.TFrame", padding=14)
        self.tab_appearance = ttk.Frame(self.notebook, style="Dark.TFrame", padding=14)
        self.tab_items = ttk.Frame(self.notebook, style="Dark.TFrame", padding=14)
        self.tab_widgets = ttk.Frame(self.notebook, style="Dark.TFrame", padding=14)

        self.notebook.add(self.tab_general, text="  ⚙️ General  ")
        self.notebook.add(self.tab_appearance, text="  🎨 Apariencia y Zoom  ")
        self.notebook.add(self.tab_items, text="  📌 Iconos y Elementos  ")
        self.notebook.add(self.tab_widgets, text="  🖥️ Widgets y Escritorio  ")

        self._build_general_tab()
        self._build_appearance_tab()
        self._build_items_tab()
        self._build_widgets_tab()
        self._build_footer()
        enable_native_motion(self.root)

    def _apply_win11_dark_and_rounded(self):
        try:
            self.root.update_idletasks()
            hwnd = int(self.root.wm_frame(), 16) if self.root.wm_frame() else self.root.winfo_id()
            dark_mode = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_mode), 4)
            corner_pref = ctypes.c_int(2)
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner_pref), 4)
        except Exception as e:
            print(f"[Settings] Win11 styling note: {e}")

    def _setup_dark_theme(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("Dark.TFrame", background=BG_COLOR)
        style.configure("Dark.TNotebook", background=BG_COLOR, borderwidth=0)
        style.configure("Dark.TNotebook.Tab", background=CARD_BG, foreground=TEXT_MUTED, padding=[16, 8], font=("Segoe UI", 10, "bold"))
        style.map("Dark.TNotebook.Tab",
            background=[("selected", ACCENT_COLOR), ("active", "#2c303c")],
            foreground=[("selected", "#ffffff"), ("active", "#ffffff")]
        )

        style.configure("Card.TLabelframe", background=CARD_BG, bordercolor=BORDER_COLOR, darkcolor=BORDER_COLOR, lightcolor=BORDER_COLOR)
        style.configure("Card.TLabelframe.Label", background=CARD_BG, foreground=ACCENT_COLOR, font=("Segoe UI", 10, "bold"))

        style.configure("Dark.TLabel", background=CARD_BG, foreground=TEXT_COLOR, font=("Segoe UI", 9))
        style.configure("Muted.TLabel", background=CARD_BG, foreground=TEXT_MUTED, font=("Segoe UI", 9))

        style.configure("Dark.TRadiobutton", background=CARD_BG, foreground=TEXT_COLOR, font=("Segoe UI", 9))
        style.map("Dark.TRadiobutton", background=[("active", CARD_BG)], foreground=[("active", "#ffffff")])

        style.configure("Dark.TCheckbutton", background=CARD_BG, foreground=TEXT_COLOR, font=("Segoe UI", 9))
        style.map("Dark.TCheckbutton", background=[("active", CARD_BG)], foreground=[("active", "#ffffff")])

        style.configure("Dark.TButton", background="#2a2e39", foreground=TEXT_COLOR, borderwidth=0, padding=[10, 6], font=("Segoe UI", 9))
        style.map("Dark.TButton", background=[("active", ACCENT_COLOR), ("pressed", "#2563eb")], foreground=[("active", "#ffffff")])

        style.configure("Accent.TButton", background=ACCENT_COLOR, foreground="#ffffff", borderwidth=0, padding=[14, 6], font=("Segoe UI", 9, "bold"))
        style.map("Accent.TButton", background=[("active", "#2563eb")], foreground=[("active", "#ffffff")])

    def _trigger_live_update(self):
        if self.on_applied:
            self.on_applied()

    # -------------------------------------------------------------
    # Tab 1: General
    # -------------------------------------------------------------
    def _build_general_tab(self):
        pos_frame = ttk.LabelFrame(self.tab_general, text="  Posición en Pantalla  ", style="Card.TLabelframe", padding=12)
        pos_frame.pack(fill=tk.X, pady=6)

        self.pos_var = tk.StringVar(value=self.config_mgr.get("general", "position", "bottom"))
        positions = [
            ("bottom", "Borde Inferior (Abajo - Predeterminado)"),
            ("top", "Borde Superior (Arriba - Debajo de la barra de tareas)"),
            ("left", "Borde Izquierdo"),
            ("right", "Borde Derecho")
        ]
        for val, txt in positions:
            ttk.Radiobutton(
                pos_frame,
                text=txt,
                value=val,
                variable=self.pos_var,
                style="Dark.TRadiobutton",
                command=self._on_pos_changed
            ).pack(anchor=tk.W, pady=2)

        dist_frame = ttk.Frame(pos_frame, style="Dark.TFrame")
        dist_frame.pack(fill=tk.X, pady=8)
        ttk.Label(dist_frame, text="Separación del borde de pantalla (Margen):", style="Dark.TLabel").pack(side=tk.LEFT)
        self.edge_offset_var = tk.IntVar(value=self.config_mgr.get("appearance", "edge_offset", 6))
        self.edge_offset_lbl = ttk.Label(dist_frame, text=f"{self.edge_offset_var.get()} px", style="Muted.TLabel")
        self.edge_offset_lbl.pack(side=tk.RIGHT)
        ttk.Scale(dist_frame, from_=0, to=50, variable=self.edge_offset_var, command=self._on_edge_offset_changed).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

        # Behavior / Auto-hide
        beh_frame = ttk.LabelFrame(self.tab_general, text="  Comportamiento con Ventanas Maximizadas  ", style="Card.TLabelframe", padding=12)
        beh_frame.pack(fill=tk.X, pady=6)

        self.autohide_var = tk.BooleanVar(value=self.config_mgr.get("general", "autohide", True))
        ttk.Checkbutton(
            beh_frame,
            text="Ocultar detrás de ventanas maximizadas y emerger al frente cuando el cursor toque el borde",
            variable=self.autohide_var,
            style="Dark.TCheckbutton",
            command=self._on_autohide_toggle
        ).pack(anchor=tk.W, pady=4)

        delay_frame = ttk.Frame(beh_frame, style="Dark.TFrame")
        delay_frame.pack(fill=tk.X, pady=4)
        ttk.Label(delay_frame, text="Tiempo de espera antes de ocultar (segundos):", style="Dark.TLabel").pack(side=tk.LEFT)
        self.delay_var = tk.DoubleVar(value=float(self.config_mgr.get("general", "autohide_delay", 1.2)))
        self.delay_lbl = ttk.Label(delay_frame, text=f"{self.delay_var.get():.1f} s", style="Muted.TLabel")
        self.delay_lbl.pack(side=tk.RIGHT)
        ttk.Scale(delay_frame, from_=0.2, to=4.0, variable=self.delay_var, command=self._on_delay_changed).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

        # System Integration
        sys_frame = ttk.LabelFrame(self.tab_general, text="  Inicio del Sistema y Seguridad  ", style="Card.TLabelframe", padding=12)
        sys_frame.pack(fill=tk.X, pady=6)

        self.startup_var = tk.BooleanVar(value=self.config_mgr.get("general", "start_with_windows", False))
        ttk.Checkbutton(
            sys_frame,
            text="Iniciar automáticamente con Windows (en tu inicio de usuario sin requerir admin)",
            variable=self.startup_var,
            style="Dark.TCheckbutton",
            command=self._on_startup_toggle
        ).pack(anchor=tk.W, pady=4)

        self.locked_var = tk.BooleanVar(value=self.config_mgr.get("general", "locked", False))
        ttk.Checkbutton(
            sys_frame,
            text="Bloquear iconos (evitar arrastrar o eliminar iconos por error)",
            variable=self.locked_var,
            style="Dark.TCheckbutton",
            command=self._on_lock_toggle
        ).pack(anchor=tk.W, pady=4)

    def _on_pos_changed(self):
        self.config_mgr.set("general", "position", self.pos_var.get())
        self._trigger_live_update()

    def _on_edge_offset_changed(self, v):
        val = int(float(v))
        self.edge_offset_lbl.config(text=f"{val} px")
        self.config_mgr.set("appearance", "edge_offset", val)
        self._trigger_live_update()

    def _on_autohide_toggle(self):
        self.config_mgr.set("general", "autohide", self.autohide_var.get())
        self._trigger_live_update()

    def _on_delay_changed(self, v):
        val = round(float(v), 1)
        self.delay_lbl.config(text=f"{val:.1f} s")
        self.config_mgr.set("general", "autohide_delay", val)
        self._trigger_live_update()

    def _on_startup_toggle(self):
        val = self.startup_var.get()
        self.config_mgr.set("general", "start_with_windows", val)
        self.config_mgr.update_startup_shortcut(val)

    def _on_lock_toggle(self):
        self.config_mgr.set("general", "locked", self.locked_var.get())

    # -------------------------------------------------------------
    # Tab 2: Appearance & Zoom
    # -------------------------------------------------------------
    def _build_appearance_tab(self):
        size_frame = ttk.LabelFrame(self.tab_appearance, text="  Tamaño de Iconos y Efecto Lupa (Wave Zoom)  ", style="Card.TLabelframe", padding=12)
        size_frame.pack(fill=tk.X, pady=4)

        # Base size
        f1 = ttk.Frame(size_frame, style="Dark.TFrame")
        f1.pack(fill=tk.X, pady=3)
        ttk.Label(f1, text="Tamaño base de iconos:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.icon_size_var = tk.IntVar(value=self.config_mgr.get("appearance", "base_icon_size", 48))
        self.icon_size_lbl = ttk.Label(f1, text=f"{self.icon_size_var.get()} px", style="Muted.TLabel")
        self.icon_size_lbl.pack(side=tk.RIGHT)
        ttk.Scale(f1, from_=32, to=80, variable=self.icon_size_var, command=self._on_icon_size_changed).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

        # Max zoom
        f2 = ttk.Frame(size_frame, style="Dark.TFrame")
        f2.pack(fill=tk.X, pady=3)
        ttk.Label(f2, text="Ampliación máxima de la lupa:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.max_zoom_var = tk.DoubleVar(value=float(self.config_mgr.get("appearance", "max_zoom", 1.7)))
        self.max_zoom_lbl = ttk.Label(f2, text=f"{self.max_zoom_var.get():.1f}x", style="Muted.TLabel")
        self.max_zoom_lbl.pack(side=tk.RIGHT)
        ttk.Scale(f2, from_=1.0, to=2.4, variable=self.max_zoom_var, command=self._on_max_zoom_changed).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

        # Wave radius
        f3 = ttk.Frame(size_frame, style="Dark.TFrame")
        f3.pack(fill=tk.X, pady=3)
        ttk.Label(f3, text="Radio de la ola de aumento:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.wave_radius_var = tk.IntVar(value=self.config_mgr.get("appearance", "wave_radius", 140))
        self.wave_radius_lbl = ttk.Label(f3, text=f"{self.wave_radius_var.get()} px", style="Muted.TLabel")
        self.wave_radius_lbl.pack(side=tk.RIGHT)
        ttk.Scale(f3, from_=70, to=220, variable=self.wave_radius_var, command=self._on_wave_radius_changed).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

        # Spacing
        f4 = ttk.Frame(size_frame, style="Dark.TFrame")
        f4.pack(fill=tk.X, pady=3)
        ttk.Label(f4, text="Espaciado entre iconos:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.spacing_var = tk.IntVar(value=self.config_mgr.get("appearance", "icon_spacing", 10))
        self.spacing_lbl = ttk.Label(f4, text=f"{self.spacing_var.get()} px", style="Muted.TLabel")
        self.spacing_lbl.pack(side=tk.RIGHT)
        ttk.Scale(f4, from_=2, to=28, variable=self.spacing_var, command=self._on_spacing_changed).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

        # Colors and Translucency
        color_frame = ttk.LabelFrame(self.tab_appearance, text="  Fondo Translúcido macOS y Colores  ", style="Card.TLabelframe", padding=12)
        color_frame.pack(fill=tk.X, pady=4)

        # Background color
        f5 = ttk.Frame(color_frame, style="Dark.TFrame")
        f5.pack(fill=tk.X, pady=3)
        ttk.Label(f5, text="Color de fondo:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.bg_color_var = tk.StringVar(value=self.config_mgr.get("appearance", "bg_color", "#181a20"))
        self.bg_preview = tk.Label(f5, bg=self.bg_color_var.get(), width=6, relief=tk.SOLID, bd=1)
        self.bg_preview.pack(side=tk.LEFT, padx=10)
        ttk.Button(f5, text="Elegir Color...", style="Dark.TButton", command=self._pick_bg_color).pack(side=tk.LEFT)

        # Background opacity
        f6 = ttk.Frame(color_frame, style="Dark.TFrame")
        f6.pack(fill=tk.X, pady=3)
        ttk.Label(f6, text="Opacidad del fondo:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.opacity_var = tk.DoubleVar(value=float(self.config_mgr.get("appearance", "bg_opacity", 0.82)))
        self.opacity_lbl = ttk.Label(f6, text=f"{int(self.opacity_var.get() * 100)} %", style="Muted.TLabel")
        self.opacity_lbl.pack(side=tk.RIGHT)
        ttk.Scale(f6, from_=0.15, to=1.0, variable=self.opacity_var, command=self._on_opacity_changed).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

        # Corner radius
        f7 = ttk.Frame(color_frame, style="Dark.TFrame")
        f7.pack(fill=tk.X, pady=3)
        ttk.Label(f7, text="Radio de esquinas redondeadas:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.radius_var = tk.IntVar(value=self.config_mgr.get("appearance", "corner_radius", 20))
        self.radius_lbl = ttk.Label(f7, text=f"{self.radius_var.get()} px", style="Muted.TLabel")
        self.radius_lbl.pack(side=tk.RIGHT)
        ttk.Scale(f7, from_=4, to=35, variable=self.radius_var, command=self._on_radius_changed).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

        # Tooltip Labels Customization (Full user controls)
        lbl_frame = ttk.LabelFrame(self.tab_appearance, text="  🏷️ Personalización de Etiquetas Flotantes (Tooltips)  ", style="Card.TLabelframe", padding=12)
        lbl_frame.pack(fill=tk.X, pady=4)

        # Show labels toggle
        self.labels_var = tk.BooleanVar(value=self.config_mgr.get("appearance", "show_labels", True))
        ttk.Checkbutton(lbl_frame, text="Mostrar etiquetas emergentes al pasar el cursor", variable=self.labels_var, style="Dark.TCheckbutton", command=self._on_labels_toggle).pack(anchor=tk.W, pady=2)

        # Font & Size
        f_font = ttk.Frame(lbl_frame, style="Dark.TFrame")
        f_font.pack(fill=tk.X, pady=3)
        ttk.Label(f_font, text="Tipografía:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.label_font_var = tk.StringVar(value=self.config_mgr.get("appearance", "label_font_family", "Segoe UI"))
        font_combo = ttk.Combobox(f_font, textvariable=self.label_font_var, values=["Segoe UI", "Arial", "Consolas", "Trebuchet MS", "Tahoma", "Verdana"], state="readonly", width=14)
        font_combo.pack(side=tk.LEFT, padx=8)
        font_combo.bind("<<ComboboxSelected>>", self._on_label_font_changed)

        ttk.Label(f_font, text="Tamaño fuente:", style="Dark.TLabel").pack(side=tk.LEFT, padx=(10, 0))
        self.label_size_var = tk.IntVar(value=int(self.config_mgr.get("appearance", "label_font_size", 11)))
        self.label_size_lbl = ttk.Label(f_font, text=f"{self.label_size_var.get()} px", style="Muted.TLabel")
        self.label_size_lbl.pack(side=tk.RIGHT)
        ttk.Scale(f_font, from_=8, to=32, variable=self.label_size_var, command=self._on_label_size_changed).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=8)

        # Colors & Opacity
        f_colors = ttk.Frame(lbl_frame, style="Dark.TFrame")
        f_colors.pack(fill=tk.X, pady=3)

        ttk.Label(f_colors, text="Texto:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.label_fg_var = tk.StringVar(value=self.config_mgr.get("appearance", "label_fg", "#ffffff"))
        self.label_fg_preview = tk.Label(f_colors, bg=self.label_fg_var.get(), width=3, relief=tk.SOLID, bd=1)
        self.label_fg_preview.pack(side=tk.LEFT, padx=6)
        ttk.Button(f_colors, text="Color...", style="Dark.TButton", command=self._pick_label_fg_color).pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(f_colors, text="Fondo:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.label_bg_var = tk.StringVar(value=self.config_mgr.get("appearance", "label_bg", "#121318"))
        self.label_bg_preview = tk.Label(f_colors, bg=self.label_bg_var.get(), width=3, relief=tk.SOLID, bd=1)
        self.label_bg_preview.pack(side=tk.LEFT, padx=6)
        ttk.Button(f_colors, text="Color...", style="Dark.TButton", command=self._pick_label_bg_color).pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(f_colors, text="Opacidad:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.label_op_var = tk.DoubleVar(value=float(self.config_mgr.get("appearance", "label_bg_opacity", 0.90)))
        self.label_op_lbl = ttk.Label(f_colors, text=f"{int(self.label_op_var.get() * 100)}%", style="Muted.TLabel")
        self.label_op_lbl.pack(side=tk.RIGHT)
        ttk.Scale(f_colors, from_=0.20, to=1.0, variable=self.label_op_var, command=self._on_label_op_changed).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=8)

    def _on_icon_size_changed(self, v):
        val = int(float(v))
        self.icon_size_lbl.config(text=f"{val} px")
        self.config_mgr.set("appearance", "base_icon_size", val)
        self._trigger_live_update()

    def _on_max_zoom_changed(self, v):
        val = round(float(v), 2)
        self.max_zoom_lbl.config(text=f"{val:.1f}x")
        self.config_mgr.set("appearance", "max_zoom", val)
        self._trigger_live_update()

    def _on_wave_radius_changed(self, v):
        val = int(float(v))
        self.wave_radius_lbl.config(text=f"{val} px")
        self.config_mgr.set("appearance", "wave_radius", val)
        self._trigger_live_update()

    def _on_spacing_changed(self, v):
        val = int(float(v))
        self.spacing_lbl.config(text=f"{val} px")
        self.config_mgr.set("appearance", "icon_spacing", val)
        self._trigger_live_update()

    def _pick_bg_color(self):
        color = colorchooser.askcolor(initialcolor=self.bg_color_var.get(), title="Seleccionar Color de Fondo", parent=self.root)[1]
        if color:
            self.bg_color_var.set(color)
            self.bg_preview.config(bg=color)
            self.config_mgr.set("appearance", "bg_color", color)
            self._trigger_live_update()

    def _on_opacity_changed(self, v):
        val = round(float(v), 2)
        self.opacity_lbl.config(text=f"{int(val * 100)} %")
        self.config_mgr.set("appearance", "bg_opacity", val)
        self._trigger_live_update()

    def _on_radius_changed(self, v):
        val = int(float(v))
        self.radius_lbl.config(text=f"{val} px")
        self.config_mgr.set("appearance", "corner_radius", val)
        self._trigger_live_update()

    def _on_labels_toggle(self):
        self.config_mgr.set("appearance", "show_labels", self.labels_var.get())
        self._trigger_live_update()

    def _on_label_font_changed(self, event=None):
        self.config_mgr.set("appearance", "label_font_family", self.label_font_var.get())
        self._trigger_live_update()

    def _on_label_size_changed(self, v):
        val = int(float(v))
        self.label_size_lbl.config(text=f"{val} px")
        self.config_mgr.set("appearance", "label_font_size", val)
        self._trigger_live_update()

    def _pick_label_fg_color(self):
        color = colorchooser.askcolor(initialcolor=self.label_fg_var.get(), title="Color de Texto de Etiqueta", parent=self.root)[1]
        if color:
            self.label_fg_var.set(color)
            self.label_fg_preview.config(bg=color)
            self.config_mgr.set("appearance", "label_fg", color)
            self._trigger_live_update()

    def _pick_label_bg_color(self):
        color = colorchooser.askcolor(initialcolor=self.label_bg_var.get(), title="Color de Fondo de Etiqueta", parent=self.root)[1]
        if color:
            self.label_bg_var.set(color)
            self.label_bg_preview.config(bg=color)
            self.config_mgr.set("appearance", "label_bg", color)
            self._trigger_live_update()

    def _on_label_op_changed(self, v):
        val = round(float(v), 2)
        self.label_op_lbl.config(text=f"{int(val * 100)}%")
        self.config_mgr.set("appearance", "label_bg_opacity", val)
        self._trigger_live_update()

    # -------------------------------------------------------------
    # Tab 3: Items Manager
    # -------------------------------------------------------------
    def _build_items_tab(self):
        ttk.Label(self.tab_items, text="Organiza, edita o añade accesos directos y acciones del sistema:", style="Dark.TLabel").pack(anchor=tk.W, pady=2)

        content = ttk.Frame(self.tab_items, style="Dark.TFrame")
        content.pack(fill=tk.BOTH, expand=True, pady=6)

        # Listbox with custom dark theme
        self.item_listbox = tk.Listbox(
            content,
            selectmode=tk.SINGLE,
            font=("Segoe UI", 10),
            bg=CARD_BG,
            fg=TEXT_COLOR,
            selectbackground=ACCENT_COLOR,
            selectforeground="#ffffff",
            highlightthickness=1,
            highlightbackground=BORDER_COLOR,
            relief=tk.FLAT
        )
        self.item_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(content, orient=tk.VERTICAL, command=self.item_listbox.yview)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.item_listbox.config(yscrollcommand=scrollbar.set)

        # Action Buttons
        btn_frame = ttk.Frame(content, style="Dark.TFrame")
        btn_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=10)

        ttk.Button(btn_frame, text="▲ Subir", style="Dark.TButton", command=self._item_move_up).pack(fill=tk.X, pady=2)
        ttk.Button(btn_frame, text="▼ Bajar", style="Dark.TButton", command=self._item_move_down).pack(fill=tk.X, pady=2)
        ttk.Separator(btn_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=4)
        ttk.Button(btn_frame, text="⚡ Acción del Sistema...", style="Dark.TButton", command=self._add_system_action_dialog).pack(fill=tk.X, pady=2)
        ttk.Button(btn_frame, text="➕ Añadir Programa", style="Dark.TButton", command=self._item_add_file).pack(fill=tk.X, pady=2)
        ttk.Button(btn_frame, text="➕ Separador", style="Dark.TButton", command=self._item_add_separator).pack(fill=tk.X, pady=2)
        ttk.Separator(btn_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=4)
        ttk.Button(btn_frame, text="🖼️ Cambiar Icono...", style="Dark.TButton", command=self._item_change_icon).pack(fill=tk.X, pady=2)
        ttk.Button(btn_frame, text="✏️ Renombrar", style="Dark.TButton", command=self._item_rename).pack(fill=tk.X, pady=2)
        ttk.Button(btn_frame, text="🔓 Desagrupar", style="Dark.TButton", command=self._item_ungroup).pack(fill=tk.X, pady=2)
        ttk.Separator(btn_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=4)
        ttk.Button(btn_frame, text="🗑️ Eliminar", style="Dark.TButton", command=self._item_delete).pack(fill=tk.X, pady=2)

        self._refresh_item_list()

    def _refresh_item_list(self):
        self.item_listbox.delete(0, tk.END)
        items = self.config_mgr.get_items()
        for it in items:
            it_type = it.get("type", "app")
            if it_type == "separator":
                self.item_listbox.insert(tk.END, "─────────── [ SEPARADOR ] ───────────")
            elif it_type == "group":
                name = it.get("name", "Grupo")
                count = len(it.get("items", []))
                self.item_listbox.insert(tk.END, f"📁 {name}  [{count} elementos]")
            elif it_type == "action":
                name = it.get("name", "Acción")
                self.item_listbox.insert(tk.END, f"⚡ {name}  [Acción del Sistema]")
            else:
                name = it.get("name", "App")
                target = it.get("target", "")
                self.item_listbox.insert(tk.END, f"📌 {name}  ({os.path.basename(target)})")

    def _add_system_action_dialog(self):
        diag = tk.Toplevel(self.root)
        diag.title("Añadir Acción del Sistema")
        diag.geometry("440x480")
        diag.configure(bg=BG_COLOR)
        diag.transient(self.root)
        diag.grab_set()

        try:
            hwnd = int(diag.wm_frame(), 16) if diag.wm_frame() else diag.winfo_id()
            dark_mode = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_mode), 4)
            corner_pref = ctypes.c_int(2)
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner_pref), 4)
        except Exception:
            pass

        ttk.Label(diag, text="Selecciona una acción del sistema para añadir:", style="Dark.TLabel", font=("Segoe UI", 10, "bold")).pack(padx=16, pady=12, anchor=tk.W)

        actions_list = [
            ("show_desktop", "🖥️ Mostrar Escritorio", "Minimiza o restaura todas las ventanas abiertas al instante."),
            ("lock_pc", "🔒 Bloquear Equipo", "Bloquea tu sesión de Windows para proteger tu privacidad."),
            ("taskmgr", "📊 Administrador de Tareas", "Abre el Administrador de Tareas para monitorear CPU y memoria."),
            ("recycle_bin", "🗑️ Papelera de Reciclaje", "Abre la papelera de reciclaje de Windows."),
            ("snipping_tool", "✂️ Herramienta Recortes", "Inicia la captura de pantalla rápida de Windows."),
            ("settings", "⚙️ Configuración de Windows", "Abre el panel principal de Configuración de Windows."),
            ("dock_settings", "🛠️ Configuración de Alioth Dockbar", "Abre este panel de control de la barra.")
        ]

        listbox_frame = ttk.Frame(diag, style="Dark.TFrame")
        listbox_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=4)

        lb = tk.Listbox(
            listbox_frame,
            bg=CARD_BG,
            fg=TEXT_COLOR,
            selectbackground=ACCENT_COLOR,
            selectforeground="#ffffff",
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            highlightthickness=1,
            highlightcolor=ACCENT_COLOR,
            highlightbackground=BORDER_COLOR
        )
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for act_id, title, desc in actions_list:
            lb.insert(tk.END, f"  {title}")

        lb.selection_set(0)

        desc_lbl = ttk.Label(diag, text=actions_list[0][2], style="Muted.TLabel", wraplength=400)
        desc_lbl.pack(padx=16, pady=8, anchor=tk.W)

        def on_select(e):
            sel = lb.curselection()
            if sel:
                desc_lbl.config(text=actions_list[sel[0]][2])

        lb.bind("<<ListboxSelect>>", on_select)

        btn_box = ttk.Frame(diag, style="Dark.TFrame")
        btn_box.pack(fill=tk.X, padx=16, pady=12)

        def do_add():
            sel = lb.curselection()
            if not sel:
                return
            act_id, title, desc = actions_list[sel[0]]
            clean_name = title.split(" ", 1)[1] if " " in title else title
            new_item = {
                "type": "action",
                "action_type": act_id,
                "name": clean_name,
                "icon_path": ""
            }
            self.config_mgr.add_item(new_item)
            self._refresh_item_list()
            self._trigger_live_update()
            diag.destroy()

        ttk.Button(btn_box, text="Cancelar", style="Dark.TButton", command=diag.destroy).pack(side=tk.RIGHT, padx=6)
        ttk.Button(btn_box, text="Añadir al Dock", style="Accent.TButton", command=do_add).pack(side=tk.RIGHT, padx=6)

    def _item_move_up(self):
        sel = self.item_listbox.curselection()
        if sel and sel[0] > 0:
            idx = sel[0]
            self.config_mgr.move_item(idx, idx - 1)
            self._refresh_item_list()
            self.item_listbox.selection_set(idx - 1)
            self._trigger_live_update()

    def _item_move_down(self):
        sel = self.item_listbox.curselection()
        items = self.config_mgr.get_items()
        if sel and sel[0] < len(items) - 1:
            idx = sel[0]
            self.config_mgr.move_item(idx, idx + 1)
            self._refresh_item_list()
            self.item_listbox.selection_set(idx + 1)
            self._trigger_live_update()

    def _item_change_icon(self):
        sel = self.item_listbox.curselection()
        if not sel:
            messagebox.showinfo("Selección", "Por favor selecciona un icono de la lista para cambiar su imagen.", parent=self.root)
            return
        idx = sel[0]
        items = self.config_mgr.get_items()
        it = items[idx]
        if it.get("type") == "separator":
            return

        self.root.lift()
        initial_dir = resolve_resource("assets", "icon_packs")
        if it.get("type") == "group":
            group_icons_dir = os.path.join(initial_dir, "Carpetas de Colores")
            if os.path.isdir(group_icons_dir):
                initial_dir = group_icons_dir
        path = filedialog.askopenfilename(
            parent=self.root,
            initialdir=initial_dir,
            title="Seleccionar Icono (PNG, ICO, DLL, EXE)",
            filetypes=[
                ("Todos los formatos de icono e imagen", "*.png;*.ico;*.dll;*.exe;*.jpg;*.jpeg;*.bmp"),
                ("Imágenes PNG", "*.png"),
                ("Iconos de Windows ICO", "*.ico"),
                ("Librerías DLL / Ejecutables EXE", "*.dll;*.exe"),
                ("Todos los archivos", "*.*")
            ]
        )
        if path:
            cached_path = icon_extractor.import_custom_icon(path)
            if cached_path:
                it["icon_path"] = cached_path
                self.config_mgr.update_item(it["id"], {"icon_path": cached_path})
                self._trigger_live_update()
                messagebox.showinfo("Icono Actualizado", "El icono fue actualizado con éxito.", parent=self.root)
            else:
                messagebox.showwarning("Formato No Soportado", "No se pudo extraer la imagen del archivo seleccionado.", parent=self.root)

    def _item_add_file(self):
        self.root.lift()
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Seleccionar Programa o Archivo",
            filetypes=[("Todos los archivos", "*.*")]
        )
        if path:
            item_data = icon_extractor.resolve_target_and_icon(path)
            new_item = {
                "type": "app",
                "name": item_data["name"],
                "target": item_data["target"],
                "args": "",
                "icon_path": item_data["icon_path"],
                "run_as_admin": False
            }
            self.config_mgr.add_item(new_item)
            self._refresh_item_list()
            self._trigger_live_update()

    def _item_add_separator(self):
        new_sep = {"type": "separator"}
        self.config_mgr.add_item(new_sep)
        self._refresh_item_list()
        self._trigger_live_update()

    def _item_rename(self):
        sel = self.item_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        items = self.config_mgr.get_items()
        it = items[idx]
        if it.get("type") == "separator":
            return

        new_name = simpledialog.askstring("Renombrar", f"Introduce el nuevo nombre para '{it.get('name', '')}':", initialvalue=it.get("name", ""), parent=self.root)
        if new_name is not None and new_name.strip():
            it["name"] = new_name.strip()
            self.config_mgr.update_item(it["id"], {"name": new_name.strip()})
            self._refresh_item_list()
            self.item_listbox.selection_set(idx)
            self._trigger_live_update()

    def _item_ungroup(self):
        sel = self.item_listbox.curselection()
        if not sel:
            messagebox.showinfo("Selección", "Selecciona una carpeta o grupo de la lista para desagrupar.", parent=self.root)
            return
        idx = sel[0]
        items = self.config_mgr.get_items()
        it = items[idx]
        if it.get("type") == "group":
            self.config_mgr.ungroup(it.get("id"))
            self._refresh_item_list()
            self._trigger_live_update()
            messagebox.showinfo("Desagrupado", "Los elementos han sido extraídos a la barra principal.", parent=self.root)
        else:
            messagebox.showinfo("Aviso", "El elemento seleccionado no es un grupo.", parent=self.root)

    def _item_delete(self):
        sel = self.item_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        items = self.config_mgr.get_items()
        it = items[idx]
        self.config_mgr.remove_item(it.get("id"))
        self._refresh_item_list()
        self._trigger_live_update()

    # -------------------------------------------------------------
    # Tab 4: Widgets & Desktop
    # -------------------------------------------------------------
    def _build_widgets_tab(self):
        # 1. Clock Widget Card
        clock_frame = ttk.LabelFrame(self.tab_widgets, text="  🕒 Widget de Reloj y Calendario Digital  ", style="Card.TLabelframe", padding=12)
        clock_frame.pack(fill=tk.X, pady=4)

        self.clock_enabled_var = tk.BooleanVar(value=bool(self.config_mgr.get_widget("clock", "enabled", True)))
        ttk.Checkbutton(clock_frame, text="Activar Reloj y Fecha en el Escritorio", variable=self.clock_enabled_var, style="Dark.TCheckbutton", command=self._on_clock_toggle).pack(anchor=tk.W, pady=2)

        c_opt_frame = ttk.Frame(clock_frame, style="Dark.TFrame")
        c_opt_frame.pack(fill=tk.X, pady=3)

        self.clock_24h_var = tk.BooleanVar(value=bool(self.config_mgr.get_widget("clock", "format_24h", True)))
        ttk.Checkbutton(c_opt_frame, text="Formato 24 Horas (Ej: 14:30 vs 02:30 PM)", variable=self.clock_24h_var, style="Dark.TCheckbutton", command=self._on_clock_format_toggle).pack(side=tk.LEFT, padx=4)

        self.clock_sec_var = tk.BooleanVar(value=bool(self.config_mgr.get_widget("clock", "show_seconds", True)))
        ttk.Checkbutton(c_opt_frame, text="Mostrar Segundos", variable=self.clock_sec_var, style="Dark.TCheckbutton", command=self._on_clock_sec_toggle).pack(side=tk.LEFT, padx=12)

        c_slider_frame = ttk.Frame(clock_frame, style="Dark.TFrame")
        c_slider_frame.pack(fill=tk.X, pady=3)
        ttk.Label(c_slider_frame, text="Opacidad del Fondo:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.clock_op_var = tk.IntVar(value=int(float(self.config_mgr.get_widget("clock", "opacity", 0.88)) * 100))
        self.clock_op_lbl = ttk.Label(c_slider_frame, text=f"{self.clock_op_var.get()}%", style="Muted.TLabel")
        self.clock_op_lbl.pack(side=tk.RIGHT)
        ttk.Scale(c_slider_frame, from_=20, to=100, variable=self.clock_op_var, command=self._on_clock_opacity_change).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

        c_btn_frame = ttk.Frame(clock_frame, style="Dark.TFrame")
        c_btn_frame.pack(fill=tk.X, pady=3)
        self.clock_pinned_var = tk.BooleanVar(value=bool(self.config_mgr.get_widget("clock", "pinned_to_desktop", True)))
        ttk.Checkbutton(c_btn_frame, text="Fijar al fondo del escritorio (detrás de ventanas normales)", variable=self.clock_pinned_var, style="Dark.TCheckbutton", command=self._on_clock_pinned_toggle).pack(side=tk.LEFT)
        ttk.Button(c_btn_frame, text="Restablecer Posición", style="Dark.TButton", command=lambda: self._reset_widget_pos("clock", 24, 48)).pack(side=tk.RIGHT)

        # 2. System Monitor Card
        sys_frame = ttk.LabelFrame(self.tab_widgets, text="  📊 Widget de Monitor de Recursos (CPU, RAM y Disco C:)  ", style="Card.TLabelframe", padding=12)
        sys_frame.pack(fill=tk.X, pady=4)

        self.sys_enabled_var = tk.BooleanVar(value=bool(self.config_mgr.get_widget("system_monitor", "enabled", True)))
        ttk.Checkbutton(sys_frame, text="Activar Monitor de Recursos en el Escritorio", variable=self.sys_enabled_var, style="Dark.TCheckbutton", command=self._on_sys_toggle).pack(anchor=tk.W, pady=2)

        s_slider_frame = ttk.Frame(sys_frame, style="Dark.TFrame")
        s_slider_frame.pack(fill=tk.X, pady=3)
        ttk.Label(s_slider_frame, text="Opacidad del Fondo:", style="Dark.TLabel").pack(side=tk.LEFT)
        self.sys_op_var = tk.IntVar(value=int(float(self.config_mgr.get_widget("system_monitor", "opacity", 0.88)) * 100))
        self.sys_op_lbl = ttk.Label(s_slider_frame, text=f"{self.sys_op_var.get()}%", style="Muted.TLabel")
        self.sys_op_lbl.pack(side=tk.RIGHT)
        ttk.Scale(s_slider_frame, from_=20, to=100, variable=self.sys_op_var, command=self._on_sys_opacity_change).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

        s_btn_frame = ttk.Frame(sys_frame, style="Dark.TFrame")
        s_btn_frame.pack(fill=tk.X, pady=3)
        self.sys_pinned_var = tk.BooleanVar(value=bool(self.config_mgr.get_widget("system_monitor", "pinned_to_desktop", True)))
        ttk.Checkbutton(s_btn_frame, text="Fijar al fondo del escritorio", variable=self.sys_pinned_var, style="Dark.TCheckbutton", command=self._on_sys_pinned_toggle).pack(side=tk.LEFT)
        ttk.Button(s_btn_frame, text="Restablecer Posición", style="Dark.TButton", command=lambda: self._reset_widget_pos("system_monitor", 24, 170)).pack(side=tk.RIGHT)

        # 3. Desktop Mini Dock Card
        mini_frame = ttk.LabelFrame(self.tab_widgets, text="  ⚡ Mini Dock de Accesos Rápidos en Escritorio  ", style="Card.TLabelframe", padding=12)
        mini_frame.pack(fill=tk.X, pady=4)

        self.mini_enabled_var = tk.BooleanVar(value=bool(self.config_mgr.get_widget("desktop_minidock", "enabled", False)))
        ttk.Checkbutton(mini_frame, text="Activar Mini Dock flotante en el Escritorio", variable=self.mini_enabled_var, style="Dark.TCheckbutton", command=self._on_mini_toggle).pack(anchor=tk.W, pady=2)

        m_btn_frame = ttk.Frame(mini_frame, style="Dark.TFrame")
        m_btn_frame.pack(fill=tk.X, pady=3)
        self.mini_pinned_var = tk.BooleanVar(value=bool(self.config_mgr.get_widget("desktop_minidock", "pinned_to_desktop", True)))
        ttk.Checkbutton(m_btn_frame, text="Fijar al fondo del escritorio", variable=self.mini_pinned_var, style="Dark.TCheckbutton", command=self._on_mini_pinned_toggle).pack(side=tk.LEFT)
        ttk.Button(m_btn_frame, text="Restablecer Posición", style="Dark.TButton", command=lambda: self._reset_widget_pos("desktop_minidock", 24, 340)).pack(side=tk.RIGHT)

    def _on_clock_toggle(self):
        self.config_mgr.set_widget("clock", "enabled", self.clock_enabled_var.get())
        self._trigger_live_update()

    def _on_clock_format_toggle(self):
        self.config_mgr.set_widget("clock", "format_24h", self.clock_24h_var.get())
        self._trigger_live_update()

    def _on_clock_sec_toggle(self):
        self.config_mgr.set_widget("clock", "show_seconds", self.clock_sec_var.get())
        self._trigger_live_update()

    def _on_clock_opacity_change(self, v):
        val = int(float(v))
        self.clock_op_lbl.config(text=f"{val}%")
        self.config_mgr.set_widget("clock", "opacity", round(val / 100.0, 2))
        self._trigger_live_update()

    def _on_clock_pinned_toggle(self):
        self.config_mgr.set_widget("clock", "pinned_to_desktop", self.clock_pinned_var.get())
        self._trigger_live_update()

    def _on_sys_toggle(self):
        self.config_mgr.set_widget("system_monitor", "enabled", self.sys_enabled_var.get())
        self._trigger_live_update()

    def _on_sys_opacity_change(self, v):
        val = int(float(v))
        self.sys_op_lbl.config(text=f"{val}%")
        self.config_mgr.set_widget("system_monitor", "opacity", round(val / 100.0, 2))
        self._trigger_live_update()

    def _on_sys_pinned_toggle(self):
        self.config_mgr.set_widget("system_monitor", "pinned_to_desktop", self.sys_pinned_var.get())
        self._trigger_live_update()

    def _on_mini_toggle(self):
        self.config_mgr.set_widget("desktop_minidock", "enabled", self.mini_enabled_var.get())
        self._trigger_live_update()

    def _on_mini_pinned_toggle(self):
        self.config_mgr.set_widget("desktop_minidock", "pinned_to_desktop", self.mini_pinned_var.get())
        self._trigger_live_update()

    def _reset_widget_pos(self, w_id, def_x, def_y):
        self.config_mgr.set_widget(w_id, "x", def_x)
        self.config_mgr.set_widget(w_id, "y", def_y)
        self._trigger_live_update()

    # -------------------------------------------------------------
    # Footer
    # -------------------------------------------------------------
    def _save_and_close(self):
        try:
            self.root.update_idletasks()
            self.config_mgr.data["panel_bounds"] = {
                "width": max(640, int(self.root.winfo_width())),
                "height": max(540, int(self.root.winfo_height())),
                "x": int(self.root.winfo_x()),
                "y": int(self.root.winfo_y()),
            }
            self.config_mgr.save()
        except Exception:
            pass
        animate_window(self.root, showing=False, on_done=self.root.destroy)

    def _build_footer(self):
        footer = ttk.Frame(self.root, style="Dark.TFrame")
        footer.pack(fill=tk.X, padx=14, pady=10)

        ttk.Button(footer, text="Cerrar", style="Dark.TButton", command=self._save_and_close).pack(side=tk.RIGHT, padx=4)
        ttk.Button(footer, text="🌐 Abrir Panel Web (HTML)", style="Dark.TButton", command=self._open_web_settings).pack(side=tk.RIGHT, padx=6)
        ttk.Label(footer, text=f"✨ Alioth Dockbar v{APP_VERSION} · Los cambios se aplican automáticamente en tiempo real.", style="Muted.TLabel").pack(side=tk.LEFT, padx=4)

    def _open_web_settings(self):
        import settings_server
        settings_server.open_settings_ui(self.config_mgr, on_applied=self.on_applied, tk_root=self.root)
