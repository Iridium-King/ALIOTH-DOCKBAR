import os
import json
import time
import winreg
import threading
import unicodedata

try:
    import pythoncom
    import win32com.client
    HAS_WIN32COM = True
except Exception:
    HAS_WIN32COM = False

_catalog_lock = threading.RLock()
_cached_catalog = []
_catalog_timestamp = 0
_cached_pwas = []
_pwas_timestamp = 0


def clear_caches():
    """Releases cached app catalog & PWA lists from RAM when Settings UI closes."""
    global _cached_catalog, _catalog_timestamp, _cached_pwas, _pwas_timestamp
    with _catalog_lock:
        _cached_catalog = []
        _catalog_timestamp = 0
        _cached_pwas = []
        _pwas_timestamp = 0

def _iter_shortcut_files():
    """Yields (full_path, source_tag) efficiently: recursive only in Start Menu, shallow in Desktop."""
    start_dirs = [
        (os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"), "Menú Inicio"),
        (os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"), "Menú Inicio"),
    ]
    desk_dirs = [
        (os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"), "Escritorio"),
        (os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop"), "Escritorio"),
    ]
    user_prof = os.environ.get("USERPROFILE", "")
    if user_prof and os.path.isdir(user_prof):
        try:
            for entry in os.listdir(user_prof):
                if entry.lower().startswith("onedrive"):
                    for sub in ("Desktop", "Escritorio"):
                        od_desk = os.path.join(user_prof, entry, sub)
                        if os.path.isdir(od_desk):
                            desk_dirs.append((od_desk, "Escritorio"))
        except Exception:
            pass

    for d, tag in start_dirs:
        if d and os.path.isdir(d):
            for root, _, files in os.walk(d):
                for fname in files:
                    yield os.path.join(root, fname), fname, tag

    for d, tag in desk_dirs:
        if d and os.path.isdir(d):
            try:
                for fname in os.listdir(d):
                    full = os.path.join(d, fname)
                    if os.path.isfile(full):
                        yield full, fname, tag
            except Exception:
                pass

def _norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()

def _is_ignored_name(name_lower):
    bad = ("uninstall", "desinstalar", "readme", "léame", "help", "ayuda", "license", "release notes", "website", "changelog", "manual")
    return any(b in name_lower for b in bad)

def _clean_path(raw):
    if not raw:
        return ""
    p = raw.strip()
    if p.startswith('"') and '"' in p[1:]:
        p = p[1:p.index('"', 1)]
    elif "," in p and not os.path.exists(p):
        p = p.split(",")[0].strip().strip('"')
    p = os.path.expandvars(p).strip('"').strip()
    return p

def _get_shortcut_dirs():
    dirs = [
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
        os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop"),
    ]
    user_prof = os.environ.get("USERPROFILE", "")
    if user_prof and os.path.isdir(user_prof):
        try:
            for entry in os.listdir(user_prof):
                if entry.lower().startswith("onedrive"):
                    for sub in ("Desktop", "Escritorio"):
                        od_desk = os.path.join(user_prof, entry, sub)
                        if os.path.isdir(od_desk):
                            dirs.append(od_desk)
        except Exception:
            pass
    return dirs

def get_installed_pwas(force=False):
    """
    Discovers installed Chrome and Edge Web Apps (PWAs) directly from Start Menu,
    Desktop shortcuts, and browser Web Applications folders without spawning any shell.
    """
    global _cached_pwas, _pwas_timestamp
    if not force and _cached_pwas and (time.time() - _pwas_timestamp < 120):
        return _cached_pwas

    pwas = []
    seen_ids = set()

    shell = None
    if HAS_WIN32COM:
        try:
            pythoncom.CoInitialize()
            shell = win32com.client.Dispatch("WScript.Shell")
        except Exception:
            shell = None

    # 1. Scan .lnk shortcuts (recursive only in Start Menu, shallow on Desktop)
    if shell is not None:
        for full, fname, _ in _iter_shortcut_files():
            if not fname.lower().endswith(".lnk"):
                continue
            try:
                sc = shell.CreateShortCut(full)
                tgt = _clean_path(sc.Targetpath)
                args = (sc.Arguments or "").strip()
                low_t = tgt.lower()
                low_a = args.lower()
                is_pwa = (
                    "chrome_proxy.exe" in low_t
                    or "msedge_proxy.exe" in low_t
                    or "--app-id=" in low_a
                    or "--app=" in low_a
                )
                if not is_pwa or not tgt:
                    continue

                app_id = ""
                if "--app-id=" in low_a:
                    part = args.split("--app-id=")[-1].strip()
                    app_id = part.split()[0].strip('"')
                key = app_id or (tgt.lower() + "|" + low_a)
                if key in seen_ids:
                    continue
                seen_ids.add(key)

                ic_loc = _clean_path(sc.IconLocation)
                if not (ic_loc and os.path.exists(ic_loc)):
                    ic_loc = tgt
                browser_tag = "Edge PWA" if "edge" in low_t else "Chrome PWA"
                disp_name = os.path.splitext(fname)[0].strip()
                pwas.append({
                    "id": f"pwa_{app_id[:12]}" if app_id else f"pwa_{abs(hash(key)) & 0xFFFFFF:x}",
                    "app_id": app_id,
                    "name": disp_name,
                    "target": tgt,
                    "args": args,
                    "icon_src": ic_loc,
                    "browser": browser_tag,
                })
            except Exception:
                continue

    # 2. Scan browser Web Applications\_crx_* folders for any additional PWAs
    local_app = os.environ.get("LOCALAPPDATA", "")
    browser_specs = [
        (
            "Chrome PWA",
            os.path.join(local_app, r"Google\Chrome\User Data"),
            [r"C:\Program Files\Google\Chrome\Application\chrome_proxy.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome_proxy.exe"],
        ),
        (
            "Edge PWA",
            os.path.join(local_app, r"Microsoft\Edge\User Data"),
            [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge_proxy.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge_proxy.exe"],
        ),
    ]
    for b_tag, user_data_dir, proxy_candidates in browser_specs:
        if not os.path.isdir(user_data_dir):
            continue
        proxy_exe = next((p for p in proxy_candidates if os.path.exists(p)), "")
        if not proxy_exe:
            continue
        try:
            for prof in os.listdir(user_data_dir):
                wa_dir = os.path.join(user_data_dir, prof, "Web Applications")
                if not os.path.isdir(wa_dir):
                    continue
                for folder in os.listdir(wa_dir):
                    if not folder.startswith("_crx_"):
                        continue
                    app_id = folder[5:]
                    if not app_id or app_id in seen_ids:
                        continue
                    app_folder = os.path.join(wa_dir, folder)
                    if not os.path.isdir(app_folder):
                        continue
                    ico_file = next((os.path.join(app_folder, f) for f in os.listdir(app_folder) if f.lower().endswith(".ico")), "")
                    if not ico_file:
                        continue
                    seen_ids.add(app_id)
                    disp_name = os.path.splitext(os.path.basename(ico_file))[0].strip()
                    pwas.append({
                        "id": f"pwa_{app_id[:12]}",
                        "app_id": app_id,
                        "name": disp_name,
                        "target": proxy_exe,
                        "args": f'--profile-directory="{prof}" --app-id={app_id}',
                        "icon_src": ico_file,
                        "browser": b_tag,
                    })
        except Exception:
            pass

    # 3. Scan Windows shell:AppsFolder via pure COM (Edge/Store PWAs & MSIX apps like ChatGPT, Codex, Claude, WhatsApp)
    for app_item in _scan_apps_folder_com():
        if app_item.get("is_pwa"):
            aumid = app_item.get("aumid", "")
            key = f"aumid:{aumid.lower()}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            pwas.append({
                "id": f"pwa_{abs(hash(key)) & 0xFFFFFF:x}",
                "app_id": aumid,
                "name": app_item["name"],
                "target": app_item["target"],
                "args": app_item["args"],
                "icon_src": app_item["icon_src"],
                "browser": app_item["source"],
            })

    pwas.sort(key=lambda x: _norm(x["name"]))
    _cached_pwas = pwas
    _pwas_timestamp = time.time()
    return pwas

_KNOWN_PWA_HINTS = (
    "chatgpt", "codex", "claude", "gemini", "whatsapp", "keep",
    "copilot", "teams", "outlook", "canva", "spotify", "notion",
    "slack", "discord", "telegram", "youtube", "gmail", "drive",
    "calendar", "meet", "figma", "linear", "trello", "todoist",
    "perplexity", "deepseek", "grok", "proxy", ".website", "openai", "anthropic"
)

def _scan_apps_folder_com():
    """Enumerates Windows shell:AppsFolder via pure Shell.Application COM (zero PowerShell/shell calls)."""
    results = []
    if not HAS_WIN32COM:
        return results
    try:
        pythoncom.CoInitialize()
        sh = win32com.client.Dispatch("Shell.Application")
        ns = sh.NameSpace("shell:AppsFolder")
        if ns is None:
            return results
        items = ns.Items()
        count = items.Count
        seen_aumids = set()
        for idx in range(count):
            try:
                it = items.Item(idx)
                name = str(getattr(it, "Name", "") or "").strip()
                aumid = str(getattr(it, "Path", "") or "").strip()
                if not name or not aumid:
                    continue
                low_name = name.lower()
                low_aumid = aumid.lower()
                if _is_ignored_name(low_name) or aumid in seen_aumids:
                    continue
                seen_aumids.add(aumid)

                is_store_or_pwa = ("!" in aumid) or aumid.endswith(".Website") or low_aumid.startswith("com.squirrel.")
                looks_like_web_domain = (
                    ".com-" in low_aumid or ".ai-" in low_aumid or ".org-" in low_aumid
                    or ".app-" in low_aumid or ".io-" in low_aumid or ".net-" in low_aumid
                )
                is_pwa = is_store_or_pwa and (
                    looks_like_web_domain
                    or any(h in low_aumid or h in low_name for h in _KNOWN_PWA_HINTS)
                )

                disp_name = name
                if "codex" in low_aumid:
                    disp_name = "Codex (ChatGPT / OpenAI)"
                elif looks_like_web_domain and "(pwa)" not in low_name:
                    disp_name = f"{name} (PWA)"

                if is_store_or_pwa or not os.path.exists(aumid):
                    target = "explorer.exe"
                    args = f"shell:AppsFolder\\{aumid}"
                    icon_src = f"shell:AppsFolder\\{aumid}"
                    if looks_like_web_domain:
                        source_tag = "PWA Web"
                    elif is_pwa:
                        source_tag = "App Store / MSIX"
                    else:
                        source_tag = "Windows App"
                else:
                    target = aumid
                    args = ""
                    icon_src = aumid
                    source_tag = "AppsFolder"

                results.append({
                    "name": disp_name,
                    "aumid": aumid,
                    "target": target,
                    "args": args,
                    "icon_src": icon_src,
                    "source": source_tag,
                    "is_pwa": bool(is_pwa),
                })
            except Exception:
                continue
    except Exception:
        pass
    return results

def _scan_shortcuts():
    results = []
    shell = None
    if HAS_WIN32COM:
        try:
            pythoncom.CoInitialize()
            shell = win32com.client.Dispatch("WScript.Shell")
        except Exception:
            shell = None

    seen_lnks = set()
    for full, fname, source_tag in _iter_shortcut_files():
        low = fname.lower()
        if not low.endswith((".lnk", ".url", ".exe", ".bat")):
            continue
        if _is_ignored_name(low) or full in seen_lnks:
            continue
        seen_lnks.add(full)

        disp_name = os.path.splitext(fname)[0]
        target = full
        args = ""
        icon_src = full

        if low.endswith(".lnk") and shell is not None:
            try:
                sc = shell.CreateShortCut(full)
                t = _clean_path(sc.Targetpath)
                if t:
                    target = t
                    args = sc.Arguments or ""
                    ic_loc = _clean_path(sc.IconLocation)
                    if ic_loc and os.path.exists(ic_loc):
                        icon_src = ic_loc
                    elif os.path.exists(t):
                        icon_src = t
            except Exception:
                pass

        if _is_ignored_name(disp_name.lower()) or _is_ignored_name(os.path.basename(target).lower()):
            continue

        results.append({
            "name": disp_name,
            "target": target,
            "args": args,
            "icon_src": icon_src,
            "source": source_tag
        })
    return results

def _scan_registry_app_paths():
    results = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths") as key:
                i = 0
                while True:
                    try:
                        subname = winreg.EnumKey(key, i)
                        i += 1
                        with winreg.OpenKey(key, subname) as sk:
                            val, _ = winreg.QueryValueEx(sk, "")
                            exe_path = _clean_path(val)
                            if exe_path and os.path.exists(exe_path):
                                pretty = os.path.splitext(subname)[0].replace("-", " ").replace("_", " ").title()
                                results.append({
                                    "name": pretty,
                                    "target": exe_path,
                                    "args": "",
                                    "icon_src": exe_path,
                                    "source": "Registro Windows"
                                })
                    except OSError:
                        break
                    except Exception:
                        continue
        except Exception:
            pass
    return results

def _scan_registry_uninstall():
    results = []
    paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, reg_path in paths:
        try:
            with winreg.OpenKey(hive, reg_path) as key:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, i)
                        i += 1
                        with winreg.OpenKey(key, sub) as sk:
                            try:
                                name, _ = winreg.QueryValueEx(sk, "DisplayName")
                            except Exception:
                                continue
                            if not name or _is_ignored_name(name.lower()):
                                continue
                            disp_icon = ""
                            install_loc = ""
                            try:
                                disp_icon, _ = winreg.QueryValueEx(sk, "DisplayIcon")
                            except Exception:
                                pass
                            try:
                                install_loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                            except Exception:
                                pass

                            candidate = _clean_path(disp_icon)
                            if not (candidate and candidate.lower().endswith(".exe") and os.path.exists(candidate)):
                                candidate = ""
                                loc = _clean_path(install_loc)
                                if loc and os.path.isdir(loc):
                                    try:
                                        exes = [f for f in os.listdir(loc) if f.lower().endswith(".exe") and not _is_ignored_name(f.lower())]
                                        if exes:
                                            n_tok = _norm(name).split()[0] if _norm(name).split() else ""
                                            best = next((e for e in exes if n_tok and n_tok in _norm(e)), exes[0])
                                            candidate = os.path.join(loc, best)
                                    except Exception:
                                        pass
                            if candidate and os.path.exists(candidate):
                                results.append({
                                    "name": name.strip(),
                                    "target": candidate,
                                    "args": "",
                                    "icon_src": candidate,
                                    "source": "Instalados"
                                })
                    except OSError:
                        break
                    except Exception:
                        continue
        except Exception:
            pass
    return results

def query_windows_search_index(query_str, limit=12):
    """Queries native Windows Search (SystemIndex) for matching executables & shortcuts."""
    results = []
    if not HAS_WIN32COM or not query_str or len(query_str.strip()) < 2:
        return results
    q_safe = query_str.strip().replace("'", "''")
    try:
        pythoncom.CoInitialize()
        conn = win32com.client.Dispatch("ADODB.Connection")
        conn.Open('Provider=Search.CollatorDSO;Extended Properties="Application=Windows";')
        sql = (
            f"SELECT TOP {limit} System.ItemName, System.ItemPathDisplay "
            f"FROM SystemIndex "
            f"WHERE (System.FileExtension = '.exe' OR System.FileExtension = '.lnk') "
            f"AND System.ItemName LIKE '%{q_safe}%'"
        )
        rs = conn.Execute(sql)
        while not rs.EOF:
            iname = rs.Fields(0).Value
            ipath = rs.Fields(1).Value
            if iname and ipath and not _is_ignored_name(str(iname).lower()):
                results.append({
                    "name": os.path.splitext(str(iname))[0],
                    "target": str(ipath),
                    "args": "",
                    "icon_src": str(ipath),
                    "source": "Índice de Windows"
                })
            rs.MoveNext()
        rs.Close()
        conn.Close()
    except Exception:
        pass
    return results

def build_catalog(force=False):
    global _cached_catalog, _catalog_timestamp
    with _catalog_lock:
        if not force and _cached_catalog and (time.time() - _catalog_timestamp < 300):
            return _cached_catalog

        raw = []
        raw.extend(_scan_shortcuts())
        raw.extend(_scan_registry_app_paths())
        raw.extend(_scan_registry_uninstall())
        for app_it in _scan_apps_folder_com():
            raw.append({
                "name": app_it["name"],
                "target": app_it["target"],
                "args": app_it["args"],
                "icon_src": app_it["icon_src"],
                "source": app_it["source"],
                "is_pwa": bool(app_it.get("is_pwa")),
            })
        for p in get_installed_pwas(force=force):
            raw.append({
                "name": p["name"],
                "target": p["target"],
                "args": p["args"],
                "icon_src": p["icon_src"],
                "source": p["browser"],
                "is_pwa": True,
            })

        by_target = {}
        for item in raw:
            tgt_key = (item["target"].lower(), item.get("args", "").lower())
            if tgt_key not in by_target:
                by_target[tgt_key] = item
            else:
                prev = by_target[tgt_key]
                if item.get("is_pwa") and not prev.get("is_pwa"):
                    by_target[tgt_key] = item
                elif not prev.get("icon_src") and item.get("icon_src"):
                    by_target[tgt_key] = item

        deduped = list(by_target.values())
        deduped.sort(key=lambda x: (0 if x.get("is_pwa") else 1, _norm(x["name"])))

        _cached_catalog = deduped
        _catalog_timestamp = time.time()
        print(f"[AppSearch] Catalog built natively: {len(deduped)} apps indexed.")
        return deduped

def search_apps(query="", limit=50, category="all", force=False):
    catalog = build_catalog(force=force)
    q = _norm(query.strip())
    q_compact = q.replace(" ", "")

    if category == "pwa":
        pool = [it for it in catalog if it.get("is_pwa") or "pwa" in _norm(it.get("source", "")) or "store" in _norm(it.get("source", ""))]
    elif category == "desktop":
        pool = [it for it in catalog if not it.get("is_pwa") and "pwa" not in _norm(it.get("source", ""))]
    else:
        pool = catalog

    if not q:
        return pool[:limit]

    tokens = [t for t in q.split() if t]
    scored = []
    seen_targets = set()

    for item in pool:
        n_norm = _norm(item["name"])
        t_norm = _norm(item["target"])
        a_norm = _norm(item.get("args", ""))
        s_norm = _norm(item.get("source", ""))
        combined = f"{n_norm} {t_norm} {a_norm} {s_norm}"
        combined_compact = combined.replace(" ", "")

        matched = all(tok in combined for tok in tokens) or (q_compact and q_compact in combined_compact)
        if matched:
            score = 0
            if n_norm == q or n_norm.replace(" ", "") == q_compact:
                score += 110
            elif n_norm.startswith(q) or n_norm.replace(" ", "").startswith(q_compact):
                score += 70
            elif any(w.startswith(tok) for tok in tokens for w in n_norm.split()):
                score += 45
            elif q in n_norm or (q_compact and q_compact in n_norm.replace(" ", "")):
                score += 30
            else:
                score += 10

            if item.get("is_pwa") or s_norm in ("pwa web", "app store / msix", "edge pwa", "chrome pwa"):
                score += 22
            elif item["source"] in ("Escritorio", "Menú Inicio"):
                score += 6

            # Demote plain folders compared to actual apps/PWAs
            tgt_raw = item.get("target", "")
            if tgt_raw and os.path.isdir(tgt_raw):
                score -= 45

            scored.append((score, item))
            seen_targets.add((t_norm, a_norm))

    # Only query slow Windows ADODB SystemIndex when local catalog returned < 3 hits
    if len(q) >= 3 and len(scored) < 3 and category != "pwa":
        for idx_item in query_windows_search_index(query, limit=12):
            key = (_norm(idx_item["target"]), "")
            if key not in seen_targets:
                seen_targets.add(key)
                scored.append((12, idx_item))

    scored.sort(key=lambda x: (-x[0], _norm(x[1]["name"])))
    return [x[1] for x in scored[:limit]]
