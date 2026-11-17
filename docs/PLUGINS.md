# Writing a Syncworthy plugin

A plugin is a Python object with a `name` (normally a subclass of
`syncworthy.Plugin`; every hook has a default).  Register it with an entry point,

```toml
[project.entry-points."syncworthy.plugins"]
myplugin = "mypkg:MyPlugin"        # class, instance or zero-arg factory
```

and enable it in the project's `syncworthy.toml` with `plugins = ["myplugin"]`
(`"pkg.mod:attr"` works for importable but uninstalled code).  Embedding programs
skip discovery: `syncworthy.create_app(root, plugins=[MyPlugin()])`.
Per-plugin settings from the project: `[plugin.myplugin]` -> `ctx.settings`.

## What a plugin may provide

| Hook | What |
|---|---|
| `state_dir`, `mcp_token_env`, `ignore`, `tree_ignore`, `roots`, `create_roots`, `text_suffixes` | project layout defaults (config wins) |
| `previewer` | `Previewer` protocol: `extensions`, `placeholder(_selected)`, `attach(ctx)`, `handles(path)`, `start/stop(path)`, `async initial_messages(path)`, `status()`, `shutdown()`. The hub starts/stops it as users open/leave files; it pushes JSON via `ctx.broadcast_to_file`. Shown as the preview panel (hidden when no previewer). |
| `diagnostics` | `extensions` + `async check(ctx, path) -> {"diagnostics": [{message, severity, line, col, file}]}`, served at `POST /api/check`; the frontend re-checks 2 s after edits and draws markers. |
| `actions()` | `Action(id, label, run(ctx, params), icon, description, custom_ui)`; `POST /api/actions/<id>`; progress via `{"type": "action_progress", action, completed, total, message}`. Generic page unless `custom_ui`. |
| `settings_pages()` | `SettingsPage(id, title, fields, load, save)` for schema forms at `/api/settings/<id>` (field types text/textarea/number/boolean/select/list), or `custom=True` with your own JS + routes. |
| `mcp_tools()` | `McpTool(name, description, input_schema, handler(ctx, args))`; returns text or MCP content blocks. |
| `languages()` | `Language(id, extensions, aliases, monarch, configuration)`; data only in Phase 1. |
| `branding()` | dict merged over defaults: `name`, `title`, `welcome_text`, `welcome_logo`, `new_file_name`, `new_file_dir`, `file_icons` (ext -> svg), `tree_top`, `tree_bottom`, `themes`, `base_theme`, `info`. |
| `routers()` | FastAPI routers, mounted before the static catch-all. |
| `configure(ctx)`, `async on_startup(ctx)`, `async on_shutdown(ctx)` | lifecycle; `ctx.loop` is set from startup on. |
| `static_dir`, `static_scripts`, `static_styles` | served at `/plugins/<name>/`, loaded after the core scripts. |

`PluginContext`: `root`, `state_dir`, `config`, `settings`, `rooms` (RoomManager),
`hub`, `loop`, `log`, `resolve(rel)`, and thread-safe `broadcast(msg)` /
`broadcast_to_file(path, msg)`.

## Frontend bundle

Scripts in `static_scripts` call:

```js
Syncworthy.registerPlugin('name', mixin, {
  init(app) {},                       // after core init: app.registerPage({id,label,icon,html,onShow})
  messages: { preview(msg) {} },      // doc-socket message types you handle
  fileOpen(path, app) {},
});
Syncworthy.registerSettingsPage('id', { render(container) {}, save() {} });
```
`mixin` is `Object.assign`ed onto `app` (the existing mixin seam), so your methods
are `app.x()` and inline handlers like `onclick="app.x()"` work.  Branding, preview
and diagnostics capabilities arrive from `GET /api/app`.
