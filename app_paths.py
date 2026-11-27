"""
Shared runtime version and path resolution for Alioth Dockbar.
Works identically in source mode, portable Python (`runtime/`), and PyInstaller builds (`sys.frozen`).
"""
import os
import sys

APP_VERSION = "2.0.0"


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def app_dir():
    """Writable directory beside AliothDockbar.exe (when frozen) or beside the source .py files."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_dir():
    """Read-only bundled resources directory (prefers external files next to the exe, falls back to _MEIPASS)."""
    if is_frozen():
        return getattr(sys, "_MEIPASS", app_dir())
    return app_dir()


def resolve_resource(*parts):
    """Resolves an asset/HTML path, preferring an external override next to the executable."""
    external = os.path.join(app_dir(), *parts)
    if os.path.exists(external):
        return external
    bundled = os.path.join(resource_dir(), *parts)
    if os.path.exists(bundled):
        return bundled
    return external
