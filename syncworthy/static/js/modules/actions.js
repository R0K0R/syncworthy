// ============================================================
// ACTIONS MODULE
// Generic page for plugin actions (build-like things with progress).  An
// action marked custom_ui is drawn by the plugin's own JavaScript instead.
//   POST /api/actions/<id>             -> {success, output, download?}
//   doc socket {type:'action_progress', action, completed, total, message, phase}
// ============================================================

(function () {
    const ActionsMixin = {
        setupActions: function () {
            const actions = ((this.state.appInfo && this.state.appInfo.actions) || []).filter(a => !a.custom_ui);
            actions.forEach(a => {
                this.registerPage({
                    id: 'action-' + a.id,
                    label: a.label,
                    icon: a.icon,
                    html: `
                        <section class="config-content glass">
                            <div class="config-section">
                                <h3></h3><p></p>
                            </div>
                            <div class="build-progress" style="display:none;">
                                <div class="progress-bar"><div class="progress-fill" style="width:0%"></div></div>
                                <div class="progress-info"><span class="a-msg">Preparing...</span><span class="a-pct">0%</span></div>
                            </div>
                            <div class="build-footer">
                                <button class="btn btn-primary a-run"><i data-lucide="${a.icon}"></i> Run</button>
                            </div>
                            <div class="build-log glass a-log" style="margin-top:16px;display:none;"></div>
                        </section>`,
                });
                const page = document.getElementById('page-action-' + a.id);
                page.querySelector('h3').textContent = a.label;
                page.querySelector('p').textContent = a.description || '';
                page.querySelector('.a-run').onclick = () => this.runAction(a.id);
            });
        },

        _actionPage: function (id) { return document.getElementById('page-action-' + id); },

        runAction: async function (id, params) {
            const page = this._actionPage(id);
            const btn = page && page.querySelector('.a-run');
            const log = page && page.querySelector('.a-log');
            if (page) {
                page.querySelector('.build-progress').style.display = 'block';
                page.querySelector('.progress-fill').style.width = '0%';
                page.querySelector('.a-msg').textContent = 'Preparing...';
                page.querySelector('.a-pct').textContent = '0%';
                log.style.display = 'none';
                btn.disabled = true;
            }
            try {
                const res = await fetch('/api/actions/' + encodeURIComponent(id), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params || {})
                });
                const result = await res.json();
                if (page) {
                    page.querySelector('.progress-fill').style.width = '100%';
                    page.querySelector('.a-pct').textContent = '100%';
                    page.querySelector('.a-msg').textContent = result.success ? 'Done' : 'Failed';
                    log.style.display = 'block';
                    log.textContent = result.output || (result.success ? 'Done' : 'Failed');
                    log.style.color = result.success ? 'var(--success)' : 'var(--danger)';
                }
                if (result.success && result.download) {
                    const a = document.createElement('a');
                    a.href = result.download; a.download = '';
                    document.body.appendChild(a); a.click(); document.body.removeChild(a);
                }
                return result;
            } catch (e) {
                if (page) { log.style.display = 'block'; log.textContent = e.message || 'Network error'; log.style.color = 'var(--danger)'; }
            } finally {
                if (btn) btn.disabled = false;
            }
        },

        updateActionProgress: function (msg) {
            const page = this._actionPage(msg.action);
            if (!page) return;
            const total = msg.total || 0, completed = msg.completed || 0;
            const pct = total > 0 ? Math.min(100, Math.round(completed / total * 100)) : 0;
            page.querySelector('.progress-fill').style.width = pct + '%';
            page.querySelector('.a-pct').textContent = total > 0 ? pct + '%' : '…';
            if (msg.message) page.querySelector('.a-msg').textContent = msg.message;
            else if (total > 0) page.querySelector('.a-msg').textContent = `${completed}/${total}`;
        },
    };

    window._actionsMixin = ActionsMixin;
})();
