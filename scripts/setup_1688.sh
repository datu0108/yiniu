#!/bin/bash
# 1688 主图脚本环境安装
# - macOS 没有 pip 命令：用 python3 -m pip
# - 有 Google Chrome 时：不必下载 Chromium（约 170MB）
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PW_MIRROR="${PLAYWRIGHT_DOWNLOAD_HOST:-https://npmmirror.com/mirrors/playwright}"
PW_CHROME_MIRROR="${PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST:-https://cdn.npmmirror.com/binaries/chrome-for-testing}"
PW_TIMEOUT_MS="${PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT:-600000}"
CHROME_APP="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。可执行: brew install python"
  exit 1
fi

echo "使用: $(python3 --version)"

if [ ! -d .venv ]; then
  echo "创建虚拟环境: $ROOT/.venv"
  python3 -m venv .venv
fi

echo "安装 Python 依赖（清华 pip 镜像）…"
.venv/bin/python -m pip install --upgrade pip -i "$PIP_INDEX"
.venv/bin/python -m pip install -r requirements-scrape.txt -i "$PIP_INDEX"
echo "Python 依赖已就绪。"

if [ -x "$CHROME_APP" ]; then
  echo ""
  echo "已检测到本机 Google Chrome —— 脚本将直接用它，无需下载 Chromium。"
  echo ""
  echo "安装完成。推荐运行方式："
  echo "  bash scripts/open_chrome_debug.sh    # 终端1：打开真 Chrome"
  echo "  .venv/bin/python scripts/1688_main_images.py -k 纯棉毛巾 -n 20   # 终端2"
  exit 0
fi

install_playwright_browser() {
  local mode="$1"
  export PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT="$PW_TIMEOUT_MS"
  if [ "$mode" = "mirror" ]; then
    export PLAYWRIGHT_DOWNLOAD_HOST="$PW_MIRROR"
    export PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST="$PW_CHROME_MIRROR"
    echo "尝试国内镜像下载 Chromium…"
  else
    unset PLAYWRIGHT_DOWNLOAD_HOST PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST
    echo "尝试官方源下载 Chromium…"
  fi
  .venv/bin/python -m playwright install chromium
}

echo ""
echo "未检测到 Google Chrome（推荐安装，免下载 170MB Chromium）："
echo "  https://www.google.com/chrome/"
echo ""
echo "装好后直接运行："
echo "  .venv/bin/python scripts/1688_main_images.py -k 纯棉毛巾 -n 20"
echo ""
echo "若必须下载 Chromium，在终端执行（国内镜像）："
echo "  export PLAYWRIGHT_DOWNLOAD_HOST=$PW_MIRROR"
echo "  export PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST=$PW_CHROME_MIRROR"
echo "  .venv/bin/python -m playwright install chromium"
