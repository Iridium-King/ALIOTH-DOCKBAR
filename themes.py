"""
Theme engine for Alioth Dockbar:
Applies curated visual themes (macOS X, Windows 11, Windows 10, Android Material,
Cyberpunk Neón, Minimal Cristal), maps item icons cleanly to matching icon packs,
and saves/restores user custom themes.
"""
import copy
import os
import re
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PACKS_ROOT = os.path.join(BASE_DIR, "assets", "icon_packs")

BUILTIN_THEMES = [
    {
        "id": "macos_x",
        "name": "macOS X / Sequoia",
        "desc": "Cristal esmerilado flotante, iconos estilo Apple y punto indicador blanco",
        "swatch": ["#252b3b", "#60a5fa"],
        "packs": ["macOS", "macOS Carpetas", "Apps Originales"],
        "appearance": {
            "bg_color": "#1e222d",
            "bg_opacity": 0.78,
            "corner_radius": 24,
            "round_all_corners": True,
            "border_enabled": True,
            "border_color": "#ffffff",
            "border_opacity": 0.24,
            "border_width": 1,
            "border_style": "solid",
            "separator_color": "#ffffff",
            "separator_opacity": 0.32,
            "separator_width": 2,
            "separator_style": "solid",
            "indicator_style": "dot",
            "indicator_color": "#e2e8f0",
            "indicator_size": 5,
            "launch_effect": "gota_rebote",
            "launch_effect_color": "#60a5fa",
        },
    },
    {
        "id": "windows_11",
        "name": "Windows 11 Fluent",
        "desc": "Mica oscura moderna con acento azul Fluent e indicador tipo píldora",
        "swatch": ["#141926", "#38bdf8"],
        "packs": ["Windows 11 Fluent", "Carpetas de Colores", "Apps Originales"],
        "appearance": {
            "bg_color": "#141926",
            "bg_opacity": 0.86,
            "corner_radius": 16,
            "round_all_corners": True,
            "border_enabled": True,
            "border_color": "#60a5fa",
            "border_opacity": 0.28,
            "border_width": 1,
            "border_style": "solid",
            "separator_color": "#94a3b8",
            "separator_opacity": 0.30,
            "separator_width": 2,
            "separator_style": "solid",
            "indicator_style": "pill",
            "indicator_color": "#38bdf8",
            "indicator_size": 6,
            "launch_effect": "gota_energia",
            "launch_effect_color": "#38bdf8",
        },
    },
    {
        "id": "windows_10",
        "name": "Windows 10 Clásico",
        "desc": "Barra oscura plana de bordes rectos, línea inferior acentuada y estilo sólido",
        "swatch": ["#101216", "#0078d7"],
        "packs": ["Apps Originales", "Windows 11 Fluent", "Carpetas de Colores"],
        "appearance": {
            "bg_color": "#0e1014",
            "bg_opacity": 0.94,
            "corner_radius": 4,
            "round_all_corners": False,
            "border_enabled": True,
            "border_color": "#0078d7",
            "border_opacity": 0.45,
            "border_width": 2,
            "border_style": "solid",
            "separator_color": "#cbd5e1",
            "separator_opacity": 0.40,
            "separator_width": 2,
            "separator_style": "solid",
            "indicator_style": "line",
            "indicator_color": "#0078d7",
            "indicator_size": 6,
            "launch_effect": "levitacion",
            "launch_effect_color": "#0ea5e9",
        },
    },
    {
        "id": "android_material",
        "name": "Android Material You",
        "desc": "Píldora superredondeada en tono esmeralda/verde azulado con iconos Samsung One UI",
        "swatch": ["#132620", "#34d399"],
        "packs": ["Samsung One UI", "Carpetas Frosted Glass", "Acciones Pro"],
        "appearance": {
            "bg_color": "#13221e",
            "bg_opacity": 0.88,
            "corner_radius": 32,
            "round_all_corners": True,
            "border_enabled": True,
            "border_color": "#34d399",
            "border_opacity": 0.38,
            "border_width": 2,
            "border_style": "solid",
            "separator_color": "#6ee7b7",
            "separator_opacity": 0.35,
            "separator_width": 3,
            "separator_style": "dotted",
            "indicator_style": "pill",
            "indicator_color": "#34d399",
            "indicator_size": 7,
            "launch_effect": "pop_elastico",
            "launch_effect_color": "#34d399",
        },
    },
    {
        "id": "cyberpunk_neon",
        "name": "Cyberpunk Neón",
        "desc": "Borde brillante doble en cian eléctrico sobre negro OLED e iconos cristal",
        "swatch": ["#07090f", "#22d3ee"],
        "packs": ["Outlinear Minimal", "Cristal Minimal", "Acciones Pro"],
        "appearance": {
            "bg_color": "#07090f",
            "bg_opacity": 0.92,
            "corner_radius": 18,
            "round_all_corners": True,
            "border_enabled": True,
            "border_color": "#22d3ee",
            "border_opacity": 0.85,
            "border_width": 2,
            "border_style": "neon",
            "separator_color": "#22d3ee",
            "separator_opacity": 0.65,
            "separator_width": 2,
            "separator_style": "glow",
            "indicator_style": "diamond",
            "indicator_color": "#22d3ee",
            "indicator_size": 7,
            "launch_effect": "pulso_neon",
            "launch_effect_color": "#22d3ee",
        },
    },
    {
        "id": "minimal_cristal",
        "name": "Minimal Cristal",
        "desc": "Alta transparencia elegante, borde punteado sutil y estética limpia",
        "swatch": ["#181c24", "#e2e8f0"],
        "packs": ["Cristal Minimal", "Apps Originales"],
        "appearance": {
            "bg_color": "#161922",
            "bg_opacity": 0.56,
            "corner_radius": 22,
            "round_all_corners": True,
            "border_enabled": True,
            "border_color": "#e2e8f0",
            "border_opacity": 0.28,
            "border_width": 1,
            "border_style": "solid",
            "separator_color": "#cbd5e1",
            "separator_opacity": 0.25,
            "separator_width": 1,
            "separator_style": "dashed",
            "indicator_style": "ring",
            "indicator_color": "#f8fafc",
            "indicator_size": 6,
            "launch_effect": "gota_energia",
            "launch_effect_color": "#38bdf8",
        },
    },
]

