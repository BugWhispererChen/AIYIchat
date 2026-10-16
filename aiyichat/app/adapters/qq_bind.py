"""QQ 开放平台扫码绑定协议模块。

实现 QQ 机器人“扫码接入”凭据绑定流程：
1. create_bind_task: 本地生成 AES-256 bind_key，创建绑定任务获取 task_id
2. 二维码内容指向平台 connect 页面，用户手机 QQ 扫码选择机器人
3. poll_bind_result: 轮询绑定结果，完成后解密得到 AppID/AppSecret

凭据下发使用 AES-256-GCM 加密（nonce 12B + 密文 + tag 16B），
解密密钥即创建任务时提交的 bind_key。
"""

import base64
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import httpx
from Crypto.Cipher import AES

logger = logging.getLogger(__name__)

# QQ 开放平台绑定服务
BIND_HOST = "q.qq.com"
CREATE_TASK_URL = f"https://{BIND_HOST}/lite/create_bind_task"
POLL_RESULT_URL = f"https://{BIND_HOST}/lite/poll_bind_result"
CONNECT_URL_TEMPLATE = (
    f"https://{BIND_HOST}/qqbot/openclaw/connect.html"
    "?task_id={task_id}&_wv=2"
)
API_TIMEOUT = 10.0

# 绑定状态（与平台侧定义一致）
STATUS_NONE = 0
STATUS_COMPLETED = 2
STATUS_EXPIRED = 3


@dataclass
class QQBindSession:
    """单次扫码绑定会话。"""

    task_id: str
    bind_key: str  # base64 AES-256 密钥，用于解密返回的 AppSecret
    qr_url: str  # 二维码内容 URL


def generate_bind_key() -> str:
    """生成 base64 编码的 AES-256 随机密钥。"""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def decrypt_app_secret(encrypted_secret: str, bind_key: str) -> str:
    """解密平台下发的 AppSecret。

    Args:
        encrypted_secret: base64 密文（12字节nonce + 密文 + 16字节tag）。
        bind_key: 创建绑定任务时提交的 base64 AES-256 密钥。

    Raises:
        ValueError: 密文格式异常或解密失败。
    """
    try:
        key = base64.b64decode(bind_key)
        raw = base64.b64decode(encrypted_secret)
    except Exception as exc:
        raise ValueError("QQ 机器人凭证解码失败") from exc
    if len(key) != 32 or len(raw) <= 28:
        raise ValueError("QQ 机器人凭证密文格式异常")

    nonce = raw[:12]
    tag = raw[-16:]
    ciphertext = raw[12:-16]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    try:
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
    except Exception as exc:
        raise ValueError("QQ 机器人凭证解密失败") from exc


async def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST JSON 并校验平台返回码。"""
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("QQ 机器人绑定接口响应格式异常")
    retcode = data.get("retcode")
    if retcode is not None:
        try:
            retcode_ok = int(retcode) == 0
        except (TypeError, ValueError):
            retcode_ok = False
        if not retcode_ok:
            message = (
                str(data.get("msg") or "").strip()
                or str(data.get("message") or "").strip()
                or "QQ 机器人绑定接口返回失败"
            )
            raise RuntimeError(message)
    return data


async def create_bind_task() -> QQBindSession:
    """创建扫码绑定任务，返回会话（含二维码内容）。"""
    bind_key = generate_bind_key()
    data = await _post_json(CREATE_TASK_URL, {"key": bind_key})

    payload = data.get("data")
    if not isinstance(payload, dict):
        payload = {}
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("QQ 机器人绑定任务响应缺少 task_id")

    return QQBindSession(
        task_id=task_id,
        bind_key=bind_key,
        qr_url=CONNECT_URL_TEMPLATE.format(task_id=quote(task_id, safe="")),
    )


async def poll_bind_result(session: QQBindSession) -> dict[str, Any]:
    """轮询一次绑定结果。

    返回值：
    - {"status": "waiting"}                       等待扫码
    - {"status": "expired"}                       二维码已过期
    - {"status": "success", "app_id", "app_secret"} 绑定成功
    - {"status": "error", "message"}              绑定失败
    """
    data = await _post_json(POLL_RESULT_URL, {"task_id": session.task_id})

    payload = data.get("data")
    if not isinstance(payload, dict):
        payload = {}
    try:
        raw_status = int(payload.get("status", STATUS_NONE))
    except (TypeError, ValueError):
        raw_status = STATUS_NONE

    if raw_status == STATUS_EXPIRED:
        return {"status": "expired"}
    if raw_status != STATUS_COMPLETED:
        return {"status": "waiting"}

    app_id = str(payload.get("bot_appid") or "").strip()
    encrypted_secret = str(payload.get("bot_encrypt_secret") or "").strip()
    if not app_id or not encrypted_secret:
        return {
            "status": "error",
            "message": "扫码成功但未返回完整 QQ 机器人凭证",
        }
    try:
        app_secret: Optional[str] = decrypt_app_secret(
            encrypted_secret, session.bind_key
        )
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    logger.info("QQ 机器人扫码绑定成功: app_id=%s", app_id)
    return {"status": "success", "app_id": app_id, "app_secret": app_secret}
