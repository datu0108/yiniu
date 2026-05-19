#!/usr/bin/env python3
"""
亿牛广告看板 - 本地 Web 界面（浏览器打开，避免 macOS 系统 Tk 黑屏）。
"""

from __future__ import annotations

import cgi
import io
import socket
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

# PyInstaller 打包支持
if getattr(sys, "frozen", False):
    import multiprocessing

    multiprocessing.freeze_support()

def _app_dir() -> Path:
    """打包为可执行文件时资源在 sys._MEIPASS。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


SCRIPT_DIR = _app_dir()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from merge_customer_data import default_output_filename, run_merge  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8766
PORT = DEFAULT_PORT
LOG_FILE = Path.home() / "Library/Logs/亿牛广告看板.log"


def setup_runtime() -> None:
    """打包后无终端窗口，日志写入文件便于排查。"""
    if not getattr(sys, "frozen", False):
        return
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(LOG_FILE, "a", encoding="utf-8")
    sys.stdout = sys.stderr = log_fp
    print(f"\n========== 启动 {datetime.now():%Y-%m-%d %H:%M:%S} ==========")


def mac_alert(title: str, message: str) -> None:
    """macOS 原生弹窗（双击 .app 无终端时用于提示错误）。"""
    safe = message.replace("\\", "\\\\").replace('"', '\\"')[:500]
    safe_title = title.replace('"', '\\"')
    script = f'display alert "{safe_title}" message "{safe}" as warning'
    subprocess.run(["osascript", "-e", script], check=False)


def find_free_port(start: int = DEFAULT_PORT, tries: int = 30) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, port))
                return port
            except OSError:
                continue
    raise OSError(f"无法绑定端口 {start}～{start + tries - 1}，请关闭占用端口的程序后重试")


def safe_upload_basename(filename: str) -> str:
    """取上传文件的 basename，并拒绝路径穿越。"""
    base = Path(filename).name
    if not base or base in (".", "..") or "/" in filename or "\\" in filename:
        raise ValueError(f"非法文件名: {filename}")
    return base


def content_disposition_attachment(filename: str) -> str:
    """HTTP 头仅支持 latin-1，中文文件名用 filename* UTF-8 编码。"""
    utf8_name = quote(filename, safe="")
    try:
        filename.encode("latin-1")
        return f'attachment; filename="{filename}"; filename*=UTF-8\'\'{utf8_name}'
    except UnicodeEncodeError:
        # filename 仅作兼容占位，真实名称见 filename* 与 X-Download-Filename
        return f'attachment; filename="download.xlsx"; filename*=UTF-8\'\'{utf8_name}'

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>亿牛广告看板</title>
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
      background: #eef2f7;
      color: #222;
      margin: 0;
      padding: 28px 20px;
    }
    .wrap { max-width: 760px; margin: 0 auto; }
    .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    @media (max-width: 640px) { .row2 { grid-template-columns: 1fr; } }
    .optional-tag { font-size: 11px; color: #888; font-weight: normal; }
    h1 { font-size: 24px; margin: 0 0 8px; }
    .sub { color: #555; font-size: 14px; margin-bottom: 20px; }
    .note {
      background: #fffbe6;
      border: 1px solid #ffe58f;
      border-radius: 8px;
      padding: 12px 14px;
      font-size: 13px;
      margin-bottom: 20px;
    }
    .card {
      background: #fff;
      border-radius: 12px;
      padding: 20px 22px;
      margin-bottom: 16px;
      box-shadow: 0 2px 8px rgba(0,0,0,.06);
    }
    .card h2 { font-size: 15px; margin: 0 0 16px; color: #333; }
    .field { margin-bottom: 18px; }
    .field:last-child { margin-bottom: 0; }
    .field label { display: block; font-weight: 600; font-size: 14px; margin-bottom: 4px; }
    .hint { font-size: 12px; color: #888; margin-bottom: 8px; }
    input[type=file] {
      display: block;
      width: 100%;
      padding: 12px;
      border: 2px dashed #b8c5e0;
      border-radius: 8px;
      background: #f8faff;
      font-size: 13px;
      cursor: pointer;
    }
    input[type=text] {
      width: 100%;
      padding: 11px 12px;
      border: 1px solid #ccc;
      border-radius: 8px;
      font-size: 14px;
    }
    button {
      padding: 14px 32px;
      font-size: 16px;
      font-weight: 600;
      color: #fff;
      background: #007aff;
      border: none;
      border-radius: 10px;
      cursor: pointer;
    }
    button:hover { background: #0066dd; }
    button:disabled { opacity: 0.55; cursor: wait; }
    #log {
      display: none;
      margin-top: 18px;
      padding: 14px;
      background: #1e1e1e;
      color: #ccc;
      border-radius: 8px;
      font: 12px/1.5 Menlo, Monaco, monospace;
      white-space: pre-wrap;
      max-height: 240px;
      overflow: auto;
    }
    #log.show { display: block; }
    .ok { color: #6fcf97; }
    .err { color: #ff8787; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>亿牛广告看板</h1>
    <p class="sub">上传今日推广数据及匹配表，可选昨日数据做对比；点击「输出结果」下载报表。</p>
    <div class="note">输出含：基础信息表、统计数据、销售汇报、运营汇报。</div>

    <form id="form">
      <div class="card">
        <h2>① 推广数据</h2>
        <div class="row2">
          <div class="field">
            <label>今日推广数据 <span class="optional-tag">必填</span></label>
            <div class="hint">主数据：admin_mbr_id、主管姓名、消耗/预算、店铺产品数等</div>
            <input type="file" id="table1" name="table1" accept=".xlsx,.xls,.xlsm" required />
          </div>
          <div class="field">
            <label>昨日推广数据 <span class="optional-tag">选填</span></label>
            <div class="hint">上传则统计数据含今日/昨日/差值；不上传仅输出今日统计</div>
            <input type="file" name="table1_yesterday" accept=".xlsx,.xls,.xlsm" />
          </div>
        </div>
      </div>

      <div class="card">
        <h2>② 匹配数据</h2>
        <div class="field">
          <label>客户公司名称匹配</label>
          <div class="hint">主账号ID、公司名称</div>
          <input type="file" name="table2" accept=".xlsx,.xls,.xlsm" required />
        </div>
        <div class="field">
          <label>客户-运营+销售匹配</label>
          <div class="hint">admin_mbr_id、运营、销售等</div>
          <input type="file" name="table3" accept=".xlsx,.xls,.xlsm" required />
        </div>
      </div>

      <div class="card">
        <h2>③ 输出文件名</h2>
        <div class="field">
          <label>保存为</label>
          <input type="text" id="output_name" name="output_name" value="" placeholder="默认：今日文件名+广告数据.xlsx" />
        </div>
      </div>

      <button type="submit" id="btn">输出结果</button>
    </form>
    <pre id="log"></pre>
  </div>
  <script>
    function defaultOutName(file) {
      if (!file || !file.name) return '';
      const base = file.name.replace(/\.[^.]+$/, '');
      return base + '广告数据.xlsx';
    }
    function getOutputName(form) {
      const out = form.output_name.value.trim();
      if (out) return out.toLowerCase().endsWith('.xlsx') ? out : out + '.xlsx';
      const f = form.table1.files[0];
      return f ? defaultOutName(f) : '广告数据.xlsx';
    }
    function parseDownloadName(res, fallback) {
      const xfn = res.headers.get('X-Download-Filename');
      if (xfn) {
        try { return decodeURIComponent(xfn); } catch (e) {}
      }
      const cd = res.headers.get('Content-Disposition') || '';
      const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
      if (star) {
        try { return decodeURIComponent(star[1]); } catch (e) {}
      }
      const plain = cd.match(/filename="([^"]+)"/i);
      if (plain && plain[1] !== 'download.xlsx') return plain[1];
      return fallback;
    }
    document.getElementById('table1').addEventListener('change', (e) => {
      const out = document.getElementById('output_name');
      const n = defaultOutName(e.target.files[0]);
      if (!n) return;
      if (!out.value.trim()) out.value = n;
      out.placeholder = n;
    });
    document.getElementById('form').onsubmit = async (e) => {
      e.preventDefault();
      const log = document.getElementById('log');
      const btn = document.getElementById('btn');
      log.className = 'show';
      log.textContent = '正在处理，请稍候…';
      btn.disabled = true;
      const expectedName = getOutputName(e.target);
      try {
        const res = await fetch('/merge', { method: 'POST', body: new FormData(e.target) });
        if (!res.ok) throw new Error(await res.text() || res.statusText);
        const blob = await res.blob();
        const name = parseDownloadName(res, expectedName);
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = name;
        a.click();
        URL.revokeObjectURL(a.href);
        log.innerHTML = '<span class="ok">✓ 已生成并下载：' + name + '</span>';
      } catch (err) {
        log.innerHTML = '<span class="err">✗ ' + (err.message || err) + '</span>';
      } finally {
        btn.disabled = false;
      }
    };
  </script>
</body>
</html>
"""




