#!/usr/bin/env python3
"""图片编辑 HTTP 服务（封装 image_model_core，供 HTTP 客户端调用）。"""

from __future__ import annotations

import cgi
import json
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from image_model_core import (
    TOOL_VERSION,
    call_model_api,
    get_config,
    load_dotenv,
    save_image,
    _mime,
)

load_dotenv()
_cfg = get_config()
HOST = __import__("os").getenv("HOST", "127.0.0.1")
PORT = int(__import__("os").getenv("PORT", "8780"))
SERVICE_VERSION = f"http-{TOOL_VERSION}"


def parse_multipart(handler: BaseHTTPRequestHandler) -> dict[str, tuple[str, bytes]]:
    env = {
        "REQUEST_METHOD": "POST",
        "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
        "CONTENT_LENGTH": handler.headers.get("Content-Length", "0"),
    }
    form = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers, environ=env)
    out: dict[str, tuple[str, bytes]] = {}
    for key in form.keys():
        field = form[key]
        items = field if isinstance(field, list) else [field]
        for item in items:
            if not getattr(item, "filename", None):
                raw = item.value
                out[key] = ("", raw.encode("utf-8") if isinstance(raw, str) else raw)
            else:
                out[key] = (item.filename, item.file.read() if item.file else b"")
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "ImageLLM/HTTP"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("", "/health"):
            c = get_config()
            self._json(
                200,
                {
                    "ok": True,
                    "version": SERVICE_VERSION,
                    "tool": "edit_image",
                    "protocol": "openai-chat-completions",
                    "endpoint": c["api_url"],
                    "model": c["model"],
                    "api_base": c["api_base"],
                    "has_key": bool(c["api_key"]),
                    "dry_run": c["dry_run"],
                },
            )
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/process":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            fields = parse_multipart(self)
        except Exception as exc:
            self._json(400, {"ok": False, "error": f"解析表单失败: {exc}"})
            return
        if "image" not in fields:
            self._json(400, {"ok": False, "error": "缺少 image 字段"})
            return

        filename, data = fields["image"]
        if not data:
            self._json(400, {"ok": False, "error": "图片为空"})
            return
        c = get_config()
        if len(data) > c["max_bytes"]:
            self._json(400, {"ok": False, "error": f"图片超过 {c['max_bytes']} 字节"})

        mime = _mime(filename or "image.jpg", data)
        if not mime.startswith("image/"):
            self._json(400, {"ok": False, "error": "仅支持图片"})
            return

        _, prompt_raw = fields.get("prompt", ("", b""))
        prompt = (
            prompt_raw.decode("utf-8", errors="replace").strip()
            if isinstance(prompt_raw, bytes)
            else str(prompt_raw).strip()
        )
        text_prompt = prompt or c["default_prompt"]
        stem = Path(filename or "image").stem

        try:
            result = call_model_api(data, mime, text_prompt)
            out_path = save_image(result["image_b64"], stem)
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return

        self._json(
            200,
            {
                "ok": True,
                "model": c["model"],
                "prompt": text_prompt,
                "filename": Path(filename or "image").name,
                "output_path": str(out_path),
                "result": result.get("text") or f"已生成: {out_path}",
                "has_image": True,
            },
        )


def find_free_port(start: int = PORT, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, port))
                return port
            except OSError:
                continue
    raise OSError(f"端口 {start}～{start + tries - 1} 均被占用")


def main() -> None:
    c = get_config()
    if not c["api_key"]:
        print("警告: 未设置 LLM_API_KEY", file=sys.stderr)
    port = find_free_port()
    httpd = HTTPServer((HOST, port), Handler)
    print(f"http://{HOST}:{port}  tool=edit_image  模型={c['model']}")
    print(f"API: {c['api_url']}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
