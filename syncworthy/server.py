"""Syncworthy server: the FastAPI app factory.

``create_app(project_root, config, plugins)`` returns an app holding only the
generic routes -- the Yjs endpoint, the doc socket, the file API, the MCP
transport -- plus whatever the given plugins bring (routers, static bundles,
settings pages, actions, diagnostics, MCP tools).  Nothing is module-global:
two apps can live in one process.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pycrdt import Text

from . import plugins as plugin_api
from .auth import Auth, AuthMiddleware
from .config import DEFAULT_IGNORE, DEFAULT_MCP_TOKEN_ENV, Config, resolve_state_dir
from .files import TEXT_SUFFIXES, IgnoreRules, resolve_in_project, scan_tree
from .hub import Hub
from .mcp import McpServer, authorize, expected_token
from .plugins import PluginContext
from .rooms import RoomManager
from .state import AppState
from .watcher import Watcher

log = logging.getLogger("syncworthy")

STATIC_DIR = Path(__file__).parent / "static"

# FastAPI's File()/Form() params need python-multipart to parse multipart
# bodies — and it inspects this at route-registration time, so a missing
# dependency would otherwise take down the whole module import, not just
# the upload endpoint. See /api/upload below.
try:
    from python_multipart import __version__ as _  # noqa: F401
    _MULTIPART_AVAILABLE = True
except ImportError:
    _MULTIPART_AVAILABLE = False


DEFAULT_BRANDING = {
    "name": "Syncworthy",
    "welcome_text": "SYNCWORTHY",
    "welcome_logo": "",
    "new_file_name": "new-file.txt",
    "new_file_dir": "",
    "file_icons": {},               # extension -> inline SVG
    "tree_top": [],                 # top-level names pinned first
    "tree_bottom": [],              # ... and last
    "themes": None,                 # id -> {label, base, colors}; None = the built-in pair
    "base_theme": "syncworthy-dark",  # the theme id that leaves body[data-theme] unset
    "info": {},                     # {tagline, version, links: [{label, url}], html}
}


class SafeStaticFiles(StaticFiles):
    """Static file mount that safely ignores websocket fallthrough."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1000})
            return
        await super().__call__(scope, receive, send)


def _merge_branding(plugins: list, config: Config) -> dict:
    out = copy.deepcopy(DEFAULT_BRANDING)
    layers = [p.branding() for p in plugins if callable(getattr(p, "branding", None))]
    layers.append(config.branding)
    for layer in layers:
        for key, value in (layer or {}).items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key].update(value)
            elif isinstance(value, dict) and key == "themes":
                out[key] = dict(value)
            else:
                out[key] = value
    return out


