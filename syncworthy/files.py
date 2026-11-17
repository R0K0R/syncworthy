"""The project's files: path safety, the tree, and ignore rules."""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

log = logging.getLogger("syncworthy")


def resolve_in_project(root: Path, path: str) -> Path | None:
    """Resolve a user-supplied path, rejecting anything outside ROOT."""
    if not path:
        return None
    target = root / path
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return target


class IgnoreRules:
    """Glob rules for names and relative paths.

    A pattern with no ``/`` is matched against every path component (so
    ``build`` hides a ``build`` directory at any depth, ``*.pdf`` hides PDFs
    anywhere); one with a ``/`` is matched against the whole relative path.  A
    trailing ``/`` is ignored (everything is matched as a name or a path).
    """

    def __init__(self, patterns: list[str]):
        self.patterns = [p.rstrip("/") for p in patterns if p and p.strip("/")]

    def matches(self, rel: str) -> bool:
        parts = Path(rel).parts
        for pat in self.patterns:
            if "/" in pat:
                if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat + "/*"):
                    return True
            elif any(fnmatch.fnmatch(part, pat) for part in parts):
                return True
        return False


def scan_tree(root: Path, ignore: IgnoreRules, *, roots: list[str] | None = None,
              create_roots: bool = False) -> dict:
    """The tree the editor shows.

    ``roots`` restricts the top level to those names (anything else in the
    project directory is hidden); ``create_roots`` makes sure each exists so it
    always shows.  Directories first, then by name.
    """
    root = Path(root)
    if roots and create_roots:
        for name in roots:
            try:
                (root / name).mkdir(exist_ok=True)
            except OSError as e:
                log.warning("could not create %s: %s", name, e)

    def scan(path: Path) -> list[dict]:
        items = []
        try:
            for entry in sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
                rel = str(entry.relative_to(root))
                if ignore.matches(rel):
                    continue
                if roots and path == root and entry.name not in roots:
                    continue
                item = {"name": entry.name, "path": rel, "is_dir": entry.is_dir()}
                if entry.is_dir():
                    item["children"] = scan(entry)
                items.append(item)
        except Exception:
            pass
        return items

    return {"root": root.name, "items": scan(root)}


# Editable text, as far as an agent is concerned: the generic list.  Plugins add to it.
TEXT_SUFFIXES = {
    ".md", ".txt", ".rst", ".tex", ".bib", ".typ", ".json", ".toml", ".yml", ".yaml",
    ".csv", ".el", ".py", ".sh", ".nix", ".js", ".ts", ".css", ".html",
    ".svg", ".xml", ".cfg", ".ini", ".lock", ".org", ".c", ".h", ".cpp", ".rs",
    ".go", ".java", ".lua", ".sql",
}


def list_documents(root: Path, ignore: IgnoreRules, suffixes: set[str],
                   under: str | None = None) -> list[str]:
    """Project-relative paths of editable text files (sorted), under UNDER if given."""
    root = root.resolve()
    start = (root / under).resolve() if under else root
    if start != root and root not in start.parents:
        raise ValueError(f"path outside the project: {under}")
    out = []
    for p in sorted(start.rglob("*")):
        if not p.is_file() or p.suffix not in suffixes:
            continue
        rel = p.relative_to(root).as_posix()
        if ignore.matches(rel):
            continue
        out.append(rel)
    return out
