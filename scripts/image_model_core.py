"""图片模型 API 核心逻辑（PackyAPI / OpenAI images/edits）。"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import uuid
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL_VERSION = "1.0.0"  # image_model_tool / HTTP 服务共用
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def get_config() -> dict:
    load_dotenv()
    api_path = os.getenv("LLM_API_PATH", "/chat/completions")
    api_base = os.getenv("LLM_API_BASE", "https://www.packyapi.com/v1").rstrip("/")
    path = api_path if api_path.startswith("/") else f"/{api_path}"
    return {
        "api_base": api_base,
        "api_path": api_path,
        "api_url": f"{api_base}{path}",
        "api_key": os.getenv("LLM_API_KEY", ""),
        "model": os.getenv("LLM_MODEL", "deepseek-v4-flash"),
        "default_prompt": os.getenv(
            "LLM_PROMPT",
            "去除图片上所有红色文字、红色方框和水印，自然修复背后的墙面与背景，保持其余内容不变。",
        ),
        "output_dir": Path(os.getenv("LLM_OUTPUT_DIR", str(ROOT / "1688_images" / "processed"))),
        "max_bytes": int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024))),
        "dry_run": os.getenv("LLM_DRY_RUN", "").lower() in ("1", "true", "yes"),
        "service_url": os.getenv("IMAGE_TOOL_SERVICE_URL", "http://127.0.0.1:8780"),
    }


def _mime(filename: str, data: bytes) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    if guessed and guessed.startswith("image/"):
        return guessed
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) > 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _api_headers(content_type: str = "application/json") -> dict[str, str]:
    cfg = get_config()
    return {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": content_type,
        "User-Agent": _USER_AGENT,
    }


def _maybe_resize(image_bytes: bytes, mime: str) -> tuple[bytes, str]:
    if len(image_bytes) <= 800_000:
        return image_bytes, mime
    try:
        import io
        from PIL import Image
    except ImportError:
        return image_bytes, mime
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((1536, 1536))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue(), "image/jpeg"


def _encode_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    for name, (filename, content, mime) in files.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _parse_images_edits_response(data: dict) -> dict:
    items = data.get("data") or []
    if not items:
        raise RuntimeError(f"响应中无图片: {data}")
    first = items[0]
    b64_out = first.get("b64_json")
    url_out = first.get("url")
    if not b64_out and url_out:
        req = urllib.request.Request(url_out, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            b64_out = base64.standard_b64encode(resp.read()).decode("ascii")
    if not b64_out:
        raise RuntimeError(f"无法获取结果图片: {first}")
    return {"image_b64": b64_out, "text": first.get("revised_prompt", ""), "raw": data}


def _parse_chat_response(data: dict) -> dict:
    message = data["choices"][0]["message"]
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
    if not image_b64:
        raise RuntimeError(f"模型未返回图片: {''.join(text_parts)[:500]}")
    return {"image_b64": image_b64, "text": "".join(text_parts).strip(), "raw": data}


def call_model_api(image_bytes: bytes, mime: str, prompt: str) -> dict:
    cfg = get_config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置 LLM_API_KEY")
    if cfg["dry_run"]:
        raise RuntimeError("LLM_DRY_RUN=1，已跳过 API 调用")

    image_bytes, mime = _maybe_resize(image_bytes, mime)
    url = cfg["api_url"]
    model = cfg["model"]

    if cfg["api_path"].rstrip("/").endswith("chat/completions"):
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST", headers=_api_headers()
        )
        parser = _parse_chat_response
    else:
        ext = "png" if mime == "image/png" else "jpg"
        body, ctype = _encode_multipart(
            {"model": model, "prompt": prompt, "n": "1"},
            {"image": (f"upload.{ext}", image_bytes, mime)},
        )
        h = _api_headers()
        h["Content-Type"] = ctype
        req = urllib.request.Request(url, data=body, method="POST", headers=h)
        parser = _parse_images_edits_response

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return parser(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:1500]
        raise RuntimeError(f"{url} → HTTP {exc.code}: {err}") from exc
    except (urllib.error.URLError, ConnectionError) as exc:
        raise RuntimeError(f"{url} → 请求失败: {exc}") from exc


def call_service_http(image_bytes: bytes, filename: str, prompt: str) -> dict:
    cfg = get_config()
    import uuid as _uuid

    boundary = f"----ToolBoundary{_uuid.uuid4().hex}"
    mime = _mime(filename, image_bytes)
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n{prompt}\r\n".encode(),
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
        + image_bytes
        + f"\r\n--{boundary}--\r\n".encode(),
    ]
    url = f"{cfg['service_url'].rstrip('/')}/process"
    req = urllib.request.Request(
        url,
        data=b"".join(parts),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def save_image(b64_data: str, stem: str, output_dir: Path | None = None) -> Path:
    cfg = get_config()
    out_dir = output_dir or cfg["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{stem}_edited.png"
    out.write_bytes(base64.standard_b64decode(b64_data))
    return out
