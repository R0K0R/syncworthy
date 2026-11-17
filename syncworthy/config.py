"""Project configuration: the optional ``syncworthy.toml`` in the project root.

Everything in it is optional; a project with no file at all is served with the
defaults.  Example::

    plugins = ["noteworthy"]            # entry points of group syncworthy.plugins,
                                        # or "package.module:attribute"
    ignore = ["*.pdf", "drafts/"]       # hidden from the tree and from MCP listings
    roots = ["content", "assets"]       # show only these top-level entries in the tree
    state_dir = ".syncworthy"           # where CRDT state lives (see below)

    [mcp]
    token_env = "SYNCWORTHY_MCP_TOKEN"  # env var holding the bearer token
                                        # (NAME_FILE names a file holding it instead)

    [branding]
    name = "My Notes"

    [plugin.noteworthy]                 # free-form settings handed to that plugin

State directory.  An explicit ``state_dir`` wins.  Otherwise a plugin may name
one (Noteworthy keeps ``.noteworthy-crdt``), otherwise an existing legacy
``.noteworthy-crdt`` is kept so a project's CRDT identity survives the move to
Syncworthy, otherwise ``.syncworthy``.
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("syncworthy")

CONFIG_FILENAME = "syncworthy.toml"
DEFAULT_STATE_DIR = ".syncworthy"
LEGACY_STATE_DIRS = (".noteworthy-crdt",)
DEFAULT_MCP_TOKEN_ENV = "SYNCWORTHY_MCP_TOKEN"

# Never worth showing or listing, whatever the project.
DEFAULT_IGNORE = [
    ".git", "__pycache__", "node_modules", ".venv", "venv", "build",
    "*.sw-tmp",
]


@dataclass
class Config:
    plugins: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    tree_ignore: list[str] = field(default_factory=list)
    roots: list[str] | None = None
    create_roots: bool | None = None
    state_dir: str | None = None          # None: decide (see module docstring)
    mcp_token_env: str | None = None      # None: plugin's, else the default
    branding: dict = field(default_factory=dict)
    plugin_settings: dict[str, dict] = field(default_factory=dict)
    path: Path | None = None              # the file it came from, if any

    @classmethod
    def load(cls, project_root: Path) -> "Config":
        path = Path(project_root) / CONFIG_FILENAME
        if not path.is_file():
            return cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise SystemExit(f"{path}: {e}")
        return cls.from_dict(data, path)

    @classmethod
    def from_dict(cls, data: dict, path: Path | None = None) -> "Config":
        mcp = data.get("mcp") or {}
        roots = data.get("roots")
        return cls(
            plugins=[str(p) for p in data.get("plugins", [])],
            ignore=[str(p) for p in data.get("ignore", [])],
            tree_ignore=[str(p) for p in data.get("tree_ignore", [])],
            roots=[str(r) for r in roots] if roots is not None else None,
            create_roots=data.get("create_roots"),
            state_dir=data.get("state_dir"),
            mcp_token_env=mcp.get("token_env"),
            branding=dict(data.get("branding") or {}),
            plugin_settings={k: dict(v) for k, v in (data.get("plugin") or {}).items()},
            path=path,
        )


def resolve_state_dir(root: Path, config: Config, plugin_choice: str | None) -> Path:
    """Where this project's CRDT state lives; see the module docstring."""
    root = Path(root)
    if config.state_dir:
        return root / config.state_dir
    if plugin_choice:
        return root / plugin_choice
    if not (root / DEFAULT_STATE_DIR).exists():
        for legacy in LEGACY_STATE_DIRS:
            if (root / legacy).is_dir():
                log.info("keeping the existing %s state directory", legacy)
                return root / legacy
    return root / DEFAULT_STATE_DIR
