"""Syncworthy: a real-time collaborative editor for any project, with plugins."""

__version__ = "0.1.0"

from .config import Config
from .plugins import (Action, Diagnostics, Language, McpTool, Plugin, PluginContext,
                      Previewer, SettingsPage)
from .server import create_app

__all__ = ["Action", "Config", "Diagnostics", "Language", "McpTool", "Plugin",
           "PluginContext", "Previewer", "SettingsPage", "create_app", "run_server",
           "__version__"]


def run_server(project_root, *, host: str = "127.0.0.1", port: int = 8000,
               open_browser: bool = True, packet_log: bool = False,
               config=None, plugins=None, app_name: str | None = None) -> None:
    """Serve PROJECT_ROOT with uvicorn (what ``syncworthy serve`` and ``noteworthy -g`` call)."""
    import logging
    import os
    import threading
    import time
    import webbrowser

    try:
        import uvicorn
    except ImportError:
        print("Error: the server requires uvicorn. Install with: pip install uvicorn fastapi")
        return

    debug = any(os.environ.get(n, "").lower() in {"1", "true", "yes", "on"}
                for n in ("SYNCWORTHY_DEBUG", "NOTEWORTHY_DEBUG"))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="[%(levelname)s] %(message)s")

    app = create_app(project_root, config=config, plugins=plugins, packet_log=packet_log)
    name = app_name or app.state.sync.app_name
    print(f"Starting {name} at http://{host}:{port}")
    print(f"Project: {app.state.sync.root}")
    print(f"Plugins: {', '.join(p.name for p in app.state.sync.plugins) or 'none'}")
    print(f"Yjs packet logging: {'enabled' if packet_log else 'disabled'}")

    if open_browser:
        def open_delayed():
            time.sleep(1)
            webbrowser.open(f"http://{host}:{port}")
        threading.Thread(target=open_delayed, daemon=True).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")
