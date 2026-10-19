/* C 端注册页：密码强度即时反馈、验证码、合规勾选与服务端提交。
 * 开关由模板通过 form[data-*] 注入，前端不再内联渲染任何逻辑。 */
(function () {
    'use strict';

    var form = document.getElementById('register-form');
    if (!form) return;

    // 服务端子开关随模板注入：关闭时前端不再取码/不再强求答案，但表单结构保持一致
    var CAPTCHA_ENABLED = form.dataset.captchaEnabled === 'true';
    var REGISTER_ENABLED = form.dataset.registerEnabled === 'true';

    // 常见弱密码本地黑名单（与服务端词表同源思路）：仅用于即时提示，服务端仍会复核
    var COMMON_PASSWORDS = [
        'password', 'password1', 'password123', 'passw0rd', 'p@ssw0rd', 'qwerty123',
        '12345678', '123456789', '1234567890', '1234567', '11111111', '00000000',
        'abc123456', 'abcd1234', 'abcdef123', 'a12345678', '12345678a', 'iloveyou',
        'letmein123', 'welcome123', 'admin123', '1q2w3e4r', 'qazwsx123', '123abc456',
        'secret123', 'master123', 'trustno1', 'asdf1234', 'zxcvbnm123'
    ];

    var alertBox = document.getElementById('form-alert');
    var successPanel = document.getElementById('success-panel');
    var captchaImg = document.getElementById('captcha-img');
    var captchaAnswer = document.getElementById('captcha-answer');
    var captchaField = document.getElementById('captcha-field');
    var submitBtn = document.getElementById('submit-btn');
    var captchaId = '';

    function showAlert(text) {
        successPanel.hidden = true;
        alertBox.textContent = text;
        alertBox.hidden = false;
    }

    function clearAlert() {
        alertBox.hidden = true;
        alertBox.textContent = '';
    }

    async function loadCaptcha() {
        if (!CAPTCHA_ENABLED) return;
        try {
            var resp = await fetch('/api/auth/captcha');
            if (!resp.ok) throw new Error('验证码获取失败，请稍后重试');
            var data = await resp.json();
            captchaId = data.captcha_id;
            captchaImg.src = data.image;
        } catch (err) {
            showAlert(err.message || '验证码获取失败，请稍后重试');
        }
    }

    /* 强度分级与后端规则一致（长度 8-64、含字母与数字、规避常见密码）：
       弱=未过基线；中=过基线；强=基线 + 长度≥12 且含大写或符号 */
    function evaluatePassword() {
        var pwd = document.getElementById('password').value;
        var user = document.getElementById('username').value.trim().toLowerCase();
        var fill = document.getElementById('strength-fill');
        var level = document.getElementById('strength-level');
        var tip = document.getElementById('password-tip');
        var lower = pwd.toLowerCase();
        var hasLetter = /[a-zA-Z]/.test(pwd);
        var hasDigit = /[0-9]/.test(pwd);
        var hasSymbol = /[^a-zA-Z0-9]/.test(pwd);
        var baseline = pwd.length >= 8 && pwd.length <= 64 && hasLetter && hasDigit
            && !COMMON_PASSWORDS.includes(lower)
            && !(user.length > 0 && pwd.toLowerCase().includes(user));

        var grade = '—';
        var width = '0';
        var cls = '';
        if (!pwd) {
            tip.textContent = '密码长度需为 8-64 位，且同时包含字母和数字。';
        } else if (!baseline) {
            grade = '弱'; width = '33%'; cls = 'lvl-weak';
            tip.textContent = pwd.length < 8
                ? '至少 8 位，并同时包含字母和数字。'
                : '该密码过于常见或包含用户名，请更换。';
        } else if (pwd.length >= 12 && (hasSymbol || /[A-Z]/.test(pwd))) {
            grade = '强'; width = '100%'; cls = 'lvl-strong';
            tip.textContent = '强度良好。建议再启用大小写与符号混排。';
        } else {
            grade = '中'; width = '66%'; cls = 'lvl-mid';
            tip.textContent = '可以用，但加长或混用符号会更安全。';
        }
        fill.style.width = width;
        fill.className = 'strength-fill' + (cls ? ' ' + cls : '');
        level.textContent = grade;
        level.className = cls;
        return { pwd: pwd, baseline: baseline };
    }

    async function submitRegister(event) {
        event.preventDefault();
        clearAlert();

        var username = document.getElementById('username').value.trim();
        var password = document.getElementById('password').value;
        var confirm = document.getElementById('password-confirm').value;
        if (password !== confirm) {
            showAlert('两次输入的密码不一致。');
            return;
        }
        if (!evaluatePassword().baseline) {
            showAlert('密码不符合要求：长度需为 8-64 位，同时包含字母和数字，且不能使用常见弱密码。');
            return;
        }
        if (!document.getElementById('age-confirmed').checked) {
            showAlert('未满 14 周岁不得注册，请确认年龄后再提交。');
            return;
        }
        if (!document.getElementById('terms-accepted').checked
            || !document.getElementById('privacy-accepted').checked) {
            showAlert('请先阅读并勾选《身份协议》与《隐私政策》。');
            return;
        }

        var payload = {
            username: username,
            password: password,
            terms_accepted: document.getElementById('terms-accepted').checked,
            privacy_accepted: document.getElementById('privacy-accepted').checked,
            age_confirmed: document.getElementById('age-confirmed').checked,
            captcha_id: captchaId,
            captcha_answer: captchaAnswer.value.trim()
        };

        submitBtn.disabled = true;
        submitBtn.textContent = '提交中…';
        try {
            var resp = await fetch('/api/auth/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            var data = await resp.json().catch(function () { return {}; });
            if (!resp.ok) {
                // 服务端无论答对答错都一次性消费验证码，失败后必须重新取码
                if (CAPTCHA_ENABLED) {
                    captchaAnswer.value = '';
                    await loadCaptcha();
                }
                var fallback = resp.status === 429 ? '操作过于频繁，请稍后再试。' : '注册失败，请稍后重试。';
                showAlert(data.detail || fallback);
                return;
            }
            // 成功文案来自服务端 detail（已含"等待审核/请登录"分支），补一个登录入口
            form.hidden = true;
            successPanel.textContent = data.detail
                || (data.status === 'pending' ? '注册成功，等待管理员审核' : '注册成功，请登录');
            var loginLink = document.createElement('a');
            loginLink.href = '/web/login';
            loginLink.textContent = '前往登录';
            successPanel.appendChild(loginLink);
            successPanel.hidden = false;
            successPanel.scrollIntoView({ block: 'nearest' });
        } catch (err) {
            showAlert('网络异常，注册未提交，请检查连接后重试。');
            if (CAPTCHA_ENABLED) await loadCaptcha();
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = '注册';
        }
    }

    form.addEventListener('submit', submitRegister);
    document.getElementById('password').addEventListener('input', evaluatePassword);
    document.getElementById('username').addEventListener('input', evaluatePassword);
    if (captchaImg) captchaImg.addEventListener('click', loadCaptcha);
    if (captchaField) captchaField.hidden = !CAPTCHA_ENABLED;

    if (!REGISTER_ENABLED) {
        submitBtn.disabled = true;
        submitBtn.textContent = '暂未开放注册';
    }
    evaluatePassword();
    loadCaptcha();
})();
