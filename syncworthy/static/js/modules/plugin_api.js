// ============================================================
// PLUGIN API
//
// What a plugin's JavaScript bundle talks to.  A bundle is loaded from
// /plugins/<name>/<file> after the core modules and before app.init(), and
// calls these (see docs/PLUGINS.md):
//
//   Syncworthy.registerPlugin(name, mixin, {
//       init(app)              // after core init: register pages, debouncers, ...
//       messages: { type: fn } // doc-socket message types the plugin handles
//       fileOpen(path, app)    // after a file's editor/viewer has been set up
//   });
//   Syncworthy.registerSettingsPage(id, { render(container), save() });
//
// `mixin` is Object.assign()ed onto `app`, the same seam the core modules use
// (window._xMixin + Object.assign), so a plugin's methods are `app.method()`.
// ============================================================

(function () {
    const registry = {
        plugins: [],        // {name, mixin, opts}
        settingsPages: {},  // id -> {render, save}
    };

    window.Syncworthy = {
        registry,

        registerPlugin: function (name, mixin, opts) {
            registry.plugins.push({ name, mixin: mixin || {}, opts: opts || {} });
        },

        registerSettingsPage: function (id, page) {
            registry.settingsPages[id] = page;
        },
    };

    // The core app mixes this in (window._pluginApiMixin): page registration and
    // the hook runners app.js calls.
    window._pluginApiMixin = {
        // Add a dock page.  {id, label, icon, html, onShow, viewable}
        // A page is assumed to act (build, save...), which the server refuses a
        // read-only (`view`) token, so it is not offered to one unless the plugin
        // says `viewable: true`.
        registerPage: function (spec) {
            if (this.state.role === 'view' && !spec.viewable) return;
            const main = document.querySelector('.main-content');
            const dock = document.querySelector('.dock');
            if (!main || !dock || document.getElementById('page-' + spec.id)) return;

            const page = document.createElement('div');
            page.className = 'page';
            page.id = 'page-' + spec.id;
            page.innerHTML = spec.html || '';
            main.appendChild(page);

            const item = document.createElement('div');
            item.className = 'dock-item';
            item.dataset.page = spec.id;
            item.onclick = () => this.showPage(spec.id);
            item.innerHTML = '<div class="dock-icon"><i data-lucide="' + (spec.icon || 'box') + '"></i></div>'
                + '<span class="dock-label"></span><span class="dock-dot"></span>';
            item.querySelector('.dock-label').textContent = spec.label || spec.id;
            const chat = document.getElementById('dock-chat-btn');
            dock.insertBefore(item, chat || null);

            this._pageHooks = this._pageHooks || {};
            this._pageHooks[spec.id] = { onShow: spec.onShow };
            if (window.lucide) lucide.createIcons();
        },

        _runPluginHooks: function (kind, ...args) {
            for (const p of window.Syncworthy.registry.plugins) {
                const fn = p.opts[kind];
                if (typeof fn !== 'function') continue;
                try { fn.call(this, ...args, this); } catch (e) { console.error('[plugin ' + p.name + '] ' + kind + ' failed:', e); }
            }
        },

        // true when some plugin handled the message
        _dispatchPluginMessage: function (msg) {
            let handled = false;
            for (const p of window.Syncworthy.registry.plugins) {
                const fn = p.opts.messages && p.opts.messages[msg.type];
                if (typeof fn !== 'function') continue;
                try { fn.call(this, msg); handled = true; } catch (e) { console.error('[plugin ' + p.name + '] message ' + msg.type + ' failed:', e); }
            }
            return handled;
        },
    };
})();
