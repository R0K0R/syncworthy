"""The plugin interface, and finding plugins.

A plugin is an object (usually an instance of a :class:`Plugin` subclass) with
a ``name``.  Everything else is optional; see ``docs/PLUGINS.md``.

Discovery, in the order given by ``plugins = [...]`` in ``syncworthy.toml``:

* an entry point of group ``syncworthy.plugins`` with that name, which may be
  a Plugin subclass, an instance, or a zero-argument factory;
* or an explicit ``"package.module:attribute"`` for something importable but
  not installed.

Embedding programs (Noteworthy's ``-g``) skip discovery and pass instances to
``create_app`` directly.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

log = logging.getLogger("syncworthy")

ENTRY_POINT_GROUP = "syncworthy.plugins"


# ---------------------------------------------------------------------------
# What a plugin is handed
# ---------------------------------------------------------------------------

class PluginContext:
    """Everything a plugin may reach, and the thread-safe ways to talk to clients.

    Plugins run on the server's event loop but often hold worker threads (a
    compiler's stdout reader, a build running in the threadpool).  ``broadcast``
    and ``broadcast_to_file`` may be called from any thread.
    """

    def __init__(self, *, root: Path, state_dir: Path, config, plugin_name: str,
                 settings: dict, rooms, hub, app_name: str = "Syncworthy"):
        self.root = Path(root)
        self.state_dir = Path(state_dir)
        self.config = config              # syncworthy.config.Config
        self.plugin_name = plugin_name
        self.settings = settings          # the [plugin.<name>] table, possibly empty
        self.rooms = rooms                # syncworthy.rooms.RoomManager
        self.hub = hub                    # syncworthy.hub.Hub
        self.app_name = app_name
        self.loop: asyncio.AbstractEventLoop | None = None   # set at startup
        self.log = logging.getLogger(f"syncworthy.plugin.{plugin_name}")

    # -- paths ----------------------------------------------------------
    def resolve(self, rel: str) -> Path | None:
        """A project-relative path as an absolute one, or None if it escapes the project."""
        if not rel:
            return None
        target = self.root / rel
        try:
            target.resolve().relative_to(self.root.resolve())
        except ValueError:
            return None
        return target

    # -- talking to clients (any thread) ---------------------------------
    def _submit(self, coro: Awaitable) -> None:
        if self.loop is None:
            coro.close()                  # not started: nobody is connected
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        except Exception as e:             # loop closed during shutdown
            coro.close()
            self.log.debug("dropped a broadcast: %s", e)

    def broadcast(self, message: dict) -> None:
        """Send a JSON message to every client on the doc socket."""
        self._submit(self.hub.broadcast(message))

    def broadcast_to_file(self, path: str, message: dict) -> None:
        """Send a JSON message to the clients that have ``path`` open."""
        self._submit(self.hub.broadcast_to_file(path, message))


# ---------------------------------------------------------------------------
# Pieces a plugin may provide
# ---------------------------------------------------------------------------

@runtime_checkable
class Previewer(Protocol):
    """A live preview of the open file, pushed to clients as ``preview`` messages.

    The hub calls ``handles`` when someone opens or leaves a file.  Whatever the
    previewer wants shown is sent through ``ctx.broadcast_to_file`` with a JSON
    message the plugin's own JavaScript understands (Noteworthy sends
    ``{"type": "preview", "updates": [...]}``).
    """

    #: File extensions (with the dot) the preview panel is shown for.
    extensions: tuple[str, ...]
    #: Text for the empty preview panel: before any file / with a file that has no preview.
    placeholder: str
    placeholder_selected: str

    def attach(self, ctx: PluginContext) -> None: ...
    def handles(self, path: str) -> bool: ...
    def start(self, path: str) -> None: ...
    def stop(self, path: str) -> None: ...
    async def initial_messages(self, path: str) -> list[dict]:
        """Messages for the one user who just opened ``path`` (cached state)."""
        ...
    def status(self) -> dict:
        """Merged into ``/api/status``."""
        ...
    def shutdown(self) -> None: ...


@runtime_checkable
class Diagnostics(Protocol):
    """File -> error markers.  Served at ``POST /api/check``."""

    extensions: tuple[str, ...]

    async def check(self, ctx: PluginContext, path: str) -> dict:
        """Return ``{"diagnostics": [{"message", "severity", "line", "col", "file"}], "error"?: str}``."""
        ...


@dataclass
class Action:
    """A build-like thing with a button and progress.

    ``run(ctx, params)`` may be sync (run in the threadpool) or async and returns
    ``{"success": bool, "output": str, "download": url?}``.  Progress goes out
    with ``ctx.broadcast({"type": "action_progress", "action": id, "completed": n,
    "total": m, "message": str, "phase": "progress|log|done|error"})``.  Served at
    ``POST /api/actions/<id>``.  With ``custom_ui=True`` the plugin's JavaScript
    renders the page and the generic one is not drawn.
    """
    id: str
    label: str
    run: Callable[[PluginContext, dict], Any]
    icon: str = "zap"
    description: str = ""
    custom_ui: bool = False


@dataclass
class SettingsPage:
    """A tab on the Settings page.

    Either schema-driven (``fields`` + ``load``/``save``, served at
    ``/api/settings/<id>`` and drawn by the generic frontend), or ``custom``:
    the plugin's JavaScript registers a renderer with
    ``Syncworthy.registerSettingsPage(id, {render, save})`` and brings its own
    API routes through a router.

    Field: ``{"key", "label", "type": "text|textarea|number|boolean|select|list",
    "help"?, "options"?, "placeholder"?}``.
    """
    id: str
    title: str
    fields: list[dict] = field(default_factory=list)
    load: Callable[[PluginContext], dict] | None = None
    save: Callable[[PluginContext, dict], dict | None] | None = None
    description: str = ""
    custom: bool = False


@dataclass
class McpTool:
    """An extra MCP tool.  ``handler(ctx, args)`` returns text or a list of MCP content blocks."""
    name: str
    description: str
    input_schema: dict
    handler: Callable[[PluginContext, dict], Any]


@dataclass
class Language:
    """A language for the editor, as data (served in ``/api/app``, registered by the frontend).

    ``extensions`` (with the dot) map files to it; ``monarch`` is a Monaco Monarch
    tokenizer definition and ``configuration`` a language configuration (comments,
    brackets, auto-closing pairs...), both as JSON-able dicts -- a regex is a string
    in them (Monarch takes strings as regex sources; ``configuration.wordPattern``
    and ``folding.markers`` are turned into RegExps by the frontend).
    """
    id: str
    extensions: list[str]
    aliases: list[str] = field(default_factory=list)
    monarch: dict | None = None
    configuration: dict | None = None


# ---------------------------------------------------------------------------
# The base class
# ---------------------------------------------------------------------------

class Plugin:
    """Base class.  Override what you need; every hook has a harmless default."""

    name: str = ""
    version: str = "0"

    # -- project layout ---------------------------------------------------
    #: Name of the CRDT state directory this plugin wants (None: no opinion).
    state_dir: str | None = None
    #: Env var holding the MCP bearer token (None: no opinion).
    mcp_token_env: str | None = None
    #: Globs hidden everywhere (tree, MCP listings).
    ignore: list[str] = []
    #: Globs hidden from the file tree only.
    tree_ignore: list[str] = []
    #: Show only these top-level entries in the tree (None: everything).
    roots: list[str] | None = None
    #: Create missing ``roots`` directories so they always show.
    create_roots: bool = False
    #: Extra suffixes MCP treats as editable text.
    text_suffixes: set[str] = set()

    # -- frontend ----------------------------------------------------------
    #: Directory served at ``/plugins/<name>/``.
    static_dir: Path | None = None
    #: Files in ``static_dir`` loaded, in order, after the core scripts.
    static_scripts: list[str] = []
    static_styles: list[str] = []

    # -- features ------------------------------------------------------------
    previewer: Previewer | None = None
    diagnostics: Diagnostics | None = None

    def actions(self) -> list[Action]:
        return []

    def settings_pages(self) -> list[SettingsPage]:
        return []

    def mcp_tools(self) -> list[McpTool]:
        return []

    def languages(self) -> list[Language]:
        return []

    def routers(self) -> list:
        """FastAPI ``APIRouter`` objects mounted into the app (before the static mount)."""
        return []

    def branding(self) -> dict:
        """Overrides for the generic look; see docs/PLUGINS.md for the keys."""
        return {}

    def status(self, ctx: PluginContext) -> dict:
        """Merged into ``/api/status``."""
        return {}

    # -- lifecycle -------------------------------------------------------------
    def configure(self, ctx: PluginContext) -> None:
        """Called once while the app is built, before it serves anything (sync)."""

    async def on_startup(self, ctx: PluginContext) -> None:
        """Called when the server starts, on the event loop; ``ctx.loop`` is set."""

    async def on_shutdown(self, ctx: PluginContext) -> None:
        """Called when the server stops."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _instantiate(obj: Any, label: str) -> Any:
    if inspect.isclass(obj) or (callable(obj) and not hasattr(obj, "name")):
        obj = obj()
    if not getattr(obj, "name", ""):
        raise ValueError(f"plugin {label!r} has no name")
    return obj


def load_plugin(spec: str) -> Any:
    """Load one plugin by entry-point name or ``module:attribute``."""
    if ":" in spec:
        mod_name, _, attr = spec.partition(":")
        obj = getattr(importlib.import_module(mod_name), attr)
        return _instantiate(obj, spec)
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        if ep.name == spec:
            return _instantiate(ep.load(), spec)
    known = ", ".join(sorted(ep.name for ep in metadata.entry_points(group=ENTRY_POINT_GROUP))) or "none"
    raise LookupError(f"no syncworthy plugin named {spec!r} (installed: {known})")


def load_plugins(specs: list[str]) -> list[Any]:
    plugins, seen = [], set()
    for spec in specs:
        plugin = load_plugin(spec)
        if plugin.name in seen:
            log.warning("plugin %s listed twice; ignoring the second", plugin.name)
            continue
        seen.add(plugin.name)
        plugins.append(plugin)
    return plugins


def get(plugin: Any, attr: str, default: Any = None) -> Any:
    """Read an optional attribute (plugins need not subclass Plugin)."""
    value = getattr(plugin, attr, default)
    return default if value is None else value


def call_list(plugin: Any, method: str) -> list:
    fn = getattr(plugin, method, None)
    return list(fn()) if callable(fn) else []
