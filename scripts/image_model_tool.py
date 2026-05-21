#!/usr/bin/env python3
"""
图片编辑 Tool — 供其他 Agent 调用。

调用方式（任选）：
  1. Python:  from image_model_tool import process_image, TOOL_SCHEMA
  2. CLI:     python scripts/image_model_tool.py run --image a.png --prompt "去水印"
  3. JSON:    echo '{"image":"a.png","prompt":"去水印"}' | python scripts/image_model_tool.py
  4. HTTP:    IMAGE_TOOL_MODE=http python scripts/image_model_tool.py run ...

环境变量见 image_model_core.get_config()；LLM_DRY_RUN=1 不扣费。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from image_model_core import (
    TOOL_VERSION,
    call_model_api,
    call_service_http,
    get_config,
    load_dotenv,
    save_image,
    _mime,
)

TOOL_NAME = "edit_image"
TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "使用 gpt-image-2 等图像模型编辑图片：去水印、去文字、去人物、修复背景等。"
        "输入本地图片路径和编辑说明，返回处理后图片路径。每次调用消耗 API 额度。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "image": {
                "type": "string",
                "description": "输入图片的本地绝对或相对路径",
            },
            "prompt": {
                "type": "string",
                "description": "编辑指令，例如：去掉所有文字和小男孩，只保留面料背景",
            },
            "output_path": {
                "type": "string",
                "description": "可选，指定输出文件路径；默认保存到 1688_images/processed/",
            },
            "mode": {
                "type": "string",
                "enum": ["direct", "http"],
                "description": "direct=直连 PackyAPI；http=调用本地 image_llm_service（需先启动）",
                "default": "direct",
            },
        },
        "required": ["image", "prompt"],
    },
}


def _read_image(image: str | Path | bytes) -> tuple[bytes, str, Path]:
    if isinstance(image, bytes):
        return image, "image/jpeg", Path("image.jpg")
    path = Path(image).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"图片不存在: {path}")
    data = path.read_bytes()
    return data, _mime(path.name, data), path


def process_image(
    image: str | Path | bytes,
    prompt: str,
    *,
    output_path: str | Path | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """
    Agent 主入口。成功返回 ok=True 与 output_path；失败 ok=False 与 error。
    """
    load_dotenv()
    cfg = get_config()
    use_mode = (mode or os.getenv("IMAGE_TOOL_MODE", "direct")).lower()

    try:
        data, mime, src = _read_image(image)
        if len(data) > cfg["max_bytes"]:
            return {"ok": False, "error": f"图片超过 {cfg['max_bytes']} 字节"}

        if use_mode == "http":
            resp = call_service_http(data, src.name, prompt)
            if not resp.get("ok"):
                return {"ok": False, "error": resp.get("error", resp)}
            out = Path(resp["output_path"])
            return {
                "ok": True,
                "output_path": str(out),
                "input_path": str(src),
                "prompt": prompt,
                "model": resp.get("model", cfg["model"]),
                "message": resp.get("result", ""),
                "mode": "http",
            }

        result = call_model_api(data, mime, prompt)
        if output_path:
            out = Path(output_path).expanduser().resolve()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(__import__("base64").standard_b64decode(result["image_b64"]))
        else:
            out = save_image(result["image_b64"], src.stem)

        return {
            "ok": True,
            "output_path": str(out),
            "input_path": str(src),
            "prompt": prompt,
            "model": cfg["model"],
            "message": result.get("text") or f"已保存: {out}",
            "mode": "direct",
            "endpoint": cfg["api_url"],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def health() -> dict[str, Any]:
    load_dotenv()
    cfg = get_config()
    return {
        "ok": bool(cfg["api_key"]),
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "model": cfg["model"],
        "endpoint": cfg["api_url"],
        "dry_run": cfg["dry_run"],
        "service_url": cfg["service_url"],
    }


def _cli_run(args: argparse.Namespace) -> int:
    out = process_image(args.image, args.prompt, output_path=args.output, mode=args.mode)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def _cli_schema(_: argparse.Namespace) -> int:
    print(json.dumps({"tool": TOOL_SCHEMA, "health": health()}, ensure_ascii=False, indent=2))
    return 0


def _cli_from_stdin(_: argparse.Namespace) -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"ok": False, "error": "stdin 为空"}, ensure_ascii=False))
        return 1
    params = json.loads(raw)
    out = process_image(
        params["image"],
        params.get("prompt") or get_config()["default_prompt"],
        output_path=params.get("output_path"),
        mode=params.get("mode"),
    )
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="图片编辑 Tool（Agent 可调用）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="处理一张图片")
    run_p.add_argument("--image", "-i", required=True, help="输入图片路径")
    run_p.add_argument("--prompt", "-p", required=True, help="编辑说明")
    run_p.add_argument("--output", "-o", default=None, help="输出路径")
    run_p.add_argument("--mode", choices=("direct", "http"), default=None)
    run_p.set_defaults(func=_cli_run)

    sub.add_parser("schema", help="输出 tool JSON schema").set_defaults(func=_cli_schema)
    sub.add_parser("health", help="检查配置").set_defaults(
        func=lambda _: (print(json.dumps(health(), ensure_ascii=False, indent=2)), 0)[1]
    )

    stdin_p = sub.add_parser("json", help="从 stdin 读 JSON 参数")
    stdin_p.set_defaults(func=_cli_from_stdin)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