_PACK_SYNONYMS = {
    "edge": ["edge", "safari", "navegador"],
    "chrome": ["chrome", "safari", "navegador"],
    "outlook": ["outlook", "mail", "correo"],
    "teams": ["teams", "messages", "chat"],
    "whatsapp": ["whatsapp", "messages"],
    "excel": ["excel", "numbers", "stocks"],
    "word": ["word", "pages", "notes"],
    "notepad": ["notes", "textedit", "bloc", "notas"],
    "bloc": ["notes", "textedit", "notas"],
    "explorer": ["explorer", "finder", "carpeta", "folder", "files"],
    "explorador": ["explorer", "finder", "carpeta", "folder"],
    "code": ["vscode", "xcode", "code", "codigo", "terminal"],
    "vscode": ["vscode", "xcode", "code", "codigo"],
    "terminal": ["terminal", "consola", "cmd", "powershell"],
    "powershell": ["terminal", "powershell"],
    "pdf": ["adobe acrobat pdf", "adobe pdf", "pdf", "preview", "books"],
    "acrobat": ["adobe acrobat pdf", "adobe pdf", "pdf"],
    "settings": ["system settings", "ajustes", "settings"],
    "ajustes": ["system settings", "ajustes", "settings"],
    "calc": ["calculator", "calculadora"],
    "calculadora": ["calculator", "calculadora"],
    "clock": ["clock", "reloj"],
    "calendar": ["calendar", "calendario"],
    "chatgpt": ["chatgpt", "ia", "ai"],
    "claude": ["claude", "ia", "ai"],
    "gemini": ["gemini", "ia", "ai"],
    "spotify": ["spotify", "music", "musica"],
    "music": ["music", "musica", "spotify"],
    "recycle_bin": ["trash", "papelera"],
    "show_desktop": ["desktop", "mission control", "escritorio"],
    "snipping_tool": ["capture", "screenshot", "recortes"],
    "lock_pc": ["lock", "bloquear", "candado"],
    "app_launcher": ["apps", "launch", "finder", "dock"],
}


