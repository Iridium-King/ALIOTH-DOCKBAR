import json
import os
import time
import uuid
import hashlib
import ctypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_INBOX_PATH = os.path.join(BASE_DIR, "todo_inbox.json")

PRIORITY_MAP = {
    "urgente": ("urgente", "Urgente", "#ef4444"),
    "high": ("urgente", "Urgente", "#ef4444"),
    "alta": ("urgente", "Urgente", "#ef4444"),
    "importante": ("importante", "Importante", "#f59e0b"),
    "medium": ("importante", "Importante", "#f59e0b"),
    "media": ("importante", "Importante", "#f59e0b"),
    "normal": ("normal", "Normal", "#3b82f6"),
    "enfoque": ("enfoque", "Enfoque", "#a855f7"),
    "focus": ("enfoque", "Enfoque", "#a855f7"),
    "rapida": ("rapida", "Rápida", "#10b981"),
    "low": ("rapida", "Rápida", "#10b981"),
    "baja": ("rapida", "Rápida", "#10b981"),
}

OUTLOOK_NOTEBOOK_DEFAULT = {
    "id": "nb_outlook",
    "name": "Outlook",
    "color": "#0ea5e9",
}


class _InterProcessLock:
    """Lightweight Win32 Named Mutex guard so Widget UI, CLI, and MCP never clobber config.json."""
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


def _norm_text(s):
    return " ".join(str(s or "").strip().lower().split())


def resolve_tag(raw_tag):
    key = _norm_text(raw_tag)
    if not key or key in ("none", "ninguna", "sin etiqueta"):
        return "", "", ""
    return PRIORITY_MAP.get(key, ("normal", str(raw_tag).strip()[:14], "#3b82f6"))


def compute_dedup_key(text, external_id="", sender="", notebook_name=""):
    ext = str(external_id or "").strip().strip("<>").strip()
    if ext:
        return "id:" + hashlib.sha1(ext.lower().encode("utf-8", "ignore")).hexdigest()[:20]
    norm = f"{_norm_text(notebook_name)}|{_norm_text(sender)}|{_norm_text(text)}"
    return "txt:" + hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:20]


def _load_raw_config(config_path=DEFAULT_CONFIG_PATH):
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    from config import DEFAULT_CONFIG
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _save_raw_config(data, config_path=DEFAULT_CONFIG_PATH):
    tmp_path = f"{config_path}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.flush()
    last_err = None
    for attempt in range(6):
        try:
            os.replace(tmp_path, config_path)
            last_err = None
            break
        except OSError as err:
            last_err = err
            time.sleep(0.06 * (attempt + 1))
    if last_err is not None:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise last_err


def ensure_todo_structure(todo_cfg):
    notebooks = todo_cfg.get("notebooks")
    if not isinstance(notebooks, list) or not notebooks:
        legacy_tasks = list(todo_cfg.get("tasks") or [])
        notebooks = [
            {"id": "nb_general", "name": "General", "color": "#3b82f6", "tasks": legacy_tasks},
            {"id": "nb_trabajo", "name": "Trabajo", "color": "#10b981", "tasks": []},
            {"id": "nb_outlook", "name": "Outlook", "color": "#0ea5e9", "tasks": []},
            {"id": "nb_ideas", "name": "Ideas", "color": "#a855f7", "tasks": []},
        ]
        todo_cfg["notebooks"] = notebooks
    for nb in notebooks:
        nb.setdefault("id", f"nb_{uuid.uuid4().hex[:6]}")
        nb.setdefault("name", "Lista")
        nb.setdefault("color", "#3b82f6")
        nb.setdefault("tasks", [])
    active_id = todo_cfg.get("active_notebook_id")
    if not any(nb["id"] == active_id for nb in notebooks):
        active_id = notebooks[0]["id"]
        todo_cfg["active_notebook_id"] = active_id
    todo_cfg["tasks"] = next((nb["tasks"] for nb in notebooks if nb["id"] == active_id), notebooks[0]["tasks"])
    seen = todo_cfg.get("processed_inbox_ids")
    if not isinstance(seen, list):
        todo_cfg["processed_inbox_ids"] = []
    return notebooks, active_id


def find_or_create_notebook(notebooks, name_or_id, default_color="#0ea5e9"):
    target = _norm_text(name_or_id)
    if not target:
        return notebooks[0]
    for nb in notebooks:
        if _norm_text(nb.get("id")) == target or _norm_text(nb.get("name")) == target:
            return nb
    if target == "outlook":
        new_nb = {"id": "nb_outlook", "name": "Outlook", "color": "#0ea5e9", "tasks": []}
    else:
        new_nb = {
            "id": f"nb_{uuid.uuid4().hex[:6]}",
            "name": str(name_or_id).strip()[:22],
            "color": default_color,
            "tasks": [],
        }
    notebooks.append(new_nb)
    return new_nb


