#!/usr/bin/env python3
"""
Alioth Dockbar — Local Mini MCP Server (Model Context Protocol over stdio, zero third-party deps).
Compatible with Antigravity, Claude Desktop, Cursor, Codex, and any JSON-RPC 2.0 MCP host
(auto-detects both newline-delimited JSON-RPC and Content-Length framed stdio).
"""
import json
import sys
import traceback

import todo_store

SERVER_INFO = {
    "name": "alioth-todo-mcp",
    "version": "1.0.0",
}

TOOLS_SCHEMA = [
    {
        "name": "todo_list_tasks",
        "description": "Lista las tareas del widget To-Do de Alioth Dockbar filtrando por cuaderno/pestaña, estado (pending, completed, all) o texto.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "notebook": {"type": "string", "description": "Nombre o ID del cuaderno (ej. General, Trabajo, Outlook). Vacío = todos."},
                "status": {"type": "string", "enum": ["pending", "completed", "all"], "default": "pending"},
                "query": {"type": "string", "description": "Filtro opcional de búsqueda por texto."},
            },
        },
    },
    {
        "name": "todo_add_task",
        "description": "Añade una nueva tarea al widget To-Do con deduplicación automática, selector de cuaderno/pestaña y etiqueta de prioridad.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Descripción o título de la tarea."},
                "notebook": {"type": "string", "description": "Cuaderno destino (ej. General, Trabajo, Outlook, Ideas).", "default": "General"},
                "tag": {"type": "string", "description": "Prioridad opcional: urgente, importante, normal, enfoque, rapida."},
                "external_id": {"type": "string", "description": "ID externo opcional (ej. internetMessageId de Outlook) para deduplicar."},
                "sender": {"type": "string", "description": "Remitente opcional."},
                "switch_active": {"type": "boolean", "default": False},
            },
            "required": ["text"],
        },
    },
    {
        "name": "todo_complete_task",
        "description": "Marca una tarea como completada (done=true) o pendiente (done=false) por su ID o coincidencia de texto.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "ID de la tarea o fragmento de su texto."},
                "done": {"type": "boolean", "default": True},
                "notebook": {"type": "string"},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "todo_delete_task",
        "description": "Elimina una tarea del widget To-Do por su ID o texto exacto.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "notebook": {"type": "string"},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "todo_clear_completed",
        "description": "Limpia todas las tareas completadas de un cuaderno o de todos los cuadernos.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "notebook": {"type": "string"},
            },
        },
    },
    {
        "name": "todo_list_notebooks",
        "description": "Lista todas las pestañas/cuadernos del widget To-Do con sus colores y conteo de tareas.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "todo_manage_notebook",
        "description": "Crea, renombra, cambia el color, activa o elimina un cuaderno/pestaña del widget To-Do.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "switch", "rename", "color", "delete"]},
                "name": {"type": "string", "description": "Nombre o ID del cuaderno."},
                "new_name": {"type": "string"},
                "color": {"type": "string", "description": "Color Hex (ej. #0ea5e9)."},
                "switch_active": {"type": "boolean", "default": False},
            },
            "required": ["action", "name"],
        },
    },
    {
        "name": "todo_ingest_inbox",
        "description": "Importa y deduplica de inmediato los correos/tareas pendientes en todo_inbox.json (integración Power Automate + Outlook).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "notebook": {"type": "string", "default": "Outlook"},
            },
        },
    },
]


def _call_tool(name, args):
    args = args or {}
    if name == "todo_list_tasks":
        return todo_store.list_tasks(
            notebook=args.get("notebook") or None,
            status=args.get("status", "pending"),
            query=args.get("query", ""),
        )
    if name == "todo_add_task":
        return todo_store.add_task(
            text=args.get("text", ""),
            notebook=args.get("notebook", "General"),
            tag=args.get("tag", ""),
            external_id=args.get("external_id", ""),
            sender=args.get("sender", ""),
            source="mcp",
            deduplicate=True,
            switch_active=bool(args.get("switch_active", False)),
        )
    if name == "todo_complete_task":
        return todo_store.set_task_done(
            args.get("identifier", ""),
            done=bool(args.get("done", True)),
            notebook=args.get("notebook") or None,
        )
    if name == "todo_delete_task":
        return todo_store.delete_task(args.get("identifier", ""), notebook=args.get("notebook") or None)
    if name == "todo_clear_completed":
        return todo_store.clear_completed(notebook=args.get("notebook") or None)
    if name == "todo_list_notebooks":
        return todo_store.list_notebooks()
    if name == "todo_manage_notebook":
        return todo_store.manage_notebook(
            action=args.get("action", ""),
            name=args.get("name", ""),
            new_name=args.get("new_name", ""),
            color=args.get("color", ""),
            switch_active=bool(args.get("switch_active", False)),
        )
    if name == "todo_ingest_inbox":
        return todo_store.ingest_todo_inbox(default_notebook=args.get("notebook", "Outlook"))
    raise ValueError(f"Unknown tool: {name}")


def handle_rpc_message(msg):
    method = msg.get("method", "")
    req_id = msg.get("id", None)
    params = msg.get("params") or {}

    if req_id is None:
        return None

    try:
        if method == "initialize":
            proto = params.get("protocolVersion") or "2024-11-05"
            result = {
                "protocolVersion": proto,
                "capabilities": {
                    "tools": {},
                    "resources": {},
                },
                "serverInfo": SERVER_INFO,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS_SCHEMA}
        elif method == "tools/call":
            t_name = params.get("name", "")
            t_args = params.get("arguments") or {}
            out = _call_tool(t_name, t_args)
            result = {
                "content": [{"type": "text", "text": json.dumps(out, indent=2, ensure_ascii=False)}],
                "isError": False,
            }
        elif method == "resources/list":
            result = {
                "resources": [
                    {
                        "uri": "todo://notebooks",
                        "name": "Cuadernos To-Do (Alioth Dockbar)",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": "todo://tasks/pending",
                        "name": "Tareas Pendientes (Alioth Dockbar)",
                        "mimeType": "application/json",
                    },
                ]
            }
        elif method == "resources/read":
            uri = params.get("uri", "")
            payload = todo_store.list_notebooks() if uri == "todo://notebooks" else todo_store.list_tasks(status="pending")
            result = {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(payload, indent=2, ensure_ascii=False),
                    }
                ]
            }
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": f"Error: {exc}\n{traceback.format_exc()}"}],
                "isError": True,
            },
        }


def serve_stdio():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        first_line = stdin.readline()
        if not first_line:
            break
        stripped = first_line.strip()
        if not stripped:
            continue

        use_framed = False
        if stripped.lower().startswith(b"content-length:"):
            use_framed = True
            content_len = int(stripped.split(b":", 1)[1].strip())
            while True:
                hdr = stdin.readline()
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
            raw_body = stdin.read(content_len)
        else:
            raw_body = stripped

        try:
            msg = json.loads(raw_body.decode("utf-8"))
        except Exception:
            continue

        resp = handle_rpc_message(msg)
        if resp is not None:
            encoded = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            if use_framed:
                stdout.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
            else:
                stdout.write(encoded + b"\n")
            stdout.flush()


if __name__ == "__main__":
    serve_stdio()
