// ============================================================
// LANGUAGE REGISTRY
//
// File name -> Monaco language id.  Three layers, later ones win:
//   1. Monaco's own languages (their registered extensions and file names);
//   2. the ones this file adds: LaTeX (+ .sty/.cls) and TOML, which Monaco lacks;
//   3. plugins, in two ways: the server's Plugin.languages() data (served in
//      /api/app as {id, extensions, aliases, monarch, configuration}), and
//      Syncworthy.registerLanguage({...}) from a plugin's script, which may pass
//      real RegExp objects in its Monarch grammar.
//
// Unknown extensions open as plain text.
// ============================================================

(function () {
    // ---- LaTeX ---------------------------------------------------------------
    const MATH_ENVS = 'equation|align|alignat|flalign|gather|multline|eqnarray|displaymath|math|split|aligned|gathered|cases|array|[pbBvV]?matrix|subequations';
    const VERBATIM_LIST = ['verbatim', 'lstlisting', 'minted', 'Verbatim', 'comment'];
    const latexMath = [
        [/%.*$/, 'comment'],
        [/\\\\/, 'keyword.control'],                                   // line break
        [/\\(?:left|right|big|Big|bigg|Bigg)[lr]?(?![a-zA-Z])/, 'keyword'],
        [/\\(?:text|textrm|textbf|textit|mathrm|mathbf|mathit|mathcal|mathbb|mathfrak|mathsf|mathtt|operatorname|mbox|hbox)(?![a-zA-Z])/, 'keyword'],
        [/\\[a-zA-Z@]+\*?/, 'tag'],                                    // \alpha, \frac, ...
        [/\\[^a-zA-Z]/, 'string.escape'],                              // \, \; \{ ...
        [/[{}]/, 'delimiter.curly'],
        [/[()[\]]/, 'delimiter.bracket'],
        [/\d+(?:\.\d+)?/, 'number'],
        [/[&]/, 'keyword.control'],
        [/[=+\-*/<>^_|!]/, 'operator'],
    ];

    const LATEX = {
        defaultToken: '',
        tokenPostfix: '.latex',
        tokenizer: {
            root: [
                [/%.*$/, 'comment'],
                // verbatim-like environments: opaque to their \end (one state each, see below)
                ...VERBATIM_LIST.map(env => [
                    new RegExp('\\\\begin(?=\\s*\\{' + env + '\\*?\\})'), { token: 'keyword.control', next: '@verbBegin_' + env }]),
                [/\\verb\*?(?![a-zA-Z])(\S)[^\n]*?\1/, 'string'],
                // math environments
                [new RegExp('\\\\begin(?=\\s*\\{(?:' + MATH_ENVS + ')\\*?\\})'), { token: 'keyword.control', next: '@beginMath' }],
                [/\\(?:begin|end)(?=\s*\{)/, { token: 'keyword.control', next: '@envName' }],
                // sectioning and the commands that name things
                [/\\(?:part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?(?![a-zA-Z])/, 'keyword.section'],
                [/\\(?:documentclass|usepackage|RequirePackage|input|include|includegraphics|bibliography|bibliographystyle|newcommand|renewcommand|providecommand|newenvironment|renewenvironment|DeclareMathOperator|def|let|newtheorem|setlength|title|author|date|maketitle|tableofcontents|item|caption|footnote|centering|hline|noindent|newpage|clearpage|vspace|hspace)(?![a-zA-Z@])/, 'keyword'],
                [/\\(?:label|ref|eqref|autoref|cref|Cref|pageref|nameref|cite[a-zA-Z]*|footcite)(?![a-zA-Z])/, { token: 'keyword', next: '@refArg' }],
                [/\\[a-zA-Z@]+\*?/, 'keyword'],
                [/\\[^a-zA-Z]/, 'string.escape'],
                // math delimiters
                [/\$\$/, { token: 'string.math', next: '@displayMath' }],
                [/\$/, { token: 'string.math', next: '@inlineMath' }],
                [/\\\(/, { token: 'string.math', next: '@parenMath' }],
                [/\\\[/, { token: 'string.math', next: '@bracketMath' }],
                [/[{}]/, 'delimiter.curly'],
                [/[[\]]/, 'delimiter.square'],
                [/[&~]/, 'keyword.control'],
                [/#\d/, 'variable.parameter'],
            ],
            envName: [
                [/\s*\{/, 'delimiter.curly'],
                [/[A-Za-z@*]+/, 'type'],
                [/\}/, { token: 'delimiter.curly', next: '@pop' }],
            ],
            refArg: [
                [/\s*(?:\[[^\]]*\])?\s*\{/, 'delimiter.curly'],
                [/[^}]+/, 'string.link'],
                [/\}/, { token: 'delimiter.curly', next: '@pop' }],
            ],
            beginMath: [
                [/\s*\{/, 'delimiter.curly'],
                [/[A-Za-z]+\*?/, 'type'],
                [/\}/, { token: 'delimiter.curly', switchTo: '@mathEnv' }],
            ],
            mathEnv: [
                [new RegExp('\\\\end(?=\\s*\\{(?:' + MATH_ENVS + ')\\*?\\})'), { token: 'keyword.control', switchTo: '@endMath' }],
                ...latexMath,
            ],
            endMath: [
                [/\s*\{/, 'delimiter.curly'],
                [/[A-Za-z]+\*?/, 'type'],
                [/\}/, { token: 'delimiter.curly', next: '@pop' }],
            ],
            inlineMath: [[/\$/, { token: 'string.math', next: '@pop' }], ...latexMath],
            displayMath: [[/\$\$/, { token: 'string.math', next: '@pop' }], ...latexMath],
            parenMath: [[/\\\)/, { token: 'string.math', next: '@pop' }], ...latexMath],
            bracketMath: [[/\\\]/, { token: 'string.math', next: '@pop' }], ...latexMath],
        },
    };
    // Per verbatim environment: \begin{env} ... the body ... \end{env}.
    for (const env of VERBATIM_LIST) {
        LATEX.tokenizer['verbBegin_' + env] = [
            [/\s*\{/, 'delimiter.curly'],
            [/[A-Za-z]+\*?/, 'type'],
            [/\}/, { token: 'delimiter.curly', switchTo: '@verbBody_' + env }],
        ];
        LATEX.tokenizer['verbBody_' + env] = [
            [new RegExp('\\\\end(?=\\s*\\{' + env + '\\*?\\})'), { token: 'keyword.control', switchTo: '@endMath' }],
            [/[^\\]+/, 'string'],
            [/\\/, 'string'],
        ];
    }

    const LATEX_CONFIG = {
        comments: { lineComment: '%' },
        brackets: [['{', '}'], ['[', ']'], ['(', ')']],
        autoClosingPairs: [
            { open: '{', close: '}' }, { open: '[', close: ']' }, { open: '(', close: ')' },
            { open: '`', close: "'", notIn: ['comment'] }, { open: '$', close: '$', notIn: ['comment'] },
        ],
        surroundingPairs: [
            { open: '{', close: '}' }, { open: '[', close: ']' }, { open: '(', close: ')' },
            { open: '$', close: '$' }, { open: '`', close: "'" },
        ],
        folding: { markers: { start: /^\s*\\begin\{/, end: /^\s*\\end\{/ } },
        wordPattern: /\\?[a-zA-Z@]+\*?|-?\d*\.\d\w*|[^\s`~!@#%^&*()\-=+[{\]}\\|;:'",.<>/?$]+/,
    };

    // ---- TOML (Monaco has none; syncworthy.toml is a file people open here) -----
    const TOML = {
        defaultToken: '',
        tokenPostfix: '.toml',
        tokenizer: {
            root: [
                [/#.*$/, 'comment'],
                [/^\s*\[\[?[^\]\n]*\]\]?/, 'type'],
                [/^\s*[A-Za-z0-9_\-"'.]+(?=\s*=)/, 'variable'],
                [/"""/, { token: 'string', next: '@mlBasic' }],
                [/'''/, { token: 'string', next: '@mlLiteral' }],
                [/"/, { token: 'string', next: '@basic' }],
                [/'[^'\n]*'/, 'string'],
                [/\b(?:true|false)\b/, 'keyword'],
                [/[+-]?(?:inf|nan)\b/, 'number'],
                [/\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})?)?/, 'number.date'],
                [/[+-]?0x[0-9a-fA-F_]+|[+-]?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?/, 'number'],
                [/[{}[\]]/, '@brackets'],
                [/[=,]/, 'delimiter'],
            ],
            basic: [[/[^\\"]+/, 'string'], [/\\./, 'string.escape'], [/"/, { token: 'string', next: '@pop' }]],
            mlBasic: [[/[^\\"]+/, 'string'], [/\\./, 'string.escape'], [/"""/, { token: 'string', next: '@pop' }], [/"/, 'string']],
            mlLiteral: [[/[^']+/, 'string'], [/'''/, { token: 'string', next: '@pop' }], [/'/, 'string']],
        },
    };
    const TOML_CONFIG = {
        comments: { lineComment: '#' },
        brackets: [['{', '}'], ['[', ']']],
        autoClosingPairs: [{ open: '{', close: '}' }, { open: '[', close: ']' },
                           { open: '"', close: '"', notIn: ['string'] }, { open: "'", close: "'", notIn: ['string'] }],
    };

    // ---- JSON (its stock tokenizer lives in a language-service mode that is not shipped) ----
    const JSON_LANG = {
        defaultToken: '',
        tokenPostfix: '.json',
        tokenizer: {
            root: [
                [/\/\/.*$/, 'comment'],
                [/\/\*/, { token: 'comment', next: '@comment' }],
                [/"(?:[^"\\]|\\.)*"(?=\s*:)/, 'type'],                  // keys
                [/"/, { token: 'string', next: '@string' }],
                [/-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/, 'number'],
                [/\b(?:true|false|null)\b/, 'keyword'],
                [/[{}[\]]/, '@brackets'],
                [/[,:]/, 'delimiter'],
            ],
            string: [[/[^\\"]+/, 'string'], [/\\./, 'string.escape'], [/"/, { token: 'string', next: '@pop' }]],
            comment: [[/[^/*]+/, 'comment'], [/\*\//, { token: 'comment', next: '@pop' }], [/[/*]/, 'comment']],
        },
    };
    const JSON_CONFIG = {
        comments: { lineComment: '//', blockComment: ['/*', '*/'] },
        brackets: [['{', '}'], ['[', ']']],
        autoClosingPairs: [{ open: '{', close: '}' }, { open: '[', close: ']' }, { open: '"', close: '"', notIn: ['string'] }],
    };

    const BUILTIN = [
        { id: 'json', extensions: ['.json', '.jsonc', '.json5', '.webmanifest', '.ipynb'], aliases: ['JSON', 'json'],
          monarch: JSON_LANG, configuration: JSON_CONFIG },
        { id: 'latex', extensions: ['.tex', '.sty', '.cls', '.ltx', '.dtx'], aliases: ['LaTeX', 'latex', 'tex'],
          monarch: LATEX, configuration: LATEX_CONFIG },
        { id: 'toml', extensions: ['.toml'], aliases: ['TOML'], filenames: ['Cargo.lock', 'poetry.lock'],
          monarch: TOML, configuration: TOML_CONFIG },
    ];

    // ---- the registry ------------------------------------------------------------
    const registry = { byExt: {}, byName: {}, ids: new Set(), specs: [] };

    function claim(spec) {
        for (const e of spec.extensions || []) registry.byExt[e.toLowerCase().startsWith('.') ? e.toLowerCase() : '.' + e.toLowerCase()] = spec.id;
        for (const f of spec.filenames || []) registry.byName[f.toLowerCase()] = spec.id;
    }

    // Languages the server sends are JSON: a regex is a string in them.  Monarch
    // takes strings as regex sources, but a configuration's patterns need RegExp.
    function reviveConfiguration(cfg) {
        if (!cfg) return cfg;
        const out = Object.assign({}, cfg);
        if (typeof out.wordPattern === 'string') out.wordPattern = new RegExp(out.wordPattern);
        if (out.folding && out.folding.markers) {
            const m = out.folding.markers;
            out.folding = { markers: { start: new RegExp(m.start), end: new RegExp(m.end) } };
        }
        return out;
    }

    function register(monaco, spec) {
        const known = monaco.languages.getLanguages().some(l => l.id === spec.id);
        if (!known) {
            monaco.languages.register({ id: spec.id, extensions: spec.extensions, aliases: spec.aliases, filenames: spec.filenames });
        }
        if (spec.monarch) {
            try { monaco.languages.setMonarchTokensProvider(spec.id, spec.monarch); }
            catch (e) { console.error('[languages] bad grammar for ' + spec.id, e); }
        }
        if (spec.configuration) {
            try { monaco.languages.setLanguageConfiguration(spec.id, reviveConfiguration(spec.configuration)); }
            catch (e) { console.error('[languages] bad configuration for ' + spec.id, e); }
        }
        claim(spec);
        registry.ids.add(spec.id);
    }

    // Plugin scripts can call this (even before Monaco is up: it is applied then).
    window.Syncworthy = window.Syncworthy || {};
    window.Syncworthy.registerLanguage = function (spec) {
        registry.specs.push(spec);
        if (window.monaco && registry.ready) register(window.monaco, spec);
    };
    window.Syncworthy.languages = registry;

    window._languagesMixin = {
        // Called once Monaco exists.
        _registerLanguages: function () {
            const monaco = window.monaco;
            for (const lang of monaco.languages.getLanguages()) {
                claim({ id: lang.id, extensions: lang.extensions, filenames: lang.filenames });
                registry.ids.add(lang.id);
            }
            for (const spec of BUILTIN) register(monaco, spec);
            for (const spec of (this.state.appInfo && this.state.appInfo.languages) || []) register(monaco, spec);
            registry.ready = true;
            for (const spec of registry.specs) register(monaco, spec);     // from plugin scripts
        },

        // Monaco language id for a project path.
        languageFor: function (path) {
            const name = String(path || '').split('/').pop().toLowerCase();
            if (registry.byName[name]) return registry.byName[name];
            // longest extension first: a.d.ts -> .d.ts, then .ts
            for (let i = name.indexOf('.'); i >= 0; i = name.indexOf('.', i + 1)) {
                const id = registry.byExt[name.slice(i)];
                if (id) return id;
            }
            return 'plaintext';
        },

        setEditorLanguage: function (path) {
            const model = this.state.editor && this.state.editor.getModel();
            if (!model) return;
            const id = this.languageFor(path);
            if (model.getLanguageId() !== id) window.monaco.editor.setModelLanguage(model, id);
        },
    };
})();
