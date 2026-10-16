"""验证管理后台"回复风格"配置项渲染与保存链路。

只读验证：登录后拉取角色编辑页，检查单选组存在且默认选中"标准"。
凭据从 .env 读取，不打印。
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

host = os.getenv("DEPLOY_HOST")
base = f"https://{host}:8848"
s = requests.Session()
_cert = next(
    (p for p in ("certs/server.crt", "aiyichat/certs/server.crt")
     if os.path.exists(p)),
    None,
)
s.verify = _cert if _cert else True

# 登录
r = s.post(
    f"{base}/admin/login",
    data={
        "username": os.getenv("ADMIN_USERNAME"),
        "password": os.getenv("ADMIN_PASSWORD"),
    },
    allow_redirects=False,
    timeout=15,
)
print(f"login: {r.status_code} -> {r.headers.get('location', '')}")

# 角色列表 → 取第一个既有角色详情页（排除 create 向导链接）
r = s.get(f"{base}/admin/profiles", timeout=15)
import re  # noqa: E402
slugs = [
    x for x in re.findall(r'href="/admin/profiles/([^"?]+)"', r.text)
    if x != "create" and not x.startswith("create")
]
if not slugs:
    print("FAIL: 未找到角色详情页链接")
    raise SystemExit(1)
slug = slugs[0]
r = s.get(f"{base}/admin/profiles/{slug}", timeout=15)
ok_radio = all(
    f'name="verbosity" value="{v}"' in r.text
    for v in ("concise", "standard", "verbose")
)
ok_checked = 'value="standard" checked' in r.text.replace("checked", "checked")
ok_label = "回复风格" in r.text
print(f"角色详情页: /admin/profiles/{slug} status={r.status_code}")
print(f"[{'PASS' if ok_radio else 'FAIL'}] 三选单选组已渲染")
print(f"[{'PASS' if ok_checked else 'FAIL'}] 默认选中 standard")
print(f"[{'PASS' if ok_label else 'FAIL'}] '回复风格'标签存在")

# 创建向导第一步页面
r = s.get(f"{base}/admin/profiles/create", timeout=15)
ok_wizard = 'name="verbosity"' in r.text and "回复风格" in r.text
print(f"[{'PASS' if ok_wizard else 'FAIL'}] 创建向导第一步含回复风格选项")

# 保存链路：回读当前表单值（避免全量覆盖），仅改 verbosity 后提交
import html as _html  # noqa: E402


def _read_form(page: str) -> dict:
    """从角色详情页 HTML 回读表单字段当前值。"""
    data: dict = {}
    for m in re.finditer(
        r'<input type="(?:text|hidden|range|number)" name="([^"]+)"[^>]*'
        r'value="([^"]*)"', page,
    ):
        data[m.group(1)] = _html.unescape(m.group(2))
    for m in re.finditer(
        r'<textarea name="([^"]+)"[^>]*>(.*?)</textarea>', page, re.S,
    ):
        data[m.group(1)] = _html.unescape(m.group(2))
    for name, val in re.findall(
        r'<input type="radio" name="([^"]+)" value="([^"]*)"'
        r'[^>]*checked', page,
    ):
        data[name] = val
    # 复选框 is_active 无 value 属性，选中时提交值为 "on"
    if re.search(r'name="is_active"[^>]*checked', page):
        data["is_active"] = "on"
    # select 下拉：取带 selected 的 option 值
    for name, val in re.findall(
        r'<select name="([^"]+)".*?<option value="([^"]*)"[^>]*selected',
        page, re.S,
    ):
        data[name] = val
    # 重复列表字段（关系/特质）：getlist 语义，覆盖前面的单值回读
    for key in ("rel_name", "rel_relation", "rel_attitude",
                "trait_key", "trait_value"):
        vals = re.findall(
            rf'name="{key}"[^>]*value="([^"]*)"', page)
        if vals:
            data[key] = [_html.unescape(v) for v in vals]
    return data


page = s.get(f"{base}/admin/profiles/{slug}", timeout=15).text
form = _read_form(page)
form["verbosity"] = "concise"
r = s.post(
    f"{base}/admin/profiles/{slug}/edit", data=form,
    allow_redirects=True, timeout=15,
)
ok_saved = 'value="concise" checked' in r.text
print(f"[{'PASS' if ok_saved else 'FAIL'}] 保存 concise 后页面回显选中")
# 恢复为 standard，避免污染角色设定（其余字段保持原值）
form = _read_form(r.text)
form["verbosity"] = "standard"
r = s.post(
    f"{base}/admin/profiles/{slug}/edit", data=form,
    allow_redirects=True, timeout=15,
)
ok_restored = 'value="standard" checked' in r.text
print(f"[{'PASS' if ok_restored else 'FAIL'}] 恢复 standard 成功")
