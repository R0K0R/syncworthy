// ============================================================
// SETTINGS MODULE
// The tabs on the Settings page.  Tabs come from the server (/api/app
// settings_pages, declared by plugins) plus the built-in Session and Info.
// A page is rendered by, in order: a renderer a plugin registered with
// Syncworthy.registerSettingsPage(id, ...), a built-in, or the generic
// schema-driven form for pages that declare `fields`.
// ============================================================

(function () {
    const BUILTIN = ['session', 'info'];

    const SettingsMixin = {
        buildSettingsTabs: function () {
            const side = document.querySelector('.config-sidebar');
            if (!side) return;
            const declared = (this.state.appInfo && this.state.appInfo.settings_pages) || [];
            const pages = declared.map(p => ({ id: p.id, title: p.title }));
            const titles = { session: 'Session', info: 'Info' };
            BUILTIN.forEach(id => {
                if (!pages.some(p => p.id === id)) pages.push({ id, title: titles[id] });
            });
            side.innerHTML = '';
            pages.forEach((p, i) => {
                const b = document.createElement('button');
                b.className = 'config-tab' + (i === 0 ? ' active' : '');
                b.dataset.tab = p.id;
                b.textContent = p.title;
                b.onclick = () => this.showConfigTab(p.id);
                side.appendChild(b);
            });
            this._firstSettingsTab = pages.length ? pages[0].id : 'session';
        },

        _settingsSchemaPage: function (id) {
            const declared = (this.state.appInfo && this.state.appInfo.settings_pages) || [];
            return declared.find(p => p.id === id && p.fields && p.fields.length);
        },

        showConfigTab: function (tabId) {
            document.querySelectorAll('.config-tab').forEach(t => t.classList.remove('active'));
            const tabEl = document.querySelector(`.config-tab[data-tab="${tabId}"]`);
            if (tabEl) tabEl.classList.add('active');

            const container = document.getElementById('config-content');
            const custom = window.Syncworthy.registry.settingsPages[tabId];
            if (custom && custom.render) { custom.render.call(this, container); return; }
            const schema = this._settingsSchemaPage(tabId);
            if (schema) { this.renderSchemaPage(schema, container); return; }
            if (tabId === 'session') this.renderSessionTab(container);
            else if (tabId === 'info') this.renderInfoTab(container);
        },

        saveCurrentConfigTab: function () {
            const activeTab = document.querySelector('.config-tab.active');
            if (!activeTab) return;
            const tabId = activeTab.dataset.tab;
            const custom = window.Syncworthy.registry.settingsPages[tabId];
            if (custom && custom.save) { custom.save.call(this); return; }
            const schema = this._settingsSchemaPage(tabId);
            if (schema) this.saveSchemaPage(schema);
        },

        // --- generic schema-driven page -----------------------------------
        renderSchemaPage: async function (page, container) {
            let data = {};
            try { data = await (await fetch('/api/settings/' + encodeURIComponent(page.id))).json(); } catch (e) { /* empty form */ }
            container.innerHTML = '';
            const section = document.createElement('div');
            section.className = 'config-section';
            const h = document.createElement('h3'); h.textContent = page.title; section.appendChild(h);
            if (page.description) { const d = document.createElement('p'); d.textContent = page.description; section.appendChild(d); }
            page.fields.forEach(f => {
                const group = document.createElement('div');
                group.className = 'form-group';
                const label = document.createElement('label');
                label.textContent = f.label || f.key;
                group.appendChild(label);
                const value = data[f.key];
                let input;
                if (f.type === 'textarea') { input = document.createElement('textarea'); input.rows = 6; input.value = value ?? ''; }
                else if (f.type === 'boolean') { input = document.createElement('input'); input.type = 'checkbox'; input.checked = !!value; }
                else if (f.type === 'select') {
                    input = document.createElement('select');
                    (f.options || []).forEach(o => {
                        const opt = document.createElement('option');
                        opt.value = typeof o === 'object' ? o.value : o;
                        opt.textContent = typeof o === 'object' ? (o.label || o.value) : o;
                        input.appendChild(opt);
                    });
                    input.value = value ?? '';
                }
                else if (f.type === 'list') { input = document.createElement('textarea'); input.rows = 4; input.value = (value || []).join('\n'); input.placeholder = 'one per line'; }
                else { input = document.createElement('input'); input.type = f.type === 'number' ? 'number' : 'text'; input.value = value ?? ''; if (f.placeholder) input.placeholder = f.placeholder; }
                input.dataset.key = f.key;
                input.dataset.type = f.type || 'text';
                input.addEventListener('input', () => this.debouncedSaveSchema(page));
                input.addEventListener('change', () => this.debouncedSaveSchema(page));
                group.appendChild(input);
                if (f.help) { const hp = document.createElement('p'); hp.style.cssText = 'font-size:12px;color:var(--text-muted);margin-top:8px;'; hp.textContent = f.help; group.appendChild(hp); }
                section.appendChild(group);
            });
            container.appendChild(section);
        },

        debouncedSaveSchema: function (page) {
            clearTimeout(this._schemaSaveTimer);
            this._schemaSaveTimer = setTimeout(() => this.saveSchemaPage(page), 500);
        },

        saveSchemaPage: async function (page) {
            const data = {};
            document.querySelectorAll('#config-content [data-key]').forEach(el => {
                const t = el.dataset.type;
                if (t === 'boolean') data[el.dataset.key] = el.checked;
                else if (t === 'number') data[el.dataset.key] = el.value === '' ? null : Number(el.value);
                else if (t === 'list') data[el.dataset.key] = el.value.split('\n').map(s => s.trim()).filter(Boolean);
                else data[el.dataset.key] = el.value;
            });
            try {
                await fetch('/api/settings/' + encodeURIComponent(page.id), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data)
                });
                this.showSaveStatus(page.title + ' Saved');
            } catch (e) {
                this.showSaveStatus('Error Saving');
            }
        },

        // --- Session Tab ---
        renderSessionTab: function (container) {
            const themes = this.EDITOR_THEMES;
            const themeOptions = themes
                ? Object.entries(themes).map(([id, theme]) => {
                    const selected = this.state.editorTheme === id ? 'selected' : '';
                    return `<option value="${id}" ${selected}>${theme.label}</option>`;
                }).join('')
                : '<option value="vs-dark">VS Dark</option>';
            container.innerHTML = `
                <div class="config-section">
                    <h3>Session Settings</h3>
                    <p>Configure your appearance in collaboration sessions</p>
                    <div class="form-group">
                        <label>Display Name</label>
                        <input type="text" id="session-name" value="${this.state.sessionName}" oninput="app.updateSessionName(this.value)" placeholder="Anonymous">
                        <p style="font-size:12px;color:var(--text-muted);margin-top:8px;">This name will be visible to other users editing the same file. It is saved in your browser.</p>
                    </div>
                    <div class="form-group">
                        <label>Editor Theme</label>
                        <select id="editor-theme-select" onchange="app.setEditorTheme(this.value)">${themeOptions}</select>
                        <p style="font-size:12px;color:var(--text-muted);margin-top:8px;">Choose a color scheme for the code editor. Your preference is saved in your browser.</p>
                    </div>
                </div>
            `;
        },

        updateSessionName: function (name) {
            this.state.sessionName = name || 'Anonymous';
            localStorage.setItem('sessionName', this.state.sessionName);
            // Presence/identity is handled by docSocket, not Yjs awareness.
            if (this.state.docSocket && this.state.docSocket.readyState === WebSocket.OPEN) {
                this.state.docSocket.send(JSON.stringify({
                    type: 'identity',
                    name: this.state.sessionName,
                }));
            }
            this.showSaveStatus('Name Updated');
        },

        // --- Info Tab (generic; a plugin may replace it) ---
        renderInfoTab: function (container) {
            const b = this.brand();
            const info = b.info || {};
            container.innerHTML = '';
            const wrap = document.createElement('div');
            wrap.className = 'config-section';
            wrap.style.cssText = 'text-align:center;padding:32px 24px;';
            if (b.welcome_logo) {
                const pre = document.createElement('pre');
                pre.style.cssText = "font-family:'JetBrains Mono',monospace;font-size:14px;line-height:1.2;color:var(--accent-primary);margin-bottom:16px;display:inline-block;text-align:left;";
                pre.textContent = b.welcome_logo;
                wrap.appendChild(pre);
            }
            const h = document.createElement('h1');
            h.style.cssText = 'font-family:var(--font-display);font-size:28px;font-weight:700;margin-bottom:4px;';
            h.textContent = b.name;
            wrap.appendChild(h);
            if (info.tagline) {
                const t = document.createElement('p');
                t.style.cssText = 'color:var(--text-muted);margin-bottom:16px;font-size:14px;';
                t.textContent = info.tagline;
                wrap.appendChild(t);
            }
            if (info.version) {
                const v = document.createElement('span');
                v.style.cssText = "background:var(--bg-secondary);border-radius:6px;padding:6px 16px;font-family:'JetBrains Mono',monospace;font-size:13px;";
                v.textContent = 'v' + info.version;
                wrap.appendChild(v);
            }
            container.appendChild(wrap);
            if (info.links && info.links.length) {
                const links = document.createElement('div');
                links.className = 'config-section';
                links.style.cssText = 'display:flex;justify-content:center;gap:24px;padding:16px 24px;flex-wrap:wrap;';
                info.links.forEach(l => {
                    const a = document.createElement('a');
                    a.href = l.url; a.target = '_blank'; a.rel = 'noopener';
                    a.style.cssText = 'color:var(--accent-primary);text-decoration:none;font-size:14px;';
                    a.textContent = l.label;
                    links.appendChild(a);
                });
                container.appendChild(links);
            }
        },
    };

    window._settingsMixin = SettingsMixin;
})();
