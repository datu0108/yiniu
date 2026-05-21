#!/usr/bin/env python3
"""
诊断 API 连通性（会多次调用接口、消耗额度！）。

仅在手动确认后运行：
  CONFIRM_DIAGNOSE=1 python scripts/diagnose_llm_api.py
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    p = _ROOT / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def probe(name: str, url: str, headers: dict, body: dict | None = None) -> None:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method="POST" if body else "GET", headers=headers)
    print(f"\n=== {name} ===")
    print(f"URL: {url}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(800).decode("utf-8", errors="replace")
            print(f"OK {resp.status}")
            print(raw[:500])
    except urllib.error.HTTPError as e:
        raw = e.read(800).decode("utf-8", errors="replace")
        print(f"HTTP {e.code}")
        print("Response headers:", dict(e.headers))
        print(raw[:500])
    except Exception as e:
        print(f"ERR: {type(e).__name__}: {e}")


def main() -> None:
    if os.getenv("CONFIRM_DIAGNOSE") != "1":
        print("已阻止运行：诊断脚本会多次请求 API 并消耗额度。", file=sys.stderr)
        print("若确需运行：CONFIRM_DIAGNOSE=1 python scripts/diagnose_llm_api.py", file=sys.stderr)
        sys.exit(2)
    load_env()
    key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    bases = [
        os.getenv("LLM_API_BASE", "https://api.mytokenland.com/v1").rstrip("/"),
        os.getenv("LLM_API_BASE_FALLBACK", "https://api.mytokenland.com").rstrip("/"),
    ]

    text_payload = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "回复 OK"}],
    }

    header_sets = [
        ("anthropic-default", {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }),
        ("bearer-only", {
            "Authorization": f"Bearer {key}",
            "content-type": "application/json",
        }),
        ("anthropic+browser-ua", {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }),
        ("anthropic+openclaw", {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "User-Agent": "openclaw/1.0",
            "anthropic-beta": "messages-2024-05-15",
        }),
    ]

    for base in dict.fromkeys(bases):
        probe(f"GET {base}", base, {"User-Agent": "curl/8.0"})
        for hname, hdrs in header_sets:
            probe(f"POST {hname} @ {base}", f"{base}/messages", hdrs, text_payload)

    # 无 /v1 的 messages 路径变体
    for alt in ("https://api.mytokenland.com/v1/messages", "https://api.mytokenland.com/anthropic/v1/messages"):
        probe("alt-path", alt, header_sets[0][1], text_payload)


if __name__ == "__main__":
    main()
