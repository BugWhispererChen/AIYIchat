"""参考图编辑人物一致性 - 手动集成验证脚本。

用同一张定妆照 + 多个场景 prompt，分别调用候选编辑模型生成图片，
输出到本地目录供人工核验面部一致性与场景服从度，据此确定线上默认模型。

用法（需环境变量 DASHSCOPE_API_KEY）：
    python tools/test/manual_imagegen_edit_check.py --ref 定妆照.png \
        --models qwen-image-edit,wan2.5-i2i-preview --out _imagegen_check

不依赖数据库，直接构造模型配置调用 ImageGenService 的编辑通路。
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.imagegen_service import (  # noqa: E402
    ImageGenService,
    _detect_image_mime,
    _detect_provider,
)

# 覆盖典型场景：自拍 / 日常动作 / 环境人物 / 手持展示
SCENE_PROMPTS = [
    "realistic photo, selfie, smiling at camera, sitting in a cozy cafe, "
    "warm afternoon sunlight through the window",
    "realistic photo, cooking in a home kitchen, candid shot, "
    "wearing a casual apron, soft indoor lighting",
    "realistic photo, standing in a park in autumn, golden leaves, "
    "full of vitality, eye-level shot on smartphone",
    "realistic photo, holding a cup of bubble tea towards the camera, "
    "street background at dusk, neon lights bokeh",
]


async def run(ref_path: str, models: list[str], out_dir: str) -> None:
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("请先设置环境变量 DASHSCOPE_API_KEY")
        sys.exit(1)

    ref_bytes = Path(ref_path).read_bytes()
    mime = _detect_image_mime(ref_bytes)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    svc = ImageGenService()
    for model in models:
        cfg = SimpleNamespace(
            model=model, base_url="", api_key=api_key, timeout=120
        )
        provider = _detect_provider(cfg)
        for idx, prompt in enumerate(SCENE_PROMPTS, 1):
            edit_prompt = (
                "Generate a new photo of the person in the reference image. "
                "Keep the face, facial features and hairstyle exactly the "
                "same as in the reference image. New scene: " + prompt
            )
            try:
                if provider == "dashscope":
                    data = await svc._edit_dashscope(
                        cfg, edit_prompt, ref_bytes, mime
                    )
                elif provider == "gemini":
                    data = await svc._edit_gemini(
                        cfg, edit_prompt, ref_bytes, mime
                    )
                else:
                    data = await svc._edit_openai(
                        cfg, edit_prompt, ref_bytes, mime
                    )
                dest = out / f"{model.replace('/', '_')}_scene{idx}.png"
                dest.write_bytes(data)
                print(f"[OK] {model} scene{idx} -> {dest} ({len(data)} bytes)")
            except Exception as e:
                print(f"[FAIL] {model} scene{idx}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="参考图编辑一致性验证")
    parser.add_argument("--ref", required=True, help="定妆照图片路径")
    parser.add_argument(
        "--models", default="qwen-image-edit",
        help="逗号分隔的候选模型列表",
    )
    parser.add_argument(
        "--out", default="_imagegen_check", help="输出目录"
    )
    args = parser.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    asyncio.run(run(args.ref, models, args.out))


if __name__ == "__main__":
    main()
