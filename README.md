# Syncworthy

A real-time collaborative editor for any project: Monaco + Yjs in the browser, one
CRDT room per file, live presence, cursors and chat, an MCP endpoint so an agent
edits the live document rather than the file, and plugins for everything specific
to a kind of project.  Extracted from the Noteworthy web GUI; Noteworthy is now a
plugin (`noteworthy.syncworthy_plugin`).

```
syncworthy serve <project-dir> [--bind HOST] [--port N] [--headless]
```

Optional `syncworthy.toml` in the project (see `syncworthy/config.py`): `plugins`,
`ignore`, `tree_ignore`, `roots`, `state_dir`, `[mcp] token_env`, `[branding]`,
`[plugin.<name>]`.  CRDT state lives in `.syncworthy/`; an existing
`.noteworthy-crdt/` is kept, so a Noteworthy project keeps its CRDT identity.

Layout: `syncworthy/server.py` (`create_app(project_root, config, plugins)`),
`rooms.py`, `hub.py`, `files.py`, `mcp.py`, `plugins.py`, `config.py`, `static/`.
The plugin interface is in [docs/PLUGINS.md](docs/PLUGINS.md).  Tests: `pytest`.

MCP: `POST /mcp`, bearer token from `$SYNCWORTHY_MCP_TOKEN` (or `..._FILE`), or the
plugin's/config's variable.  Fails closed with no token.

## Not in Phase 1 (TODO)

- **File watcher** (watchfiles): merge external edits into live rooms; refresh the tree.
- **Auth**: project tokens with `edit`/`view` roles for `/yjs`, `/ws/doc`, `/api/*`,
  static; only `/mcp` has a bearer check today.
- **Language registry**: plugin `languages()` data is served in `/api/app` but the
  frontend still opens everything as `markdown`.
- **Monaco de-duplication**: Monaco is still loaded twice (cdnjs AMD 0.45 and the
  vendored ESM used by y-monaco).
- Move the Noteworthy build-page CSS (`#page-build`, `.build-*`) from `main.css`
  into the plugin's static bundle.
- Delete `noteworthy/gui/` and `gui_solo/`; deployment; docs updates.
