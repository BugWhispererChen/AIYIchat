/* 聊天服务页 · 角色分区：列表、编辑、启停与删除。
   逻辑从原 /web/profiles 页面迁入，表单字段 ID 保持不变。 */
(function () {
    'use strict';

    const listEl = document.getElementById('role-list');
    const quotaEl = document.getElementById('role-quota-line');
    const bannerEl = document.getElementById('role-banner');
    const editorEl = document.getElementById('role-editor-card');
    const titleEl = editorEl ? editorEl.querySelector('.ui-card-title') : null;
    const saveBtn = document.getElementById('save-btn');
    const cancelBtn = document.getElementById('cancel-btn');

    let items = [];          // 后端返回的角色条目
    let editingId = null;    // null=新建，否则为正在编辑的私有角色 ID
    let base = null;         // 编辑时载入的完整角色对象（保留表单未展示的字段）

    /* ── 表单读写 ─────────────────────────────────── */
    const F = {
        name: 'f-name', category: 'f-category', verbosity: 'f-verbosity',
        background: 'f-background', appearance: 'f-appearance', era: 'f-era',
        ageAppearance: 'f-age-appearance', coreValues: 'f-core-values',
        tone: 'f-tone', vocabulary: 'f-vocabulary', sentence: 'f-sentence',
        emoji: 'f-emoji', forbidden: 'f-forbidden', example: 'f-example',
        mbti: 'f-mbti', careStyle: 'f-care-style', traits: 'f-traits',
        greeting: 'f-greeting', negative: 'f-negative', positive: 'f-positive',
        silence: 'f-silence', boundaries: 'f-boundaries', guardrails: 'f-guardrails',
        meta: 'f-meta'
    };
    function el(key) { return document.getElementById(F[key]); }
    function toLines(arr) { return (arr || []).join('\n'); }
    function fromLines(text) {
        return text.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
    }
    function traitsToText(obj) {
        return Object.keys(obj || {}).map(function (k) { return k + '=' + obj[k]; }).join('\n');
    }
    function traitsFromText(text) {
        const out = {};
        text.split('\n').forEach(function (line) {
            const idx = line.indexOf('=');
            if (idx <= 0) return;
            const k = line.slice(0, idx).trim();
            const v = Number(line.slice(idx + 1).trim());
            if (k) out[k] = isFinite(v) ? Math.max(0, Math.min(1, v)) : 0.5;
        });
        return out;
    }

    function fillForm(profile) {
        const src = profile || {};
        const persona = src.persona || {};
        const ls = src.language_style || {};
        const p = src.personality || {};
        const ds = src.dialogue_strategy || {};
        el('name').value = src.name || '';
        el('category').value = (src.category === 'custom') ? 'custom' : 'original';
        el('verbosity').value = src.verbosity || 'standard';
        el('background').value = persona.background || '';
        el('appearance').value = persona.appearance || '';
        el('era').value = persona.era || '';
        el('ageAppearance').value = persona.age_appearance || '';
        el('coreValues').value = toLines(persona.core_values);
        el('tone').value = ls.tone || '';
        el('vocabulary').value = toLines(ls.vocabulary);
        el('sentence').value = toLines(ls.sentence_patterns);
        el('emoji').value = toLines(ls.emoji_habits);
        el('forbidden').value = toLines(ls.forbidden_words);
        el('example').value = toLines(ls.example_utterances);
        el('mbti').value = p.mbti_hint || '';
        el('careStyle').value = p.care_style || '';
        el('traits').value = traitsToText(p.traits);
        el('greeting').value = ds.greeting || '';
        el('negative').value = ds.on_negative_emotion || '';
        el('positive').value = ds.on_positive_emotion || '';
        el('silence').value = ds.on_silence || '';
        el('boundaries').value = toLines(ds.boundaries);
        el('guardrails').value = ds.safety_guardrails || '';
        el('meta').value = src.meta_instructions || '';
    }

    /* 以载入对象为底、表单覆盖，避免 relationships 等未展示字段被清空 */
    function buildPayload() {
        const src = base || {};
        const persona = Object.assign({}, src.persona || {});
        const ls = Object.assign({}, src.language_style || {});
        const p = Object.assign({}, src.personality || {});
        const ds = Object.assign({}, src.dialogue_strategy || {});
        persona.background = el('background').value.trim();
        persona.appearance = el('appearance').value.trim();
        persona.era = el('era').value.trim();
        persona.age_appearance = el('ageAppearance').value.trim();
        persona.core_values = fromLines(el('coreValues').value);
        ls.tone = el('tone').value.trim();
        ls.vocabulary = fromLines(el('vocabulary').value);
        ls.sentence_patterns = fromLines(el('sentence').value);
        ls.emoji_habits = fromLines(el('emoji').value);
        ls.forbidden_words = fromLines(el('forbidden').value);
        ls.example_utterances = fromLines(el('example').value);
        p.mbti_hint = el('mbti').value.trim();
        p.care_style = el('careStyle').value.trim();
        p.traits = traitsFromText(el('traits').value);
        ds.greeting = el('greeting').value.trim();
        ds.on_negative_emotion = el('negative').value.trim();
        ds.on_positive_emotion = el('positive').value.trim();
        ds.on_silence = el('silence').value.trim();
        ds.boundaries = fromLines(el('boundaries').value);
        ds.safety_guardrails = el('guardrails').value.trim();
        return {
            name: el('name').value.trim(),
            category: el('category').value,
            verbosity: el('verbosity').value,
            meta_instructions: el('meta').value,
            persona: persona,
            language_style: ls,
            personality: p,
            dialogue_strategy: ds
        };
    }

    /* 只读态：预览公共角色时锁定整张表单。select 不支持 readOnly，改用 disabled；
       两者的 .value 仍可被 JS 读到，故「以此新建角色」的取值逻辑不受影响。 */
    function setReadonly(flag) {
        Object.keys(F).forEach(function (key) {
            const node = el(key);
            if (node.tagName === 'SELECT') node.disabled = flag;
            else node.readOnly = flag;
        });
        const badge = titleEl ? titleEl.querySelector('.ui-badge') : null;
        if (badge) badge.remove();
        if (!flag) return;
        const b = document.createElement('span');
        b.className = 'ui-badge ui-badge--line';
        b.textContent = '只读';
        if (titleEl) titleEl.appendChild(b);
    }

    /* ── 列表渲染（一律 textContent，禁止把后端文本当 HTML 解析）── */
    function show(kind, text) {
        bannerEl.hidden = false;
        bannerEl.className = 'ui-alert' + (kind === 'err' ? ' ui-alert--err' : kind === 'ok' ? ' ui-alert--ok' : '');
        bannerEl.textContent = text;
    }

    function mkBtn(text, handler, danger) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'ui-btn ui-btn--ghost ui-btn--sm' + (danger ? ' ui-btn--danger' : '');
        b.textContent = text;
        b.addEventListener('click', handler);
        return b;
    }

    function renderList() {
        listEl.textContent = '';
        if (!items.length) {
            const e = document.createElement('div');
            e.className = 'ui-empty';
            e.textContent = '还没有可用角色，请联系管理员开放公共角色，或先自建一个。';
            listEl.appendChild(e);
            return;
        }
        items.forEach(function (it) {
            const row = document.createElement('div');
            row.className = 'ui-list-row ui-cluster';

            const grow = document.createElement('div');
            grow.className = 'ui-stack ui-stack--tight ui-cluster--fill';
            const t = document.createElement('div');
            t.className = 'u-fw-600';
            t.textContent = it.name;
            const m = document.createElement('div');
            m.className = 'u-muted u-fs-82';
            m.textContent = (it.owned ? '自建 · ' : '公共 · ')
                + (it.category === 'custom' ? '自定义设定' : '原创角色');
            grow.appendChild(t);
            grow.appendChild(m);
            row.appendChild(grow);

            const tag = document.createElement('span');
            tag.className = 'ui-badge ' + (it.is_active ? 'ui-badge--ok' : 'ui-badge--err');
            tag.textContent = it.is_active ? '启用中' : '已停用';
            row.appendChild(tag);

            const acts = document.createElement('div');
            acts.className = 'ui-cluster ui-cluster--nowrap';
            if (it.owned) {
                acts.appendChild(mkBtn('编辑', function () { openEditor(it.id); }));
                acts.appendChild(mkBtn(it.is_active ? '停用' : '启用', function () { toggle(it); }));
                acts.appendChild(mkBtn('删除', function () { remove(it); }, true));
            } else {
                acts.appendChild(mkBtn('查看', function () { preview(it); }));
            }
            row.appendChild(acts);
            listEl.appendChild(row);
        });
    }

    function renderQuota(data) {
        const q = data.quota || {};
        quotaEl.textContent = data.can_create
            ? '自建角色 ' + (q.used || 0) + ' / ' + (q.limit || 0) + '（已含停用）'
            : '管理员未开放自建角色，仅可使用公共角色';
        const btn = document.getElementById('role-new-btn');
        if (btn) btn.hidden = !data.can_create;
    }

    async function load() {
        const r = await window.WebAuth.call('/api/web/profiles');
        if (!r.ok) { show('err', r.detail || '角色列表读取失败'); return; }
        items = r.data.items || [];
        renderQuota(r.data);
        renderList();
        window.dispatchEvent(new CustomEvent('web-roles-updated'));
    }

    /* ── 编辑流 ─────────────────────────────────── */
    async function openEditor(id) {
        const r = await window.WebAuth.call('/api/web/profiles/' + encodeURIComponent(id));
        if (!r.ok) { show('err', r.detail || '角色读取失败'); return; }
        base = r.data;
        editingId = id;
        fillForm(r.data);
        titleEl.textContent = '编辑角色：' + (r.data.name || '');
        setReadonly(false);
        saveBtn.textContent = '保存角色';
        cancelBtn.textContent = '取消';
        editorEl.hidden = false;
        editorEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    async function preview(it) {
        const r = await window.WebAuth.call('/api/web/profiles/' + encodeURIComponent(it.id));
        if (!r.ok) { show('err', r.detail || '角色读取失败'); return; }
        base = null;
        editingId = null;
        fillForm(r.data);
        titleEl.textContent = '公共角色（只读参考）：' + (r.data.name || '');
        setReadonly(true);
        saveBtn.textContent = '以此新建角色';
        cancelBtn.textContent = '关闭';
        editorEl.hidden = false;
        editorEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
        show('info', '公共角色不可修改；需要改动请以此为基础新建自己的角色。');
    }

    function closeEditor() {
        editorEl.hidden = true;
        editingId = null;
        base = null;
        setReadonly(false);
        saveBtn.textContent = '保存角色';
        cancelBtn.textContent = '取消';
        bannerEl.hidden = true;
    }

    async function save() {
        if (!el('name').value.trim()) { show('err', '请填写角色名称'); return; }
        const payload = buildPayload();
        const creating = !editingId;
        const url = creating ? '/api/web/profiles' : '/api/web/profiles/' + encodeURIComponent(editingId);
        const r = await window.WebAuth.call(url, {
            method: creating ? 'POST' : 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!r.ok) { show('err', r.detail || '保存失败'); return; }
        show('ok', '角色已保存。到「服务」分区创建服务并选择渠道即可开聊。');
        closeEditor();
        await load();
    }

    async function toggle(it) {
        const r = await window.WebAuth.call('/api/web/profiles/' + encodeURIComponent(it.id) + '/toggle', { method: 'POST' });
        if (!r.ok) { show('err', r.detail || '操作失败'); return; }
        await load();
    }

    async function remove(it) {
        if (!window.confirm('确定删除角色「' + it.name + '」？删除后不可恢复。')) return;
        const r = await window.WebAuth.call('/api/web/profiles/' + encodeURIComponent(it.id), { method: 'DELETE' });
        if (!r.ok) { show('err', r.detail || '删除失败'); return; }
        show('ok', '角色已删除。');
        await load();
    }

    if (cancelBtn) cancelBtn.addEventListener('click', closeEditor);
    if (saveBtn) saveBtn.addEventListener('click', save);
    const newBtn = document.getElementById('role-new-btn');
    if (newBtn) {
        newBtn.addEventListener('click', function () {
            base = null;
            editingId = null;
            fillForm(null);
            titleEl.textContent = '新建角色';
            setReadonly(false);
            saveBtn.textContent = '保存角色';
            cancelBtn.textContent = '取消';
            bannerEl.hidden = true;
            editorEl.hidden = false;
            editorEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
            el('name').focus();
        });
    }
    /* 内联脚本解析期 WebAuth 尚未定义；defer 脚本在 DOMContentLoaded 前执行完，
       故把首屏加载挂到这个事件上。 */
    document.addEventListener('DOMContentLoaded', function () {
        load().catch(function (e) {
            console.error('角色列表加载失败', e);
            show('err', '角色列表读取失败，请稍后重试');
        });
    });
})();
