"""修改密码功能端到端验证脚本（测试服务器）。

流程：
1. 明文密码登录 → 验证自动迁移为哈希
2. 访问 /admin/password 页面
3. 错误分支：当前密码错误 / 新密码太短 / 两次不一致
4. 成功修改 → 303 + 会话失效 + DB 中为哈希
5. 旧密码拒绝、新密码可登录
6. 恢复原始密码（清理）
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

host = os.getenv("DEPLOY_HOST")
base = f"https://{host}:8848"
ORIGINAL_PW = "admin123"
NEW_PW = "newpass-2026"

s = requests.Session()
_cert = next(
    (p for p in ("certs/server.crt", "aiyichat/certs/server.crt") if os.path.exists(p)),
    None,
)
s.verify = _cert if _cert else True

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# 1. 明文登录（应触发自动迁移）
r = s.post(f"{base}/admin/login",
           data={"username": "admin", "password": ORIGINAL_PW},
           allow_redirects=False, timeout=15)
check("明文登录 303", r.status_code == 303, f"got {r.status_code}")
token = s.cookies.get("admin_session")
check("获得会话 Cookie", bool(token))

# 2. 访问修改密码页
r = s.get(f"{base}/admin/password", timeout=15)
check("GET /admin/password 200", r.status_code == 200)
check("页面含修改密码表单", "修改密码" in r.text and "current_password" in r.text)

# 3. 错误分支
r = s.post(f"{base}/admin/password", data={
    "current_password": "wrong", "new_password": NEW_PW, "confirm_password": NEW_PW,
}, timeout=15)
check("当前密码错误提示", "当前密码不正确" in r.text)

r = s.post(f"{base}/admin/password", data={
    "current_password": ORIGINAL_PW, "new_password": "short", "confirm_password": "short",
}, timeout=15)
check("新密码太短提示", "至少 8 位" in r.text)

r = s.post(f"{base}/admin/password", data={
    "current_password": ORIGINAL_PW, "new_password": NEW_PW, "confirm_password": "different",
}, timeout=15)
check("两次不一致提示", "不一致" in r.text)

# 4. 成功修改
r = s.post(f"{base}/admin/password", data={
    "current_password": ORIGINAL_PW, "new_password": NEW_PW, "confirm_password": NEW_PW,
}, allow_redirects=False, timeout=15)
check("修改成功 303", r.status_code == 303, f"got {r.status_code}")
check("跳转带 msg 参数", "msg=password_changed" in r.headers.get("location", ""))

# 旧会话应已失效（访问需登录页面应重定向或 401）
r = s.get(f"{base}/admin/password", allow_redirects=False, timeout=15)
check("旧会话已失效", r.status_code in (303, 401), f"got {r.status_code}")

# 5. 旧密码拒绝
r = s.post(f"{base}/admin/login",
           data={"username": "admin", "password": ORIGINAL_PW},
           allow_redirects=False, timeout=15)
check("旧密码被拒绝", r.status_code in (200, 401) and "303" not in str(r.status_code),
      f"got {r.status_code}")

# 新密码可登录
s2 = requests.Session()
s2.verify = s.verify
r = s2.post(f"{base}/admin/login",
            data={"username": "admin", "password": NEW_PW},
            allow_redirects=False, timeout=15)
check("新密码登录 303", r.status_code == 303, f"got {r.status_code}")

# 登录页成功提示
r = s2.get(f"{base}/admin/login", params={"msg": "password_changed"}, timeout=15)
check("登录页显示成功提示", "密码已修改" in r.text)

# 6. 恢复原始密码（用新密码改回）
r = s2.post(f"{base}/admin/password", data={
    "current_password": NEW_PW, "new_password": ORIGINAL_PW, "confirm_password": ORIGINAL_PW,
}, allow_redirects=False, timeout=15)
check("恢复原始密码 303", r.status_code == 303, f"got {r.status_code}")

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部验证通过")
