"""预置表情包资源筛选脚本（开发期执行一次，产物随仓库/部署包携带）。

数据源：ChineseBQB 开源表情包数据集（zhaoolee/ChineseBQB），
索引 JSON 含人工标注的文件名关键词，按系列文件夹分类。

用法：
    # 列出全部系列分类及数量（首次运行，确认白名单）
    python tools/seed_sticker_assets.py --list-categories

    # 筛选并下载到 data/stickers/（默认 180 张，增量模式：既有文件跳过下载）
    python tools/seed_sticker_assets.py --count 180

产物：
    aiyichat/data/stickers/          图片文件 + manifest.json + NOTICE.md

注意：ChineseBQB 素材多为网络流行图，商用版权需部署方自行评估，
因此默认严格白名单筛选且数量受限，产物附 NOTICE 说明。
"""

import argparse
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path

import httpx

# ChineseBQB 开放数据源索引
_INDEX_URL = (
    "https://raw.githubusercontent.com/zhaoolee/ChineseBQB/master/"
    "chinesebqb_github.json"
)

# 输出目录：aiyichat/data/stickers/
_OUTPUT_DIR = Path(__file__).parent.parent / "data" / "stickers"

# 允许的图片后缀（GIF 优先，动画表情更自然）
_ALLOWED_EXTS = {".gif", ".png", ".jpg", ".jpeg"}

# 单张大小上限（QQ 上传限制 1MB，留足余量）
_MAX_SIZE = 500 * 1024

# 敏感词黑名单（命中文件名即排除，降低合规风险）
_SENSITIVE_WORDS = (
    "死", "杀", "血", "操", "妈", "逼", "滚", "贱", "婊", "绿帽",
    "傻逼", "沙比", "艹", "fuck", "shit", "TMD", "TM再", "食屎",
    "中指", "娘的", "你妈", "操你", "狗屁",
)

# 默认分类白名单：语义化命名、萌系/日常情绪类，版权风险相对较低，
# 文件名含中文关键词，可直接作为标签/描述参与语义检索
_DEFAULT_CATEGORIES = (
    "010Cat_是喵星人啦🐱BQB",
    "011Dog_狗🐶BQB",
    "008HappyDuck_开心鸭🐥BQB",
    "057HappyDuck_开心鸭BQB",
    "055AllHeart_全是心心BQB",
    "023Emoji_表情符号BQB",
    "014Pig_猪🐖BQB",
    "027Turtle_乌龟🐢BQB",
    "029Pigeon_鸽子🕊BQB",
    "012Parrot_鹦鹉🐦BQB",
    "006Hamster_仓鼠🐹BQB",
    "018WangEgg_汪蛋🥚BQB",
    "109_Opossum_负鼠_BQB",
    "049CatEveryday_猫咪日常BQB",
    "065TravelFrog_旅行青蛙🐸BQB",
    "033Eat_吃🍔BQB",
    "017Playing_演奏🎻BQB",
    "030AntennaBaby_天线宝宝👶BQB",
)

# 分类 → 表情包分组映射（约 6 组，导入时按名称 upsert 分组）；
# 未列出的分类归入 _DEFAULT_GROUP，保证每张表情都有归属分组
_CATEGORY_GROUP_MAP = {
    "010Cat_是喵星人啦🐱BQB": "萌猫日常",
    "049CatEveryday_猫咪日常BQB": "萌猫日常",
    "008HappyDuck_开心鸭🐥BQB": "开心鸭",
    "057HappyDuck_开心鸭BQB": "开心鸭",
    "011Dog_狗🐶BQB": "汪星萌宠",
    "018WangEgg_汪蛋🥚BQB": "汪星萌宠",
    "006Hamster_仓鼠🐹BQB": "小仓鼠",
    "023Emoji_表情符号BQB": "表情符号",
}
_DEFAULT_GROUP = "趣味日常"


def _group_for_category(category: str) -> str:
    """分类名映射到分组名，未命中映射时回退默认分组。"""
    return _CATEGORY_GROUP_MAP.get(category, _DEFAULT_GROUP)


def _infer_group(entry: dict) -> str:
    """从旧 manifest 条目推断分组（描述内嵌分类名），推断失败归默认分组。"""
    text = "%s %s" % (entry.get("description", ""), entry.get("category", ""))
    for cat, group in _CATEGORY_GROUP_MAP.items():
        if cat in text:
            return group
    return _DEFAULT_GROUP


