"""The frontend loads: every module the page imports is served, and there is one Monaco.

(A browser is not available to the tests; the real rendering is checked by hand.  This
catches what breaks without one: a missing vendored file, a wrong relative import, a
second Monaco creeping back in.)
"""
import re
from pathlib import Path
from urllib.parse import urljoin

from helpers import http, run_app
from syncworthy import create_app
from syncworthy.config import Config

STATIC = Path(__file__).parent.parent / "syncworthy" / "static"
IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)["'](\.{1,2}/[^"']+)["']""")


def _page_resources(html):
    return (re.findall(r'<script[^>]+src="([^"]+)"', html) + re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', html))


def test_index_has_no_second_monaco_and_one_big_bundle():
    html = (STATIC / "index.html").read_text()
    assert "cdnjs" not in html and "loader.min.js" not in html and "require.config" not in (STATIC / "js/app.js").read_text()
    assert "./vendor/monaco.mjs" in html
    big = [p.name for p in (STATIC / "vendor").rglob("*.mjs") if p.stat().st_size > 1_000_000]
    assert big == ["esm.sh_monaco-editor_0.56.0_es2022_esm_vs_editor_editor.main.mjs"], big


def test_every_module_the_page_imports_is_served(tmp_path):
    app = create_app(tmp_path, Config(), [])
    with run_app(app) as port:
        html = http(port, "GET", "/").text
        todo = ["/" + r.split("?")[0] for r in _page_resources(html) if not r.startswith(("http", "//"))]
        todo += ["/vendor/yjs.mjs", "/vendor/y-websocket.mjs", "/vendor/y-monaco.mjs", "/vendor/monaco.mjs",
                 "/vendor/monaco-editor.worker.mjs"]       # the inline module script and the worker
        seen, bad = set(), []
        while todo:
            url = todo.pop()
            if url in seen:
                continue
            seen.add(url)
            r = http(port, "GET", url)
            if r.status_code != 200:
                bad.append((url, r.status_code))
                continue
            if url.endswith(".mjs"):
                for ref in IMPORT.findall(r.text):
                    todo.append(urljoin(url, ref))
        assert not bad, bad
        assert any(u.endswith("python/python.mjs") for u in seen)        # lazily loaded languages included
        assert sum(1 for u in seen if "editor.main" in u) == 1
