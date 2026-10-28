/* C 端会话页：额度、服务切换、SSE 流式对话、历史会话回看。
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
    var micBtn = document.getElementById('mic-btn');
    var voiceCancel = document.getElementById('voice-cancel');
    var composerHintText = document.getElementById('composer-hint-text');
    var quotaInfo = document.getElementById('quota-info');
    var loadingNote = document.getElementById('loading-note');
    var serviceSelect = document.getElementById('service-select');
    var channelNote = document.getElementById('partner-channel');
    var sessionList = document.getElementById('session-list');
    var sessionRail = document.getElementById('session-rail');
    var banner = document.getElementById('browse-banner');
    var STREAM_URL = (composer && composer.dataset.streamEndpoint) || '/api/conversations/chat/stream';
    var VOICE_URL = (composer && composer.dataset.voiceEndpoint) || '/api/conversations/chat/voice/stream';
    var VOICE_INPUT_ENABLED = !!(composer && composer.dataset.voiceInput === '1');
    var VOICE_REPLY_ENABLED = !!(composer && composer.dataset.voiceReply === '1');
    var MAX_VOICE_SECONDS = 60;

    var streaming = false;
    var hasService = false;   // 零服务是合法状态：为 false 时禁用发送、语音与输入
    var browsing = null;      // 正在回看的历史会话 ID；null 表示处于当前对话
    var abort = null;         // 进行中的流请求，便于中止
    var recording = false;
    var voiceRecorder = null;
    var voiceStream = null;
    var voiceChunks = [];
    var voiceTimer = null;
    var voiceStartedAt = 0;
    var voiceCancelled = false;

    function authHeader(extra) {
        return window.WebAuth.authHeader(extra);
    }

    function toLogin() { window.WebAuth.toLogin(); }

    /* 气泡一律走 textContent：模型输出与对方输入都不能被当作 HTML 解析 */
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

    /* 媒体地址只接受服务端能力 URL（/api/media/...）；本地录音气泡额外放行 blob:。
       绝不把外部地址塞进 img/audio.src，也不用 HTML 注入渲染。 */
    function safeMediaUrl(url, allowBlob) {
        if (typeof url !== 'string' || !url) return '';
        if (url.indexOf('\n') >= 0 || url.indexOf('\r') >= 0) return '';
        if (isMediaPath(url)) return url;
        if (allowBlob && url.indexOf('blob:') === 0) return url;
        return '';
    }

    function isMediaPath(url) {
        return url.indexOf('/api/media/') === 0;
    }

    function addMediaBubble(role, kind, url, name, caption, allowBlob) {
        var safe = safeMediaUrl(url, allowBlob);
        if (!safe) return null;
        dropEmpty();
        var div = document.createElement('div');
        div.className = 'bubble ' + (role === 'user' ? 'bubble-user' : 'bubble-assistant') + ' bubble-media';
        if (role !== 'user' && name) {
            var label = document.createElement('span');
            label.className = 'bubble-name';
            label.textContent = name;
            div.appendChild(label);
        }
        if (kind === 'audio') {
            var audioLabel = document.createElement('span');
            audioLabel.className = 'bubble-media-label';
            audioLabel.textContent = role === 'user' ? '语音消息' : '语音回复';
            div.appendChild(audioLabel);
            var audio = document.createElement('audio');
            audio.className = 'bubble-audio';
            audio.controls = true;
            audio.preload = 'metadata';
            audio.src = safe;
            div.appendChild(audio);
        } else {
            var img = document.createElement('img');
            img.className = 'bubble-image' + (kind === 'sticker' ? ' is-sticker' : '');
            img.src = safe;
            img.alt = kind === 'sticker' ? '表情包' : '图片';
            img.loading = 'lazy';
            img.addEventListener('error', function () {
                img.replaceWith(document.createTextNode('（媒体加载失败）'));
            });
            div.appendChild(img);
        }
        if (caption) {
            var cap = document.createElement('span');
            cap.className = 'bubble-caption';
            cap.textContent = caption;
            div.appendChild(cap);
        }
        streamBox.appendChild(div);
        scrollToEnd();
        return div;
    }

    function setBubbleCaption(node, text) {
        if (!node) return;
        var cap = node.querySelector('.bubble-caption');
        if (!cap) {
            cap = document.createElement('span');
            cap.className = 'bubble-caption';
            node.appendChild(cap);
        }
        cap.textContent = text || '';
    }

    function playAudioIn(node) {
        if (!node) return;
        var audio = node.querySelector('audio');
        if (!audio) return;
        var promise = audio.play();
        if (promise && promise.catch) promise.catch(function () {});
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
    var MEDIA_FRAME = '[MEDIA]';
    var USER_TEXT_FRAME = '[USER_TEXT]';
    var MEDIA_KINDS = { image: true, sticker: true, audio: true };

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

    /* 服务（对话服务）列表：只有一个时锁死选择器，多个时供账号切换角色。
       返回账号是否存在可用的对话服务；零服务时由 boot 走空态引导。 */
    async function loadServices() {
        var result = await window.WebAuth.call('/api/web/services');
        if (!result.ok) {
            serviceSelect.disabled = true;
            hasService = false;
            return false;
        }
        var items = ((result.data || {}).items || []).filter(function (s) { return s.is_active; });
        hasService = items.length > 0;
        serviceSelect.textContent = '';
        if (items.length <= 1) {
            var one = items[0];
            var only = document.createElement('option');
            only.value = one ? one.id : '';
            only.textContent = one ? one.profile_name + channelSuffix(one.bot_platform) : '还没有对话服务';
            if (one) {
                only.dataset.label = one.profile_name;
                only.dataset.platform = one.bot_platform || '';
            }
            serviceSelect.appendChild(only);
            serviceSelect.disabled = true;
            if (one) { try { localStorage.setItem(SERVICE_KEY, one.id); } catch (e) {} }
            updateChannelNote();
            return hasService;
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
        return hasService;
    }

    /* 零服务空态：明确告诉用户原因，并给出创建服务的入口 */
    function renderZeroServiceState() {
        var cta = document.getElementById('empty-service-cta');
        if (cta) cta.hidden = false;
        var note = document.getElementById('no-service-note');
        if (note) note.hidden = false;
        var emptyText = document.getElementById('transcript-empty-text');
        if (emptyText) emptyText.textContent = '还没有对话服务。创建服务后即可开始对话。';
        input.disabled = true;
        sendBtn.disabled = true;
        if (micBtn) micBtn.disabled = true;
        if (composerHintText) composerHintText.textContent = '还没有可用的对话服务，请先创建服务';
    }

    /* 单服务时下拉被禁用，仍必须带上 service_id：否则历史与发送会退化成"按身份"口径 */
    function currentServiceId() {
        return serviceSelect.value || '';
    }

    /* 历史会话侧栏：后端按服务归组返回会话，点击即只读回看该会话消息 */
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
            var contentType = m.content_type || 'text';
            var mediaUrl = m.media_url || '';
            if (mediaUrl && (contentType === 'voice' || contentType === 'image')) {
                var kind = contentType === 'voice'
                    ? 'audio'
                    : (String(m.content || '').indexOf('表情') >= 0 ? 'sticker' : 'image');
                var caption = '';
                if (contentType === 'voice' && role === 'user') {
                    caption = m.content || '';
                } else if (
                    contentType === 'image'
                    && m.content
                    && m.content.charAt(0) !== '('
                    && m.content.indexOf('[对方发送了') !== 0
                ) {
                    caption = m.content;
                }
                var mediaNode = addMediaBubble(
                    role, kind, mediaUrl,
                    role === 'assistant' ? partnerLabel() : '',
                    caption, false
                );
                if (!mediaNode) {
                    addBubble(role, m.content || m.text || '', '', partnerLabel());
                }
            } else {
                addBubble(role, m.content || m.text || '', '', partnerLabel());
            }
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
        sendBtn.textContent = on ? '停止' : '发送';
        // 流式中保留"停止"可点；录音中或零服务时发送按钮禁用
        sendBtn.disabled = recording || !hasService;
        input.disabled = on || recording || !hasService;
        if (micBtn) micBtn.disabled = on || !hasService;
        if (!on && !recording && hasService) input.focus();
    }

    async function consumeSse(resp, onPayload) {
        var reader = resp.body.getReader();
        var decoder = new TextDecoder('utf-8');
        var buffer = '';
        while (true) {
            var chunk = await reader.read();
            if (chunk.done) break;
            // 网络分片可能把一帧切断，残片留在 buffer 等下一批
            buffer += decoder.decode(chunk.value, { stream: true });
            var consumed = consumeFrames(buffer);
            buffer = consumed.rest;
            consumed.frames.forEach(function (frame) {
                payloadOf(frame).forEach(onPayload);
            });
        }
    }

    function newStreamState(bubble, extras, userBubble) {
        return {
            bubble: bubble,
            extras: extras,
            reply: '',
            current: bubble,
            paced: false,   // 收到过 [MSG] 即进入闸门模式（服务端控气泡边界与节奏）
            opened: false,  // 本轮首条气泡是否已经开过（[RESET] 后归零）
            hasMedia: false,
            userBubble: userBubble || null
        };
    }

    function handleMediaFrame(rest) {
        var sep = rest.indexOf('|');
        if (sep <= 0) return;
        var kind = rest.slice(0, sep);
        var url = rest.slice(sep + 1);
        if (!MEDIA_KINDS[kind]) return;
        var node = addMediaBubble('assistant', kind, url, partnerLabel(), '', false);
        if (node && kind === 'audio') playAudioIn(node);
    }

    function handleStreamPayload(payload, state) {
        if (payload === '[DONE]') return;
        if (payload === RESET_FRAME) {
            resetTurn(state.bubble, state.extras);
            state.current = state.bubble;
            state.opened = false;
            state.reply = '';
            return;
        }
        if (payload.indexOf('[ERROR]') === 0) {
            dropExtras(state.extras);
            state.bubble.classList.remove('typing');
            state.bubble.classList.add('bubble-error');
            state.bubble.lastChild.textContent = payload.slice(7).trim() || '对话服务返回异常。';
            return;
        }
        if (payload.indexOf(USER_TEXT_FRAME) === 0) {
            setBubbleCaption(state.userBubble, payload.slice(USER_TEXT_FRAME.length));
            return;
        }
        if (payload.indexOf(MEDIA_FRAME) === 0) {
            state.hasMedia = true;
            handleMediaFrame(payload.slice(MEDIA_FRAME.length));
            return;
        }
        if (payload === MSG_FRAME) {
            state.paced = true;
            state.current = openBubble(state.bubble, state.extras, state.opened);
            state.opened = true;
            return;
        }
        state.reply += payload;
        if (state.paced) appendInto(state.current, payload);
        else renderReply(state.bubble, state.extras, state.reply, false);
    }

    function finishStream(state) {
        state.bubble.classList.remove('typing');
        if (!state.paced) renderReply(state.bubble, state.extras, state.reply, true);
        if (!state.reply && !state.bubble.classList.contains('bubble-error')) {
            if (state.hasMedia) {
                // 只有媒体没有文字：空气泡没有存在意义，删掉只留图片/语音气泡
                dropExtras(state.extras);
                state.bubble.remove();
            } else {
                state.bubble.lastChild.textContent = '（本次没有收到回复，可能是额度已用尽）';
            }
        } else if (!state.bubble.classList.contains('bubble-error')) {
            addFeedbackActions(state.bubble);
        }
    }

    function addFeedbackActions(bubble) {
        var serviceId = currentServiceId();
        if (!serviceId || !bubble) return;
        var bar = document.createElement('div');
        bar.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;font-size:12px;opacity:.78';
        var hint = document.createElement('span');
        hint.textContent = '像本人吗？';
        bar.appendChild(hint);

        function postFeedback(rating, reasons, edited) {
            return fetch('/api/web/clone/feedback', {
                method: 'POST',
                headers: authHeader({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({
                    service_id: serviceId, rating: rating,
                    reasons: reasons || [], edited_reply: edited || ''
                })
            }).then(function (resp) {
                hint.textContent = resp.ok ? '已记录，谢谢反馈' : '暂时无法记录';
                bar.replaceChildren(hint);
            }).catch(function () {
                hint.textContent = '暂时无法记录';
                bar.replaceChildren(hint);
            });
        }

        function showDislikeOptions() {
            bar.replaceChildren(hint);
            hint.textContent = '哪里不像？';
            var selected = [];
            var reasons = [
                ['tone', '口吻'], ['length', '长短'],
                ['memory', '记忆'], ['facts', '事实']
            ];
            var reasonWrap = document.createElement('span');
            reasonWrap.style.cssText = 'display:inline-flex;gap:6px';
            reasons.forEach(function (item) {
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.textContent = item[1];
                btn.style.cssText = 'border:1px solid currentColor;border-radius:999px;background:transparent;color:inherit;cursor:pointer;padding:1px 7px';
                btn.addEventListener('click', function () {
                    var idx = selected.indexOf(item[0]);
                    if (idx >= 0) selected.splice(idx, 1); else selected.push(item[0]);
                    btn.style.fontWeight = idx >= 0 ? '400' : '600';
                });
                reasonWrap.appendChild(btn);
            });
            bar.appendChild(reasonWrap);
            var edit = document.createElement('input');
            edit.type = 'text';
            edit.maxLength = 300;
            edit.placeholder = '可选：更像本人会怎么说';
            edit.style.cssText = 'flex:1 1 180px;min-width:140px;border:1px solid rgba(127,127,127,.35);border-radius:8px;background:transparent;color:inherit;padding:3px 8px;font-size:12px';
            bar.appendChild(edit);
            var submit = document.createElement('button');
            submit.type = 'button';
            submit.textContent = '提交';
            submit.style.cssText = 'border:0;background:transparent;color:inherit;cursor:pointer;padding:0;font-weight:600';
            submit.addEventListener('click', function () {
                postFeedback('dislike', selected, edit.value.trim());
            });
            bar.appendChild(submit);
        }

        var like = document.createElement('button');
        like.type = 'button';
        like.textContent = '像';
        like.style.cssText = 'border:0;background:transparent;color:inherit;cursor:pointer;padding:0';
        like.addEventListener('click', function () { postFeedback('like', [], ''); });
        var dislike = document.createElement('button');
        dislike.type = 'button';
        dislike.textContent = '不像';
        dislike.style.cssText = 'border:0;background:transparent;color:inherit;cursor:pointer;padding:0';
        dislike.addEventListener('click', showDislikeOptions);
        var actions = document.createElement('span');
        actions.style.cssText = 'display:inline-flex;gap:6px';
        actions.appendChild(like);
        actions.appendChild(dislike);
        bar.appendChild(actions);
        bubble.appendChild(bar);
    }

    async function send() {
        // "停止"按钮也走表单提交，必须先处理中止，否则空输入会提前 return
        if (streaming && abort) { abort.abort(); return; }
        if (!hasService) { renderZeroServiceState(); return; }
        var text = input.value.trim();
        if (!text || recording) return;
        if (browsing) await resumeCurrent();

        abort = new AbortController();
        setStreaming(true);
        addBubble('user', text);
        input.value = '';
        autoGrow();
        var name = partnerLabel();
        var bubble = addBubble('assistant', '', 'typing', name);
        var extras = [];
        var state = newStreamState(bubble, extras, null);
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
                dropExtras(state.extras);
                state.bubble.classList.remove('typing');
                state.bubble.classList.add('bubble-error');
                state.bubble.lastChild.textContent = data.detail || '发送失败，请稍后重试。';
                return;
            }
            await consumeSse(resp, function (payload) {
                handleStreamPayload(payload, state);
            });
            finishStream(state);
            try {
                await loadMe();       // 回复落库后再刷新余量，额度显示才跟手
                await loadSessions(); // 会话计数与时间同步更新
            } catch (e) {}
        } catch (err) {
            dropExtras(state.extras);
            state.bubble.classList.remove('typing');
            state.bubble.classList.add('bubble-error');
            state.bubble.lastChild.textContent = err && err.name === 'AbortError'
                ? '（已中止，回复可能不完整）'
                : '网络异常，回复可能不完整，请重试。';
        } finally {
            abort = null;
            setStreaming(false);
        }
    }

    function pickVoiceMimeType() {
        if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
        var types = [
            'audio/webm;codecs=opus',
            'audio/webm',
            'audio/mp4',
            'audio/ogg;codecs=opus',
            'audio/ogg'
        ];
        for (var i = 0; i < types.length; i += 1) {
            if (MediaRecorder.isTypeSupported(types[i])) return types[i];
        }
        return '';
    }

    function releaseVoiceStream() {
        if (!voiceStream) return;
        voiceStream.getTracks().forEach(function (track) { track.stop(); });
        voiceStream = null;
    }

    function updateRecordingUi(on) {
        recording = on;
        if (micBtn) {
            micBtn.classList.toggle('is-recording', on);
            micBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
            micBtn.title = on
                ? '停止并发送'
                : (VOICE_REPLY_ENABLED ? '录音发送语音（AI 会语音回复）' : '录音发送语音');
        }
        if (voiceCancel) voiceCancel.hidden = !on;
        if (on) {
            sendBtn.disabled = true;
            input.disabled = true;
            if (composerHintText) {
                composerHintText.textContent = '录音中 0:00 / 1:00 · 再点麦克风发送';
            }
        } else {
            if (composerHintText) {
                composerHintText.textContent = 'Enter 发送 · Shift + Enter 换行';
            }
            setStreaming(streaming);
        }
    }

    function updateVoiceTimer() {
        if (!recording) return;
        var seconds = Math.floor((Date.now() - voiceStartedAt) / 1000);
        if (seconds >= MAX_VOICE_SECONDS) {
            stopRecording(false);
            return;
        }
        if (composerHintText) {
            composerHintText.textContent = '录音中 0:'
                + String(seconds).padStart(2, '0')
                + ' / 1:00 · 再点麦克风发送';
        }
    }

    async function startRecording() {
        if (streaming || recording) return;
        if (!hasService) { renderZeroServiceState(); return; }
        if (browsing) await resumeCurrent();
        try {
            voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        } catch (err) {
            addNote('无法使用麦克风，请检查浏览器权限或改用文字输入。');
            return;
        }
        var mime = pickVoiceMimeType();
        try {
            voiceRecorder = mime
                ? new MediaRecorder(voiceStream, { mimeType: mime })
                : new MediaRecorder(voiceStream);
        } catch (err) {
            releaseVoiceStream();
            addNote('当前浏览器不支持录音，请改用文字输入。');
            return;
        }
        voiceChunks = [];
        voiceCancelled = false;
        voiceRecorder.ondataavailable = function (event) {
            if (event.data && event.data.size > 0) voiceChunks.push(event.data);
        };
        voiceRecorder.onstop = finalizeRecording;
        try {
            voiceRecorder.start();
        } catch (err) {
            releaseVoiceStream();
            voiceRecorder = null;
            addNote('录音启动失败，请重试或改用文字输入。');
            return;
        }
        voiceStartedAt = Date.now();
        updateRecordingUi(true);
        voiceTimer = setInterval(updateVoiceTimer, 250);
    }

    function stopRecording(cancel) {
        voiceCancelled = !!cancel;
        if (voiceTimer) {
            clearInterval(voiceTimer);
            voiceTimer = null;
        }
        if (voiceRecorder && voiceRecorder.state !== 'inactive') {
            voiceRecorder.stop();
        } else {
            releaseVoiceStream();
            updateRecordingUi(false);
        }
    }

    function finalizeRecording() {
        if (voiceTimer) {
            clearInterval(voiceTimer);
            voiceTimer = null;
        }
        releaseVoiceStream();
        updateRecordingUi(false);
        var cancelled = voiceCancelled;
        voiceCancelled = false;
        var chunks = voiceChunks;
        voiceChunks = [];
        var recorder = voiceRecorder;
        voiceRecorder = null;
        if (cancelled) return;
        var type = (recorder && recorder.mimeType) || 'audio/webm';
        var blob = new Blob(chunks, { type: type });
        if (!blob.size) {
            addNote('没有录到声音，请重试。');
            return;
        }
        sendVoice(blob);
    }

    async function sendVoice(blob) {
        if (streaming && abort) { abort.abort(); return; }
        if (!hasService) { renderZeroServiceState(); return; }
        if (browsing) await resumeCurrent();

        var localUrl = URL.createObjectURL(blob);
        var userBubble = addMediaBubble(
            'user', 'audio', localUrl, '', '识别中…', true
        );
        abort = new AbortController();
        setStreaming(true);
        var name = partnerLabel();
        var bubble = addBubble('assistant', '', 'typing', name);
        var extras = [];
        var state = newStreamState(bubble, extras, userBubble);
        var form = new FormData();
        var ext = 'webm';
        if (blob.type.indexOf('mp4') >= 0) ext = 'm4a';
        else if (blob.type.indexOf('ogg') >= 0) ext = 'ogg';
        form.append('audio', blob, 'voice.' + ext);
        form.append('service_id', currentServiceId());
        try {
            var resp = await fetch(VOICE_URL, {
                method: 'POST',
                headers: authHeader(),
                body: form,
                signal: abort.signal
            });
            if (resp.status === 401) { toLogin(); return; }
            if (!resp.ok) {
                var data = await resp.json().catch(function () { return {}; });
                setBubbleCaption(userBubble, '发送失败');
                dropExtras(state.extras);
                state.bubble.classList.remove('typing');
                state.bubble.classList.add('bubble-error');
                state.bubble.lastChild.textContent = data.detail || '语音发送失败，请重试。';
                return;
            }
            await consumeSse(resp, function (payload) {
                handleStreamPayload(payload, state);
            });
            finishStream(state);
            try {
                await loadMe();
                await loadSessions();
            } catch (e) {}
        } catch (err) {
            setBubbleCaption(userBubble, '发送失败');
            dropExtras(state.extras);
            state.bubble.classList.remove('typing');
            state.bubble.classList.add('bubble-error');
            state.bubble.lastChild.textContent = err && err.name === 'AbortError'
                ? '（已中止，回复可能不完整）'
                : '网络异常，语音回复可能不完整，请重试。';
        } finally {
            abort = null;
            setStreaming(false);
        }
    }

    function setupVoice() {
        if (!micBtn) return;
        if (
            !VOICE_INPUT_ENABLED
            || !window.MediaRecorder
            || !navigator.mediaDevices
            || !navigator.mediaDevices.getUserMedia
        ) {
            micBtn.hidden = true;
            return;
        }
        micBtn.hidden = false;
        micBtn.title = VOICE_REPLY_ENABLED
            ? '录音发送语音（AI 会语音回复）'
            : '录音发送语音';
        micBtn.addEventListener('click', function () {
            if (recording) stopRecording(false);
            else startRecording();
        });
        if (voiceCancel) {
            voiceCancel.addEventListener('click', function () {
                stopRecording(true);
            });
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
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && recording) {
            event.preventDefault();
            stopRecording(true);
        }
    });
    window.addEventListener('beforeunload', releaseVoiceStream);
    serviceSelect.addEventListener('change', function () {
        try { localStorage.setItem(SERVICE_KEY, serviceSelect.value); } catch (e) {}
        var platform = selectedPlatform();
        var shared = platform && platform !== 'web';
        addNote(
            '已切换为「' + partnerLabel() + '」' +
            (shared ? '，该服务的对话与记忆和' + channelName(platform) + '渠道共享。'
                    : '，各服务的对话与记忆相互独立。')
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
        setStreaming(false);
        if (hasService) {
            addNote('对话已就绪，直接输入即可开始。');
            sendBtn.disabled = false;
            input.disabled = false;
            setupVoice();
            input.focus();
        } else {
            renderZeroServiceState();
        }
        await loadSessions();
    })().catch(function () {
        quotaInfo.textContent = '账号信息读取失败';
        if (loadingNote) loadingNote.textContent = '账号信息读取失败，请检查网络或重新登录。';
    });
})();
