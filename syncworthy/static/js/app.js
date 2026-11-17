/**
 * Syncworthy - Main Application (Core)
 *
 * Module files loaded first (see index.html), in order:
 *   js/modules/utils.js       - Shared helpers
 *   js/modules/plugin_api.js  - window.Syncworthy: what plugin bundles register with
 *   js/modules/languages.js   - Language registry: extension -> Monaco language
 *   js/modules/file_tree.js   - File tree & file CRUD
 *   js/modules/collab.js      - WebSocket, cursors, user presence, diagnostics markers
 *   js/modules/settings.js    - Settings tabs (plugin pages + Session/Info)
 *   js/modules/actions.js     - Generic page for plugin actions
 *   js/modules/chat.js        - Chat panel
 * Plugin bundles (/plugins/<name>/...) are loaded by app.boot() before app.init().
 */

const app = {
    state: {
        activeFile: null,
        editor: null,
        ws: null,
        configData: {},
        editorTheme: localStorage.getItem('editorTheme') || 'vs-dark',
        sessionName: localStorage.getItem('sessionName') || 'Anonymous',

        // Yjs — per-file (recreated on each openFile, content sync only)
        ydoc: null,
        yjsProvider: null,
        yjsBinding: null,

        // Identity (stable across file switches, from localStorage)
        userToken: null,
        userColor: null,

        // Cursor / presence
        userId: null,
        onlineUsers: {},
        remoteCursorDecorations: [],
        externalCursorDecorations: {},
        projectPath: null,

        // From /api/app: branding, plugins, preview/diagnostics capabilities, pages
        appInfo: null,
    },

    // Branding with defaults filled in (the server merges core, plugin and config branding).
    brand: function () {
        return (this.state.appInfo && this.state.appInfo.branding) || { name: 'Syncworthy', info: {}, file_icons: {} };
    },

    // Load /api/app, then each plugin's bundle (scripts in order, styles), then init.
    boot: async function () {
        try {
            this.state.appInfo = await (await fetch('/api/app')).json();
        } catch (e) {
            console.error('Could not load /api/app', e);
            this.state.appInfo = { name: 'Syncworthy', branding: { name: 'Syncworthy', info: {}, file_icons: {} }, plugins: [], settings_pages: [], actions: [] };
        }
        document.title = this.brand().title || this.brand().name;
        // A `view` token is read-only: the editor says so (the server drops its writes regardless).
        this.state.role = (this.state.appInfo.auth && this.state.appInfo.auth.role) || 'edit';
        document.body.classList.toggle('role-view', this.state.role === 'view');
        for (const p of this.state.appInfo.plugins || []) {
            if (!p.base) continue;
            for (const href of p.styles || []) {
                const link = document.createElement('link');
                link.rel = 'stylesheet'; link.href = p.base + href;
                document.head.appendChild(link);
            }
            for (const src of p.scripts || []) {
                await new Promise(resolve => {
                    const el = document.createElement('script');
                    el.src = p.base + src; el.onload = resolve;
                    el.onerror = () => { console.error('Failed to load plugin script', el.src); resolve(); };
                    document.body.appendChild(el);
                });
            }
        }
        await this.init();
    },

    // ============================================================
    // INITIALIZATION
    // ============================================================

    init: async function () {
        // Wire in module mixins
        Object.assign(this, window._utilsMixin || {});
        Object.assign(this, window._pluginApiMixin || {});
        Object.assign(this, window._languagesMixin || {});
        Object.assign(this, window._fileTreeMixin || {});
        Object.assign(this, window._collabMixin || {});
        Object.assign(this, window._settingsMixin || {});
        Object.assign(this, window._actionsMixin || {});
        Object.assign(this, window._chatMixin || {});
        // Plugin bundles, in load order: their methods land on `app` too.
        for (const p of window.Syncworthy.registry.plugins) Object.assign(this, p.mixin);

        // No previewer plugin: no preview panel.
        const hasPreview = !!(this.state.appInfo && this.state.appInfo.preview);
        document.getElementById('page-editor').classList.toggle('no-preview', !hasPreview);
        const phText = document.getElementById('preview-placeholder-text');
        if (phText && hasPreview) phText.textContent = this.state.appInfo.preview.placeholder;

        this.buildSettingsTabs();
        this.setupActions();
        this._runPluginHooks('init');

        await this._initMonaco();

        // Apply saved theme
        if (this.state.editorTheme === this.brand().base_theme) {
            document.body.removeAttribute('data-theme');
        } else {
            document.body.dataset.theme = this.state.editorTheme;
        }
        const themeSelect = document.getElementById('editor-theme-select');
        if (themeSelect) themeSelect.value = this.state.editorTheme;

        // ── Stable identity ───────────────────────────────────────
        // Must be set BEFORE connectDocSocket() so the URL includes the token.
        let token = localStorage.getItem('sw_token') || localStorage.getItem('nw_token');
        if (!token) {
            token = ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, c =>
                (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
            localStorage.setItem('sw_token', token);
        }
        this.state.userToken = token;

        let color = localStorage.getItem('sw_color') || localStorage.getItem('nw_color');
        const validStoredColor = typeof color === 'string'
            && color.trim() !== ''
            && color !== 'undefined'
            && color !== 'null'
            && (!window.CSS || !window.CSS.supports || window.CSS.supports('color', color));
        if (!validStoredColor) {
            color = this.getUserColor(token);
            localStorage.setItem('sw_color', color);
        }
        this.state.userColor = color;
        // ─────────────────────────────────────────────────────────

        this.connectDocSocket();

        this.createWelcomeOverlay();

        const container = document.getElementById('monaco-container');
        if (container) container.style.display = 'none';

        await this.refreshTree();
        await this.loadStatus();

        this.showConfigTab(this._firstSettingsTab || 'session');
        this.initResizer();

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                const activePage = document.querySelector('.page.active');
                if (activePage && activePage.id === 'page-config') {
                    this.saveCurrentConfigTab();
                    this.showSaveStatus('Changes saved');
                }
            }
        });
    },

    // ============================================================
    // MONACO SETUP
    // ============================================================

    EDITOR_THEMES: null,

    // The one Monaco: the vendored ESM build y-monaco binds to, imported by the
    // module script in index.html and published as window.monaco.
    _initMonaco: async function () {
        if (!window.monaco) throw new Error('Monaco did not load (vendor/monaco.mjs)');
        return new Promise((resolve) => {
            {
                this._registerLanguages();
                this._defineEditorThemes();
                monaco.editor.setTheme(this.state.editorTheme);

                this.state.editor = monaco.editor.create(document.getElementById('monaco-container'), {
                    value: '',
                    language: 'markdown',
                    theme: this.state.editorTheme,
                    automaticLayout: true,
                    fontSize: 14,
                    fontFamily: "'JetBrains Mono', 'SF Mono', 'Fira Code', monospace",
                    fontLigatures: true,
                    minimap: { enabled: false },
                    padding: { top: 20, bottom: 20 },
                    scrollBeyondLastLine: false,
                    wordWrap: 'off',
                    lineNumbers: 'on',
                    renderLineHighlight: 'none',
                    overviewRulerLanes: 0,
                    hideCursorInOverviewRuler: true,
                    scrollbar: {
                        vertical: 'auto', horizontal: 'auto',
                        verticalScrollbarSize: 6, horizontalScrollbarSize: 6,
                    },
                    smoothScrolling: true,
                    cursorBlinking: 'smooth',
                    cursorSmoothCaretAnimation: 'on',
                    bracketPairColorization: { enabled: true },
                    guides: { bracketPairs: true },
                    suggest: { showKeywords: true, showSnippets: true },
                });

                // Cursor change -> broadcast position via docSocket (low latency).
                // We do NOT touch Yjs awareness here — MonacoBinding owns that field.
                // Sending via docSocket is async (no Monaco API call), so no recursion risk.
                let _cursorSendTimer = null;
                this.state.editor.onDidChangeCursorSelection((e) => {
                    if (_cursorSendTimer) return; // throttle at 50ms
                    _cursorSendTimer = setTimeout(() => {
                        _cursorSendTimer = null;
                        if (!this.state.docSocket || this.state.docSocket.readyState !== WebSocket.OPEN) return;
                        if (!this.state.activeFile) return;
                        const pos = this.state.editor.getPosition();
                        const sel = this.state.editor.getSelection();
                        if (!pos || !sel) return;
                        this.state.docSocket.send(JSON.stringify({
                            type: 'cursor',
                            file: this.state.activeFile,
                            line: pos.lineNumber,
                            col: pos.column,
                            selStartLine: sel.startLineNumber,
                            selStartCol: sel.startColumn,
                            selEndLine: sel.endLineNumber,
                            selEndCol: sel.endColumn,
                            name: this.state.sessionName,
                            color: this.state.userColor,
                            token: this.state.userToken,
                        }));
                    }, 50);
                });

                // Diagnostics: re-check after edits settle, not on every
                // keystroke — a plugin's checker may shell out to a compiler, so
                // firing it per-keystroke would spam the process.
                let _diagnosticsDebounceTimer = null;
                this.state.editor.onDidChangeModelContent(() => {
                    if (!this.hasDiagnostics(this.state.activeFile)) return;
                    if (_diagnosticsDebounceTimer) clearTimeout(_diagnosticsDebounceTimer);
                    _diagnosticsDebounceTimer = setTimeout(() => {
                        _diagnosticsDebounceTimer = null;
                        this.checkDiagnostics();
                    }, 2000);
                });

                this._setupSmartEditing();
                resolve();
            }
        });
    },

    _setupSmartEditing: function () {
        this.state.editor.onKeyDown((e) => {
            const model = this.state.editor.getModel();
            const pos = this.state.editor.getPosition();
            if (!model || !pos) return;

            if (e.keyCode === monaco.KeyCode.Enter) {
                const line = model.getLineContent(pos.lineNumber);
                const bulletMatch = line.match(/^(\s*)([-*+]|\d+\.)\s/);
                if (bulletMatch) {
                    const lineText = line.trim();
                    if (lineText === '-' || lineText === '*' || lineText === '+' || /^\d+\.$/.test(lineText)) {
                        e.preventDefault();
                        const range = new monaco.Range(pos.lineNumber, 1, pos.lineNumber, line.length + 1);
                        this.state.editor.executeEdits('smart', [{ range, text: '\n' }]);
                        return;
                    }
                    e.preventDefault();
                    const indent = bulletMatch[1];
                    const bullet = bulletMatch[2];
                    let nextBullet = bullet;
                    if (/^\d+$/.test(bullet.replace('.', ''))) nextBullet = (parseInt(bullet) + 1) + '.';
                    const insertText = '\n' + indent + nextBullet + ' ';
                    const range = new monaco.Range(pos.lineNumber, pos.column, pos.lineNumber, pos.column);
                    this.state.editor.executeEdits('smart', [{ range, text: insertText }]);
                    this.state.editor.setPosition({ lineNumber: pos.lineNumber + 1, column: indent.length + nextBullet.length + 2 });
                }
            }

            if (e.keyCode === monaco.KeyCode.Tab) {
                const line = model.getLineContent(pos.lineNumber);
                if (/^\s*([-*+]|\d+\.)\s/.test(line)) {
                    e.preventDefault();
                    if (e.shiftKey) {
                        if (line.startsWith('  ')) {
                            const range = new monaco.Range(pos.lineNumber, 1, pos.lineNumber, 3);
                            this.state.editor.executeEdits('smart', [{ range, text: '' }]);
                        }
                    } else {
                        const range = new monaco.Range(pos.lineNumber, 1, pos.lineNumber, 1);
                        this.state.editor.executeEdits('smart', [{ range, text: '  ' }]);
                        this.state.editor.setPosition({ lineNumber: pos.lineNumber, column: pos.column + 2 });
                    }
                }
            }
        });
    },

    // Core themes, plus whatever the branding brings.  Without branded themes the
    // built-in Syncworthy pair is offered.  Branded ones come first.
    _defineEditorThemes: function () {
        const brandThemes = this.brand().themes;
        const fallback = {
            'syncworthy-dark': {
                label: 'Syncworthy Dark',
                base: 'vs-dark',
                colors: {
                    'editor.background': '#0d0d0f',
                    'editor.foreground': '#e8e8f0',
                    'editor.lineHighlightBackground': '#16161a',
                    'editor.selectionBackground': '#4ECDC430',
                    'editor.inactiveSelectionBackground': '#4ECDC415',
                    'editorCursor.foreground': '#4ECDC4',
                    'editorLineNumber.foreground': '#3a3a4a',
                    'editorLineNumber.activeForeground': '#4ECDC4',
                    'editor.findMatchBackground': '#FFE66D30',
                    'editor.findMatchHighlightBackground': '#FFE66D15',
                    'scrollbar.shadow': '#00000000',
                    'scrollbarSlider.background': '#4ECDC420',
                    'scrollbarSlider.hoverBackground': '#4ECDC440',
                    'scrollbarSlider.activeBackground': '#4ECDC460',
                }
            },
            'syncworthy-light': {
                label: 'Syncworthy Light',
                base: 'vs',
                colors: {
                    'editor.background': '#f5f5f7',
                    'editor.foreground': '#1d1d1f',
                    'editor.lineHighlightBackground': '#e8e8ec',
                    'editor.selectionBackground': '#4ECDC440',
                    'editorCursor.foreground': '#0066cc',
                    'editorLineNumber.foreground': '#999aab',
                    'editorLineNumber.activeForeground': '#0066cc',
                }
            },
        };
        this.EDITOR_THEMES = Object.assign({}, brandThemes || fallback, {
            'vs-dark': { label: 'VS Dark', base: 'vs-dark', colors: {} },
            'vs': { label: 'VS Light', base: 'vs', colors: {} },
            'hc-black': { label: 'High Contrast', base: 'hc-black', colors: {} },
        });

        Object.entries(this.EDITOR_THEMES).forEach(([id, theme]) => {
            if (id !== 'vs-dark' && id !== 'vs' && id !== 'hc-black') {
                monaco.editor.defineTheme(id, {
                    base: theme.base,
                    inherit: true,
                    rules: [],
                    colors: theme.colors,
                });
            }
        });
    },

    // ============================================================
    // NAVIGATION
    // ============================================================

    showPage: function (pageId) {
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        document.getElementById('page-' + pageId).classList.add('active');
        document.querySelectorAll('.dock-item').forEach(item => {
            item.classList.toggle('active', item.dataset.page === pageId);
        });
        if (window.lucide) lucide.createIcons();
        const hook = this._pageHooks && this._pageHooks[pageId];
        if (hook && hook.onShow) hook.onShow.call(this);
    },

    setEditorTheme: function (theme) {
        this.state.editorTheme = theme;
        localStorage.setItem('editorTheme', theme);
        if (theme === this.brand().base_theme) {
            document.body.removeAttribute('data-theme');
        } else {
            document.body.dataset.theme = theme;
        }
        if (this.state.editor && window.monaco) window.monaco.editor.setTheme(theme);
        this.showSaveStatus('Theme Updated');
    },

    // ============================================================
    // FILE OPEN & YJS LIFECYCLE
    // ============================================================

    openFile: async function (path, el) {
        if (this.state.activeFile === path) {
            document.querySelectorAll('.tree-item').forEach(e => e.classList.remove('selected'));
            if (el) el.classList.add('selected');
            return;
        }

        const overlay = document.getElementById('welcome-overlay');
        if (overlay) overlay.remove();

        document.querySelectorAll('.tree-item').forEach(e => e.classList.remove('selected'));
        if (el) el.classList.add('selected');

        this.state.activeFile = path;
        document.getElementById('active-filename').textContent = path;

        const monacoContainer = document.getElementById('monaco-container');
        const previewContainer = document.getElementById('preview-container');
        const ext = path.split('.').pop().toLowerCase();
        const imageExts = ['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico'];

        if (ext === 'pdf') {
            monacoContainer.style.display = 'none';
            let pdfViewer = document.getElementById('pdf-viewer');
            if (!pdfViewer) {
                pdfViewer = document.createElement('div');
                pdfViewer.id = 'pdf-viewer';
                pdfViewer.style.cssText = 'flex:1;width:100%;height:100%;background:#1e1e1e;border-radius:0 0 20px 20px;overflow:hidden;';
                monacoContainer.parentNode.insertBefore(pdfViewer, monacoContainer.nextSibling);
            }
            pdfViewer.style.display = 'block';
            pdfViewer.innerHTML = '<iframe src="/api/file?path=' + encodeURIComponent(path) + '&raw=1" style="width:100%;height:100%;border:none;"></iframe>';
            previewContainer.innerHTML = this.previewPlaceholderHtml('file-text');
            if (window.lucide) lucide.createIcons();
            return;

        } else if (imageExts.includes(ext)) {
            monacoContainer.style.display = 'none';
            let imageViewer = document.getElementById('image-viewer');
            if (!imageViewer) {
                imageViewer = document.createElement('div');
                imageViewer.id = 'image-viewer';
                imageViewer.style.cssText = 'flex:1;width:100%;height:100%;background:#0a0a0a;border-radius:0 0 20px 20px;overflow:auto;display:flex;align-items:center;justify-content:center;padding:20px;';
                monacoContainer.parentNode.insertBefore(imageViewer, monacoContainer.nextSibling);
            }
            imageViewer.style.display = 'flex';
            imageViewer.innerHTML = '<img src="/api/file?path=' + encodeURIComponent(path) + '&raw=1" style="max-width:100%;max-height:100%;object-fit:contain;border-radius:8px;box-shadow:0 4px 20px rgba(0,0,0,0.3);">';
            const pdfViewer = document.getElementById('pdf-viewer');
            if (pdfViewer) pdfViewer.style.display = 'none';
            previewContainer.innerHTML = this.previewPlaceholderHtml('image');
            if (window.lucide) lucide.createIcons();
            return;

        } else {
            monacoContainer.style.display = 'block';
            if (this.state.editor) this.state.editor.layout();
            const pdfViewer = document.getElementById('pdf-viewer');
            const imageViewer = document.getElementById('image-viewer');
            if (pdfViewer) pdfViewer.style.display = 'none';
            if (imageViewer) imageViewer.style.display = 'none';

            if (this.hasPreview(path)) {
                previewContainer.innerHTML = '<div class="preview-loading"><div class="skeleton-page"></div><div class="skeleton-page"></div></div>';
            } else {
                previewContainer.innerHTML = this.previewPlaceholderHtml('file-code');
                if (window.lucide) lucide.createIcons();
            }
        }

        // ---- Yjs Provider Lifecycle ----
        if (this.state.yjsBinding) {
            this.state.yjsBinding.destroy();
            this.state.yjsBinding = null;
        }
        if (this.state.yjsProvider) {
            this.state.yjsProvider.destroy();
            this.state.yjsProvider = null;
        }
        if (this.state.ydoc) {
            this.state.ydoc.destroy();
        }

        this.state.externalCursorDecorations = {};
        this.state.remoteCursorDecorations = [];
        this.clearAllRemoteCursors();

        if (this.state.editor) {
            this.state.editor.setValue('');
            this.state.editor.updateOptions({ readOnly: true });
            this.setEditorLanguage(path);
        }

        this.state.ydoc = new Y.Doc();
        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = wsProtocol + '//' + window.location.host + '/yjs';

        console.log('[Yjs] Connecting to ' + wsUrl + ', room: ' + path);
        this.state.yjsProvider = new WebsocketProvider(
            wsUrl, path, this.state.ydoc,
            { maxBackoffTime: 2500, disableBc: true }
        );

        this.state.yjsProvider.on('status', event => {
            console.log('[Yjs] Status: ' + event.status);
            document.getElementById('save-status').textContent = event.status === 'connected' ? 'Live' : 'Offline';
            if (event.status === 'connected' && this.state.editor) {
                this.state.editor.updateOptions({ readOnly: this.isReadOnlyRole() });
            }

            // Hook the underlying WebSocket for raw packet debugging.
            // y-websocket recreates provider.ws on each (re)connect.
            if (event.status === 'connected' && (window.__SYNCWORTHY_DEBUG_PACKETS || window.__NOTEWORTHY_DEBUG_PACKETS)) {
                const ws = this.state.yjsProvider.ws;
                if (ws && !ws.__debugPatched) {
                    ws.__debugPatched = true;
                    const room = path;
                    // Python-style b'...' repr: printable ASCII as chars, rest as \xNN
                    const bytesRepr = buf => {
                        let s = "b'";
                        for (const b of buf) {
                            if (b >= 0x20 && b < 0x7f && b !== 0x27 && b !== 0x5c) {
                                s += String.fromCharCode(b);
                            } else {
                                s += '\\x' + b.toString(16).padStart(2, '0');
                            }
                        }
                        return s + "'";
                    };
                    const msgType = t => ['Sync', 'Awareness', 'Auth', 'QueryAwareness'][t] ?? `Type=${t}`;
                    const origOnMessage = ws.onmessage;
                    ws.onmessage = function (e) {
                        if (e.data instanceof ArrayBuffer) {
                            const buf = new Uint8Array(e.data);
                            console.log(`[Yjs] <<< RECV ${room} (${buf.length}b) [${msgType(buf[0])}]`);
                            console.log(bytesRepr(buf));
                        }
                        if (origOnMessage) origOnMessage.call(this, e);
                    };
                    const origSend = ws.send.bind(ws);
                    ws.send = function (data) {
                        if (data instanceof Uint8Array || data instanceof ArrayBuffer) {
                            const buf = data instanceof Uint8Array ? data : new Uint8Array(data);
                            console.log(`[Yjs] >>> SEND ${room} (${buf.length}b) [${msgType(buf[0])}]`);
                            console.log(bytesRepr(buf));
                        }
                        origSend(data);
                    };
                }
            }
        });

        this.state.yjsProvider.on('sync', isSynced => {
            console.log('[Yjs] Sync: ' + isSynced);
            if (isSynced && this.state.editor) {
                this.state.editor.updateOptions({ readOnly: this.isReadOnlyRole() });
                document.getElementById('save-status').textContent = this.isReadOnlyRole() ? 'Synced (read-only)' : 'Synced';

                // Render existing peers immediately — 'change' only fires for
                // incremental deltas, not the initial awareness snapshot.
                if (this._scheduleRenderOnlineUsers) this._scheduleRenderOnlineUsers();
                else requestAnimationFrame(() => this.renderOnlineUsers());

                // Focus the editor so MonacoBinding publishes our cursor
                // position to awareness immediately (it tracks cursor via
                // onDidChangeCursorSelection internally).
                requestAnimationFrame(() => this.state.editor.focus());
            }
        });

        // Yjs per-file awareness is still passed to MonacoBinding for its internal
        // content-binding mechanics, but we no longer read or write user identity to it.

        // MonacoBinding
        const ytext = this.state.ydoc.getText('content');
        if (this.state.editor) {
            try {
                this.state.yjsBinding = new MonacoBinding(
                    ytext,
                    this.state.editor.getModel(),
                    new Set([this.state.editor]),
                    this.state.yjsProvider.awareness
                );
                console.log('[Yjs] MonacoBinding created');
                // Render right away — some peers may already be in the
                // awareness map even before the first 'change' event fires.
                if (this._scheduleRenderOnlineUsers) this._scheduleRenderOnlineUsers();
                else requestAnimationFrame(() => this.renderOnlineUsers());
            } catch (e) {
                console.error('[Yjs] Error creating MonacoBinding:', e);
            }
        }

        this.joinFile(path);
        this._runPluginHooks('fileOpen', path);
    },

    isReadOnlyRole: function () {
        return this.state.role === 'view';
    },

    // Does a previewer plugin render this file?
    hasPreview: function (path) {
        const pv = this.state.appInfo && this.state.appInfo.preview;
        return !!(pv && path && pv.extensions.some(e => path.toLowerCase().endsWith(e)));
    },

    previewPlaceholderHtml: function (icon) {
        const pv = this.state.appInfo && this.state.appInfo.preview;
        const text = pv ? pv.placeholder_selected : 'No preview';
        const span = document.createElement('span');
        span.textContent = text;
        return '<div class="preview-placeholder"><i data-lucide="' + icon + '"></i>' + span.outerHTML + '</div>';
    },

    hasDiagnostics: function (path) {
        const dg = this.state.appInfo && this.state.appInfo.diagnostics;
        return !!(dg && path && dg.extensions.some(e => path.toLowerCase().endsWith(e)));
    },

    joinFile: function (path) {
        if (this.state.docSocket && this.state.docSocket.readyState === WebSocket.OPEN) {
            this.state.docSocket.send(JSON.stringify({ type: 'join', path }));
        }
    },

    // ============================================================
    // EDITOR HELPERS
    // ============================================================

    // NOTE: no manual save — the Yjs room persists every edit to disk
    // server-side. Posting raw editor text to /api/file here would let a
    // stale tab overwrite collaborators' work.

    toggleErrorDetails: function () {
        const detailsEl = document.getElementById('error-details');
        const chevron = document.querySelector('.error-chevron');
        if (detailsEl) {
            const isVisible = detailsEl.style.display === 'block';
            detailsEl.style.display = isVisible ? 'none' : 'block';
            if (chevron) chevron.style.transform = isVisible ? 'rotate(0deg)' : 'rotate(180deg)';
        }
    },

    checkDiagnostics: async function () {
        if (!this.state.editor || !this.hasDiagnostics(this.state.activeFile)) return;
        try {
            const res = await fetch('/api/check', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: this.state.activeFile })
            });
            const data = await res.json();
            this.applyDiagnostics(data.diagnostics || []);
        } catch (e) {
            console.error('Diagnostic error:', e);
        }
    },

    startSyncVerification: function () { /* placeholder for drift detection */ },

    // ============================================================
    // WELCOME OVERLAY
    // ============================================================

    createWelcomeOverlay: function () {
        const overlay = document.createElement('div');
        overlay.id = 'welcome-overlay';
        const logo = document.createElement('div');
        logo.className = 'ascii-logo';
        logo.textContent = this.brand().welcome_logo || '';
        const text = document.createElement('div');
        text.className = 'welcome-text';
        text.textContent = this.brand().welcome_text;
        if (logo.textContent) overlay.appendChild(logo);
        overlay.appendChild(text);
        const container = document.getElementById('monaco-container');
        if (container && container.parentElement) container.parentElement.appendChild(overlay);
    },

    // ============================================================
    // STATUS
    // ============================================================

    loadStatus: async function () {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();
            this.state.projectPath = data.path || null;
            const el = document.getElementById('project-name');
            if (el) el.textContent = data.project;
        } catch (e) {
            console.error('Status load failed', e);
        }
    },

    // ============================================================
    // RESIZER
    // ============================================================

    initResizer: function () {
        const resizer = document.getElementById('editor-resizer');
        const previewPanel = document.querySelector('.preview-panel');
        const mainContent = document.querySelector('.main-content');
        if (!resizer || !previewPanel || !mainContent) return;

        let isResizing = false;

        resizer.addEventListener('mousedown', () => {
            isResizing = true;
            resizer.classList.add('active');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
        });

        document.addEventListener('mousemove', (e) => {
            if (!isResizing) return;
            const containerRect = mainContent.getBoundingClientRect();
            let newWidth = containerRect.right - e.clientX;
            const minWidth = 200, maxWidth = containerRect.width - 200;
            if (newWidth < minWidth) newWidth = minWidth;
            if (newWidth > maxWidth) newWidth = maxWidth;
            previewPanel.style.width = newWidth + 'px';
            if (this.state.editor) this.state.editor.layout();
        });

        document.addEventListener('mouseup', () => {
            if (isResizing) {
                isResizing = false;
                resizer.classList.remove('active');
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                if (this.state.editor) this.state.editor.layout();
            }
        });
    },
};

window.onload = () => app.boot();
window.app = app;
