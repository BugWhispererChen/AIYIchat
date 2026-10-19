/* C 端登录页：验证码取图、表单提交、令牌落库后跳转会话页。 */
(function () {
    'use strict';

    var form = document.getElementById('login-form');
    if (!form) return;

    var CAPTCHA_ENABLED = form.dataset.captchaEnabled === 'true';
    var TOKEN_KEY = document.body.dataset.tokenKey || 'aiyichat_web_token';

    var alertBox = document.getElementById('form-alert');
    var captchaField = document.getElementById('captcha-field');
    var captchaImg = document.getElementById('captcha-img');
    var captchaAnswer = document.getElementById('captcha-answer');
    var submitBtn = document.getElementById('submit-btn');
    var captchaId = '';

    function showAlert(text) {
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

    async function submitLogin(event) {
        event.preventDefault();
        clearAlert();

        var payload = {
            username: document.getElementById('username').value.trim(),
            password: document.getElementById('password').value,
            captcha_id: captchaId,
            captcha_answer: captchaAnswer.value.trim()
        };

        submitBtn.disabled = true;
        submitBtn.textContent = '登录中…';
        try {
            var resp = await fetch('/api/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            var data = await resp.json().catch(function () { return {}; });
            if (!resp.ok) {
                // 服务端一次性消费验证码（答对答错都失效），失败后必须重新取码
                if (CAPTCHA_ENABLED) {
                    captchaAnswer.value = '';
                    await loadCaptcha();
                }
                showAlert(data.detail || '登录失败，请检查用户名与密码。');
                return;
            }
            localStorage.setItem(TOKEN_KEY, data.token);
            window.location.href = '/web/chat';
        } catch (err) {
            showAlert('网络异常，登录未完成，请稍后重试。');
            if (CAPTCHA_ENABLED) await loadCaptcha();
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = '登录';
        }
    }

    form.addEventListener('submit', submitLogin);
    if (captchaImg) captchaImg.addEventListener('click', loadCaptcha);
    if (captchaField) captchaField.hidden = !CAPTCHA_ENABLED;

    loadCaptcha();
})();
