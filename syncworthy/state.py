"""The per-app state object, so nothing in the package is a module-level singleton."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .files import IgnoreRules
from .hub import Hub
from .plugins import PluginContext
from .rooms import RoomManager


@dataclass
class AppState:
    root: Path
    state_dir: Path
    config: Config
    rooms: RoomManager
    hub: Hub
    ignore: IgnoreRules            # everywhere: tree and MCP listings
    tree_ignore: IgnoreRules       # the tree as well
    roots: list[str] | None
    create_roots: bool
    text_suffixes: set[str]
    mcp_token_env: str
    app_name: str
    plugins: list[Any] = field(default_factory=list)
    contexts: dict[str, PluginContext] = field(default_factory=dict)   # by plugin name
    branding: dict = field(default_factory=dict)
