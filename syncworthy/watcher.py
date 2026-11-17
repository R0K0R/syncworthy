"""The file watcher: the project directory, kept in step with its live rooms.

A room holds the document and the file is its export, so a file changed behind
a room's back would be overwritten by the room's next save.  This module hears
of such changes (``watchfiles``, i.e. inotify and friends) and reconciles them:

* an **external edit** to a file with a live room is merged into the room by the
  existing minimal-diff ``reload_from_disk``, so connected editors see an edit,
  not a swapped buffer;
* a file with **no room** only changes the tree, so the clients are told to
  refresh it (``tree_changed``);
* the server's **own writes** are not fed back: every save is recorded in the
  ``DiskLedger`` (content hash per path) and an event for a file that still holds
  the server's last write is skipped -- re-reading it would delete whatever was
  typed since that save;
* **delete / create / rename**: a rename arrives as a delete plus a create.  A
  live room whose file vanished is held for a short grace period; if the same
  content turns up under another path in that window the room is rebound there
  (clients keep their connection and are told, ``file_renamed``), if the file
  comes back under the same path (an editor saving by delete-and-create) the room
  just reloads, and otherwise the room is closed cleanly (``file_removed``).

Ignore globs and the state directory are respected: events for paths the
project ignores are dropped by the watch filter itself.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from watchfiles import Change, awatch

from .rooms import DiskLedger

log = logging.getLogger("syncworthy")

DEBOUNCE_MS = 300          # coalesce an editor's burst of events into one batch
GRACE_SECONDS = 1.5        # how long a vanished file's room waits for it to reappear


@dataclass
class _Pending:
    """A live room whose file vanished, waiting to see where it went."""
    deadline: float
    digests: set[str]


class Watcher:
    def __init__(self, state, *, debounce_ms: int | None = None, grace: float | None = None,
                 force_polling: bool = False):
        self.state = state
        self.root: Path = Path(state.root)
        self.rooms = state.rooms
        self.hub = state.hub
        self.ledger: DiskLedger = state.rooms.ledger
        self.debounce_ms = DEBOUNCE_MS if debounce_ms is None else debounce_ms
        self.grace = GRACE_SECONDS if grace is None else grace
        self.force_polling = force_polling
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._sweeper: asyncio.Task | None = None
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()

    # ---- paths ---------------------------------------------------------------
    def rel(self, path: str | Path) -> str | None:
        """Project-relative posix path, or None for anything outside the project."""
        try:
            return Path(path).relative_to(self.root).as_posix()
        except ValueError:
            return None

    def ignored(self, rel: str | None) -> bool:
        return rel is None or rel == "." or self.state.ignore.matches(rel)

    def visible(self, rel: str) -> bool:
        """Would the tree show this path?"""
        if self.state.tree_ignore.matches(rel):
            return False
        roots = self.state.roots
        return not roots or Path(rel).parts[0] in roots

    def _filter(self, change: Change, path: str) -> bool:
        return not self.ignored(self.rel(path))

    # ---- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="syncworthy-watcher")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        for task in (self._sweeper, self._task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = self._sweeper = None

    async def _run(self) -> None:
        try:
            async for changes in awatch(self.root, watch_filter=self._filter,
                                        debounce=self.debounce_ms, stop_event=self._stop,
                                        force_polling=self.force_polling or None,
                                        ignore_permission_denied=True):
                try:
                    await self.handle(changes)
                except Exception:
                    log.exception("[Watcher] error handling %d change(s)", len(changes))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[Watcher] stopped")

    # ---- one batch of events ---------------------------------------------------------
    async def handle(self, changes) -> None:
        """Reconcile a batch of ``(Change, abspath)`` events."""
        async with self._lock:
            await self._handle(changes)

    async def _handle(self, changes) -> None:
        added: set[str] = set()
        deleted: set[str] = set()
        modified: set[str] = set()
        for change, path in changes:
            rel = self.rel(path)
            if self.ignored(rel):
                continue
            {Change.added: added, Change.deleted: deleted, Change.modified: modified}[change].add(rel)

        # Deleted and re-added in one batch: an editor saving by replacement.
        for rel in added & deleted:
            added.discard(rel)
            deleted.discard(rel)
            modified.add(rel)

        tree_dirty = False

        # Deletions first, so an addition in the same batch can claim the room.
        for rel in sorted(deleted):
            tree_dirty |= self.visible(rel)
            for name in self.rooms._rooms_under(rel):
                self._hold(name)

        new_files: list[str] = []
        for rel in sorted(added):
            target = self.root / rel
            if rel not in self.rooms.rooms:       # a live room's file already is in the tree
                tree_dirty |= self.visible(rel)
            if target.is_dir():
                new_files += await asyncio.to_thread(self._walk, rel)
            elif rel in self.rooms.rooms:
                modified.add(rel)
            else:
                new_files.append(rel)

        for rel in sorted(modified):
            await self._modified(rel)

        renamed = await self._pair(new_files)
        for old, new in renamed:
            await self.hub.broadcast({"type": "file_renamed", "old": old, "new": new})
            tree_dirty = True

        if tree_dirty:
            await self.hub.broadcast({"type": "tree_changed"})
        if self._pending:
            self._schedule_sweep()

    def _walk(self, rel: str) -> list[str]:
        out = []
        base = self.root / rel
        for p in sorted(base.rglob("*")):
            r = self.rel(p)
            if p.is_file() and not self.ignored(r):
                out.append(r)
        return out

    # ---- the cases -------------------------------------------------------------------
    async def _modified(self, rel: str) -> None:
        """A file changed (or reappeared).  Merge it into the live room, if any."""
        pending = self._pending.pop(rel, None)
        room = self.rooms.rooms.get(rel)
        if pending is not None and room is not None:
            room.suspended = False
        if room is None:
            return
        try:
            data = await asyncio.to_thread((self.root / rel).read_bytes)
        except OSError:
            return
        if self.ledger.is_own(rel, data):
            return                      # the echo of our own save
        log.info("[Watcher] %s changed on disk; merging into its room", rel)
        await self.rooms.reload_room(rel)

    def _hold(self, name: str) -> None:
        """The file behind a live room vanished: wait before deciding what happened."""
        room = self.rooms.rooms.get(name)
        if room is None:
            return
        digests = set()
        last = self.ledger.last(name)
        if last:
            digests.add(last)
        try:
            digests.add(DiskLedger.digest(str(room._text).encode("utf-8")))
        except Exception:
            pass
        # Without this the room's debounced save would write the file straight back.
        room.suspended = True
        self._pending[name] = _Pending(time.monotonic() + self.grace, digests)

    async def _pair(self, new_files: list[str]) -> list[tuple[str, str]]:
        """Match vanished rooms with newly appeared files of the same content."""
        if not self._pending or not new_files:
            return []
        digests: dict[str, str] = {}
        for rel in new_files:
            try:
                data = await asyncio.to_thread((self.root / rel).read_bytes)
            except OSError:
                continue
            if data:                    # empty files are all alike: never evidence of a move
                digests[rel] = DiskLedger.digest(data)
        renamed, used = [], set()
        for old in sorted(self._pending):
            cands = [n for n, d in digests.items()
                     if n not in used and d in self._pending[old].digests]
            if not cands:
                continue
            same_name = [n for n in cands if Path(n).name == Path(old).name]
            new = (same_name or cands)[0]
            used.add(new)
            self._pending.pop(old)
            room = self.rooms.rooms.get(old)
            if room is not None:
                room.suspended = False
            await self.rooms.rename_room(old, new)
            log.info("[Watcher] %s moved to %s; room rebound", old, new)
            renamed.append((old, new))
        return renamed

    # ---- grace period ----------------------------------------------------------------------
    def _schedule_sweep(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while self._pending:
            wake = min(p.deadline for p in self._pending.values())
            await asyncio.sleep(max(0.0, wake - time.monotonic()) + 0.02)
            try:
                async with self._lock:
                    await self._sweep()
            except Exception:
                log.exception("[Watcher] sweep failed")

    async def _sweep(self) -> None:
        now = time.monotonic()
        gone = []
        for name, pending in list(self._pending.items()):
            if pending.deadline > now:
                continue
            self._pending.pop(name)
            if (self.root / name).is_file():
                await self._modified_after_hold(name)
                continue
            gone.append(name)
        for name in gone:
            await self.rooms.close_room(name)
            log.info("[Watcher] %s was deleted; room closed", name)
            await self.hub.broadcast({"type": "file_removed", "path": name})
        if gone:
            await self.hub.broadcast({"type": "tree_changed"})

    async def _modified_after_hold(self, name: str) -> None:
        room = self.rooms.rooms.get(name)
        if room is not None:
            room.suspended = False
        await self._modified(name)
