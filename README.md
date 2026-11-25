# ALIOTH DOCKBAR 🚀
**Dockbar moderna, ligera y suite de Widgets de Escritorio para Windows 11 (Edición Corporativa sin permisos de Administrador)**

Diseñada por **Alioth TDAH Labs** para ofrecer máxima fluidez visual (~60+ FPS con `UpdateLayeredWindow` de 32 bits), consumo mínimo de memoria RAM, compatibilidad total con entornos corporativos (**cero alertas de CrowdStrike / Netskope**, cero hooks globales `SetWindowsHookEx`) e integración directa con **PWAs**, **Outlook / Power Automate** y agentes IA mediante un **Mini Servidor MCP Local**.

---

## ✨ Características Principales

### 1. Dockbar Translúcida de Alto Rendimiento
* **Efecto Ola / Lupa Magnificadora (~60 FPS):** Ampliación suave tipo macOS con iconos 100% nítidos e independientes de la opacidad del cristal.
* **6 Efectos de Apertura Personalizables:** *Gota de Agua / Energía Azul (`#38bdf8`)*, *Gota + Rebote*, *Rebote Clásico*, *Pulso Neón*, *Levitación con Haz de Luz* y *Pop Elástico*, con control de rapidez, color y botón **Probar**.
* **Programas Abiertos y PWAs en Tiempo Real:** Detecta aplicaciones abiertas (incluyendo apps guardadas dentro de carpetas y **PWAs de Chrome/Edge** con su icono original) y permite **Finalizar tarea** aislando únicamente la ventana seleccionada.
* **Carpetas Estilo macOS Stacks:** Agrupa iconos arrastrando uno sobre otro, personaliza fuente/tamaño/negrita/cursiva/color de sus etiquetas o usa el modo cuadrícula limpia de solo iconos.
* **Protección Inteligente en "Mostrar Escritorio":** Ni la Dockbar ni los Widgets desaparecen al pulsar *Mostrar escritorio* en la barra de tareas, el botón del Dock o `Win + D`, manteniéndose siempre detrás de tus ventanas normales al trabajar.
* **Acciones Rápidas y Energía de Windows:** Botones listos para *Mostrar Escritorio*, *Recortes*, *Papelera*, *Bloquear*, *Cerrar Sesión*, *Reiniciar* y *Apagar Equipo*.

### 2. Suite de Widgets de Escritorio Optimizados en RAM
Ejecutados en un proceso ligero independiente (`widgets_main.py`) con recorte automático de Working Set:
* **Cuadernos To-Do con Pestañas:** Múltiples listas estilo cuaderno (*General*, *Trabajo*, *Outlook*, *Ideas*), botón `⚙` integrado, ventana oscura de **Nueva Tarea** con prioridades, **auto-alargado dinámico o scroll**, vista separada de **Completados** y sincronización con Outlook.
* **Reloj Digital y Fecha**, **Monitor de Sistema (CPU, RAM y Disco)**, **Repisa de Accesos Rápidos (Drag & Drop)** y **Lanzador de PWAs**.
* **Personalización Total:** 6 estilos visuales (*Cristal*, *Neón*, *Minimalista*, *Negro OLED*, *Acrílico Azul*, *Editorial Dorado*), **color o imagen de fondo** con velo de legibilidad, tipografía completa, **botón minimizar (`—`)**, modo **auto-desplegable al pasar el mouse** y **bloqueo/anclajes de posición fija**.

### 3. Integración To-Do: CLI + Power Automate / Outlook + Mini MCP Local
* **`todo_inbox.json`:** Ingesta automática en vivo con deduplicación para flujos de **Power Automate + Office 365 Outlook** sobre OneDrive.
* **`todo_cli.py`:** Interfaz de línea de comandos completa con salida humana o `--json`.
* **`todo_mcp_server.py`:** Servidor **Model Context Protocol (MCP)** local por `stdio` sin dependencias externas. Consulta la guía paso a paso en [POWER_AUTOMATE_OUTLOOK.md](POWER_AUTOMATE_OUTLOOK.md).

---

## 🛠️ Requisitos e Inicio Rápido

1. **Python 3.11+** en Windows 10/11 con `Pillow` y `pywin32`:
   ```powershell
   pip install pillow pywin32
   ```
2. **Iniciar en segundo plano (sin consola):**
   Haz doble clic en `iniciar.vbs` o `iniciar.bat`.
3. **Abrir Panel de Control en Vivo:**
   Haz clic derecho sobre la Dockbar → **Configuración de Alioth Dockbar...**
4. **Detener:**
   Ejecuta `detener.bat` o elige **Salir de Alioth Dockbar** en el menú contextual.