def _build_pack_index(pack_names):
    """Returns ordered list of (icon_basename_lower, full_png_path) across requested packs."""
    index = []
    for pname in pack_names:
        pdir = os.path.join(PACKS_ROOT, pname)
        if not os.path.isdir(pdir):
            continue
        try:
            for fn in sorted(os.listdir(pdir)):
                if fn.lower().endswith(".png"):
                    stem = os.path.splitext(fn)[0].lower()
                    index.append((stem, os.path.join(pdir, fn)))
        except Exception:
            pass
    return index


def _match_icon_for_item(item, pack_index):
    if not pack_index or not item:
        return None
    if item.get("type") == "group":
        group_tokens = ["folder", "carpeta", "dark folder", "light home", "apps"]
        for tok in group_tokens:
            for stem, path in pack_index:
                if stem == tok or tok in stem:
                    return path
        return None

    name_l = (item.get("name") or "").lower()
    tgt_base = os.path.splitext(os.path.basename(item.get("target") or ""))[0].lower()
    act_l = (item.get("action_type") or "").lower()
    raw_words = re.split(r"[^a-z0-9áéíóúñ]+", f"{name_l} {tgt_base} {act_l}")
    tokens = [w for w in raw_words if len(w) >= 2]
    expanded = list(tokens)
    for w in tokens:
        expanded.extend(_PACK_SYNONYMS.get(w, []))
    if act_l in _PACK_SYNONYMS:
        expanded.extend(_PACK_SYNONYMS[act_l])

    best_path = None
    best_score = 0
    for stem, path in pack_index:
        score = 0
        for idx_t, tok in enumerate(expanded):
            if not tok:
                continue
            weight = 3 if idx_t < len(tokens) else 1
            if stem == tok:
                score += 20 * weight
            elif len(tok) >= 3 and (tok in stem or stem in tok):
                score += 8 * weight
        if score > best_score:
            best_score = score
            best_path = path
    return best_path if best_score >= 8 else None


def get_themes_payload(config_mgr):
    presets = [
        {
            "id": t["id"],
            "name": t["name"],
            "desc": t["desc"],
            "swatch": t["swatch"],
            "builtin": True,
        }
        for t in BUILTIN_THEMES
    ]
    saved_map = config_mgr.data.get("saved_themes") or {}
    saved = []
    if isinstance(saved_map, dict):
        for tid, tdata in saved_map.items():
            if isinstance(tdata, dict):
                app = tdata.get("appearance") or {}
                saved.append({
                    "id": tid,
                    "name": tdata.get("name", tid),
                    "saved_at": tdata.get("saved_at", ""),
                    "swatch": [app.get("bg_color", "#181a20"), app.get("border_color", "#60a5fa")],
                    "icon_count": len(tdata.get("icon_map", {})),
                    "builtin": False,
                })
    return {
        "active_theme": config_mgr.data.get("active_theme", ""),
        "presets": presets,
        "saved": saved,
    }


