/* 聊天服务页：渠道、服务、身份与角色管理。
   渠道只负责接入，服务 = 渠道 + 角色 + 身份，零服务是合法状态。 */
(function () {
    'use strict';

    const CHANNEL_NAMES = { qq: 'QQ', wechat: '微信', web: '网页' };
    const STATE_LABELS = {
        blocked: '已停用',
        pending_bind: '待绑定',
        pending_qr: '等待扫码',
        online: '在线',
        offline: '离线',
        ready: '可用'
    };

    let bots = [];
    let services = [];
    let profiles = [];
    let identities = [];
    let quota = { limit: 0, bots: 0, services: 0 };
    let canCreateBot = false;
    let canCreateService = false;
    let botPlatform = 'qq';
    let editingBot = null;
    let editingService = null;
    let qrToken = 0;

    function el(id) { return document.getElementById(id); }

    function show(kind, text) {
        const banner = el('banner');
        if (!banner) return;
        banner.hidden = false;
        banner.className = 'ui-alert' + (kind === 'err' ? ' ui-alert--err' : kind === 'ok' ? ' ui-alert--ok' : '');
        banner.textContent = text;
        if (kind === 'ok') {
            setTimeout(function () { banner.hidden = true; }, 4500);
        }
    }

    function clear(node) { node.textContent = ''; }

    function empty(text) {
        const node = document.createElement('div');
        node.className = 'ui-empty';
        node.textContent = text;
        return node;
    }

    function badge(text, cls) {
        const node = document.createElement('span');
        node.className = 'ui-badge ' + (cls || 'ui-badge--line');
        node.textContent = text;
        return node;
    }

    function mkBtn(text, handler, opts) {
        opts = opts || {};
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'ui-btn ui-btn--sm '
            + (opts.primary ? 'ui-btn--primary' : opts.outline ? 'ui-btn--outline' : 'ui-btn--ghost')
            + (opts.danger ? ' ui-btn--danger' : '');
        button.textContent = text;
        button.addEventListener('click', handler);
        return button;
    }

    function platformName(platform) {
        return CHANNEL_NAMES[platform] || platform || '';
    }

    function stateLabel(state) { return STATE_LABELS[state] || state || ''; }

    /* 渠道连接状态与服务配置状态必须分开呈现：
       「待扫码」不等于「未配置服务」，两类问题对应不同的修复入口 */
    function connectionLabel(bot) {
        if (bot.is_blocked) return '已停用';
        if (bot.platform === 'qq') {
            return bot.state === 'pending_bind' ? '待绑定' : '已绑定';
        }
        if (bot.platform === 'wechat') {
            if (bot.state === 'online') return '在线';
            if (bot.state === 'pending_qr') return '等待扫码';
            return '离线';
        }
        return '已就绪';
    }

    function stateClass(state) {
        if (state === 'blocked') return 'ui-badge--err';
        if (state === 'online' || state === 'ready') return 'ui-badge--ok';
        return 'ui-badge--warn';
    }

    function formatDate(iso) {
        if (!iso) return '';
        const date = new Date(iso);
        if (isNaN(date.getTime())) return '';
        return date.toLocaleString('zh-CN', { hour12: false });
    }

    /* ── 数据加载与概览 ───────────────────────────── */
    async function fetchAll() {
        const [botRes, svcRes, profRes, identityRes] = await Promise.all([
            window.WebAuth.call('/api/web/bots'),
            window.WebAuth.call('/api/web/services'),
            window.WebAuth.call('/api/web/profiles'),
            window.WebAuth.call('/api/web/identities')
        ]);
        if (!botRes.ok) {
            show('err', botRes.detail || '渠道读取失败');
            return;
        }
        bots = botRes.data.items || [];
        quota = botRes.data.quota || { limit: 0, bots: 0, services: 0 };
        canCreateBot = !!botRes.data.can_create_bot;
        canCreateService = !!botRes.data.can_create_service;
        services = svcRes.ok ? (svcRes.data.items || []) : [];
        profiles = profRes.ok ? (profRes.data.items || []) : [];
        identities = identityRes.ok ? (identityRes.data.items || []) : [];
        if (!svcRes.ok) show('err', svcRes.detail || '服务读取失败');
        if (!identityRes.ok) show('err', identityRes.detail || '身份读取失败');
        renderSummary();
        renderChannels();
        renderServices();
        renderIdentities();
        syncEditors();
    }

    function renderSummary() {
        const limit = quota.limit || 0;
        el('stat-services').textContent = limit > 0 ? quota.services + ' / ' + limit : '未开放';
        el('stat-bots').textContent = limit > 0 ? quota.bots + ' / ' + limit : '未开放';
        el('stat-identities').textContent = String(identities.length);
        el('stat-roles').textContent = String(profiles.length);
        el('tab-count-bots').textContent = bots.length ? '(' + bots.length + ')' : '';
        el('tab-count-services').textContent = services.length ? '(' + services.length + ')' : '';
        el('tab-count-identities').textContent = identities.length ? '(' + identities.length + ')' : '';
        el('tab-count-roles').textContent = profiles.length ? '(' + profiles.length + ')' : '';
        const botNew = el('bot-new-btn');
        if (botNew) botNew.hidden = !canCreateBot;
        const svcNew = el('svc-new-btn');
        if (svcNew) svcNew.hidden = !canCreateService;
    }

    function activeProfiles() {
        return profiles.filter(function (p) { return p.is_active; });
    }

    function fillProfileSelect(select, selectedId) {
        if (!select) return;
        clear(select);
        const usable = activeProfiles();
        usable.forEach(function (profile) {
            const option = document.createElement('option');
            option.value = profile.id;
            option.textContent = profile.name + (profile.owned ? '（自建）' : '（公共）');
            select.appendChild(option);
        });
        if (!usable.length) {
            const option = document.createElement('option');
            option.value = '';
            option.textContent = '暂无可用角色，请先到「角色」分区创建';
            select.appendChild(option);
            select.disabled = true;
            return;
        }
        select.disabled = false;
        if (selectedId) {
            select.value = selectedId;
            if (select.selectedIndex < 0) select.selectedIndex = 0;
        }
    }

    function fillBotSelect(select, selectedId, editing, editingSvc) {
        if (!select) return;
        clear(select);
        let found = false;
        bots.forEach(function (bot) {
            const option = document.createElement('option');
            option.value = bot.id;
            option.textContent = platformName(bot.platform) + ' · ' + bot.name;
            if (bot.is_blocked) {
                option.disabled = true;
                option.textContent += '（已停用）';
            } else if (bot.platform !== 'web' && bot.service_count > 0 && bot.id !== selectedId) {
                option.disabled = true;
                option.textContent += '（已有服务）';
            }
            select.appendChild(option);
            if (bot.id === selectedId) found = true;
        });
        if (editing && editingSvc && !found) {
            const option = document.createElement('option');
            option.value = editingSvc.bot_id;
            option.textContent = platformName(editingSvc.bot_platform)
                + ' · ' + (editingSvc.bot_name || '未命名渠道')
                + (editingSvc.bot_manageable === false ? '（管理员渠道）' : '');
            select.appendChild(option);
            found = true;
        }
        if (!select.options.length) {
            const option = document.createElement('option');
            option.value = '';
            option.textContent = '暂无可用渠道，请先添加渠道';
            select.appendChild(option);
        }
        if (selectedId && found) select.value = selectedId;
        select.disabled = !!editing;
    }

    function identityUnavailable(identity, bot) {
        if (identity.is_blocked) return '已停用';
        if (!bot || bot.platform === 'web') return '';
        if (identity.bound) return '已绑定平台账号';
        if (identity.platform && identity.platform !== bot.platform) return '平台不一致';
        if (identity.external_bot_id && identity.external_bot_id !== bot.id) return '已用于其他外部渠道';
        if (identity.external_platform && identity.external_platform !== bot.platform) return '已用于其他平台';
        return '';
    }

    function usableIdentities(bot) {
        return identities.filter(function (identity) {
            return !identityUnavailable(identity, bot);
        });
    }

    function fillIdentitySelect(select, bot, selectedId, editing) {
        if (!select) return;
        clear(select);
        identities.forEach(function (identity) {
            const reason = identityUnavailable(identity, bot);
            const option = document.createElement('option');
            option.value = identity.id;
            option.textContent = identity.name + (reason ? '（' + reason + '）' : '');
            option.disabled = !!reason;
            select.appendChild(option);
        });
        if (!identities.length) {
            const option = document.createElement('option');
            option.value = '';
            option.textContent = '暂无身份，可点击右侧「新建身份」';
            select.appendChild(option);
        }
        if (selectedId) select.value = selectedId;
        if (!selectedId || select.selectedIndex < 0) {
            const first = usableIdentities(bot)[0];
            if (first) select.value = first.id;
        }
        select.disabled = !!editing;
    }

    function syncEditors() {
        const svcEditor = el('service-editor');
        if (svcEditor && !svcEditor.hidden) {
            const currentProfile = el('svc-profile').value;
            const currentBot = el('svc-bot').value;
            fillProfileSelect(el('svc-profile'), currentProfile);
            fillBotSelect(
                el('svc-bot'),
                editingService ? editingService.bot_id : currentBot,
                !!editingService,
                editingService
            );
            const selectedBot = bots.find(function (bot) { return bot.id === el('svc-bot').value; });
            fillIdentitySelect(
                el('svc-identity'),
                selectedBot,
                editingService ? editingService.identity_id : el('svc-identity').value,
                !!editingService
            );
            const saveBtn = el('svc-save-btn');
            if (saveBtn) {
                saveBtn.disabled = !activeProfiles().length
                    || (!editingService && (
                        !availableBotsForService() || !usableIdentities(selectedBot).length
                    ));
            }
        }
    }

    /* ── Tab 切换 ─────────────────────────────────── */
    const TAB_NAMES = ['channels', 'services', 'identities', 'roles'];

    function tabFromHash() {
        const name = (location.hash || '').replace('#', '');
        return TAB_NAMES.indexOf(name) >= 0 ? name : 'channels';
    }

    function activateTab(name, updateHash) {
        document.querySelectorAll('[data-svc-tab]').forEach(function (button) {
            const active = button.dataset.svcTab === name;
            button.classList.toggle('active', active);
            button.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        document.querySelectorAll('[data-svc-panel]').forEach(function (panel) {
            panel.classList.toggle('active', panel.dataset.svcPanel === name);
        });
        if (updateHash) {
            history.replaceState(null, '', '#' + name);
        }
    }

    /* ── 渠道列表 ─────────────────────────────────── */
    function renderChannels() {
        const list = el('channel-list');
        clear(list);
        if (!bots.length) {
            list.appendChild(empty(
                canCreateBot
                    ? '还没有渠道。点击「+ 添加渠道」接入 QQ、微信或网页。'
                    : '还没有渠道，管理员未开放自助添加。'
            ));
            return;
        }
        bots.forEach(function (bot) {
            const configured = bot.service_count > 0;
            const card = document.createElement('div');
            card.className = 'channel-card';
            card.dataset.botId = bot.id;
            card.dataset.serviceState = configured ? 'configured' : 'empty';

            const head = document.createElement('div');
            head.className = 'channel-head';
            head.appendChild(badge(platformName(bot.platform), 'ui-badge--line'));
            const name = document.createElement('strong');
            name.className = 'channel-name';
            name.textContent = bot.name;
            head.appendChild(name);
            const conn = badge('连接：' + connectionLabel(bot), stateClass(bot.state));
            conn.title = '连接状态';
            head.appendChild(conn);
            const svcState = badge(
                configured ? '已配置 ' + bot.service_count + ' 个服务' : '尚未配置服务',
                configured ? 'ui-badge--ok' : 'ui-badge--warn'
            );
            svcState.title = '服务配置状态';
            head.appendChild(svcState);
            card.appendChild(head);

            const meta = document.createElement('div');
            meta.className = 'channel-meta u-muted u-fs-82';
            const parts = [];
            if (bot.service_count && bot.service_names && bot.service_names.length) {
                parts.push('服务：' + bot.service_names.join('、'));
            }
            if (bot.platform === 'wechat' && bot.token_expire_at) {
                parts.push('登录有效期至 ' + formatDate(bot.token_expire_at));
            }
            if (bot.is_blocked && bot.block_reason) parts.push('停用原因：' + bot.block_reason);
            meta.textContent = parts.join(' · ');
            if (meta.textContent) card.appendChild(meta);

            const actions = document.createElement('div');
            actions.className = 'channel-actions';
            const qrPanel = document.createElement('div');
            qrPanel.className = 'qr-panel';
            qrPanel.hidden = true;

            if (bot.is_blocked) {
                const note = document.createElement('span');
                note.className = 'ui-help';
                note.textContent = '该渠道已被管理员停用，请联系管理员。';
                actions.appendChild(note);
            } else {
                if (bot.platform === 'qq') {
                    actions.appendChild(mkBtn(
                        bot.state === 'pending_bind' ? '扫码绑定' : '重新绑定',
                        function () { startQqBind(bot, qrPanel); },
                        { primary: true }
                    ));
                } else if (bot.platform === 'wechat') {
                    actions.appendChild(mkBtn(
                        bot.state === 'online' ? '重新登录' : '扫码登录',
                        function () { startWechatLogin(bot, qrPanel); },
                        { primary: true }
                    ));
                    if (bot.state === 'online') {
                        actions.appendChild(mkBtn('退出登录', function () { logoutWechat(bot); }));
                    }
                }
                if (canCreateService && (bot.platform === 'web' || bot.service_count === 0)) {
                    actions.appendChild(mkBtn(
                        configured ? '添加服务' : '配置服务',
                        function () { openServiceEditor(null, bot.id); },
                        { outline: true, primary: !configured }
                    ));
                } else if (!configured) {
                    const note = document.createElement('span');
                    note.className = 'ui-help';
                    note.textContent = '尚未配置服务，自助创建暂未开放。';
                    actions.appendChild(note);
                }
                actions.appendChild(mkBtn('编辑', function () { openBotEdit(bot); }));
                actions.appendChild(mkBtn('删除', function () { removeBot(bot); }, { danger: true }));
            }

            card.appendChild(actions);
            card.appendChild(qrPanel);
            list.appendChild(card);
        });
    }

    async function removeBot(bot) {
        const suffix = bot.service_count
            ? '其下 ' + bot.service_count + ' 个服务会一并删除，对话历史保留，服务记忆将清除。'
            : '此操作不可恢复。';
        if (!window.confirm('确定删除渠道「' + bot.name + '」？' + suffix)) return;
        const result = await window.WebAuth.call(
            '/api/web/bots/' + encodeURIComponent(bot.id),
            { method: 'DELETE' }
        );
        if (!result.ok) { show('err', result.detail || '删除失败'); return; }
        show('ok', '渠道已删除。');
        await fetchAll();
    }

    async function logoutWechat(bot) {
        if (!window.confirm('退出渠道「' + bot.name + '」的微信登录？退出后需要重新扫码才能接收消息。')) return;
        const result = await window.WebAuth.call(
            '/api/web/bots/' + encodeURIComponent(bot.id) + '/wechat/logout',
            { method: 'POST' }
        );
        if (!result.ok) { show('err', result.detail || '退出登录失败'); return; }
        show('ok', '已退出微信登录。');
        await fetchAll();
    }

    /* ── QQ 扫码绑定 ──────────────────────────────── */
    async function startQqBind(bot, panel) {
        qrToken += 1;
        const token = qrToken;
        panel.hidden = false;
        clear(panel);
        const status = document.createElement('p');
        status.className = 'ui-help';
        status.textContent = '正在获取二维码…';
        panel.appendChild(status);

        let result;
        try {
            result = await window.WebAuth.call(
                '/api/web/bots/' + encodeURIComponent(bot.id) + '/qq/qrcode',
                { method: 'POST' }
            );
        } catch (e) {
            status.textContent = '获取二维码失败，请稍后重试';
            return;
        }
        if (token !== qrToken) return;
        if (!result.ok) { status.textContent = result.detail || '获取二维码失败'; return; }

        clear(panel);
        if (result.data.image) {
            const image = document.createElement('img');
            image.className = 'qr-image';
            image.alt = 'QQ 绑定二维码';
            image.src = result.data.image;
            panel.appendChild(image);
        }
        status.textContent = result.data.message || '请使用手机QQ扫码';
        panel.appendChild(status);
        panel.appendChild(mkBtn('关闭', function () {
            qrToken += 1;
            panel.hidden = true;
            clear(panel);
        }));

        let attempts = 0;
        async function poll() {
            if (token !== qrToken) return;
            attempts += 1;
            if (attempts > 80) {
                status.textContent = '二维码已过期，请重新获取';
                return;
            }
            let polled;
            try {
                polled = await window.WebAuth.call(
                    '/api/web/bots/' + encodeURIComponent(bot.id) + '/qq/poll-qr'
                );
            } catch (e) {
                status.textContent = '轮询失败，请稍后重试';
                return;
            }
            if (token !== qrToken) return;
            if (!polled.ok) {
                if (polled.status === 429) {
                    status.textContent = '请求过于频繁，稍后自动重试…';
                    setTimeout(poll, 6000);
                    return;
                }
                status.textContent = polled.detail || '轮询失败';
                return;
            }
            const state = polled.data.status;
            if (state === 'waiting') { setTimeout(poll, 3000); return; }
            if (state === 'rate_limited') {
                status.textContent = (polled.data.message || '平台限流') + '，稍后自动重试…';
                setTimeout(poll, 6000);
                return;
            }
            if (state === 'success') {
                status.textContent = '绑定成功，渠道将在约 30 秒内上线。';
                show('ok', 'QQ 渠道绑定成功，正在等待上线。');
                setTimeout(function () { if (token === qrToken) fetchAll(); }, 800);
                return;
            }
            status.textContent = polled.data.message || '扫码失败，请重新获取二维码';
        }
        setTimeout(poll, 2500);
    }

    /* ── 微信扫码登录 ──────────────────────────────── */
    async function startWechatLogin(bot, panel) {
        qrToken += 1;
        const token = qrToken;
        panel.hidden = false;
        clear(panel);
        const status = document.createElement('p');
        status.className = 'ui-help';
        status.textContent = '正在获取登录二维码…';
        panel.appendChild(status);

        let result;
        try {
            result = await window.WebAuth.call(
                '/api/web/bots/' + encodeURIComponent(bot.id) + '/wechat/qrcode',
                { method: 'POST' }
            );
        } catch (e) {
            status.textContent = '获取二维码失败，请稍后重试';
            return;
        }
        if (token !== qrToken) return;
        if (!result.ok) { status.textContent = result.detail || '获取二维码失败'; return; }

        const qrcode = result.data.qrcode || '';
        clear(panel);
        if (result.data.image_url) {
            const image = document.createElement('img');
            image.className = 'qr-image';
            image.alt = '微信登录二维码';
            image.src = result.data.image_url;
            panel.appendChild(image);
        }
        status.textContent = qrcode ? '请使用微信扫码登录' : '二维码获取异常，请重新获取';
        panel.appendChild(status);
        panel.appendChild(mkBtn('关闭', function () {
            qrToken += 1;
            panel.hidden = true;
            clear(panel);
        }));
        if (!qrcode) return;

        let attempts = 0;
        async function poll(verifyCode) {
            if (token !== qrToken) return;
            attempts += 1;
            if (attempts > 90) {
                status.textContent = '二维码已过期，请重新获取';
                return;
            }
            let polled;
            try {
                let url = '/api/web/bots/' + encodeURIComponent(bot.id)
                    + '/wechat/poll-qr?qrcode=' + encodeURIComponent(qrcode);
                if (verifyCode) url += '&verify_code=' + encodeURIComponent(verifyCode);
                polled = await window.WebAuth.call(url);
            } catch (e) {
                status.textContent = '轮询失败，请稍后重试';
                return;
            }
            if (token !== qrToken) return;
            if (!polled.ok) {
                if (polled.status === 429) {
                    status.textContent = '请求过于频繁，稍后自动重试…';
                    setTimeout(poll, 6000);
                    return;
                }
                status.textContent = polled.detail || '轮询失败';
                return;
            }
            const state = polled.data.status;
            if (state === 'wait' || state === 'waiting' || state === 'scaned'
                || state === 'scaned_but_redirect') {
                status.textContent = polled.data.message || '等待微信确认…';
                setTimeout(poll, 2000);
                return;
            }
            if (state === 'need_verifycode') {
                const code = window.prompt(polled.data.message || '请输入微信中显示的验证码');
                if (code && code.trim()) { poll(code.trim()); return; }
                status.textContent = '需要验证码才能继续，请重新获取二维码后再试';
                return;
            }
            if (state === 'success') {
                status.textContent = '登录成功，渠道已上线。';
                show('ok', '微信渠道已登录上线。');
                setTimeout(function () { if (token === qrToken) fetchAll(); }, 800);
                return;
            }
            status.textContent = polled.data.message || '扫码失败，请重新获取二维码';
        }
        setTimeout(poll, 2000);
    }

    /* ── 渠道创建 / 编辑 ───────────────────────────── */
    function setBotPlatform(platform) {
        botPlatform = platform;
        document.querySelectorAll('[data-bot-platform]').forEach(function (button) {
            button.classList.toggle('active', button.dataset.botPlatform === platform);
        });
        const qqFields = el('qq-fields');
        if (qqFields) qqFields.hidden = platform !== 'qq';
        const nameInput = el('bot-name');
        if (nameInput) {
            nameInput.placeholder = platform === 'qq'
                ? '例如：QQ 工作助手'
                : platform === 'wechat' ? '例如：微信助手' : '例如：网页助手';
        }
        syncEditors();
    }

    function openBotCreate() {
        editingBot = null;
        el('bot-edit').hidden = true;
        el('bot-editor').hidden = false;
        el('bot-name').value = '';
        el('bot-sandbox').checked = true;
        setBotPlatform('qq');
        el('bot-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
        el('bot-name').focus();
    }

    async function createBot() {
        const name = el('bot-name').value.trim();
        if (!name) { show('err', '请填写渠道名称'); return; }

        const payload = {
            platform: botPlatform,
            name: name,
            sandbox_mode: el('bot-sandbox').checked
        };
        const button = el('bot-create-btn');
        button.disabled = true;
        let result;
        try {
            result = await window.WebAuth.call('/api/web/bots', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
        } finally {
            button.disabled = false;
        }
        if (!result.ok) { show('err', result.detail || '创建渠道失败'); return; }

        const created = result.data.bot;
        el('bot-editor').hidden = true;
        await fetchAll();
        if (!created) return;
        show('ok', '渠道已创建，可继续扫码接入或为它配置服务。');
        if (created.platform === 'qq' || created.platform === 'wechat') {
            const card = document.querySelector('.channel-card[data-bot-id="' + created.id + '"]');
            const panel = card ? card.querySelector('.qr-panel') : null;
            if (panel) {
                if (created.platform === 'qq') startQqBind(created, panel);
                else startWechatLogin(created, panel);
            }
        }
    }

    function openBotEdit(bot) {
        editingBot = bot;
        el('bot-editor').hidden = true;
        el('bot-edit').hidden = false;
        el('bot-edit-name').value = bot.name;
        el('bot-edit-sandbox').value = bot.sandbox_mode ? 'true' : 'false';
        const isQq = bot.platform === 'qq';
        el('bot-edit-qq').hidden = !isQq;
        const title = el('bot-edit').querySelector('.ui-card-title');
        if (title) title.textContent = '编辑渠道：' + bot.name;
        el('bot-edit').scrollIntoView({ behavior: 'smooth', block: 'start' });
        el('bot-edit-name').focus();
    }

    async function updateBot() {
        if (!editingBot) return;
        const name = el('bot-edit-name').value.trim();
        if (!name) { show('err', '请填写渠道名称'); return; }
        const payload = { name: name };
        if (editingBot.platform === 'qq') {
            payload.sandbox_mode = el('bot-edit-sandbox').value === 'true';
        }
        const button = el('bot-save-btn');
        button.disabled = true;
        let result;
        try {
            result = await window.WebAuth.call(
                '/api/web/bots/' + encodeURIComponent(editingBot.id),
                {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }
            );
        } finally {
            button.disabled = false;
        }
        if (!result.ok) { show('err', result.detail || '保存失败'); return; }
        el('bot-edit').hidden = true;
        editingBot = null;
        show('ok', '渠道已更新。');
        await fetchAll();
    }

    /* ── 服务列表 ─────────────────────────────────── */
    function renderServices() {
        const list = el('service-list');
        clear(list);
        if (!services.length) {
            list.appendChild(empty(
                canCreateService
                    ? '还没有服务。先添加渠道与身份，再点击「+ 添加服务」显式装配。'
                    : '还没有服务，管理员未开放自助创建。'
            ));
            return;
        }
        services.forEach(function (svc) {
            const row = document.createElement('div');
            row.className = 'ui-list-row svc-row';

            const grow = document.createElement('div');
            grow.className = 'ui-stack ui-stack--tight ui-cluster--fill svc-row-main';
            const name = document.createElement('div');
            name.className = 'u-fw-600';
            name.textContent = svc.name;
            const meta = document.createElement('div');
            meta.className = 'u-muted u-fs-82';
            const parts = [
                '扮演：' + (svc.profile_name || '（角色已失效）'),
                '身份：' + (svc.identity_name || '（身份已失效）'),
                '渠道：' + platformName(svc.bot_platform) + ' · ' + (svc.bot_name || '未命名渠道')
            ];
            if (svc.bot_manageable === false) parts.push('管理员渠道');
            if (svc.bot_state && svc.bot_state !== 'ready' && svc.bot_state !== 'online') {
                parts.push('渠道' + stateLabel(svc.bot_state));
            }
            meta.textContent = parts.join(' · ');
            grow.appendChild(name);
            grow.appendChild(meta);
            row.appendChild(grow);

            const status = document.createElement('div');
            status.className = 'svc-row-status';
            if (svc.current) status.appendChild(badge('当前对话', 'ui-badge--ink'));
            if (svc.bot_platform) status.appendChild(badge(platformName(svc.bot_platform), 'ui-badge--line'));
            status.appendChild(badge(svc.is_active ? '启用中' : '已停用', svc.is_active ? 'ui-badge--ok' : 'ui-badge--err'));
            row.appendChild(status);

            const actions = document.createElement('div');
            actions.className = 'svc-row-actions';
            actions.appendChild(mkBtn('编辑', function () { openServiceEditor(svc); }));
            actions.appendChild(mkBtn(
                svc.is_active ? '停用' : '启用',
                function () { toggleService(svc); }
            ));
            actions.appendChild(mkBtn('删除', function () { removeService(svc); }, { danger: true }));
            row.appendChild(actions);
            list.appendChild(row);
        });
    }

    function availableBotsForService() {
        return bots.some(function (bot) {
            return !bot.is_blocked && (bot.platform === 'web' || bot.service_count === 0);
        });
    }

    function openServiceEditor(svc, presetBotId) {
        editingService = svc || null;
        el('bot-editor').hidden = true;
        el('bot-edit').hidden = true;
        el('service-editor').hidden = false;
        resetQuickIdentity();

        fillProfileSelect(el('svc-profile'), svc ? svc.profile_id : '');
        fillBotSelect(
            el('svc-bot'),
            svc ? svc.bot_id : (presetBotId || ''),
            !!svc,
            svc
        );
        const selectedBot = bots.find(function (bot) {
            return bot.id === el('svc-bot').value;
        });
        fillIdentitySelect(
            el('svc-identity'), selectedBot, svc ? svc.identity_id : '', !!svc
        );
        el('svc-name').value = svc ? svc.name : '';

        const title = el('service-editor').querySelector('.ui-card-title');
        if (title) title.textContent = svc ? '编辑服务：' + svc.name : '新建服务';

        // 编辑态不能换身份，隐藏「新建身份」避免误解
        const newIdentityBtn = el('svc-identity-new-btn');
        if (newIdentityBtn) newIdentityBtn.hidden = !!svc;

        const hasProfiles = activeProfiles().length > 0;
        const canPickBot = !!svc || availableBotsForService();
        const canPickIdentity = !!svc || usableIdentities(selectedBot).length > 0;
        const saveBtn = el('svc-save-btn');
        saveBtn.disabled = !hasProfiles || !canPickBot || !canPickIdentity;
        el('svc-bot-help').textContent = svc
            ? '服务创建后不可更换渠道；如需更换请删除后重建。'
            : canPickBot
                ? 'QQ / 微信渠道已有服务时不可再选。'
                : '暂无可用渠道，请先在「渠道」分区添加。';

        el('service-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
        el('svc-name').focus();
    }

    async function saveService() {
        const profileId = el('svc-profile').value;
        if (!profileId) { show('err', '请选择扮演的角色'); return; }
        const payload = {
            profile_id: profileId,
            name: el('svc-name').value.trim()
        };
        let result;
        if (editingService) {
            result = await window.WebAuth.call(
                '/api/web/services/' + encodeURIComponent(editingService.id),
                {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }
            );
        } else {
            const botId = el('svc-bot').value;
            const identityId = el('svc-identity').value;
            if (!botId) { show('err', '请选择渠道'); return; }
            if (!identityId) { show('err', '请选择身份'); return; }
            payload.bot_id = botId;
            payload.identity_id = identityId;
            result = await window.WebAuth.call('/api/web/services', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
        }
        if (!result.ok) { show('err', result.detail || '保存失败'); return; }
        el('service-editor').hidden = true;
        editingService = null;
        show('ok', '服务已保存，回到对话页即可切换。');
        await fetchAll();
    }

    async function toggleService(svc) {
        const result = await window.WebAuth.call(
            '/api/web/services/' + encodeURIComponent(svc.id),
            {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_active: !svc.is_active })
            }
        );
        if (!result.ok) { show('err', result.detail || '操作失败'); return; }
        await fetchAll();
    }

    async function removeService(svc) {
        if (!window.confirm('确定删除服务「' + svc.name + '」？对话历史保留，服务记忆将清除。')) return;
        const result = await window.WebAuth.call(
            '/api/web/services/' + encodeURIComponent(svc.id),
            { method: 'DELETE' }
        );
        if (!result.ok) { show('err', result.detail || '删除失败'); return; }
        show('ok', '服务已删除。');
        await fetchAll();
    }

    /* ── 身份管理 ─────────────────────────────────── */
    let editingIdentity = null;

    function bindingLabel(identity) {
        if (identity.bound) {
            return '已绑定' + platformName(identity.external_platform || identity.platform);
        }
        return '未绑定外部平台';
    }

    function renderIdentities() {
        const list = el('identity-list');
        if (!list) return;
        clear(list);
        if (!identities.length) {
            list.appendChild(empty('还没有身份。点击「+ 新建身份」创建，身份可用于任意渠道的服务。'));
            return;
        }
        identities.forEach(function (identity) {
            const row = document.createElement('div');
            row.className = 'ui-list-row svc-row';
            row.dataset.identityId = identity.id;

            const grow = document.createElement('div');
            grow.className = 'ui-stack ui-stack--tight ui-cluster--fill svc-row-main';
            const name = document.createElement('div');
            name.className = 'u-fw-600';
            name.textContent = identity.name;
            grow.appendChild(name);
            const meta = document.createElement('div');
            meta.className = 'u-muted u-fs-82';
            const parts = [];
            if (identity.nickname && identity.nickname !== identity.name) {
                parts.push('昵称：' + identity.nickname);
            }
            parts.push(bindingLabel(identity));
            if (identity.self_description) parts.push(identity.self_description);
            meta.textContent = parts.join(' · ');
            grow.appendChild(meta);
            row.appendChild(grow);

            const status = document.createElement('div');
            status.className = 'svc-row-status';
            status.appendChild(badge(
                identity.service_count ? identity.service_count + ' 个服务' : '未用于服务',
                identity.service_count ? 'ui-badge--line' : 'ui-badge--warn'
            ));
            if (identity.is_minor) status.appendChild(badge('未成年人', 'ui-badge--warn'));
            if (identity.is_blocked) status.appendChild(badge('已停用', 'ui-badge--err'));
            row.appendChild(status);

            const actions = document.createElement('div');
            actions.className = 'svc-row-actions';
            actions.appendChild(mkBtn('编辑', function () { openIdentityEditor(identity); }));
            actions.appendChild(mkBtn('删除', function () { removeIdentity(identity); }, { danger: true }));
            row.appendChild(actions);
            list.appendChild(row);
        });
    }

    function resetQuickIdentity() {
        const panel = el('identity-quick');
        if (!panel) return;
        panel.hidden = true;
        el('quick-identity-name').value = '';
        el('quick-identity-nickname').value = '';
        el('quick-identity-desc').value = '';
        el('quick-identity-minor').checked = false;
    }

    function openIdentityEditor(identity) {
        editingIdentity = identity || null;
        el('identity-editor').hidden = false;
        el('identity-name').value = identity ? identity.name : '';
        el('identity-nickname').value = identity ? identity.nickname : '';
        el('identity-desc').value = identity ? (identity.self_description || '') : '';
        el('identity-minor').checked = identity ? !!identity.is_minor : false;
        const title = el('identity-editor').querySelector('.ui-card-title');
        if (title) title.textContent = identity ? '编辑身份：' + identity.name : '新建身份';
        el('identity-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
        el('identity-name').focus();
    }

    async function saveIdentity() {
        const name = el('identity-name').value.trim();
        if (!name) { show('err', '请填写身份名称'); return; }
        const payload = {
            name: name,
            nickname: el('identity-nickname').value.trim(),
            self_description: el('identity-desc').value.trim(),
            is_minor: el('identity-minor').checked
        };
        const button = el('identity-save-btn');
        button.disabled = true;
        let result;
        try {
            if (editingIdentity) {
                result = await window.WebAuth.call(
                    '/api/web/identities/' + encodeURIComponent(editingIdentity.id),
                    {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }
                );
            } else {
                result = await window.WebAuth.call('/api/web/identities', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
            }
        } finally {
            button.disabled = false;
        }
        if (!result.ok) { show('err', result.detail || '保存身份失败'); return; }
        el('identity-editor').hidden = true;
        editingIdentity = null;
        show('ok', '身份已保存。');
        await fetchAll();
    }

    async function removeIdentity(identity) {
        const referenced = identity.service_count
            ? '该身份仍被 ' + identity.service_count + ' 个服务使用，需先删除或改绑这些服务。'
            : '其对话历史与主动消息记录会一并清理，且不可恢复。';
        if (!window.confirm(
            '确定删除身份「' + identity.name + '」？\n'
            + referenced + '\n该操作不会注销网页登录账号。'
        )) return;
        const result = await window.WebAuth.call(
            '/api/web/identities/' + encodeURIComponent(identity.id),
            { method: 'DELETE' }
        );
        if (!result.ok) { show('err', result.detail || '删除身份失败'); return; }
        show('ok', '身份已删除。');
        await fetchAll();
    }

    async function quickCreateIdentity() {
        const name = el('quick-identity-name').value.trim();
        if (!name) { show('err', '请填写身份名称'); return; }
        const button = el('quick-identity-create-btn');
        button.disabled = true;
        let result;
        try {
            result = await window.WebAuth.call('/api/web/identities', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name,
                    nickname: el('quick-identity-nickname').value.trim(),
                    self_description: el('quick-identity-desc').value.trim(),
                    is_minor: el('quick-identity-minor').checked
                })
            });
        } finally {
            button.disabled = false;
        }
        if (!result.ok) { show('err', result.detail || '创建身份失败'); return; }
        resetQuickIdentity();
        await fetchAll();
        const select = el('svc-identity');
        if (select) {
            select.value = result.data.id;
            if (select.selectedIndex < 0) {
                const option = document.createElement('option');
                option.value = result.data.id;
                option.textContent = result.data.name;
                select.appendChild(option);
                select.value = result.data.id;
            }
        }
        show('ok', '身份已创建并选中，继续完成服务创建即可。');
    }

    /* ── 事件绑定与首屏 ───────────────────────────── */
    document.querySelectorAll('[data-svc-tab]').forEach(function (button) {
        button.addEventListener('click', function () {
            activateTab(button.dataset.svcTab, true);
        });
    });
    window.addEventListener('hashchange', function () {
        activateTab(tabFromHash(), false);
    });

    document.querySelectorAll('[data-bot-platform]').forEach(function (button) {
        button.addEventListener('click', function () {
            setBotPlatform(button.dataset.botPlatform);
        });
    });

    el('bot-new-btn').addEventListener('click', openBotCreate);
    el('bot-cancel-btn').addEventListener('click', function () {
        el('bot-editor').hidden = true;
    });
    el('bot-create-btn').addEventListener('click', createBot);
    el('bot-save-btn').addEventListener('click', updateBot);
    el('bot-edit-cancel-btn').addEventListener('click', function () {
        el('bot-edit').hidden = true;
        editingBot = null;
    });
    el('svc-new-btn').addEventListener('click', function () { openServiceEditor(null); });
    el('svc-cancel-btn').addEventListener('click', function () {
        el('service-editor').hidden = true;
        editingService = null;
        resetQuickIdentity();
    });
    el('svc-save-btn').addEventListener('click', saveService);
    el('svc-bot').addEventListener('change', syncEditors);
    el('svc-identity-new-btn').addEventListener('click', function () {
        const panel = el('identity-quick');
        panel.hidden = !panel.hidden;
        if (!panel.hidden) el('quick-identity-name').focus();
    });
    el('quick-identity-create-btn').addEventListener('click', quickCreateIdentity);
    el('quick-identity-cancel-btn').addEventListener('click', resetQuickIdentity);
    el('identity-new-btn').addEventListener('click', function () { openIdentityEditor(null); });
    el('identity-save-btn').addEventListener('click', saveIdentity);
    el('identity-cancel-btn').addEventListener('click', function () {
        el('identity-editor').hidden = true;
        editingIdentity = null;
    });

    window.addEventListener('web-roles-updated', function () {
        fetchAll().catch(function (e) {
            console.error('聊天服务刷新失败', e);
        });
    });

    document.addEventListener('DOMContentLoaded', function () {
        activateTab(tabFromHash(), false);
        fetchAll().catch(function (e) {
            console.error('聊天服务加载失败', e);
            show('err', '聊天服务读取失败，请稍后重试');
        });
    });
})();
