/* C 端账号中心：资料 / 密码与设备 / 额度 / 数据与隐私 / 注销。
 *
 * 页面是静态壳，所有数据带 Bearer 令牌调 /api/web/account/* 取；
 * 一律用 textContent 渲染后端文本，避免把账号内容当 HTML 解析。
 * 与 profiles/services 两页不同，本脚本外置成 defer 文件并在 DOMContentLoaded 初始化：
 * 内联脚本会在解析期执行，那时 web-auth.js 尚未定义 window.WebAuth。
 */
(function () {
    'use strict';

    var API = '/api/web/account';
    var banner = document.getElementById('banner');

    function el(id) { return document.getElementById(id); }

    function show(kind, text) {
        banner.hidden = false;
        banner.className = 'ui-alert' +
            (kind === 'err' ? ' ui-alert--err' : kind === 'ok' ? ' ui-alert--ok' : '');
        banner.textContent = text;
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function json(method, body) {
        return {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {})
        };
    }

    function mk(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function mkBtn(text, handler, variant) {
        var b = mk('button', 'ui-btn ui-btn--sm ' + (variant || 'ui-btn--ghost'));
        b.type = 'button';
        b.textContent = text;
        b.addEventListener('click', handler);
        return b;
    }

    /* 数字千分位：额度动辄六位以上，不分组很难读 */
    function num(v) {
        if (v === null || v === undefined) return '-';
        return Number(v).toLocaleString('zh-CN');
    }

    /* 后端一律下发 ISO8601（UTC），这里按浏览器本地时区渲染 */
    function date(iso, withTime) {
        if (!iso) return '—';
        var d = new Date(iso);
        if (isNaN(d.getTime())) return '—';
        var day = d.getFullYear() + '-' +
            String(d.getMonth() + 1).padStart(2, '0') + '-' +
            String(d.getDate()).padStart(2, '0');
        if (!withTime) return day;
        return day + ' ' +
            String(d.getHours()).padStart(2, '0') + ':' +
            String(d.getMinutes()).padStart(2, '0');
    }

    function relTime(iso) {
        if (!iso) return '';
        var diff = Date.now() - new Date(iso).getTime();
        if (isNaN(diff)) return '';
        var min = Math.round(diff / 60000);
        if (min < 1) return '刚刚活跃';
        if (min < 60) return min + ' 分钟前活跃';
        var hr = Math.round(min / 60);
        if (hr < 24) return hr + ' 小时前活跃';
        return Math.round(hr / 24) + ' 天前活跃';
    }

    /* ── 资料 ─────────────────────────────────────── */
    var profileCache = null;

    async function loadProfile() {
        var r = await WebAuth.call(API + '/profile');
        if (!r.ok) { show('err', r.detail || '账号资料读取失败'); return; }
        profileCache = r.data;
        el('f-nickname').value = r.data.nickname || '';
        el('f-timezone').value = r.data.timezone || '';
        el('f-desc').value = r.data.self_description || '';

        var meta = el('account-meta');
        meta.textContent = '';
        meta.appendChild(mk('span', '', '用户名：' + r.data.username + '（不可修改）'));
        meta.appendChild(mk('span', '', '注册时间：' + date(r.data.created_at, true)));
        meta.appendChild(mk('span', '', '最近对话：' + relTime(r.data.last_active_at)));
    }

    async function saveProfile() {
        var r = await WebAuth.call(API + '/profile', json('PUT', {
            nickname: el('f-nickname').value,
            self_description: el('f-desc').value,
            timezone: el('f-timezone').value
        }));
        if (!r.ok) { show('err', r.detail || '资料保存失败'); return; }
        show('ok', '资料已保存。');
        await loadProfile();
    }

    /* ── 密码 ─────────────────────────────────────── */
    async function changePassword() {
        var current = el('f-pw-current').value;
        var next = el('f-pw-new').value;
        var confirm = el('f-pw-confirm').value;
        if (!current) { show('err', '请填写当前密码'); return; }
        if (next !== confirm) { show('err', '两次输入的新密码不一致'); return; }
        var r = await WebAuth.call(API + '/password', json('POST', {
            current_password: current, new_password: next
        }));
        if (!r.ok) { show('err', r.detail || '密码修改失败'); return; }
        el('f-pw-current').value = '';
        el('f-pw-new').value = '';
        el('f-pw-confirm').value = '';
        show('ok', '密码已修改，其他设备需重新登录。');
        await loadSessions();
    }

    /* ── 登录设备 ─────────────────────────────────── */
    async function loadSessions() {
        var r = await WebAuth.call(API + '/sessions');
        if (!r.ok) { show('err', r.detail || '登录设备读取失败'); return; }
        var list = el('session-list');
        var items = r.data.items || [];
        el('session-note').textContent =
            '当前有 ' + items.length + ' 台设备在线（同一账号最多 ' + r.data.max_concurrent +
            ' 台，会话闲置 ' + r.data.session_hours + ' 小时后自动失效）。';
        list.textContent = '';
        if (!items.length) {
            list.appendChild(mk('div', 'ui-empty', '没有在线设备。'));
            return;
        }
        items.forEach(function (s) {
            var row = mk('div', 'ui-list-row ui-cluster');
            var grow = mk('div', 'ui-stack ui-stack--tight ui-cluster--fill');
            grow.appendChild(mk('div', 'u-fw-600', s.device));
            grow.appendChild(mk('div', 'session-meta',
                s.ip + ' · 登录于 ' + date(s.created_at, true) + ' · ' + relTime(s.last_activity)));
            row.appendChild(grow);
            if (s.current) {
                var badge = mk('span', 'ui-badge ui-badge--ink', '当前设备');
                row.appendChild(badge);
            } else {
                row.appendChild(mkBtn('下线', function () { revokeSession(s); }, 'ui-btn--outline'));
            }
            list.appendChild(row);
        });
    }

    async function revokeSession(s) {
        var r = await WebAuth.call(API + '/sessions/' + encodeURIComponent(s.id), { method: 'DELETE' });
        if (!r.ok) { show('err', r.detail || '下线失败'); return; }
        show('ok', '已下线该设备。');
        await loadSessions();
    }

    async function revokeOthers() {
        if (!window.confirm('确定下线除当前设备外的所有设备？其他浏览器需要重新登录。')) return;
        var r = await WebAuth.call(API + '/sessions/revoke-others', json('POST', {}));
        if (!r.ok) { show('err', r.detail || '操作失败'); return; }
        show('ok', '已下线 ' + (r.data.revoked || 0) + ' 台其他设备。');
        await loadSessions();
    }

    /* ── 额度 ─────────────────────────────────────── */
    var QUOTA_LABEL = { text: '文字', voice: '语音', image: '图片' };
    var QUOTA_UNIT = { text: 'Token', voice: '次', image: '张' };

    function quotaCell(key, q) {
        var cell = mk('div', 'quota-cell ui-inset');
        var head = mk('div', 'quota-head');
        head.appendChild(mk('strong', null, num(q.remaining)));
        head.appendChild(mk('span', 'quota-tail', '剩余 / ' + num(q.limit) + ' ' + QUOTA_UNIT[key]));
        cell.appendChild(head);

        var bar = mk('div', 'ui-bar');
        bar.setAttribute('role', 'progressbar');
        bar.setAttribute('aria-valuenow', String(Math.round(q.percent)));
        bar.setAttribute('aria-label', QUOTA_LABEL[key] + '额度使用比例');
        var tone = q.expired ? ' ui-bar__fill--err'
            : (q.percent >= 90 ? ' ui-bar__fill--warn' : '');
        var fill = mk('span', 'ui-bar__fill' + tone);
        fill.style.setProperty('--bar-pct', q.percent + '%');
        bar.appendChild(fill);
        cell.appendChild(bar);

        var tail = QUOTA_LABEL[key] + '已用 ' + q.percent + '%';
        tail += q.expires_at ? (q.expired ? ' · 已于 ' : ' · 有效至 ') + date(q.expires_at)
                             : ' · 永久有效';
        if (q.expired) tail += '（已到期）';
        cell.appendChild(mk('div', 'quota-tail', tail));
        return cell;
    }

    async function loadQuota() {
        var r = await WebAuth.call(API + '/quota');
        if (!r.ok) { show('err', r.detail || '额度读取失败'); return; }
        var grid = el('quota-grid');
        grid.textContent = '';
        if (!r.data.quota) {
            el('quota-note').textContent = '账号尚无额度记录，请联系管理员发放。';
            return;
        }
        ['text', 'voice', 'image'].forEach(function (key) {
            grid.appendChild(quotaCell(key, r.data.quota[key]));
        });
        var expired = ['text', 'voice', 'image'].some(function (k) { return r.data.quota[k].expired; });
        el('quota-note').textContent = expired
            ? '有额度已到期，到期部分不再可用；请联系管理员续期。'
            : '额度充足时可正常对话；文字额度按模型实际消耗结算。';
        renderOrders(r.data.orders);
    }

    function renderOrders(orders) {
        var list = el('order-list');
        list.textContent = '';
        var items = (orders && orders.items) || [];
        if (!items.length) {
            list.appendChild(mk('div', 'ui-empty',
                '还没有发放记录。注册赠送的体验额度不入台账，管理员开通套餐后会出现在这里。'));
            return;
        }
        items.forEach(function (o) {
            var row = mk('div', 'ui-list-row ui-cluster');
            var grow = mk('div', 'ui-stack ui-stack--tight ui-cluster--fill');
            grow.appendChild(mk('div', 'u-fw-600', o.template_name));
            grow.appendChild(mk('div', 'data-meta',
                '文字 ' + num(o.text_limit) + ' · 语音 ' + num(o.voice_limit) +
                ' · 图片 ' + num(o.image_limit) +
                (o.validity_days ? ' · ' + o.validity_days + ' 天' : ' · 永久') +
                ' · ' + date(o.created_at, true) +
                (o.remark ? ' · ' + o.remark : '')));
            row.appendChild(grow);
            list.appendChild(row);
        });
    }

    /* ── 数据与隐私 ───────────────────────────────── */
    async function loadData() {
        var r = await WebAuth.call(API + '/data');
        if (!r.ok) { show('err', r.detail || '数据统计读取失败'); return; }
        var c = r.data.counts || {};
        el('data-summary').textContent =
            '对话记录 ' + num(c.conversations) + ' 条 / 消息 ' + num(c.messages) + ' 条 · ' +
            '长期记忆 ' + num(c.facts) + ' 条 · 服务 ' + num(c.services) + ' 个';

        var memories = await WebAuth.call(API + '/memories');
        var byId = {};
        if (memories.ok) {
            (memories.data.items || []).forEach(function (m) { byId[m.service_id] = m; });
        }

        var wrap = el('service-data');
        wrap.textContent = '';
        if (!r.data.services.length) {
            wrap.appendChild(mk('div', 'ui-empty', '还没有服务，去「聊天服务」创建一个。'));
            return;
        }
        r.data.services.forEach(function (svc) {
            wrap.appendChild(serviceRow(svc, byId[svc.id]));
        });
    }

    function serviceRow(svc, memory) {
        var row = mk('div', 'ui-list-row ui-cluster');
        var grow = mk('div', 'ui-stack ui-stack--tight ui-cluster--fill');
        grow.appendChild(mk('div', 'u-fw-600', svc.name));
        grow.appendChild(mk('div', 'data-meta',
            '扮演 ' + svc.profile_name +
            ' · 对话 ' + num(svc.conversation_count) + ' 条 / 消息 ' + num(svc.message_count) + ' 条' +
            ' · 记忆 ' + num(svc.memory_facts) + ' 条' +
            (svc.shared ? ' · 与其他身份共享上下文' : '')));
        row.appendChild(grow);

        if (memory && (memory.facts || []).length) {
            row.appendChild(memoryDetails(memory));
        }
        if (!svc.shared && svc.memory_facts > 0) {
            row.appendChild(mkBtn('清空记忆', function () { clearMemory(svc); }, 'ui-btn--outline ui-btn--danger'));
        }
        return row;
    }

    function memoryDetails(memory) {
        var d = mk('details', 'ui-inset');
        var sum = mk('summary', 'ui-summary ui-summary--sm', '查看记忆（' + memory.facts.length + ' 条）');
        d.appendChild(sum);
        var body = mk('div');
        if (memory.current_topic) {
            body.appendChild(mk('div', 'fact-meta', '当前话题：' + memory.current_topic));
        }
        memory.facts.forEach(function (f) {
            var item = mk('div', 'memory-fact');
            item.appendChild(mk('div', null, f.content));
            item.appendChild(mk('div', 'fact-meta',
                f.category + ' · ' + f.tier + ' · ' + date(f.created_at)));
            body.appendChild(item);
        });
        (memory.entities || []).forEach(function (e) {
            body.appendChild(mk('div', 'fact-meta', '关系：' + e.name +
                (e.relation_to_identity ? '（' + e.relation_to_identity + '）' : '')));
        });
        d.appendChild(body);
        return d;
    }

    async function clearConversations() {
        if (!window.confirm('确定清空全部对话记录？消息与生成的图片将被删除，长期记忆保留。')) return;
        var r = await WebAuth.call(API + '/data/conversations/clear', json('POST', { confirm: true }));
        if (!r.ok) { show('err', r.detail || '清空失败'); return; }
        show('ok', '已清空对话：会话 ' + r.data.conversations + ' 条、消息 ' + r.data.messages + ' 条。');
        await loadData();
    }

    async function clearMemory(svc) {
        if (!window.confirm('确定清空服务「' + svc.name + '」的长期记忆？AI 将不再记得这些事实。')) return;
        var r = await WebAuth.call(
            API + '/data/memories/' + encodeURIComponent(svc.id) + '/clear',
            json('POST', { confirm: true })
        );
        if (!r.ok) { show('err', r.detail || '清空失败'); return; }
        show('ok', '已清空该服务的记忆（' + r.data.facts + ' 条）。');
        await loadData();
    }

    async function exportData() {
        var resp;
        try {
            resp = await fetch(API + '/export', { headers: WebAuth.authHeader() });
        } catch (e) {
            show('err', '网络异常，导出失败');
            return;
        }
        if (resp.status === 401) { WebAuth.toLogin(); return; }
        if (!resp.ok) {
            var err = await resp.json().catch(function () { return {}; });
            show('err', err.detail || '导出失败');
            return;
        }
        var blob = await resp.blob();
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'aiyichat-account-' + (profileCache ? profileCache.username : 'export') + '.json';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        show('ok', '数据包已生成，请在浏览器下载列表中查看。');
    }

    /* ── 注销 ─────────────────────────────────────── */
    async function deleteAccount() {
        var password = el('f-del-password').value;
        var username = el('f-del-username').value;
        if (!password || !username) { show('err', '请填写当前密码并输入用户名以确认'); return; }
        if (!window.confirm('注销后账号数据全部删除且不可恢复，确定继续？')) return;
        var r = await WebAuth.call(API + '/delete', json('POST', {
            current_password: password, username_confirm: username
        }));
        if (r.ok) {
            localStorage.removeItem(WebAuth.TOKEN_KEY);
            window.location.replace('/web/login');
            return;
        }
        if (r.status >= 500) {
            /* 服务端按"先删凭据、再删数据"执行：500 时账号多半已不可登录，
               令牌留着只会让页面反复报错，清掉并提示用户找管理员确认 */
            localStorage.removeItem(WebAuth.TOKEN_KEY);
            show('err', (r.detail || '注销未完成') + ' 请稍后重试或联系管理员确认。');
            return;
        }
        /* 400/403/429 只是这一次请求被拒，账号与登录态都还在：
           绝不清令牌，否则下一次提交会以"未登录"发出，用户被莫名踢回登录页 */
        show('err', r.detail || '注销失败');
    }

    document.addEventListener('DOMContentLoaded', function () {
        el('profile-save').addEventListener('click', saveProfile);
        el('password-save').addEventListener('click', changePassword);
        el('revoke-others').addEventListener('click', revokeOthers);
        el('clear-conversations').addEventListener('click', clearConversations);
        el('export-data').addEventListener('click', exportData);
        el('account-delete').addEventListener('click', deleteAccount);

        Promise.all([loadProfile(), loadSessions(), loadQuota(), loadData()])
            .catch(function (e) {
                console.error('账号中心加载失败', e);
                show('err', '账号信息读取失败，请稍后重试');
            });
    });
})();