def create_app(project_root, config: Config | None = None, plugins: list | None = None,
               packet_log: bool | None = None, auth: Auth | None = None,
               watch: bool | None = None) -> FastAPI:
    """Build the app for PROJECT_ROOT.

    CONFIG defaults to ``syncworthy.toml`` in the project (or the defaults).
    PLUGINS, if given, is the exact list of plugin objects to use; if None they
    are discovered from ``config.plugins``.  AUTH defaults to the tokens in the
    config and environment (none: open, which ``run_server`` only allows on a
    loopback bind); WATCH overrides ``config.watch``.
    """
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    config = config if config is not None else Config.load(root)
    if plugins is None:
        plugins = plugin_api.load_plugins(config.plugins)

    # ---- layout decided from config + plugins --------------------------
    plugin_state_dir = next((plugin_api.get(p, "state_dir") for p in plugins
                             if plugin_api.get(p, "state_dir")), None)
    state_dir = resolve_state_dir(root, config, plugin_state_dir)

    ignore_patterns = list(DEFAULT_IGNORE) + [state_dir.name]
    tree_patterns: list[str] = []
    text_suffixes = set(TEXT_SUFFIXES)
    roots = list(config.roots) if config.roots is not None else None
    create_roots = bool(config.create_roots)
    for p in plugins:
        ignore_patterns += list(plugin_api.get(p, "ignore", []))
        tree_patterns += list(plugin_api.get(p, "tree_ignore", []))
        text_suffixes |= set(plugin_api.get(p, "text_suffixes", set()))
        if roots is None and plugin_api.get(p, "roots") is not None:
            roots = list(p.roots)
            if config.create_roots is None:
                create_roots = bool(plugin_api.get(p, "create_roots", False))
    ignore_patterns += config.ignore
    tree_patterns += config.tree_ignore

    branding = _merge_branding(plugins, config)
    token_env = (config.mcp_token_env
                 or next((plugin_api.get(p, "mcp_token_env") for p in plugins
                          if plugin_api.get(p, "mcp_token_env")), None)
                 or DEFAULT_MCP_TOKEN_ENV)

    auth = auth if auth is not None else Auth.from_config(config)
    rooms = RoomManager(root, state_dir, packet_log=packet_log)
    hub = Hub()
    state = AppState(
        root=root, state_dir=state_dir, config=config, rooms=rooms, hub=hub,
        ignore=IgnoreRules(ignore_patterns),
        tree_ignore=IgnoreRules(ignore_patterns + tree_patterns),
        roots=roots, create_roots=create_roots, text_suffixes=text_suffixes,
        mcp_token_env=token_env, app_name=branding["name"],
        plugins=list(plugins), branding=branding, auth=auth,
    )
    use_watcher = config.watch if watch is None else watch

    # ---- plugin contexts and features ------------------------------------
    for p in plugins:
        state.contexts[p.name] = PluginContext(
            root=root, state_dir=state_dir, config=config, plugin_name=p.name,
            settings=config.plugin_settings.get(p.name, {}), rooms=rooms, hub=hub,
            app_name=branding["name"])

    previewers, diagnostics_list = [], []
    actions: dict[str, tuple[PluginContext, plugin_api.Action]] = {}
    settings_pages: dict[str, tuple[PluginContext, plugin_api.SettingsPage]] = {}
    mcp_tools: list[tuple[PluginContext, plugin_api.McpTool]] = []
    languages: list[plugin_api.Language] = []
    for p in plugins:
        ctx = state.contexts[p.name]
        configure = getattr(p, "configure", None)
        if callable(configure):
            configure(ctx)
        prev = plugin_api.get(p, "previewer")
        if prev is not None:
            prev.attach(ctx)
            previewers.append(prev)
        diag = plugin_api.get(p, "diagnostics")
        if diag is not None:
            diagnostics_list.append((ctx, diag))
        for a in plugin_api.call_list(p, "actions"):
            actions[a.id] = (ctx, a)
        for page in plugin_api.call_list(p, "settings_pages"):
            settings_pages[page.id] = (ctx, page)
        mcp_tools += [(ctx, t) for t in plugin_api.call_list(p, "mcp_tools")]
        languages += plugin_api.call_list(p, "languages")
    hub.previewers = previewers
    mcp = McpServer(state, mcp_tools)

    # ---- lifespan -----------------------------------------------------------
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        for ctx in state.contexts.values():
            ctx.loop = loop
        for p in plugins:
            await _maybe_await(getattr(p, "on_startup", None), state.contexts[p.name])

        # In pycrdt-websocket 0.16+, the server must be explicitly started
        # and we must wait for it to be ready before accepting connections.
        yjs_task = None
        if rooms.server:
            log.info("Starting Yjs WebSocket Server...")
            yjs_task = asyncio.create_task(rooms.server.start())
            await rooms.server.started.wait()
            log.info("Yjs WebSocket Server started and ready")

        if use_watcher:
            state.watcher = Watcher(state, force_polling=config.watch_poll)
            await state.watcher.start()

        yield

        if state.watcher is not None:
            await state.watcher.stop()
        if rooms.server:
            log.info("Stopping Yjs WebSocket Server...")
            await rooms.server.stop()
        if yjs_task and not yjs_task.done():
            yjs_task.cancel()
            try:
                await yjs_task
            except asyncio.CancelledError:
                pass
        for prev in previewers:
            try:
                prev.shutdown()
            except Exception as e:
                log.error(f"previewer shutdown failed: {e}")
        for p in plugins:
            try:
                await _maybe_await(getattr(p, "on_shutdown", None), state.contexts[p.name])
            except Exception as e:
                log.error(f"plugin {p.name} shutdown failed: {e}")
        log.info("Shutdown complete.")

    app = FastAPI(title=f"{branding['name']} server", lifespan=lifespan)
    app.state.sync = state
    app.add_middleware(AuthMiddleware, auth=auth, app_name=branding["name"])

    # Yjs WebSocket endpoint
    app.mount("/yjs", rooms.asgi_app())

    # ============================================================
    # DOC SOCKET: chat, presence, cursors, plugin messages
    # ============================================================

    @app.websocket("/ws/doc")
    async def doc_endpoint(websocket: WebSocket):
        """
        Doc-socket: Chat, Preview, File Presence, Identity.

        Strict packet separation — does NOT handle content sync, which is
        exclusively handled by the Yjs WebSocket (/yjs). Cursors DO ride this
        socket: they were moved off Yjs awareness so that identity fields stay
        server-owned.
        """
        user_name = websocket.query_params.get("name", "Anonymous")
        user_id = websocket.query_params.get("id", None)
        user_token = websocket.query_params.get("token", None)
        await websocket.accept()

        user = await hub.connect(websocket, user_name, user_id, token=user_token)

        try:
            # Send initial state
            await websocket.send_text(json.dumps({
                "type": "welcome",
                "userId": user.id,
                "color": user.color,
                "users": hub.get_users()
            }))

            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    log.warning(f"Ignoring malformed JSON from {user.id}")
                    continue
                msg_type = msg.get("type")

                if msg_type == "join":
                    path = msg.get("path") or msg.get("file", "")
                    await hub.join_file(user.id, path)

                elif msg_type == "identity":
                    await hub.update_identity(user.id, msg.get("name", "Anonymous"))

                elif msg_type == "chat":
                    await hub.send_chat(
                        user.id,
                        msg.get("text") or msg.get("message", ""),
                        # Stamp it here when the client did not.  A missing field
                        # defaulted to 0, which is the epoch -- rendered in a UTC+9
                        # locale that is a perfectly plausible-looking 09:00:00 on
                        # every line the Emacs client sent.
                        msg.get("timestamp") or int(time.time() * 1000)
                    )
                elif msg_type == "cursor":
                    # Real-time cursor position broadcast.
                    # Build a canonical payload from server-owned identity fields.
                    file_path = msg.get("file")
                    if not file_path:
                        continue

                    def int_field(key: str, default: int) -> int:
                        try:
                            return int(msg.get(key, default))
                        except (TypeError, ValueError):
                            return default

                    line = int_field("line", 1)
                    col = int_field("col", 1)
                    cursor_msg = {
                        "type": "cursor",
                        "userId": user.id,
                        "file": file_path,
                        "line": line,
                        "col": col,
                        "selStartLine": int_field("selStartLine", line),
                        "selStartCol": int_field("selStartCol", col),
                        "selEndLine": int_field("selEndLine", line),
                        "selEndCol": int_field("selEndCol", col),
                        "name": user.name,
                        "color": msg.get("color") or user.color,
                        "token": user.token,
                    }
                    await hub.broadcast(cursor_msg, exclude=user.id)

                # delta, users, content, operation, ack, resync are
                # Yjs-layer packets — silently ignored on this socket.

        except WebSocketDisconnect:
            await hub.disconnect(user.id, websocket)
        except Exception as e:
            log.error(f"Doc socket error: {e}")
            await hub.disconnect(user.id, websocket)

    # ============================================================
    # FILE API - Generic file read/write
    # ============================================================

    @app.get("/api/file")
    def get_file(path: str, raw: int = 0):
        """Read a file relative to project root. If raw=1, return file directly."""
        target = resolve_in_project(root, path)
        if target is None:
            return {"error": "Invalid path"}
        if target.exists() and target.is_file():
            if raw:
                # Return file directly for binary content (PDF, images)
                import mimetypes
                mime_type, _ = mimetypes.guess_type(str(target))
                return FileResponse(target, media_type=mime_type or 'application/octet-stream')
            try:
                return {"content": target.read_text(encoding='utf-8')}
            except Exception:
                return {"content": "", "error": "Could not read file"}
        return {"error": "File not found"}

    @app.post("/api/file")
    def save_file(data: dict = Body(...)):
        """Write a file relative to project root."""
        path = data.get("path")
        content = data.get("content", "")
        target = resolve_in_project(root, path)
        if target is None:
            return {"success": False, "error": "Invalid path"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        # Not noted in the ledger: a write behind a live room's back is exactly
        # what the file watcher exists to merge into it.
        return {"success": True}

    if _MULTIPART_AVAILABLE:
        @app.post("/api/upload")
        async def upload_files(files: list[UploadFile] = File(...), directory: str = Form("")):
            """Upload one or more files into a project-relative directory."""
            saved = []
            errors = []
            for f in files:
                # Only trust the basename — a crafted filename like "../../x" must
                # not be able to walk out of `directory`.
                filename = os.path.basename(f.filename or "")
                if not filename:
                    errors.append("Skipped a file with no name")
                    continue
                rel_path = f"{directory}/{filename}" if directory else filename
                dest = resolve_in_project(root, rel_path)
                if dest is None:
                    errors.append(f"Rejected unsafe path: {f.filename}")
                    continue
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    content = await f.read()
                    dest.write_bytes(content)
                    rel = str(dest.relative_to(root))
                    # A room may be live for this path, still holding the old
                    # text; its next save would put that straight back over what
                    # was just uploaded.  Hand it the new contents.
                    try:
                        await rooms.reload_room(rel)
                    except Exception as e:
                        logging.warning("Could not refresh the room for %s: %s", rel, e)
                    saved.append(rel)
                except Exception as e:
                    errors.append(f"{filename}: {e}")

            return {"success": bool(saved) and not errors, "saved": saved, "errors": errors}
    else:
        # FastAPI's File()/Form() params require python-multipart to even be
        # *registered* (it inspects the signature at route-decoration time), so
        # we can't define the real route unless the dependency is present. Fall
        # back to a route that fails cleanly instead of the module failing to
        # import.
        @app.post("/api/upload")
        async def upload_files():
            return {"success": False, "error": "Server is missing the python-multipart dependency for uploads"}

    @app.post("/api/delete")
    async def delete_file(data: dict = Body(...)):
        """Delete a file relative to project root."""
        path = data.get("path")
        if not path:
            return {"success": False, "error": "No path provided"}

        target = root / path
        if not target.exists():
            return {"success": False, "error": "File not found"}

        # Security check - ensure path is within project
        try:
            target.resolve().relative_to(root.resolve())
        except ValueError:
            return {"success": False, "error": "Invalid path"}

        # Close any live Yjs room(s) for this path (or nested under it, for a
        # directory delete) BEFORE touching disk. Otherwise a client that still
        # has the file open keeps its debounced save timer running and
        # resurrects the file on its next keystroke.
        await rooms.close_rooms_under(path)

        try:
            if target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/rename")
    async def rename_file(data: dict = Body(...)):
        """Rename or move a file relative to project root."""
        path = data.get("path")
        new_name = data.get("newName")
        new_path = data.get("newPath")  # Optional: full new path for move operations

        if not path:
            return {"success": False, "error": "Missing path"}

        if not new_name and not new_path:
            return {"success": False, "error": "Missing newName or newPath"}

        source = root / path
        if not source.exists():
            return {"success": False, "error": "File not found"}

        # Security check - ensure path is within project
        try:
            source.resolve().relative_to(root.resolve())
        except ValueError:
            return {"success": False, "error": "Invalid path"}

        # Calculate destination path
        if new_path:
            dest = root / new_path           # Full path move
        else:
            dest = source.parent / new_name  # Simple rename in same directory

        # Security check - ensure destination is within project
        try:
            dest.resolve().relative_to(root.resolve())
        except ValueError:
            return {"success": False, "error": "Invalid destination path"}

        if dest.exists():
            return {"success": False, "error": "A file with that name already exists"}

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            source.rename(dest)
            result_path = str(dest.relative_to(root))
            # Rebind any live Yjs room(s) (or nested under it, for a directory
            # move) to the new path. Otherwise the old path's room keeps saving
            # to a location that no longer exists — and a fresh room opened at
            # the new path would start split-brained against it.
            await rooms.rename_rooms_under(path, result_path)
            return {"success": True, "newPath": result_path}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/tree")
    def get_file_tree():
        """Get complete file tree for editor."""
        return scan_tree(root, state.tree_ignore, roots=state.roots,
                         create_roots=state.create_roots)

    @app.post("/api/rooms/reload")
    async def reload_rooms(data: dict = Body(default={})):
        """Take the file on disk into any live room for it.

        A room holds the document, and the file is its export -- so writing the
        file behind a live room's back does not update it, and the room's next
        save puts the old text straight back over what was written.  Deploying a
        template into a running project is exactly that, and it is why an edited
        `parser.typ' could come back stale and fail to compile.

        Pass {"paths": [...]}, or nothing to reconcile every live room.
        """
        paths = data.get("paths")
        if not paths:
            paths = list(rooms.rooms.keys())
        done, missing = [], []
        for path in paths:
            if path in rooms.rooms:
                await rooms.reload_room(path)
                done.append(path)
            else:
                missing.append(path)
        return {"success": True, "reloaded": done, "no_live_room": missing}

    # ============================================================
    # PLUGIN SEAMS: diagnostics, actions, settings pages
    # ============================================================

    @app.post("/api/check")
    async def check_diagnostics(data: dict = Body(default={})):
        """File -> diagnostics, from whichever plugin handles this file."""
        path = data.get("path") or ""
        for ctx, diag in diagnostics_list:
            exts = tuple(getattr(diag, "extensions", ()) or ())
            if not path or not exts or path.endswith(exts):
                return await diag.check(ctx, path)
        return {"diagnostics": []}

    @app.post("/api/actions/{action_id}")
    async def run_action(action_id: str, data: dict = Body(default={})):
        entry = actions.get(action_id)
        if entry is None:
            return JSONResponse({"success": False, "output": f"no action {action_id!r}"}, status_code=404)
        ctx, action = entry
        try:
            return await _call(action.run, ctx, data)
        except Exception as e:
            log.exception("action %s failed", action_id)
            return {"success": False, "output": str(e)}

    @app.get("/api/settings/{page_id}")
    async def get_settings_page(page_id: str):
        entry = settings_pages.get(page_id)
        if entry is None or entry[1].load is None:
            return JSONResponse({"error": f"no settings page {page_id!r}"}, status_code=404)
        ctx, page = entry
        return await _call(page.load, ctx)

    @app.post("/api/settings/{page_id}")
    async def save_settings_page(page_id: str, data: dict = Body(...)):
        entry = settings_pages.get(page_id)
        if entry is None or entry[1].save is None:
            return JSONResponse({"error": f"no settings page {page_id!r}"}, status_code=404)
        ctx, page = entry
        result = await _call(page.save, ctx, data)
        return result if result is not None else {"success": True}

    # ============================================================
    # APP INFO + STATUS
    # ============================================================

    @app.get("/api/app")
    def app_info(request: Request):
        """What the frontend needs to configure itself: branding, plugin bundles, pages."""
        previewer = previewers[0] if previewers else None
        diag = diagnostics_list[0][1] if diagnostics_list else None
        return {
            "name": branding["name"],
            "auth": {"enabled": auth.enabled, "role": request.scope.get("sw_role", "edit")},
            "branding": branding,
            "project": root.name,
            "plugins": [
                {
                    "name": p.name,
                    "version": plugin_api.get(p, "version", "0"),
                    "base": f"/plugins/{p.name}/" if plugin_api.get(p, "static_dir") else None,
                    "scripts": list(plugin_api.get(p, "static_scripts", [])),
                    "styles": list(plugin_api.get(p, "static_styles", [])),
                }
                for p in plugins
            ],
            "preview": None if previewer is None else {
                "extensions": list(previewer.extensions),
                "placeholder": previewer.placeholder,
                "placeholder_selected": previewer.placeholder_selected,
            },
            "diagnostics": None if diag is None else {"extensions": list(diag.extensions)},
            "actions": [{"id": a.id, "label": a.label, "icon": a.icon,
                         "description": a.description, "custom_ui": a.custom_ui}
                        for _, a in actions.values()],
            "settings_pages": [{"id": pg.id, "title": pg.title, "description": pg.description,
                                "fields": pg.fields, "custom": pg.custom}
                               for _, pg in settings_pages.values()],
            "languages": [{"id": l.id, "extensions": l.extensions, "aliases": l.aliases,
                           "monarch": l.monarch, "configuration": l.configuration}
                          for l in languages],
        }

    @app.get("/api/status")
    def get_status():
        """Get system status."""
        status = {"project": root.name, "path": str(root)}
        for p in plugins:
            fn = getattr(p, "status", None)
            if callable(fn):
                status.update(fn(state.contexts[p.name]) or {})
        return status

    @app.get("/api/debug/yjs")
    async def debug_yjs_state():
        """Debug endpoint to inspect Yjs rooms state."""
        status = {"yjs_rooms": []}
        for name, room in rooms.rooms.items():
            try:
                text = room.ydoc.get("content", type=Text)
                content_len = len(text)
                content_preview = str(text)[:50] + "..." if content_len > 0 else ""
                status["yjs_rooms"].append({
                    "name": name,
                    "initialized": getattr(room, "_initialized", False),
                    "content_length": content_len,
                    "content_preview": content_preview,
                    "file_exists": room._file_path.exists() if hasattr(room, "_file_path") else "Unknown"
                })
            except Exception as e:
                status["yjs_rooms"].append({"name": name, "error": str(e)})
        return status

    # ============================================================
    # MCP
    # ============================================================

    def _token_env() -> str:
        # The configured name, falling back to the generic one.
        for name in (state.mcp_token_env, DEFAULT_MCP_TOKEN_ENV):
            if expected_token(name):
                return name
        return state.mcp_token_env

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """MCP over Streamable HTTP.

        Here rather than as a separate process because the rooms are here: a tool
        that edits a document has to reach the live one, and an agent anywhere
        else reaches it over the same port the editor already uses.
        """
        ok, why = authorize(request.headers.get("authorization"),
                            request.query_params.get("token"), _token_env())
        if not ok:
            # 401 with a challenge, so a client knows to send one rather than
            # guessing at what it did wrong.
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32001, "message": f"unauthorized: {why}"}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "parse error"}},
                status_code=400,
            )

        # A batch is a list; a single call is not.  Notifications answer nothing,
        # which over HTTP is 202 with an empty body rather than a null result.
        batch = isinstance(payload, list)
        messages = payload if batch else [payload]
        answers = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            reply = await mcp.handle_rpc(msg)
            if reply is not None:
                answers.append(reply)

        if not answers:
            return Response(status_code=202)
        return JSONResponse(answers if batch else answers[0])

    @app.get("/mcp")
    async def mcp_no_stream():
        """No server-initiated stream: every answer rides on its own POST."""
        return Response(status_code=405, headers={"Allow": "POST"})

    # Legacy endpoints - prevent crash if old clients connect
    for legacy in ("/ws/collab", "/ws/sync", "/ws"):
        def _make(path):
            @app.websocket(path)
            async def legacy_ws(websocket: WebSocket):
                await websocket.close()
        _make(legacy)

    # ============================================================
    # PLUGIN ROUTERS AND STATIC BUNDLES, then the app itself (last!)
    # ============================================================

    for p in plugins:
        for router in plugin_api.call_list(p, "routers"):
            app.include_router(router)
        static_dir = plugin_api.get(p, "static_dir")
        if static_dir and Path(static_dir).is_dir():
            app.mount(f"/plugins/{p.name}", StaticFiles(directory=str(static_dir)),
                      name=f"plugin-{p.name}")

    if STATIC_DIR.exists():
        app.mount("/", SafeStaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


async def _call(fn, *args):
    """Call a plugin callable: coroutine functions awaited, plain ones in the threadpool
    (they may block: a build, a compile)."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    result = await run_in_threadpool(fn, *args)
    return await result if inspect.isawaitable(result) else result


async def _maybe_await(fn, *args):
    if not callable(fn):
        return None
    result = fn(*args)
    if asyncio.iscoroutine(result):
        result = await result
    return result