def apply_theme(config_mgr, theme_id, apply_icons=True):
    # 1. Check saved custom user themes first
    saved_map = config_mgr.data.get("saved_themes") or {}
    if theme_id in saved_map:
        tdata = saved_map[theme_id]
        app_snap = copy.deepcopy(tdata.get("appearance") or {})
        config_mgr.data.setdefault("appearance", {}).update(app_snap)
        config_mgr._dirty_sections.add("appearance")

        if apply_icons and isinstance(tdata.get("icon_map"), dict):
            imap = tdata["icon_map"]
            items = config_mgr.get_items()
            for it in items:
                iid = it.get("id")
                if iid and iid in imap:
                    it["icon_path"] = imap[iid]
                if it.get("type") == "group":
                    for sub in it.get("items", it.get("children", [])):
                        sid = sub.get("id")
                        if sid and sid in imap:
                            sub["icon_path"] = imap[sid]
            config_mgr.data["items"] = items
            config_mgr._dirty_sections.add("items")

        config_mgr.data["active_theme"] = theme_id
        config_mgr._dirty_sections.add("active_theme")
        config_mgr.save()
        return {"ok": True, "theme_id": theme_id, "custom": True}

    # 2. Built-in presets
    preset = next((t for t in BUILTIN_THEMES if t["id"] == theme_id), None)
    if not preset:
        return {"ok": False, "error": "Theme not found"}

    config_mgr.data.setdefault("appearance", {}).update(copy.deepcopy(preset["appearance"]))
    config_mgr._dirty_sections.add("appearance")

    matched_icons = 0
    if apply_icons:
        import icon_extractor
        pack_idx = _build_pack_index(preset["packs"])
        items = config_mgr.get_items()
        for it in items:
            it_type = it.get("type", "app")
            if it_type in ("app", "group", "action"):
                png_path = _match_icon_for_item(it, pack_idx)
                if png_path and os.path.exists(png_path):
                    it["icon_path"] = icon_extractor.import_custom_icon(png_path) or png_path
                    matched_icons += 1
            if it_type == "group":
                for sub in it.get("items", it.get("children", [])):
                    spng = _match_icon_for_item(sub, pack_idx)
                    if spng and os.path.exists(spng):
                        sub["icon_path"] = icon_extractor.import_custom_icon(spng) or spng
                        matched_icons += 1
        config_mgr.data["items"] = items
        config_mgr._dirty_sections.add("items")

    config_mgr.data["active_theme"] = theme_id
    config_mgr._dirty_sections.add("active_theme")
    config_mgr.save()
    return {"ok": True, "theme_id": theme_id, "matched_icons": matched_icons, "custom": False}


def save_custom_theme(config_mgr, name):
    clean_name = str(name or "").strip()[:32] or f"Mi Tema {time.strftime('%H:%M')}"
    slug = re.sub(r"[^a-z0-9]+", "_", clean_name.lower()).strip("_") or "tema"
    theme_id = f"custom_{slug}_{int(time.time()) % 10000}"

    icon_map = {}
    for it in config_mgr.get_items():
        if it.get("id"):
            icon_map[it["id"]] = it.get("icon_path", "")
        if it.get("type") == "group":
            for sub in it.get("items", it.get("children", [])):
                if sub.get("id"):
                    icon_map[sub["id"]] = sub.get("icon_path", "")

    saved_map = config_mgr.data.setdefault("saved_themes", {})
    saved_map[theme_id] = {
        "name": clean_name,
        "saved_at": time.strftime("%Y-%m-%d %H:%M"),
        "appearance": copy.deepcopy(config_mgr.data.get("appearance") or {}),
        "icon_map": icon_map,
    }
    config_mgr.data["active_theme"] = theme_id
    config_mgr._dirty_sections.add("saved_themes")
    config_mgr._dirty_sections.add("active_theme")
    config_mgr.save()
    return {"ok": True, "theme_id": theme_id, "name": clean_name}


def delete_custom_theme(config_mgr, theme_id):
    saved_map = config_mgr.data.setdefault("saved_themes", {})
    if theme_id in saved_map:
        del saved_map[theme_id]
        if config_mgr.data.get("active_theme") == theme_id:
            config_mgr.data["active_theme"] = ""
            config_mgr._dirty_sections.add("active_theme")
        config_mgr._dirty_sections.add("saved_themes")
        config_mgr.save()
        return {"ok": True}
    return {"ok": False, "error": "Not found"}
