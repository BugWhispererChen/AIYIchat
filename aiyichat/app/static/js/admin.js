/* ══════════════════════════════════════════════════════
   admin.js —— 后台交互层

   由 base.html 的内联脚本外置而来，行为语义保持不变：
   主题、侧栏抽屉、标签页、全局 Modal、二次确认、防重复提交、
   锚点高亮、Toast、原生校验反馈、HTMX 钩子。
   本次新增：URL query 反馈转 Toast、表格即时过滤与列排序、
   侧栏折叠记忆、一键复制。
   ══════════════════════════════════════════════════════ */
(function () {
    'use strict';

    /* ══ 主题切换：读写统一交给 theme.js 的 UiTheme ══ */
    function toggleTheme() {
        if (window.UiTheme) window.UiTheme.toggle();
    }
    window.toggleTheme = toggleTheme;

    /* ══ 侧边栏：移动端抽屉 + 桌面端折叠记忆 ══ */
    var NAV_KEY = 'admin-nav-collapsed';

    function sidebarEl() { return document.getElementById('sidebar'); }
    function backdropEl() { return document.getElementById('sidebarBackdrop'); }

    function toggleSidebar(force) {
        var sb = sidebarEl();
        var bd = backdropEl();
        if (!sb) return;
        var open = (typeof force === 'boolean') ? force : !sb.classList.contains('open');
        sb.classList.toggle('open', open);
        if (bd) bd.classList.toggle('open', open);
    }
    window.toggleSidebar = toggleSidebar;

    function setCollapsed(collapsed) {
        document.body.classList.toggle('nav-collapsed', collapsed);
        try { localStorage.setItem(NAV_KEY, collapsed ? '1' : '0'); } catch (e) {}
    }

    function initNavCollapse() {
        var collapsed = null;
        try { collapsed = localStorage.getItem(NAV_KEY); } catch (e) {}
        // 仅桌面宽度记忆折叠；窄屏始终走抽屉逻辑，避免内容被永久遮住
        if (collapsed === '1' && window.innerWidth > 1024) {
            document.body.classList.add('nav-collapsed');
        }
        var btn = document.getElementById('navCollapseBtn');
        if (btn) {
            btn.addEventListener('click', function () {
                setCollapsed(!document.body.classList.contains('nav-collapsed'));
            });
        }
    }

    document.querySelectorAll('.sidebar-nav a').forEach(function (a) {
        a.addEventListener('click', function () { toggleSidebar(false); });
    });

    /* ══ 标签页 + sessionStorage 记忆 ══
       用法：.tabs[data-tabs-key] > .tab-label[data-tab] + .tab-content[data-tab-content] */
    function initTabs() {
        document.querySelectorAll('.tabs').forEach(function (tabs) {
            var key = tabs.dataset.tabsKey || 'tabs';
            var labels = tabs.querySelectorAll('.tab-label');
            var contents = tabs.querySelectorAll('.tab-content');
            if (!labels.length) return;

            function activate(id) {
                labels.forEach(function (l) { l.classList.toggle('active', l.dataset.tab === id); });
                contents.forEach(function (c) { c.classList.toggle('active', c.dataset.tabContent === id); });
                try { sessionStorage.setItem('tab:' + key, id); } catch (e) {}
            }
            labels.forEach(function (l) {
                l.addEventListener('click', function () { activate(l.dataset.tab); });
            });
            var saved = null;
            try { saved = sessionStorage.getItem('tab:' + key); } catch (e) {}
            var valid = saved && Array.prototype.some.call(labels, function (l) { return l.dataset.tab === saved; });
            activate(valid ? saved : labels[0].dataset.tab);
        });
    }

    /* ══ 全局 Modal（确认 / 输入） ══ */
    function adminModal(opts) {
        opts = opts || {};
        var overlay = document.getElementById('adminModalOverlay');
        var titleEl = document.getElementById('adminModalTitle');
        var msgEl = document.getElementById('adminModalMessage');
        var inputEl = document.getElementById('adminModalInput');
        var okBtn = document.getElementById('adminModalConfirm');
        var cancelBtn = document.getElementById('adminModalCancel');
        if (!overlay) return;

        titleEl.textContent = opts.title || '请确认';
        msgEl.textContent = opts.message || '';
        msgEl.style.display = opts.message ? '' : 'none';
        okBtn.textContent = opts.confirmText || '确认';
        cancelBtn.textContent = opts.cancelText || '取消';
        okBtn.className = 'btn-sm' + (opts.danger ? ' danger' : '');

        if (opts.input) {
            inputEl.style.display = '';
            inputEl.value = opts.inputValue || '';
            inputEl.placeholder = opts.inputPlaceholder || '';
        } else {
            inputEl.style.display = 'none';
        }

        function close() {
            overlay.classList.remove('open');
            okBtn.onclick = null; cancelBtn.onclick = null;
            document.removeEventListener('keydown', onKey);
        }
        function doConfirm() { var v = inputEl.value; close(); if (opts.onConfirm) opts.onConfirm(v); }
        function onKey(e) {
            if (e.key === 'Escape') close();
            if (e.key === 'Enter' && !opts.input) doConfirm();
        }
        okBtn.onclick = doConfirm;
        cancelBtn.onclick = close;
        overlay.onclick = function (e) { if (e.target === overlay) close(); };
        document.addEventListener('keydown', onKey);
        overlay.classList.add('open');
        if (opts.input) setTimeout(function () { inputEl.focus(); }, 50);
    }
    window.adminModal = adminModal;

    /* 表单级确认：<form data-confirm="..." [data-danger] [data-prompt data-prompt-field=name]> */
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form.dataset || form.dataset.confirm === undefined) return;
        if (form.dataset.confirmed === '1') { form.dataset.confirmed = ''; return; }
        e.preventDefault();
        var isPrompt = form.dataset.prompt !== undefined;
        adminModal({
            title: form.dataset.confirmTitle || (form.dataset.danger !== undefined ? '危险操作确认' : '请确认'),
            message: form.dataset.confirm,
            danger: form.dataset.danger !== undefined,
            confirmText: form.dataset.confirmText || '确认',
            input: isPrompt,
            inputPlaceholder: form.dataset.promptPlaceholder || '',
            onConfirm: function (value) {
                if (isPrompt) {
                    var field = form.dataset.promptField || 'block_reason';
                    var inp = form.querySelector('input[name="' + field + '"]');
                    if (inp) inp.value = value || '';
                }
                form.dataset.confirmed = '1';
                if (form.requestSubmit) form.requestSubmit(); else form.submit();
            }
        });
    }, true);

    /* 按钮级确认（支持 formaction）：<button data-confirm="..." [data-danger]> */
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('button[data-confirm], [role="button"][data-confirm]');
        if (!btn) return;
        if (btn.dataset.confirmed === '1') { btn.dataset.confirmed = ''; return; }
        var form = btn.closest('form');
        if (!form) return;
        e.preventDefault();
        adminModal({
            title: btn.dataset.confirmTitle || (btn.dataset.danger !== undefined ? '危险操作确认' : '请确认'),
            message: btn.dataset.confirm,
            danger: btn.dataset.danger !== undefined,
            confirmText: btn.dataset.confirmText || '确认',
            onConfirm: function () {
                btn.dataset.confirmed = '1';
                if (form.requestSubmit) form.requestSubmit(btn); else form.submit();
            }
        });
    }, true);

    /* ══ 防重复提交（跳过 HTMX 表单 / 已被 modal 拦截的表单）══
       表单条目列表在 submit 事件派发之后才构造，被禁用的元素不进入提交数据；
       因此这里绝不禁用提交按钮，否则唯一携带 name/value 的那颗按钮会丢字段。 */
    function guardForms() {
        document.querySelectorAll('form').forEach(function (form) {
            if (form.hasAttribute('hx-post') || form.hasAttribute('hx-get')
                || form.hasAttribute('hx-put') || form.hasAttribute('hx-delete')
                || form.hasAttribute('hx-patch')) return;
            if (form.dataset.guarded === '1') return;
            form.dataset.guarded = '1';
            form.addEventListener('submit', function (e) {
                if (e.defaultPrevented) return;                                      // 已被确认弹窗拦下，不加锁
                if (form.dataset.submitting === '1') { e.preventDefault(); return; } // 拦截重复提交
                // 只标记视觉态：优先取真正被点击的按钮（兼容 form="..." 外置按钮），取不到才退回首个
                var btn = e.submitter || form.querySelector('button[type="submit"], input[type="submit"]');
                form.dataset.submitting = '1';
                var restore = function () {
                    form.dataset.submitting = '';
                    if (!btn) return;
                    btn.classList.remove('is-submitting');
                    btn.textContent = btn.dataset.originalText || btn.textContent;
                };
                if (btn) {
                    btn.dataset.originalText = btn.textContent;
                    btn.classList.add('is-submitting');
                    btn.textContent = '提交中...';
                }
                // 兜底：请求失败停在错误页或回退时不永久锁死表单
                setTimeout(restore, 5000);
            });
        });
    }

    /* ══ 锚点导航滚动高亮（详情页） ══ */
    function initAnchorSpy() {
        var nav = document.querySelector('.anchor-nav');
        if (!nav || !('IntersectionObserver' in window)) return;
        var links = nav.querySelectorAll('a[href^="#"]');
        if (!links.length) return;
        var map = {};
        links.forEach(function (a) {
            var id = a.getAttribute('href').slice(1);
            var sec = document.getElementById(id);
            if (sec) map[id] = a;
        });
        var obs = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                if (e.isIntersecting) {
                    links.forEach(function (l) { l.classList.remove('active'); });
                    if (map[e.target.id]) map[e.target.id].classList.add('active');
                }
            });
        }, { rootMargin: '-20% 0px -70% 0px' });
        Object.keys(map).forEach(function (id) { obs.observe(document.getElementById(id)); });
    }

    /* ══ Toast ══ */
    function toastContainer() {
        var container = document.querySelector('.toast-container');
        if (!container) {
            container = document.createElement('div');
            container.className = 'toast-container';
            document.body.appendChild(container);
        }
        return container;
    }

    function showToast(message, type) {
        if (!message) return;
        var toast = document.createElement('div');
        toast.className = 'toast ' + (type || 'success');
        toast.textContent = message;
        toastContainer().appendChild(toast);
        setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 3000);
    }
    window.showToast = showToast;

    /* 后台既有的反馈通道是 303 + URL query（saved=1 / deleted=N 等）。
       这里把它读成 Toast，避免"点了没反应"，同时不改后端一行代码。 */
    var QUERY_MESSAGES = [
        ['saved', '已保存'],
        ['deleted', '已删除'],
        ['created', '已创建'],
        ['updated', '已更新'],
        ['error', null]   // error 单独取文案
    ];

    function flashFromQuery() {
        var params = new URLSearchParams(window.location.search);
        QUERY_MESSAGES.forEach(function (pair) {
            var value = params.get(pair[0]);
            if (value === null || value === '') return;
            if (pair[0] === 'error') {
                showToast(value, 'error');
            } else {
                showToast(pair[1], 'success');
            }
        });
    }

    /* ══ 表格即时过滤：<input data-filter="#table-id"> ══ */
    function initFilters() {
        document.querySelectorAll('[data-filter]').forEach(function (input) {
            var table = document.querySelector(input.dataset.filter);
            if (!table) return;
            var rows = Array.prototype.slice.call(table.querySelectorAll('tbody tr'));
            if (!rows.length) return;
            if (!input.getAttribute('aria-label')) input.setAttribute('aria-label', '过滤当前列表');
            var apply = function () {
                var needle = input.value.trim().toLowerCase();
                var shown = 0;
                rows.forEach(function (row) {
                    var hit = !needle || row.textContent.toLowerCase().indexOf(needle) >= 0;
                    row.hidden = !hit;
                    if (hit) shown += 1;
                });
                var counter = document.querySelector(input.dataset.filterCount);
                if (counter) counter.textContent = String(shown);
            };
            input.addEventListener('input', apply);
        });
    }

    /* ══ 表头列排序：<th data-sortable> ══ */
    function cellValue(row, index) {
        var cell = row.cells[index];
        if (!cell) return '';
        var raw = cell.dataset.sortValue || cell.textContent.trim();
        return raw;
    }

    function initSortableTables() {
        document.querySelectorAll('table').forEach(function (table) {
            var headers = table.querySelectorAll('thead th[data-sortable]');
            if (!headers.length) return;
            var tbody = table.querySelector('tbody');
            if (!tbody) return;
            Array.prototype.forEach.call(headers, function (th, colIndex) {
                th.setAttribute('tabindex', '0');
                var sort = function () {
                    var current = th.getAttribute('aria-sort') === 'ascending' ? 'descending' : 'ascending';
                    Array.prototype.forEach.call(headers, function (h) { h.removeAttribute('aria-sort'); });
                    th.setAttribute('aria-sort', current);
                    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
                    rows.sort(function (a, b) {
                        var av = cellValue(a, colIndex), bv = cellValue(b, colIndex);
                        var an = parseFloat(String(av).replace(/[^\d.-]/g, ''));
                        var bn = parseFloat(String(bv).replace(/[^\d.-]/g, ''));
                        var cmp = (!isNaN(an) && !isNaN(bn))
                            ? an - bn
                            : String(av).localeCompare(String(bv), 'zh-Hans-CN');
                        return current === 'ascending' ? cmp : -cmp;
                    });
                    rows.forEach(function (r) { tbody.appendChild(r); });
                };
                th.addEventListener('click', sort);
                th.addEventListener('keydown', function (e) {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); }
                });
            });
        });
    }

    /* ══ 一键复制：<button data-copy="文本"> 或 data-copy-from="#selector" ══ */
    function initCopyButtons() {
        document.addEventListener('click', function (e) {
            var btn = e.target.closest('[data-copy], [data-copy-from]');
            if (!btn) return;
            var text = btn.dataset.copy || '';
            if (btn.dataset.copyFrom) {
                var src = document.querySelector(btn.dataset.copyFrom);
                if (src) text = src.textContent.trim();
            }
            if (!text) return;
            var done = function () { showToast('已复制：' + text, 'success'); };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(done, function () { showToast('复制失败，请手动选择', 'error'); });
            } else {
                showToast('当前浏览器不支持一键复制', 'warning');
            }
        });
    }

    /* 表单原生校验失败反馈（避免"点击无反应"错觉；invalid 不冒泡，需捕获阶段监听）*/
    var _invalidShown = false;
    document.addEventListener('invalid', function (evt) {
        var field = evt.target;
        if (!_invalidShown) {
            _invalidShown = true;
            showToast('请检查表单：有字段填写不正确', 'error');
            setTimeout(function () { _invalidShown = false; }, 800);
            /* 延迟到原生校验 UI 之后，确定性地将首个无效字段滚动到视图中央 */
            setTimeout(function () {
                if (field && typeof field.scrollIntoView === 'function') {
                    field.scrollIntoView({ block: 'center' });
                }
            }, 30);
        }
    }, true);

    /* ══ HTMX 请求结果反馈 ══ */
    document.body.addEventListener('htmx:afterRequest', function (evt) {
        var trigger = evt.detail && evt.detail.elt;
        if (evt.detail && evt.detail.successful) {
            if (trigger && trigger.dataset && trigger.dataset.successMsg) showToast(trigger.dataset.successMsg, 'success');
        } else if (evt.detail && evt.detail.failed) {
            var errMsg = (trigger && trigger.dataset && trigger.dataset.errorMsg) ? trigger.dataset.errorMsg : '操作失败，请重试';
            showToast(errMsg, 'error');
        }
    });
    /* HTMX 局部替换后重新初始化标签页与增强 */
    document.body.addEventListener('htmx:afterSwap', function () {
        initTabs();
        initFilters();
        initSortableTables();
    });

    document.addEventListener('DOMContentLoaded', function () {
        initNavCollapse();
        initTabs();
        initAnchorSpy();
        initFilters();
        initSortableTables();
        initCopyButtons();
        guardForms();
        flashFromQuery();
    });
})();