def _load_index() -> list[dict]:
    """下载并解析 ChineseBQB 索引 JSON（带重试），返回条目列表。"""
    print("下载索引: %s" % _INDEX_URL)
    last_err: Exception | None = None
    for i in range(5):
        try:
            resp = httpx.get(_INDEX_URL, timeout=90.0, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("data", [])
            print("索引条目总数: %d" % len(entries))
            return entries
        except Exception as e:
            last_err = e
            print("索引下载重试 %d: %s" % (i + 1, e))
    raise RuntimeError("索引下载失败: %s" % last_err)


def _http_get_with_retry(client: httpx.Client, url: str) -> bytes:
    """带重试的图片下载，返回字节内容；全部失败抛异常。"""
    last_err: Exception | None = None
    for i in range(3):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            last_err = e
            time.sleep(1.0 + i)
    raise RuntimeError("下载失败: %s" % last_err)


def _entry_url(entry: dict) -> str:
    """从索引条目提取图片下载地址（绝对 URL）。"""
    return str(entry.get("url", "") or "").strip()


def _entry_category(entry: dict) -> str:
    """从索引条目提取系列分类名（通常以 BQB 结尾）。"""
    return str(entry.get("category", "") or "").strip()


def list_categories() -> None:
    """打印全部分类及数量，供人工挑选白名单。"""
    entries = _load_index()
    counter: dict[str, int] = {}
    for entry in entries:
        cat = _entry_category(entry)
        if cat:
            counter[cat] = counter.get(cat, 0) + 1
    for cat, n in sorted(counter.items(), key=lambda x: -x[1]):
        print("%6d  %s" % (n, cat))


def _sanitize_filename(raw: str, idx: int) -> str:
    """生成安全的目标文件名：序号前缀防冲突，去除非法字符。"""
    stem = Path(raw).stem
    stem = re.sub(r'[\\/:*?"<>|\s]+', "_", stem)[:40].strip("_")
    ext = Path(raw).suffix.lower()
    return "st_%03d_%s%s" % (idx, stem or "sticker", ext)


def _extract_tags(stem: str) -> list[str]:
    """从文件名主体提取标签：去除序号前缀后按分隔符切词。"""
    cleaned = re.sub(r"^[\w\u4e00-\u9fff]*\d{3,}\d*[-_]?", "", stem)
    parts = re.split(r"[-_，,、。\s]+", cleaned)
    seen: set[str] = set()
    tags: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            tags.append(p)
    return tags[:6]


def _load_old_manifest() -> list[dict]:
    """读取既有 manifest 并仅保留文件仍存在的条目（增量基线）。"""
    manifest_path = _OUTPUT_DIR / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print("旧 manifest 读取失败，视为空基线: %s" % e)
        return []
    kept = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("file", ""))
        if filename and (_OUTPUT_DIR / filename).exists():
            kept.append(entry)
    return kept