class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path != "/merge":
            self.send_error(404)
            return

        env = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
        }
        try:
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=env)
            logs = io.StringIO()

            def log_fn(msg: str):
                logs.write(msg + "\n")
                print(msg)

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)

                def save_field(name: str, keep_original_name: bool = False) -> tuple[Path, str]:
                    if name not in form:
                        raise ValueError(f"缺少文件: {name}")
                    field = form[name]
                    if not field.filename:
                        raise ValueError(f"未上传: {name}")
                    original = safe_upload_basename(field.filename)
                    if keep_original_name:
                        dest = tmp_path / original
                    else:
                        dest = tmp_path / f"{name}{Path(original).suffix}"
                    with open(dest, "wb") as f:
                        f.write(field.file.read())
                    return dest, original

                def save_field_optional(name: str) -> Path | None:
                    if name not in form or not getattr(form[name], "filename", None):
                        return None
                    field = form[name]
                    original = safe_upload_basename(field.filename)
                    dest = tmp_path / f"{name}_{original}"
                    with open(dest, "wb") as f:
                        f.write(field.file.read())
                    return dest

                t1, t1_original = save_field("table1", keep_original_name=True)
                t1_y = save_field_optional("table1_yesterday")
                t2, _ = save_field("table2")
                t3, _ = save_field("table3")

                out_name = ""
                if "output_name" in form and form["output_name"].value.strip():
                    out_name = form["output_name"].value.strip()
                if not out_name:
                    out_name = default_output_filename(t1, t1_original)
                if not out_name.lower().endswith(".xlsx"):
                    out_name += ".xlsx"

                out_path = tmp_path / out_name
                run_merge(t1, t2, t3, out_path, table1_yesterday_path=t1_y, log=log_fn)
                data = out_path.read_bytes()

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self.send_header("Content-Disposition", content_disposition_attachment(out_name))
            self.send_header("X-Download-Filename", quote(out_name, safe=""))
            self.send_header("Access-Control-Expose-Headers", "Content-Disposition, X-Download-Filename")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            print(logs.getvalue())

        except Exception as e:
            err = traceback.format_exc()
            print(err)
            body = (str(e) + "\n\n" + err).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main() -> None:
    global PORT
    setup_runtime()
    try:
        PORT = find_free_port()
        server = HTTPServer((HOST, PORT), Handler)
        url = f"http://{HOST}:{PORT}/"
        print("亿牛广告看板")
        print(f"服务地址: {url}")
        if getattr(sys, "frozen", False):
            print(f"日志文件: {LOG_FILE}")
        else:
            print("按 Ctrl+C 停止服务")
        webbrowser.open(url)
        if getattr(sys, "frozen", False):
            threading.Thread(
                target=lambda: mac_alert(
                    "亿牛广告看板",
                    f"服务已启动。\n\n若浏览器未打开，请访问：\n{url}\n\n退出：Dock 右键本程序图标 → 退出",
                ),
                daemon=True,
            ).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止")
    except Exception as e:
        err = traceback.format_exc()
        print(err)
        if getattr(sys, "frozen", False):
            mac_alert("启动失败", f"{e}\n\n详见日志：\n{LOG_FILE}")
        raise
    finally:
        try:
            server.server_close()  # type: ignore[possibly-undefined]
        except Exception:
            pass


if __name__ == "__main__":
    main()
