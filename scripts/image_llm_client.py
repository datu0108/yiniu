#!/usr/bin/env python3
"""兼容入口：转发到 skill bridge（launch + 轮询）。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "skills" / "image_editor_api" / "bridge.py"
DEFAULT_OUTPUT = ROOT / "1688_images" / "processed"


def _run_bridge(args: list[str]) -> dict:
    proc = subprocess.run(
        [sys.executable, str(BRIDGE), *args],
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        data = json.loads(proc.stdout.strip())
    else:
        data = {"ok": False, "error": proc.stderr or "无输出"}
    if proc.returncode != 0 and data.get("ok") is not False:
        data["ok"] = False
    return data


def main() -> None:
    p = argparse.ArgumentParser(description="edit_image（skill bridge）")
    p.add_argument("image", type=Path)
    p.add_argument("-p", "--prompt", required=True)
    p.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--no-wait", action="store_true", help="仅 launch 不轮询")
    args = p.parse_args()

    out = _run_bridge(
        ["launch", "--input", str(args.image), "--prompt", args.prompt, "--output", str(args.output)]
    )
    if not out.get("ok") or args.no_wait:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0 if out.get("ok") else 1)

    job_id = out["job_id"]
    deadline = time.time() + out.get("max_wait_seconds", 600)
    while time.time() < deadline:
        time.sleep(out.get("poll_every_seconds", 20))
        st = _run_bridge(["status", "--job", job_id])
        if st.get("status") == "done":
            print(json.dumps(st, ensure_ascii=False, indent=2))
            sys.exit(0)
        if st.get("status") == "error":
            print(json.dumps(st, ensure_ascii=False, indent=2))
            sys.exit(1)

    print(json.dumps({"ok": False, "error": "轮询超时", "job_id": job_id}, ensure_ascii=False, indent=2))
    sys.exit(1)


if __name__ == "__main__":
    main()