def select_and_download(categories: list[str], count: int) -> None:
    """按白名单分类筛选并下载表情包，产出 manifest + NOTICE。

    增量模式：既有文件（旧 manifest 中且磁盘存在）直接保留并跳过下载，
    仅下载补齐目标数量的差额部分；全量条目统一补齐 group 字段。
    """
    entries = _load_index()
    wanted = set(categories)

    # 第一轮筛选：分类白名单 + 后缀 + 敏感词 + 有效下载地址
    candidates: list[tuple[str, str, str]] = []  # (url, 文件名, 分类)
    for entry in entries:
        category = _entry_category(entry)
        if category not in wanted:
            continue
        url = _entry_url(entry)
        filename = str(entry.get("name", "") or "")
        if not url or not filename:
            continue
        ext = Path(filename).suffix.lower()
        if ext not in _ALLOWED_EXTS:
            continue
        if any(w in filename for w in _SENSITIVE_WORDS):
            continue
        candidates.append((url, filename, category))

    print("白名单候选: %d 张（分类: %d 个）" % (len(candidates), len(wanted)))
    if not candidates:
        print("无候选，请检查分类名是否与 --list-categories 输出一致")
        return

    # 增量基线：保留既有条目（补齐 group 字段），并预加载内容哈希去重集
    manifest: list[dict] = _load_old_manifest()
    seen_hash: set[str] = set()
    for entry in manifest:
        entry.setdefault("group", _infer_group(entry))
        try:
            data = (_OUTPUT_DIR / str(entry["file"])).read_bytes()
            seen_hash.add(hashlib.sha256(data).hexdigest())
        except OSError:
            pass
    print("既有基线: %d 张" % len(manifest))

    need = count - len(manifest)
    if need <= 0:
        _write_manifest(manifest)
        print("已达目标数量 %d，仅重写 manifest（补齐 group 字段）" % count)
        return

    # 分类均衡取样：每分类先洗牌取前 N 张，不够再补齐，避免单一分类占满；
    # 候选全量入池（下载环节按 need 提前截止，预留去重/失败缓冲）
    per_cat = max(need // max(len(categories), 1), 1)
    by_cat: dict[str, list[tuple[str, str, str]]] = {}
    for item in candidates:
        by_cat.setdefault(item[2], []).append(item)
    picked: list[tuple[str, str, str]] = []
    leftovers: list[tuple[str, str, str]] = []
    for cat, items in by_cat.items():
        random.shuffle(items)
        picked.extend(items[:per_cat])
        leftovers.extend(items[per_cat:])
    random.shuffle(leftovers)
    picked.extend(leftovers)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    failed = 0
    seq = len(manifest)  # 文件名序号接在基线之后，避免覆盖既有文件

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for url, filename, category in picked:
            if downloaded >= need:
                break
            try:
                data = _http_get_with_retry(client, url)
            except Exception as e:
                failed += 1
                print("下载失败跳过 %s: %s" % (filename, e))
                continue
            if len(data) > _MAX_SIZE or len(data) < 1024:
                continue  # 过大或疑似无效文件
            content_hash = hashlib.sha256(data).hexdigest()
            if content_hash in seen_hash:
                continue  # 内容去重（含与既有基线的重复）
            seen_hash.add(content_hash)

            seq += 1
            target = _sanitize_filename(filename, seq)
            while (_OUTPUT_DIR / target).exists():
                seq += 1
                target = _sanitize_filename(filename, seq)
            (_OUTPUT_DIR / target).write_bytes(data)

            stem = Path(filename).stem
            tags = _extract_tags(stem)
            manifest.append({
                "file": target,
                "name": tags[0] if tags else stem,
                "tags": tags or [category.replace("BQB", "")],
                "description": "%s系列表情包：「%s」" % (
                    category, "，".join(tags) if tags else stem,
                ),
                "source": "ChineseBQB",
                "group": _group_for_category(category),
                "enabled": True,
            })
            downloaded += 1
            print("[%d/%d] %s (%d KB)" % (
                downloaded, need, target, len(data) // 1024,
            ))
            time.sleep(0.2)  # 限速，避免对 GitHub 施加压力

    _write_manifest(manifest)
    _write_notice()
    print("完成: 新增 %d 张（失败 %d），总计 %d 张，目标 %d 张" % (
        downloaded, failed, len(manifest), count,
    ))


def _write_manifest(manifest: list[dict]) -> None:
    """写入 manifest.json。"""
    (_OUTPUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_notice() -> None:
    """写入版权与使用说明。"""
    notice = (
        "# 预置表情包资源说明\n\n"
        "- 素材来源：ChineseBQB 开源表情包数据集"
        "（https://github.com/zhaoolee/ChineseBQB），按分类白名单筛选。\n"
        "- 素材多为网络流行图，可能存在原始作者版权，"
        "商用前请由部署方自行评估；如有异议可在管理后台"
        "「表情包管理」页逐条停用或删除，并替换为自有素材。\n"
        "- 应用启动时按内容哈希幂等导入 MongoDB"
        "（sticker_seed_enabled 可关闭），运行期不依赖外网。\n"
    )
    (_OUTPUT_DIR / "NOTICE.md").write_text(notice, encoding="utf-8")


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="预置表情包资源筛选")
    parser.add_argument(
        "--list-categories", action="store_true",
        help="仅列出全部分类及数量",
    )
    parser.add_argument(
        "--count", type=int, default=180, help="目标数量（默认180，增量补齐差额）"
    )
    parser.add_argument(
        "--categories", type=str, default=",".join(_DEFAULT_CATEGORIES),
        help="分类白名单，逗号分隔",
    )
    args = parser.parse_args()

    if args.list_categories:
        list_categories()
        return

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    select_and_download(categories, args.count)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已中断")
        sys.exit(1)
