#!/usr/bin/env python3
"""
Image Editor API Bridge — Accio Work Skill 入口。

Accio 等 Agent 平台常有 60～120s 的 bash 超时，而 gpt-image-2 常需 1～5 分钟。
请用 launch + status 轮询，勿长时间阻塞 run。

子命令：
  launch           单张修图：后台任务
  batch-launch     批量修图：文件夹内所有图片
  generate-launch  文生图：仅 prompt，无需输入图（Gemini generateContent）
  status           查询单张/生成任务
  batch-status     查询批量修图汇总
  run / generate   同步调试（Accio 勿用）
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
JOBS_DIR = SKILL_DIR / ".jobs"
BATCHES_DIR = JOBS_DIR / "_batches"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _load_dotenv() -> None:
    for path in (SKILL_DIR / ".env", SKILL_DIR.parent.parent / ".env", Path.cwd() / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_skill_config() -> dict:
    _load_dotenv()
    cfg: dict = {}
    cfg_path = SKILL_DIR / "config.json"
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    api_base = os.getenv("LLM_API_BASE", cfg.get("api_base", "https://www.packyapi.com")).rstrip("/")
    api_path = os.getenv(
        "LLM_API_PATH",
        cfg.get("api_path", "/v1beta/models/gemini-3-pro-image-preview:generateContent"),
    )
    if not api_path.startswith("/"):
        api_path = f"/{api_path}"

    key_env = cfg.get("api_key_env", "LLM_API_KEY")
    api_key = cfg.get("api_key") or os.getenv(key_env) or os.getenv("IMG_API_KEY", "")
    protocol = os.getenv("LLM_PROTOCOL", cfg.get("protocol", ""))

    return {
        "api_base": api_base,
        "api_path": api_path,
        "api_url": f"{api_base}{api_path}",
        "api_key": api_key,
        "protocol": protocol,
        "model": os.getenv("LLM_MODEL", cfg.get("model", "gemini-3-pro-image-preview")),
        "max_tokens": int(cfg.get("max_tokens", 4096)),
        "default_prompt": os.getenv("LLM_PROMPT", cfg.get("default_prompt", "")),
        "max_bytes": int(os.getenv("MAX_IMAGE_BYTES", str(cfg.get("max_image_bytes", 10 * 1024 * 1024)))),
        "api_timeout": int(os.getenv("IMG_API_TIMEOUT", str(cfg.get("api_timeout_seconds", 600)))),
        "dry_run": os.getenv("LLM_DRY_RUN", "").lower() in ("1", "true", "yes")
        or bool(cfg.get("dry_run")),
    }


def _sniff_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def _prepare_upload(data: bytes, name: str) -> tuple[bytes, str]:
    """统一转 JPEG + 限制边长，减小上传/回包压力。"""
    try:
        import io
        from PIL import Image
    except ImportError:
        mime = _sniff_mime(data)
        if len(data) <= 800_000 and mime == "image/jpeg":
            return data, mime
        return data, mime

    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((1024, 1024))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue(), "image/jpeg"


def _encode_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----Bridge{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    for name, (fname, content, mime) in files.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _parse_images_edits_response(data: dict) -> str:
    items = data.get("data") or []
    if not items:
        raise RuntimeError(f"响应中无图片: {data}")
    first = items[0]
    b64 = first.get("b64_json")
    url = first.get("url")
    if not b64 and url:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            b64 = base64.standard_b64encode(resp.read()).decode("ascii")
    if not b64:
        raise RuntimeError(f"无法获取结果图片: {first}")
    return b64


def _parse_chat_response(data: dict) -> str:
    import re

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"无法解析 chat 响应: {data}") from exc

    content = message.get("content")
    text_parts: list[str] = []
    image_b64: str | None = None

    if isinstance(content, str):
        text_parts.append(content)
        m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
        if m:
            image_b64 = m.group(1)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text") or "")
            elif block.get("type") == "image_url":
                url = (block.get("image_url") or {}).get("url", "")
                if url.startswith("data:") and ";base64," in url:
                    image_b64 = url.split(";base64,", 1)[1]
            elif block.get("type") == "image" and block.get("data"):
                image_b64 = block["data"]

    if image_b64:
        return image_b64
    text = "".join(text_parts).strip()
    raise RuntimeError(
        f"chat/completions 未返回图片（可能仅支持文本）。模型回复: {text[:800]}"
    )


def _api_mode(cfg: dict) -> str:
    p = (cfg.get("protocol") or "").lower()
    if p in ("gemini", "gemini-generatecontent"):
        return "gemini"
    if p in ("openai-chat", "chat"):
        return "chat"
    if p in ("openai-images-edits", "images-edits"):
        return "edits"
    if "generatecontent" in cfg["api_path"].lower():
        return "gemini"
    if "chat/completions" in cfg["api_path"]:
        return "chat"
    return "edits"


def _parse_gemini_response(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini 响应无 candidates: {data}")

    parts = (candidates[0].get("content") or {}).get("parts") or []
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if "text" in part:
            text_parts.append(part["text"])
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            return inline["data"]

    text = "".join(text_parts).strip()
    raise RuntimeError(f"Gemini 未返回图片。文本: {text[:800]}")


def _friendly_error(msg: str) -> str:
    if "401" in msg or "invalid_api_key" in msg.lower():
        return "API 密钥无效或未配置，请检查 LLM_API_KEY"
    if "403" in msg and ("quota" in msg.lower() or "额度" in msg):
        return "API 额度不足，请充值后再试"
    if "503" in msg or "model_not_found" in msg or "无可用渠道" in msg:
        return "模型/分组未开通，请在 PackyAPI 控制台检查 gemini-3-pro-image-preview 是否可用"
    if "Remote end closed" in msg or "timed out" in msg.lower():
        return (
            "连接在模型返回前被断开（Accio/bash 超时或网关断开）。"
            "PackyCode 可能已计费成功，请用 launch+status 轮询，或查看 .jobs 下是否已生成图片。"
        )
    return msg


def call_api(image_bytes: bytes, mime: str, prompt: str, cfg: dict) -> str:
    if not cfg["api_key"]:
        raise RuntimeError("未配置 API 密钥")
    if cfg["dry_run"]:
        raise RuntimeError("dry_run=true，已跳过 API 调用")

    image_bytes, mime = _prepare_upload(image_bytes, "upload")
    timeout = cfg["api_timeout"]
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "User-Agent": _USER_AGENT,
    }

    mode = _api_mode(cfg)
    b64_in = base64.standard_b64encode(image_bytes).decode("ascii")

    if mode == "gemini":
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime, "data": b64_in}},
                    ],
                }
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
            },
        }
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        parser = _parse_gemini_response
    elif mode == "chat":
        payload = {
            "model": cfg["model"],
            "max_tokens": cfg["max_tokens"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64_in}"},
                        },
                    ],
                }
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        parser = _parse_chat_response
    else:
        body, ctype = _encode_multipart(
            {"model": cfg["model"], "prompt": prompt, "n": "1"},
            {"image": ("upload.jpg", image_bytes, mime)},
        )
        headers["Content-Type"] = ctype
        parser = _parse_images_edits_response

    req = urllib.request.Request(cfg["api_url"], data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parser(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:1500]
        raise RuntimeError(f"HTTP {exc.code}: {err}") from exc


def call_api_generate(prompt: str, cfg: dict) -> str:
    """文生图：仅文本 prompt，无输入图。当前仅 Gemini generateContent。"""
    if not cfg["api_key"]:
        raise RuntimeError("未配置 API 密钥")
    if cfg["dry_run"]:
        raise RuntimeError("dry_run=true，已跳过 API 调用")
    if _api_mode(cfg) != "gemini":
        raise RuntimeError("文生图当前仅支持 protocol=gemini 与 generateContent 端点")

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(cfg["api_url"], data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=cfg["api_timeout"]) as resp:
            return _parse_gemini_response(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:1500]
        raise RuntimeError(f"HTTP {exc.code}: {err}") from exc


def _resolve_generate_output_path(output_dir: Path, output_name: str | None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_name:
        name = output_name if output_name.lower().endswith(".png") else f"{output_name}.png"
        return output_dir / name
    return output_dir / f"generated_{uuid.uuid4().hex[:8]}.png"


def _write_status(job_dir: Path, payload: dict) -> None:
    payload["updated_at"] = time.time()
    (job_dir / "status.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_status(job_dir: Path) -> dict:
    path = job_dir / "status.json"
    if not path.is_file():
        return {"ok": False, "error": f"任务不存在: {job_dir.name}"}
    return json.loads(path.read_text(encoding="utf-8"))


def run_sync(image_path: Path, prompt: str, output_dir: Path) -> dict:
    cfg = load_skill_config()
    if not image_path.is_file():
        return {"ok": False, "error": f"文件不存在: {image_path}"}

    data = image_path.read_bytes()
    if len(data) > cfg["max_bytes"]:
        return {"ok": False, "error": f"图片超过 {cfg['max_bytes']} 字节"}

    text_prompt = prompt.strip() or cfg["default_prompt"]
    if not text_prompt:
        return {"ok": False, "error": "prompt 为空"}

    try:
        b64_out = call_api(data, _sniff_mime(data), text_prompt, cfg)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"{image_path.stem}_edited.png"
        out_path.write_bytes(base64.standard_b64decode(b64_out))
        return {
            "ok": True,
            "status": "done",
            "output_path": str(out_path.resolve()),
            "input_path": str(image_path.resolve()),
            "prompt": text_prompt,
            "model": cfg["model"],
        }
    except Exception as exc:
        return {"ok": False, "status": "error", "error": _friendly_error(str(exc))}


def run_sync_generate(prompt: str, output_dir: Path, output_name: str | None = None) -> dict:
    cfg = load_skill_config()
    text_prompt = prompt.strip() or cfg["default_prompt"]
    if not text_prompt:
        return {"ok": False, "error": "prompt 为空"}

    try:
        b64_out = call_api_generate(text_prompt, cfg)
        out_path = _resolve_generate_output_path(output_dir, output_name)
        out_path.write_bytes(base64.standard_b64decode(b64_out))
        return {
            "ok": True,
            "status": "done",
            "mode": "generate",
            "output_path": str(out_path.resolve()),
            "prompt": text_prompt,
            "model": cfg["model"],
        }
    except Exception as exc:
        return {"ok": False, "status": "error", "mode": "generate", "error": _friendly_error(str(exc))}


def collect_images(directory: Path, *, recursive: bool = False) -> list[Path]:
    """收集目录下的图片；跳过已编辑产物与隐藏文件。"""
    if not directory.is_dir():
        return []

    def _ok(path: Path) -> bool:
        if not path.is_file():
            return False
        if path.name.startswith("."):
            return False
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            return False
        if path.stem.endswith("_edited"):
            return False
        return True

    if recursive:
        candidates = directory.rglob("*")
    else:
        candidates = directory.iterdir()

    return sorted(p.resolve() for p in candidates if _ok(p))


def _start_background_job(image_path: Path, prompt: str, output_dir: Path) -> str:
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "job_id": job_id,
        "image_path": str(image_path.resolve()),
        "prompt": prompt,
        "output_dir": str(output_dir.expanduser().resolve()),
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_status(
        job_dir,
        {
            "ok": True,
            "status": "running",
            "job_id": job_id,
            "input_path": str(image_path.resolve()),
            "message": "后台处理中",
        },
    )

    log_path = job_dir / "worker.log"
    with open(log_path, "w", encoding="utf-8") as log_fp:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "_worker",
                "--job-dir",
                str(job_dir),
            ],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(SKILL_DIR),
        )
    return job_id


def _start_generate_job(prompt: str, output_dir: Path, output_name: str | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "job_id": job_id,
        "mode": "generate",
        "prompt": prompt,
        "output_dir": str(output_dir.expanduser().resolve()),
        "output_name": output_name,
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_status(
        job_dir,
        {
            "ok": True,
            "status": "running",
            "mode": "generate",
            "job_id": job_id,
            "message": "文生图后台处理中",
        },
    )

    log_path = job_dir / "worker.log"
    with open(log_path, "w", encoding="utf-8") as log_fp:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "_worker",
                "--job-dir",
                str(job_dir),
            ],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(SKILL_DIR),
        )
    return job_id


def cmd_generate_launch(prompt: str, output_dir: Path, output_name: str | None = None) -> int:
    job_id = _start_generate_job(prompt, output_dir, output_name)
    bridge = Path(__file__).resolve()
    _emit(
        {
            "ok": True,
            "status": "running",
            "mode": "generate",
            "job_id": job_id,
            "poll_every_seconds": 20,
            "max_wait_seconds": 600,
            "status_command": f'python "{bridge}" status --job {job_id}',
            "note": "文生图：勿重复 generate-launch；轮询 status 直至 done 或 error",
        }
    )
    return 0


def cmd_launch(image_path: Path, prompt: str, output_dir: Path) -> int:
    if not image_path.is_file():
        _emit({"ok": False, "error": f"文件不存在: {image_path}"})
        return 1

    job_id = _start_background_job(image_path, prompt, output_dir)
    bridge = Path(__file__).resolve()
    _emit(
        {
            "ok": True,
            "status": "running",
            "job_id": job_id,
            "poll_every_seconds": 20,
            "max_wait_seconds": 600,
            "status_command": f'python "{bridge}" status --job {job_id}',
            "note": "勿重复 launch；仅轮询 status 直至 done 或 error",
        }
    )
    return 0


def cmd_batch_launch(
    input_dir: Path,
    prompt: str,
    output_dir: Path,
    *,
    recursive: bool = False,
    delay_seconds: float = 1.0,
) -> int:
    if not input_dir.is_dir():
        _emit({"ok": False, "error": f"目录不存在: {input_dir}"})
        return 1

    images = collect_images(input_dir, recursive=recursive)
    if not images:
        _emit({"ok": False, "error": f"目录内无可用图片: {input_dir}"})
        return 1

    batch_id = uuid.uuid4().hex[:12]
    batch_dir = BATCHES_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[dict] = []
    for i, image_path in enumerate(images):
        job_id = _start_background_job(image_path, prompt, output_dir)
        jobs.append(
            {
                "job_id": job_id,
                "input_path": str(image_path),
                "status": "running",
            }
        )
        if delay_seconds > 0 and i < len(images) - 1:
            time.sleep(delay_seconds)

    bridge = Path(__file__).resolve()
    batch_meta = {
        "batch_id": batch_id,
        "input_dir": str(input_dir.resolve()),
        "output_dir": str(output_dir.expanduser().resolve()),
        "prompt": prompt,
        "recursive": recursive,
        "jobs": jobs,
        "created_at": time.time(),
    }
    (batch_dir / "batch.json").write_text(
        json.dumps(batch_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _emit(
        {
            "ok": True,
            "status": "running",
            "batch_id": batch_id,
            "total": len(jobs),
            "done": 0,
            "error": 0,
            "running": len(jobs),
            "jobs": jobs,
            "poll_every_seconds": 20,
            "max_wait_seconds": 900,
            "status_command": f'python "{bridge}" batch-status --batch {batch_id}',
            "note": "每张图各扣 1 次 API 费；勿重复 batch-launch；轮询 batch-status 直至全部结束",
        }
    )
    return 0


def _summarize_batch(batch_id: str) -> dict:
    batch_dir = BATCHES_DIR / batch_id
    batch_path = batch_dir / "batch.json"
    if not batch_path.is_file():
        return {"ok": False, "error": f"批量任务不存在: {batch_id}"}

    batch_meta = json.loads(batch_path.read_text(encoding="utf-8"))
    items: list[dict] = []
    done = error = running = 0

    for entry in batch_meta.get("jobs", []):
        job_id = entry["job_id"]
        st = _read_status(JOBS_DIR / job_id)
        status = st.get("status", "unknown")
        item = {
            "job_id": job_id,
            "input_path": entry.get("input_path"),
            "status": status,
        }
        if status == "done":
            done += 1
            item["output_path"] = st.get("output_path")
        elif status == "error":
            error += 1
            item["error"] = st.get("error")
        elif status == "running":
            running += 1
        items.append(item)

    total = len(items)
    if running > 0:
        batch_status = "running"
    elif error == total:
        batch_status = "error"
    elif done == total:
        batch_status = "done"
    else:
        batch_status = "partial"

    return {
        "ok": batch_status in ("done", "partial", "running"),
        "status": batch_status,
        "batch_id": batch_id,
        "total": total,
        "done": done,
        "error": error,
        "running": running,
        "input_dir": batch_meta.get("input_dir"),
        "output_dir": batch_meta.get("output_dir"),
        "jobs": items,
        "updated_at": time.time(),
    }


def cmd_batch_status(batch_id: str) -> int:
    data = _summarize_batch(batch_id)
    _emit(data)
    if data.get("status") == "done":
        return 0
    if data.get("status") == "error":
        return 1
    return 0


def cmd_status(job_id: str) -> int:
    job_dir = JOBS_DIR / job_id
    data = _read_status(job_dir)
    _emit(data)
    if data.get("status") == "done" and data.get("output_path"):
        return 0
    if data.get("status") == "error":
        return 1
    return 0 if data.get("status") == "running" else 1


def cmd_worker(job_dir: Path) -> int:
    meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("mode") == "generate":
        result = run_sync_generate(
            meta["prompt"],
            Path(meta["output_dir"]),
            meta.get("output_name"),
        )
    else:
        result = run_sync(
            Path(meta["image_path"]),
            meta["prompt"],
            Path(meta["output_dir"]),
        )
    result["job_id"] = meta.get("job_id", job_dir.name)
    _write_status(job_dir, result)
    return 0 if result.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Image Editor API Bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    launch_p = sub.add_parser("launch", help="单张：后台任务，立即返回")
    launch_p.add_argument("--input", "--image_path", dest="input", required=True)
    launch_p.add_argument("--prompt", required=True)
    launch_p.add_argument("--output", "--output_dir", dest="output", required=True)

    batch_p = sub.add_parser("batch-launch", help="批量：文件夹内所有图片")
    batch_p.add_argument("--input-dir", "--input_dir", dest="input_dir", required=True)
    batch_p.add_argument("--prompt", required=True)
    batch_p.add_argument("--output", "--output_dir", dest="output", required=True)
    batch_p.add_argument(
        "--recursive",
        action="store_true",
        help="包含子目录中的图片（默认仅当前目录）",
    )
    batch_p.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="每张图启动间隔秒数，减轻并发压力（默认 1）",
    )

    status_p = sub.add_parser("status", help="查询单张后台任务（秒返回）")
    status_p.add_argument("--job", required=True)

    batch_status_p = sub.add_parser("batch-status", help="查询批量任务汇总")
    batch_status_p.add_argument("--batch", required=True)

    gen_p = sub.add_parser("generate-launch", help="文生图：无输入图，仅 prompt")
    gen_p.add_argument("--prompt", required=True)
    gen_p.add_argument("--output", "--output_dir", dest="output", required=True)
    gen_p.add_argument(
        "--name",
        dest="output_name",
        default=None,
        help="输出文件名（可选，默认 generated_<id>.png）",
    )

    gen_run_p = sub.add_parser("generate", help="文生图同步（仅本地调试）")
    gen_run_p.add_argument("--prompt", required=True)
    gen_run_p.add_argument("--output", "--output_dir", dest="output", required=True)
    gen_run_p.add_argument("--name", dest="output_name", default=None)

    run_p = sub.add_parser("run", help="修图同步（仅本地调试）")
    run_p.add_argument("--input", "--image_path", dest="input", required=True)
    run_p.add_argument("--prompt", required=True)
    run_p.add_argument("--output", "--output_dir", dest="output", required=True)

    worker_p = sub.add_parser("_worker")
    worker_p.add_argument("--job-dir", required=True)

    args = parser.parse_args()
    if args.cmd == "launch":
        return cmd_launch(
            Path(args.input).expanduser(),
            args.prompt,
            Path(args.output).expanduser(),
        )
    if args.cmd == "batch-launch":
        return cmd_batch_launch(
            Path(args.input_dir).expanduser(),
            args.prompt,
            Path(args.output).expanduser(),
            recursive=args.recursive,
            delay_seconds=args.delay,
        )
    if args.cmd == "status":
        return cmd_status(args.job)
    if args.cmd == "batch-status":
        return cmd_batch_status(args.batch)
    if args.cmd == "generate-launch":
        return cmd_generate_launch(
            args.prompt,
            Path(args.output).expanduser(),
            args.output_name,
        )
    if args.cmd == "generate":
        _emit(
            run_sync_generate(
                args.prompt,
                Path(args.output).expanduser(),
                args.output_name,
            )
        )
        return 0
    if args.cmd == "run":
        _emit(run_sync(Path(args.input).expanduser(), args.prompt, Path(args.output).expanduser()))
        return 0
    if args.cmd == "_worker":
        return cmd_worker(Path(args.job_dir))
    return 1


if __name__ == "__main__":
    sys.exit(main())
