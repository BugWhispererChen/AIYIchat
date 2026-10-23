/* C 端会话页：额度、伙伴切换、SSE 流式对话、历史会话回看。
 *
 * 与后端的约定全部来自 DOM 上的 data-* 属性：
 *   body[data-token-key]        令牌存储键
 *   form[data-stream-endpoint]  流式对话端点
 *   侧栏 /api/conversations     历史会话列表与消息分页
 */
(function () {
    'use strict';

    var SERVICE_KEY = 'aiyichat_web_service';
    var CHANNEL_NAMES = { qq: 'QQ', wechat: '微信', web: '网页' };

    var transcript = document.getElementById('transcript');
    var streamBox = document.getElementById('transcript-inner');
    var composer = document.getElementById('chat-form');
    var input = document.getElementById('chat-input');
    var sendBtn = document.getElementById('send-btn');
    var quotaInfo = document.getElementById('quota-info');
    var loadingNote = document.getElementById('loading-note');
    var serviceSelect = document.getElementById('service-select');
    var channelNote = document.getElementById('partner-channel');
    var sessionList = document.getElementById('session-list');
    var sessionRail = document.getElementById('session-rail');
    var banner = document.getElementById('browse-banner');
    var STREAM_URL = (composer && composer.dataset.streamEndpoint) || '/api/conversations/chat/stream';

    var streaming = false;
    var browsing = null;      // 正在回看的历史会话 ID；null 表示处于当前对话
    var abort = null;         // 进行中的流请求，便于中止

    function authHeader(extra) {
        return window.WebAuth.authHeader(extra);
    }

    function toLogin() { window.WebAuth.toLogin(); }

    /* 气泡一律走 textContent：模型输出与身份输入都不能被当作 HTML 解析 */
    function dropEmpty() {
        var hint = document.getElementById('transcript-empty');
        if (hint) hint.remove();
    }

    function addBubble(role, text, extraClass, name) {
        dropEmpty();
        var div = document.createElement('div');
        div.className = 'bubble ' + (role === 'user' ? 'bubble-user' : 'bubble-assistant')
            + (extraClass ? ' ' + extraClass : '');
        if (role !== 'user' && name) {
            var label = document.createElement('span');
            label.className = 'bubble-name';
            label.textContent = name;
            div.appendChild(label);
        }
        var body = document.createElement('span');
        body.textContent = text;
        div.appendChild(body);
        streamBox.appendChild(div);
        scrollToEnd();
        return div;
    }

    function addNote(text) {
        dropEmpty();
        var div = document.createElement('div');
        div.className = 'system-note';
        div.textContent = text;
        streamBox.appendChild(div);
        scrollToEnd();
        return div;
    }

    function scrollToEnd() {
        transcript.scrollTop = transcript.scrollHeight;
    }

    function clearStream() {
        streamBox.textContent = '';
    }

    /* 流式回复的气泡呈现有两条路，取决于后端有没有开启放行闸门：
       - 闸门开启：正文已在服务端按句清洗过，气泡边界由 [MSG] 控制帧给出，节奏也在服务端
         控好（下一条到达时正该出现），前端只做"开一条新气泡 + 往里追加"；
       - 闸门关闭（默认）：后端仍逐 token 下发未清洗增量，沿用原来的累计全文重绘 +
         本地按 ||| 拆气泡，保证老行为与老前端一致。
       两条路都保留一版是灰度期的必要代价：开关一旦回退，页面不能变成一坨带 ||| 的文本。 */
    var MSG_DELIM = '|||';
    var MSG_FRAME = '[MSG]';
    var RESET_FRAME = '[RESET]';

    function renderReply(bubble, extras, text, done) {
        if (!done) {
            // 网络分片可能把 ||| 切成 | 或 ||：尾部残缺分隔符先藏起来，避免闪烁
            text = text.replace(/\|{1,2}$/, '');
        }
        var parts = [];
        text.split(MSG_DELIM).forEach(function (piece) {
            var trimmed = piece.replace(/^\s+|\s+$/g, '');
            if (trimmed) parts.push(trimmed);
        });
        for (var i = 0; i < parts.length; i += 1) {
            var node = i === 0 ? bubble : extras[i - 1];
            if (!node) {
                // 同一条回复拆出的后续气泡不再重复署名：真人连发也不会每句报一次名字
                node = addBubble('assistant', '', '', '');
                extras.push(node);
            }
            node.lastChild.textContent = parts[i];
        }
        scrollToEnd();
    }

    /* 闸门模式：往当前气泡里追加已清洗文本（绝不重绘整段，也不注入 HTML） */
    function appendInto(node, text) {
        node.lastChild.textContent += text;
        scrollToEnd();
    }

    /* 开一条新气泡：本轮首条复用带"正在输入"占位的那条，后续只追加不署名。
       opened 标记由调用方持有，重置本轮时一并归零。 */
    function openBubble(bubble, extras, opened) {
        if (!opened) {
            bubble.classList.remove('typing');
            return bubble;
        }
        var node = addBubble('assistant', '', '', '');
        extras.push(node);
        scrollToEnd();
        return node;
    }

    /* 出错/中止时收回已拆出的后续气泡：首条会被改写为错误说明，留下半截回复只会更乱 */
    function dropExtras(extras) {
        extras.forEach(function (node) { node.remove(); });
        extras.length = 0;
    }

    /* 复读命中后服务端下发 [RESET]：整轮气泡收回重来，避免"半截复读"留在屏幕上 */
    function resetTurn(bubble, extras) {
        dropExtras(extras);
        bubble.lastChild.textContent = '';
        bubble.classList.add('typing');
    }

    function partnerLabel() {
        if (!serviceSelect.options.length) return '';
        var index = serviceSelect.selectedIndex;
        if (index < 0) return '';
        var opt = serviceSelect.options[index];
        return opt.dataset.label || opt.textContent;
    }

    function selectedPlatform() {
        var opt = serviceSelect.options[serviceSelect.selectedIndex];
        return (opt && opt.dataset.platform) || '';
    }

    function channelName(platform) {
        return CHANNEL_NAMES[platform] || '';
    }

    /* 网页渠道不标注，减少纯网页用户的视觉噪音 */
    function channelSuffix(platform) {
        return platform && platform !== 'web' ? ' · ' + channelName(platform) : '';
    }

    function updateChannelNote() {
        if (!channelNote) return;
        var platform = selectedPlatform();
        if (platform && platform !== 'web') {
            channelNote.textContent = '也由' + channelName(platform) + '渠道使用，对话记录共享';
            channelNote.hidden = false;
        } else {
            channelNote.textContent = '';
            channelNote.hidden = true;
        }
    }

    function relTime(iso) {
        var then = new Date(iso);
        if (isNaN(then.getTime())) return '';
        var seconds = Math.floor((Date.now() - then.getTime()) / 1000);
        if (seconds < 60) return '刚刚';
        if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟前';
        if (seconds < 86400) return Math.floor(seconds / 3600) + ' 小时前';
        if (seconds < 604800) return Math.floor(seconds / 86400) + ' 天前';
        return then.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
    }

    async function loadMe() {
        var result = await window.WebAuth.call('/api/auth/me');
        if (!result.ok) {
            quotaInfo.textContent = result.detail || '账号信息读取失败';
            return result.status === 401 ? null : null;
        }
        var data = result.data;
        var q = data.quota && data.quota.text;
        quotaInfo.textContent = q
            ? '剩余文本额度 ' + Math.max(q.limit - q.used, 0) + ' / ' + q.limit
            : '额度信息不可用';
        return data;
    }

    /* 伙伴（对话服务）列表：只有一个时锁死选择器，多个时供身份切换角色 */
    async function loadServices() {
        var result = await window.WebAuth.call('/api/web/services');
        if (!result.ok) {
            serviceSelect.disabled = true;
            return;
        }
        var items = ((result.data || {}).items || []).filter(function (s) { return s.is_active; });
        serviceSelect.textContent = '';
        if (items.length <= 1) {
            var one = items[0];
            var only = document.createElement('option');
            only.value = one ? one.id : '';
            only.textContent = one ? one.profile_name + channelSuffix(one.bot_platform) : '未配置伙伴';
            if (one) {
                only.dataset.label = one.profile_name;
                only.dataset.platform = one.bot_platform || '';
            }
            serviceSelect.appendChild(only);
            serviceSelect.disabled = true;
            if (one) { try { localStorage.setItem(SERVICE_KEY, one.id); } catch (e) {} }
            updateChannelNote();
            return;
        }
        var saved = '';
        try { saved = localStorage.getItem(SERVICE_KEY) || ''; } catch (e) {}
        var preferred = saved || result.data.current_service_id || items[0].id;
        items.forEach(function (s) {
            var opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = s.name + ' · ' + s.profile_name + channelSuffix(s.bot_platform);
            opt.dataset.label = s.name + ' · ' + s.profile_name;
            opt.dataset.platform = s.bot_platform || '';
            if (s.id === preferred) opt.selected = true;
            serviceSelect.appendChild(opt);
        });
        // saved 指向已删除的服务时回落首项，避免带着无效 ID 发请求
        if (serviceSelect.selectedIndex === -1) serviceSelect.selectedIndex = 0;
        serviceSelect.disabled = false;
        updateChannelNote();
    }

    /* 单伙伴时下拉被禁用，仍必须带上 service_id：否则历史与发送会退化成"按身份"口径 */
    function currentServiceId() {
        return serviceSelect.value || '';
    }

    /* 历史会话侧栏：后端按伙伴归组返回会话，点击即只读回看该会话消息 */
    async function loadSessions() {
        if (!sessionList) return;
        var query = currentServiceId() ? '?service_id=' + encodeURIComponent(currentServiceId()) : '';
        var result = await window.WebAuth.call('/api/conversations' + query);
        sessionList.textContent = '';
        if (!result.ok) return;
        var items = result.data || [];
        if (!items.length) {
            var tip = document.createElement('div');
            tip.className = 'session-empty';
            tip.textContent = '还没有历史对话';
            sessionList.appendChild(tip);
            return;
        }
        items.forEach(function (conv, index) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'session-item' + (index === 0 ? ' is-active' : '');
            btn.dataset.conversationId = conv.id;
            var title = document.createElement('span');
            title.className = 's-title';
            title.textContent = '对话 · ' + (conv.message_count || 0) + ' 条';
            var meta = document.createElement('span');
            meta.className = 's-meta';
            meta.textContent = relTime(conv.last_message_at);
            btn.appendChild(title);
            btn.appendChild(meta);
            btn.addEventListener('click', function () {
                Array.prototype.forEach.call(sessionList.children, function (el) {
                    el.classList && el.classList.remove('is-active');
                });
                btn.classList.add('is-active');
                viewConversation(conv.id);
                if (window.innerWidth <= 900 && sessionRail) sessionRail.classList.remove('is-open');
            });
            sessionList.appendChild(btn);
        });
    }

    async function viewConversation(id) {
        var result = await window.WebAuth.call('/api/conversations/' + encodeURIComponent(id) + '/messages?limit=80');
        if (!result.ok) { addNote(result.detail || '历史消息读取失败'); return; }
        var rows = Array.isArray(result.data) ? result.data : (result.data.messages || []);
        browsing = id;
        clearStream();
        addNote('正在回看历史对话（只读）');
        rows.forEach(function (m) {
            var role = m.role === 'user' ? 'user' : 'assistant';
            addBubble(role, m.content || m.text || '', '', partnerLabel());
        });
        if (banner) banner.hidden = false;
    }

    async function resumeCurrent() {
        browsing = null;
        if (banner) banner.hidden = true;
        clearStream();
        addNote('已回到当前对话，继续输入即可。');
        await loadSessions();
    }

    /* SSE 手工解析：EventSource 无法携带 Authorization 头，只能用 fetch + reader。
       服务端把 chunk 内的换行转义成字面量 \n，故需按帧拆分后反转义还原。 */
    function consumeFrames(buffer) {
        var frames = [];
        var sep;
        while ((sep = buffer.indexOf('\n\n')) >= 0) {
            frames.push(buffer.slice(0, sep));
            buffer = buffer.slice(sep + 2);
        }
        return { frames: frames, rest: buffer };
    }

    function payloadOf(frame) {
        var lines = frame.split('\n');
        var out = [];
        for (var i = 0; i < lines.length; i += 1) {
            var line = lines[i];
            if (line.indexOf('data:') !== 0) continue;   // 忽略注释/事件名等其他 SSE 字段
            out.push(line.slice(5).replace(/^ /, '').replace(/\\n/g, '\n'));
        }
        return out;
    }

    function setStreaming(on) {
        streaming = on;
        sendBtn.disabled = on;
        sendBtn.textContent = on ? '停止' : '发送';
        input.disabled = on;
        if (!on) input.focus();
    }

    async function send() {
        var text = input.value.trim();
        if (!text) return;
        if (browsing) await resumeCurrent();
        if (streaming && abort) { abort.abort(); return; }

        setStreaming(true);
        abort = new AbortController();
        addBubble('user', text);
        input.value = '';
        autoGrow();
        var name = partnerLabel();
        var bubble = addBubble('assistant', '', 'typing', name);
        var extras = [];
        var reply = '';
        var buffer = '';
        var current = bubble;
        var paced = false;   // 收到过 [MSG] 即进入闸门模式（服务端控气泡边界与节奏）
        var opened = false;  // 本轮首条气泡是否已经开过（[RESET] 后归零）
        try {
            var resp = await fetch(STREAM_URL, {
                method: 'POST',
                headers: authHeader({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ text: text, service_id: currentServiceId() }),
                signal: abort.signal
            });
            if (resp.status === 401) { toLogin(); return; }
            if (!resp.ok) {
                var data = await resp.json().catch(function () { return {}; });
                dropExtras(extras);
                bubble.classList.remove('typing');
                bubble.classList.add('bubble-error');
                bubble.lastChild.textContent = data.detail || '发送失败，请稍后重试。';
                return;
            }
            var reader = resp.body.getReader();
            var decoder = new TextDecoder('utf-8');
            while (true) {
                var chunk = await reader.read();
                if (chunk.done) break;
                // 网络分片可能把一帧切断，残片留在 buffer 等下一批
                buffer += decoder.decode(chunk.value, { stream: true });
                var consumed = consumeFrames(buffer);
                buffer = consumed.rest;
                consumed.frames.forEach(function (frame) {
                    payloadOf(frame).forEach(function (payload) {
                        if (payload === '[DONE]') return;
                        if (payload === RESET_FRAME) {
                            resetTurn(bubble, extras);
                            current = bubble;
                            opened = false;
                            reply = '';
                            return;
                        }
                        if (payload.indexOf('[ERROR]') === 0) {
                            dropExtras(extras);
                            bubble.classList.remove('typing');
                            bubble.classList.add('bubble-error');
                            bubble.lastChild.textContent = payload.slice(7).trim() || '对话服务返回异常。';
                            return;
                        }
                        if (payload === MSG_FRAME) {
                            paced = true;
                            current = openBubble(bubble, extras, opened);
                            opened = true;
                            return;
                        }
                        reply += payload;
                        if (paced) appendInto(current, payload);
                        else renderReply(bubble, extras, reply, false);
                    });
                });
            }
            bubble.classList.remove('typing');
            if (!paced) renderReply(bubble, extras, reply, true);
            if (!reply && !bubble.classList.contains('bubble-error')) {
                bubble.lastChild.textContent = '（本次没有收到回复，可能是额度已用尽）';
            }
            await loadMe();       // 回复落库后再刷新余量，额度显示才跟手
            await loadSessions(); // 会话计数与时间同步更新
        } catch (err) {
            dropExtras(extras);
            bubble.classList.remove('typing');
            bubble.classList.add('bubble-error');
            bubble.lastChild.textContent = err && err.name === 'AbortError'
                ? '（已中止，回复可能不完整）'
                : '网络异常，回复可能不完整，请重试。';
        } finally {
            abort = null;
            setStreaming(false);
        }
    }

    function autoGrow() {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 128) + 'px';
    }

    if (composer) composer.addEventListener('submit', function (e) { e.preventDefault(); send(); });
    input.addEventListener('input', autoGrow);
    input.addEventListener('keydown', function (event) {
        // 回车发送、Shift+回车换行：与常见聊天输入框习惯一致
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            send();
        }
    });
    serviceSelect.addEventListener('change', function () {
        try { localStorage.setItem(SERVICE_KEY, serviceSelect.value); } catch (e) {}
        var platform = selectedPlatform();
        var shared = platform && platform !== 'web';
        addNote(
            '已切换为「' + partnerLabel() + '」' +
            (shared ? '，该伙伴的对话与记忆和' + channelName(platform) + '渠道共享。'
                    : '，各伙伴的对话与记忆相互独立。')
        );
        updateChannelNote();
        loadSessions();
    });
    var toggle = document.getElementById('session-toggle');
    if (toggle && sessionRail) {
        toggle.addEventListener('click', function () { sessionRail.classList.toggle('is-open'); });
    }
    var backBtn = document.getElementById('browse-back');
    if (backBtn) backBtn.addEventListener('click', resumeCurrent);

    (async function boot() {
        var me = await loadMe();
        if (!me) {
            // 403 一类"账号可用但进不了会话"的情况：留在本页说明原因，不反复跳转
            if (loadingNote) loadingNote.textContent = '无法进入对话，请按页面提示处理或重新登录。';
            return;
        }
        if (loadingNote) loadingNote.remove();
        await loadServices();
        addNote('对话已就绪，直接输入即可开始。');
        setStreaming(false);
        sendBtn.disabled = false;
        input.disabled = false;
        input.focus();
        await loadSessions();
    })().catch(function () {
        quotaInfo.textContent = '账号信息读取失败';
        if (loadingNote) loadingNote.textContent = '账号信息读取失败，请检查网络或重新登录。';
    });
})();
