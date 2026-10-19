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

    var transcript = document.getElementById('transcript');
    var streamBox = document.getElementById('transcript-inner');
    var composer = document.getElementById('chat-form');
    var input = document.getElementById('chat-input');
    var sendBtn = document.getElementById('send-btn');
    var quotaInfo = document.getElementById('quota-info');
    var loadingNote = document.getElementById('loading-note');
    var serviceSelect = document.getElementById('service-select');
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
    function addBubble(role, text, extraClass, name) {
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

    function partnerLabel() {
        if (!serviceSelect.options.length) return '';
        var index = serviceSelect.selectedIndex;
        return index >= 0 ? serviceSelect.options[index].textContent : '';
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
            only.textContent = one ? one.profile_name : '未配置伙伴';
            serviceSelect.appendChild(only);
            serviceSelect.disabled = true;
            if (one) { try { localStorage.setItem(SERVICE_KEY, one.id); } catch (e) {} }
            return;
        }
        var saved = '';
        try { saved = localStorage.getItem(SERVICE_KEY) || ''; } catch (e) {}
        var preferred = saved || result.data.current_service_id || items[0].id;
        items.forEach(function (s) {
            var opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = s.name + ' · ' + s.profile_name;
            if (s.id === preferred) opt.selected = true;
            serviceSelect.appendChild(opt);
        });
        // saved 指向已删除的服务时回落首项，避免带着无效 ID 发请求
        if (serviceSelect.selectedIndex === -1) serviceSelect.selectedIndex = 0;
        serviceSelect.disabled = false;
    }

    function currentServiceId() {
        return serviceSelect.disabled ? '' : serviceSelect.value;
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
        var bubble = addBubble('assistant', '', 'typing', partnerLabel());
        var reply = '';
        var buffer = '';
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
                        if (payload.indexOf('[ERROR]') === 0) {
                            bubble.classList.remove('typing');
                            bubble.classList.add('bubble-error');
                            bubble.lastChild.textContent = payload.slice(7).trim() || '对话服务返回异常。';
                            return;
                        }
                        reply += payload;
                        bubble.lastChild.textContent = reply;
                        scrollToEnd();
                    });
                });
            }
            bubble.classList.remove('typing');
            if (!reply && !bubble.classList.contains('bubble-error')) {
                bubble.lastChild.textContent = '（本次没有收到回复，可能是额度已用尽）';
            }
            await loadMe();       // 回复落库后再刷新余量，额度显示才跟手
            await loadSessions(); // 会话计数与时间同步更新
        } catch (err) {
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
        addNote('已切换为「' + partnerLabel() + '」，各伙伴的对话与记忆相互独立。');
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