def add_task(
    text,
    notebook="General",
    tag="",
    external_id="",
    sender="",
    source="cli",
    deduplicate=True,
    switch_active=False,
    config_path=DEFAULT_CONFIG_PATH,
):
    clean_text = str(text or "").strip()
    if not clean_text:
        return {"added": False, "reason": "empty_text", "task": None}

    if sender:
        sender_short = str(sender).strip()
        if "<" in sender_short:
            sender_short = sender_short.split("<", 1)[0].strip().strip('"') or sender_short
        if sender_short and sender_short.lower() not in clean_text.lower():
            clean_text = f"{clean_text} ({sender_short[:28]})"

    with _InterProcessLock():
        data = _load_raw_config(config_path)
        todo_cfg = data.setdefault("widgets", {}).setdefault("todo_list", {})
        notebooks, active_id = ensure_todo_structure(todo_cfg)
        nb = find_or_create_notebook(notebooks, notebook or active_id)

        dkey = compute_dedup_key(clean_text, external_id=external_id, sender=sender, notebook_name=nb["name"])
        seen_ids = todo_cfg.setdefault("processed_inbox_ids", [])

        if deduplicate:
            if external_id and dkey in seen_ids:
                return {"added": False, "reason": "duplicate_external_id", "notebook": nb["name"], "dedup_key": dkey}
            norm_candidate = _norm_text(clean_text)
            for existing in nb.get("tasks", []):
                if existing.get("dedup_key") == dkey or _norm_text(existing.get("text")) == norm_candidate:
                    return {"added": False, "reason": "duplicate_task", "notebook": nb["name"], "task": existing}

        tag_id, tag_label, tag_color = resolve_tag(tag)
        task_obj = {
            "id": uuid.uuid4().hex[:8],
            "text": clean_text,
            "done": False,
            "tag": tag_id,
            "tag_label": tag_label,
            "tag_color": tag_color,
            "source": source,
            "dedup_key": dkey,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if external_id:
            task_obj["external_id"] = str(external_id).strip()[:120]
            if dkey not in seen_ids:
                seen_ids.append(dkey)
                if len(seen_ids) > 500:
                    del seen_ids[:-500]

        nb.setdefault("tasks", []).append(task_obj)
        if switch_active:
            todo_cfg["active_notebook_id"] = nb["id"]
        ensure_todo_structure(todo_cfg)
        _save_raw_config(data, config_path)
        return {"added": True, "notebook": nb["name"], "notebook_id": nb["id"], "task": task_obj}


def list_tasks(notebook=None, status="all", query="", config_path=DEFAULT_CONFIG_PATH):
    with _InterProcessLock():
        data = _load_raw_config(config_path)
        todo_cfg = data.setdefault("widgets", {}).setdefault("todo_list", {})
        notebooks, active_id = ensure_todo_structure(todo_cfg)

    q = _norm_text(query)
    results = []
    target_nb = _norm_text(notebook) if notebook else ""
    for nb in notebooks:
        if target_nb and _norm_text(nb["id"]) != target_nb and _norm_text(nb["name"]) != target_nb:
            continue
        for t in nb.get("tasks", []):
            done = bool(t.get("done", False))
            if status == "pending" and done:
                continue
            if status == "completed" and not done:
                continue
            if q and q not in _norm_text(t.get("text", "")):
                continue
            results.append({
                **t,
                "notebook_id": nb["id"],
                "notebook_name": nb["name"],
                "notebook_color": nb.get("color", "#3b82f6"),
            })
    return {
        "active_notebook_id": active_id,
        "count": len(results),
        "tasks": results,
    }


def set_task_done(identifier, done=True, notebook=None, config_path=DEFAULT_CONFIG_PATH):
    ident = _norm_text(identifier)
    if not ident:
        return {"ok": False, "reason": "empty_identifier"}
    with _InterProcessLock():
        data = _load_raw_config(config_path)
        todo_cfg = data.setdefault("widgets", {}).setdefault("todo_list", {})
        notebooks, _ = ensure_todo_structure(todo_cfg)
        target_nb = _norm_text(notebook) if notebook else ""
        matched = None
        matched_nb = None
        for nb in notebooks:
            if target_nb and _norm_text(nb["id"]) != target_nb and _norm_text(nb["name"]) != target_nb:
                continue
            for t in nb.get("tasks", []):
                if _norm_text(t.get("id")) == ident:
                    matched, matched_nb = t, nb
                    break
            if matched:
                break
        if not matched:
            for nb in notebooks:
                if target_nb and _norm_text(nb["id"]) != target_nb and _norm_text(nb["name"]) != target_nb:
                    continue
                for t in nb.get("tasks", []):
                    if ident in _norm_text(t.get("text", "")):
                        matched, matched_nb = t, nb
                        break
                if matched:
                    break
        if not matched:
            return {"ok": False, "reason": "not_found"}
        matched["done"] = bool(done)
        if done:
            matched["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            matched.pop("completed_at", None)
        ensure_todo_structure(todo_cfg)
        _save_raw_config(data, config_path)
        return {"ok": True, "notebook": matched_nb["name"], "task": matched}


def delete_task(identifier, notebook=None, config_path=DEFAULT_CONFIG_PATH):
    ident = _norm_text(identifier)
    if not ident:
        return {"ok": False, "reason": "empty_identifier"}
    with _InterProcessLock():
        data = _load_raw_config(config_path)
        todo_cfg = data.setdefault("widgets", {}).setdefault("todo_list", {})
        notebooks, _ = ensure_todo_structure(todo_cfg)
        target_nb = _norm_text(notebook) if notebook else ""
        for nb in notebooks:
            if target_nb and _norm_text(nb["id"]) != target_nb and _norm_text(nb["name"]) != target_nb:
                continue
            tasks = nb.get("tasks", [])
            for idx, t in enumerate(tasks):
                if _norm_text(t.get("id")) == ident or ident == _norm_text(t.get("text", "")):
                    removed = tasks.pop(idx)
                    ensure_todo_structure(todo_cfg)
                    _save_raw_config(data, config_path)
                    return {"ok": True, "notebook": nb["name"], "removed": removed}
        return {"ok": False, "reason": "not_found"}


def clear_completed(notebook=None, config_path=DEFAULT_CONFIG_PATH):
    with _InterProcessLock():
        data = _load_raw_config(config_path)
        todo_cfg = data.setdefault("widgets", {}).setdefault("todo_list", {})
        notebooks, _ = ensure_todo_structure(todo_cfg)
        target_nb = _norm_text(notebook) if notebook else ""
        removed_count = 0
        for nb in notebooks:
            if target_nb and _norm_text(nb["id"]) != target_nb and _norm_text(nb["name"]) != target_nb:
                continue
            before = len(nb.get("tasks", []))
            nb["tasks"] = [t for t in nb.get("tasks", []) if not t.get("done", False)]
            removed_count += (before - len(nb["tasks"]))
        ensure_todo_structure(todo_cfg)
        _save_raw_config(data, config_path)
        return {"ok": True, "removed_count": removed_count}


def list_notebooks(config_path=DEFAULT_CONFIG_PATH):
    with _InterProcessLock():
        data = _load_raw_config(config_path)
        todo_cfg = data.setdefault("widgets", {}).setdefault("todo_list", {})
        notebooks, active_id = ensure_todo_structure(todo_cfg)
    summary = []
    for nb in notebooks:
        tasks = nb.get("tasks", [])
        done_c = sum(1 for t in tasks if t.get("done", False))
        summary.append({
            "id": nb["id"],
            "name": nb["name"],
            "color": nb.get("color", "#3b82f6"),
            "active": (nb["id"] == active_id),
            "pending_count": len(tasks) - done_c,
            "completed_count": done_c,
            "total_count": len(tasks),
        })
    return {"active_notebook_id": active_id, "notebooks": summary}


def manage_notebook(action, name="", new_name="", color="", switch_active=False, config_path=DEFAULT_CONFIG_PATH):
    act = _norm_text(action)
    with _InterProcessLock():
        data = _load_raw_config(config_path)
        todo_cfg = data.setdefault("widgets", {}).setdefault("todo_list", {})
        notebooks, active_id = ensure_todo_structure(todo_cfg)

        if act in ("add", "create"):
            if not name.strip():
                return {"ok": False, "reason": "missing_name"}
            nb = find_or_create_notebook(notebooks, name.strip(), default_color=color or "#3b82f6")
            if color:
                nb["color"] = color
            if switch_active:
                todo_cfg["active_notebook_id"] = nb["id"]
            ensure_todo_structure(todo_cfg)
            _save_raw_config(data, config_path)
            return {"ok": True, "notebook": nb}

        target = _norm_text(name)
        nb = next((x for x in notebooks if _norm_text(x["id"]) == target or _norm_text(x["name"]) == target), None)
        if not nb:
            return {"ok": False, "reason": "notebook_not_found"}

        if act == "switch":
            todo_cfg["active_notebook_id"] = nb["id"]
        elif act == "rename" and new_name.strip():
            nb["name"] = new_name.strip()[:22]
        elif act == "color" and color.strip():
            nb["color"] = color.strip()
        elif act == "delete":
            if len(notebooks) <= 1:
                return {"ok": False, "reason": "cannot_delete_last_notebook"}
            notebooks.remove(nb)
        else:
            return {"ok": False, "reason": f"invalid_action:{action}"}

        ensure_todo_structure(todo_cfg)
        _save_raw_config(data, config_path)
        return {"ok": True, "notebooks": [x["name"] for x in notebooks]}


def ensure_inbox_file(inbox_path=DEFAULT_INBOX_PATH):
    """Creates an empty todo_inbox.json if missing so Power Automate / OneDrive can pick it immediately."""
    if not os.path.exists(inbox_path):
        try:
            with open(inbox_path, "w", encoding="utf-8") as f:
                json.dump({"items": []}, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


def _parse_inbox_payload(raw_text):
    txt = (raw_text or "").strip().lstrip("\ufeff")
    if not txt:
        return []
    try:
        parsed = json.loads(txt)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
        if isinstance(parsed, dict):
            for key in ("items", "tasks", "emails", "messages", "value"):
                if isinstance(parsed.get(key), list):
                    return [x for x in parsed[key] if isinstance(x, dict)]
            if any(parsed.get(k) for k in ("text", "title", "subject", "asunto", "bodyPreview")):
                return [parsed]
            return []
    except Exception:
        pass

    # Fallback: NDJSON (one JSON object per line, e.g. Power Automate "Append to file")
    items = []
    for line in txt.splitlines():
        ln = line.strip().rstrip(",")
        if not ln:
            continue
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                items.append(obj)
        except Exception:
            pass
    return items


def ingest_todo_inbox(inbox_path=DEFAULT_INBOX_PATH, default_notebook="Outlook", config_path=DEFAULT_CONFIG_PATH):
    """
    Reads todo_inbox.json (written by Power Automate / Outlook / scripts), adds new tasks with
    deduplication into the 'Outlook' notebook tab (or item-specified notebook), and resets
    todo_inbox.json to {"items": []} ONLY when items were processed (preventing OneDrive sync loops).
    """
    if not os.path.exists(inbox_path):
        ensure_inbox_file(inbox_path)
        return {"added": 0, "duplicates": 0, "total_read": 0}

    try:
        with open(inbox_path, "r", encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except Exception as e:
        return {"added": 0, "duplicates": 0, "total_read": 0, "error": str(e)}

    entries = _parse_inbox_payload(raw)
    if not entries:
        return {"added": 0, "duplicates": 0, "total_read": 0}

    added_count = 0
    dup_count = 0
    added_tasks = []

    for entry in entries:
        text = (
            entry.get("text")
            or entry.get("title")
            or entry.get("subject")
            or entry.get("asunto")
            or entry.get("bodyPreview")
            or ""
        ).strip()
        if not text:
            continue
        ext_id = (
            entry.get("external_id")
            or entry.get("internetMessageId")
            or entry.get("message_id")
            or entry.get("id")
            or ""
        )
        sender = entry.get("from") or entry.get("sender") or entry.get("remitente") or ""
        if isinstance(sender, dict):
            sender = (sender.get("emailAddress") or {}).get("name") or (sender.get("emailAddress") or {}).get("address") or ""
        priority = entry.get("tag") or entry.get("priority") or entry.get("importance") or entry.get("prioridad") or "normal"
        nb_name = entry.get("notebook") or entry.get("cuaderno") or default_notebook

        res = add_task(
            text=text,
            notebook=nb_name,
            tag=priority,
            external_id=ext_id,
            sender=sender,
            source=entry.get("source") or "outlook_inbox",
            deduplicate=True,
            switch_active=False,
            config_path=config_path,
        )
        if res.get("added"):
            added_count += 1
            added_tasks.append(res["task"])
        else:
            dup_count += 1

    # Clear inbox queue atomically so OneDrive stays clean and doesn't re-trigger unnecessarily
    try:
        cleared = {
            "items": [],
            "last_ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_added": added_count,
            "last_duplicates_skipped": dup_count,
        }
        tmp_in = f"{inbox_path}.{os.getpid()}.tmp"
        with open(tmp_in, "w", encoding="utf-8") as f:
            json.dump(cleared, f, indent=2, ensure_ascii=False)
        os.replace(tmp_in, inbox_path)
    except Exception:
        pass

    return {
        "added": added_count,
        "duplicates": dup_count,
        "total_read": len(entries),
        "tasks": added_tasks,
    }
