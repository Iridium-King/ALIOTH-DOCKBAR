#!/usr/bin/env python3
"""
Alioth Dockbar — To-Do CLI (con deduplicación e ingesta de todo_inbox.json para Power Automate + Outlook).

Ejemplos de uso:
  python todo_cli.py list
  python todo_cli.py add "Revisar reporte mensual" --notebook Outlook --tag urgente --ext-id "MSG-001"
  python todo_cli.py done "Revisar reporte"
  python todo_cli.py ingest
  python todo_cli.py notebooks
"""
import argparse
import json
import sys

import todo_store


def _print_output(data, as_json=False):
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if "notebooks" in data and isinstance(data["notebooks"], list) and data["notebooks"] and isinstance(data["notebooks"][0], dict):
        for nb in data["notebooks"]:
            mark = "★" if nb.get("active") else " "
            print(f"{mark} [{nb['name']}] ({nb['color']}) — {nb['pending_count']} pendientes, {nb['completed_count']} completadas")
        return
    if "tasks" in data and isinstance(data["tasks"], list):
        if not data["tasks"]:
            print("Sin tareas que coincidan.")
            return
        for t in data["tasks"]:
            chk = "✓" if t.get("done") else "☐"
            tag = f" [{t['tag_label']}]" if t.get("tag_label") else ""
            nb_n = f" ({t.get('notebook_name')})" if t.get("notebook_name") else ""
            print(f"{chk} {t['id']} | {t['text']}{tag}{nb_n}")
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description="CLI oficial del Widget To-Do de Alioth Dockbar")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Listar tareas")
    p_list.add_argument("--notebook", "-n", default="", help="Filtrar por cuaderno/pestaña")
    p_list.add_argument("--status", "-s", choices=["all", "pending", "completed"], default="pending")
    p_list.add_argument("--query", "-q", default="", help="Buscar texto")
    p_list.add_argument("--json", action="store_true", help="Salida en JSON")

    p_add = sub.add_parser("add", help="Añadir tarea (con deduplicación automática)")
    p_add.add_argument("text", help="Texto o asunto de la tarea")
    p_add.add_argument("--notebook", "-n", default="General", help="Cuaderno destino (ej. General, Trabajo, Outlook)")
    p_add.add_argument("--tag", "-t", default="", help="Prioridad: urgente, importante, normal, enfoque, rapida")
    p_add.add_argument("--ext-id", default="", help="ID externo único (ej. internetMessageId de Outlook para deduplicar)")
    p_add.add_argument("--from", dest="sender", default="", help="Remitente opcional del correo")
    p_add.add_argument("--no-dedup", action="store_true", help="Desactivar deduplicación")
    p_add.add_argument("--switch", action="store_true", help="Cambiar pestaña activa a este cuaderno")
    p_add.add_argument("--json", action="store_true")

    p_done = sub.add_parser("done", help="Marcar tarea como completada por ID o texto")
    p_done.add_argument("identifier")
    p_done.add_argument("--notebook", "-n", default="")
    p_done.add_argument("--json", action="store_true")

    p_undone = sub.add_parser("undone", help="Desmarcar tarea completada")
    p_undone.add_argument("identifier")
    p_undone.add_argument("--notebook", "-n", default="")
    p_undone.add_argument("--json", action="store_true")

    p_del = sub.add_parser("delete", help="Eliminar tarea por ID o texto exacto")
    p_del.add_argument("identifier")
    p_del.add_argument("--notebook", "-n", default="")
    p_del.add_argument("--json", action="store_true")

    p_clr = sub.add_parser("clear-done", help="Eliminar todas las tareas completadas")
    p_clr.add_argument("--notebook", "-n", default="")
    p_clr.add_argument("--json", action="store_true")

    p_nbs = sub.add_parser("notebooks", help="Listar cuadernos / pestañas")
    p_nbs.add_argument("--json", action="store_true")

    p_nb_add = sub.add_parser("notebook-add", help="Crear nuevo cuaderno / pestaña")
    p_nb_add.add_argument("name")
    p_nb_add.add_argument("--color", default="#3b82f6")
    p_nb_add.add_argument("--switch", action="store_true")
    p_nb_add.add_argument("--json", action="store_true")

    p_ing = sub.add_parser("ingest", help="Importar y deduplicar tareas desde todo_inbox.json (Power Automate / Outlook)")
    p_ing.add_argument("--file", "-f", default=todo_store.DEFAULT_INBOX_PATH, help="Ruta a todo_inbox.json")
    p_ing.add_argument("--notebook", "-n", default="Outlook", help="Cuaderno por defecto para correos entrantes")
    p_ing.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.cmd == "list":
        res = todo_store.list_tasks(notebook=args.notebook or None, status=args.status, query=args.query)
        _print_output(res, as_json=args.json)
    elif args.cmd == "add":
        res = todo_store.add_task(
            text=args.text,
            notebook=args.notebook,
            tag=args.tag,
            external_id=args.ext_id,
            sender=args.sender,
            source="cli",
            deduplicate=(not args.no_dedup),
            switch_active=args.switch,
        )
        _print_output(res, as_json=args.json)
    elif args.cmd == "done":
        res = todo_store.set_task_done(args.identifier, done=True, notebook=args.notebook or None)
        _print_output(res, as_json=args.json)
    elif args.cmd == "undone":
        res = todo_store.set_task_done(args.identifier, done=False, notebook=args.notebook or None)
        _print_output(res, as_json=args.json)
    elif args.cmd == "delete":
        res = todo_store.delete_task(args.identifier, notebook=args.notebook or None)
        _print_output(res, as_json=args.json)
    elif args.cmd == "clear-done":
        res = todo_store.clear_completed(notebook=args.notebook or None)
        _print_output(res, as_json=args.json)
    elif args.cmd == "notebooks":
        res = todo_store.list_notebooks()
        _print_output(res, as_json=args.json)
    elif args.cmd == "notebook-add":
        res = todo_store.manage_notebook("add", name=args.name, color=args.color, switch_active=args.switch)
        _print_output(res, as_json=args.json)
    elif args.cmd == "ingest":
        res = todo_store.ingest_todo_inbox(inbox_path=args.file, default_notebook=args.notebook)
        _print_output(res, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
