# Syncworthy

A real-time collaborative editor for any project: Monaco + Yjs in the browser, one
CRDT room per file, live presence, cursors and chat, an MCP endpoint so an agent
edits the live document rather than the file, and plugins for everything specific
to a kind of project.  Extracted from the Noteworthy web GUI; Noteworthy is now a
plugin (`noteworthy.syncworthy_plugin`).

```
syncworthy serve <project-dir> [--bind HOST] [--port N] [--headless]
```

Optional `syncworthy.toml` in the project (see `syncworthy/config.py`): `plugins`, `watch`, `[auth]`,
`ignore`, `tree_ignore`, `roots`, `state_dir`, `[mcp] token_env`, `[branding]`,
`[plugin.<name>]`.  CRDT state lives in `.syncworthy/`; an existing
`.noteworthy-crdt/` is kept, so a Noteworthy project keeps its CRDT identity.

Layout: `syncworthy/server.py` (`create_app(project_root, config, plugins)`),
`rooms.py`, `hub.py`, `files.py`, `watcher.py`, `auth.py`, `mcp.py`, `plugins.py`, `config.py`, `static/`.
The plugin interface is in [docs/PLUGINS.md](docs/PLUGINS.md).  Tests: `pytest`.

MCP: `POST /mcp`, bearer token from `$SYNCWORTHY_MCP_TOKEN` (or `..._FILE`), or the
plugin's/config's variable.  Fails closed with no token.

## File watcher

`watchfiles` watches the project directory (`watch = false` in `syncworthy.toml` turns it off,
`watch_poll = true` polls instead, for network filesystems).  An external edit to a file with a
live room is merged into the room by the minimal-diff reload, so editors see an edit rather than a
swapped buffer; files with no room only refresh the tree.  The server's own saves are recognised
by content hash and not fed back.  A file that vanishes under a live room is held for 1.5 s:
the same content under a new path (a rename, a directory move) rebinds the room, the same path
reloads it, otherwise the room is closed.  Ignore globs and the state directory are not watched.
Note that inotify watches every directory under the project, including ignored ones (a big
`node_modules` costs watches; point `roots` or the system `fs.inotify.max_user_watches` at it).

## Auth

```toml
[auth]
tokens = [
  { name = "me",    token_env  = "SYNCWORTHY_TOKEN_ME", role = "edit" },
  { name = "class", token_file = "/run/secrets/class",  role = "view" },
]
```

Secrets are never in the toml: `token_env` names an environment variable, `token_file` a file;
`SYNCWORTHY_EDIT_TOKEN` / `SYNCWORTHY_VIEW_TOKEN` (and `..._FILE`) work too.  A browser opens
`http://host:port/?token=...` once and gets an HttpOnly, SameSite=Lax cookie; scripts send
`Authorization: Bearer ...` (WebSockets also take `?access_token=`).  `edit` can do everything;
`view` reads documents, sees presence and chats, but its Yjs updates are dropped and every
write request is refused.  Enforced on `/yjs`, `/ws/doc`, `/api/*` and the static files; `/mcp`
keeps its own bearer token.  With no tokens the server is open, which is allowed only on a
loopback bind; on any other address it refuses to start unless `--insecure-open` is given.
WebSockets from another origin are refused (extra origins: `allowed_origins = [...]` under
`[auth]`).  `/auth/logout` clears the cookie.

## Languages and Monaco

One Monaco (0.56, vendored under `static/vendor/`, the same module y-monaco binds to; its
language definitions load lazily from `vendor/languages/`).  File extension to language: Monaco's
own languages, plus LaTeX, TOML and JSON grammars of ours, plus what plugins register
(`Plugin.languages()`, or `Syncworthy.registerLanguage` from a plugin script).  The CSS/HTML/JSON/TS
language *services* (validation, completion; they need their own workers and a second copy of
Monaco) are not shipped; highlighting is.

## Still to do

- Move the Noteworthy build-page CSS (`#page-build`, `.build-*`) from `main.css`
  into the plugin's static bundle.
- Delete `noteworthy/gui/` and `gui_solo/`; deployment; docs updates.
- Per-token identity: the hub still names users by what the browser sends.
