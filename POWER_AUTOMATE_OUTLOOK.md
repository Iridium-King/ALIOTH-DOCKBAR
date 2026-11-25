# Integración To-Do: Power Automate + Outlook + CLI + Mini MCP Local

El widget **Cuadernos To-Do** de **Alioth Dockbar** incluye tres canales integrados con **deduplicación automática** (`internetMessageId` / hash de texto):

1. **Lector en vivo de `todo_inbox.json`** (ideal para **Power Automate Cloud + OneDrive for Business**).
2. **CLI oficial (`todo_cli.py`)** para terminal, scripts y **Power Automate Desktop**.
3. **Mini Servidor MCP Local (`todo_mcp_server.py`)** por `stdio` para conectar **Antigravity**, **Claude Desktop**, **Cursor** o **Codex**.

---

## 1. Conectar Outlook con Power Automate Cloud (vía OneDrive)

Como tu carpeta `ALIOTH DOCKBAR` está sincronizada en **OneDrive**, no necesitas instalar pasarelas ni conectores premium:

1. Entra a [make.powerautomate.com](https://make.powerautomate.com) → **Crear** → **Flujo de nube automatizado**.
2. **Desencadenador (Office 365 Outlook):**
   - Elige **"Cuando se marca un correo electrónico (V3)"** (*When an email is flagged*) o **"Cuando llega un correo electrónico nuevo (V3)"**.
3. **Acción (OneDrive para la Empresa — *Crear archivo* / *Create file*):**
   - **Ruta de la carpeta:** selecciona la carpeta `.../ALIOTH TDAH LABS/ALIOTH DOCKBAR` en tu OneDrive.
   - **Nombre del archivo:** `todo_inbox.json`
   - **Contenido del archivo:** pega esta plantilla usando los campos dinámicos de Outlook:

```json
{
  "items": [
    {
      "internetMessageId": "@{triggerOutputs()?['body/internetMessageId']}",
      "subject": "@{triggerOutputs()?['body/subject']}",
      "from": "@{triggerOutputs()?['body/from']}",
      "importance": "@{triggerOutputs()?['body/importance']}",
      "notebook": "Outlook"
    }
  ]
}
```

> En Configuración de la acción **Crear archivo**, asegúrate de mantener activado **Sobrescribir (Allow Chunking / Overwrite)** para actualizar `todo_inbox.json`. En cuanto OneDrive sincronice el archivo en tu PC, **Alioth Widgets** lo detecta por `mtime`, importa la tarea sin duplicados a la pestaña **Outlook** y limpia la cola automáticamente.

---

## 2. Uso desde Terminal o Power Automate Desktop (`todo_cli.py`)

```powershell
# Listar tareas pendientes
python todo_cli.py list

# Añadir una tarea con prioridad y deduplicación por ID externo
python todo_cli.py add "Revisar informe mensual" --notebook Outlook --tag urgente --ext-id "MSG-2026-01" --from "Coordinación"

# Marcar tarea como completada (por ID o texto parcial)
python todo_cli.py done "Revisar informe"

# Importar manualmente todo_inbox.json
python todo_cli.py ingest

# Listar cuadernos / pestañas en formato JSON
python todo_cli.py notebooks --json
```

---

## 3. Conectar el Mini Servidor MCP Local (`todo_mcp_server.py`)

Agrega esta entrada en la configuración MCP de **Antigravity** (`mcp_config.json`), **Claude Desktop** o **Cursor**:

```json
{
  "mcpServers": {
    "alioth-todo": {
      "command": "python",
      "args": [
        "C:/Ruta/A/ALIOTH DOCKBAR/todo_mcp_server.py"
      ]
    }
  }
}
```

### Herramientas MCP disponibles:
- `todo_list_tasks` (`notebook`, `status`, `query`)
- `todo_add_task` (`text`, `notebook`, `tag`, `external_id`, `sender`, `switch_active`)
- `todo_complete_task` (`identifier`, `done`, `notebook`)
- `todo_delete_task` (`identifier`, `notebook`)
- `todo_clear_completed` (`notebook`)
- `todo_list_notebooks`
- `todo_manage_notebook` (`action`: `add` | `switch` | `rename` | `color` | `delete`)
- `todo_ingest_inbox` (`notebook`)
