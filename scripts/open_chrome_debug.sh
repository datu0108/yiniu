#!/bin/bash
# 用「正常 Chrome」打开 1688（非 Playwright 自动化窗口，滑块可拖动）
# 保持本终端不要关；另开终端运行 1688_main_images.py
set -e

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT="${CHROME_DEBUG_PORT:-9222}"
PROFILE="${CHROME_USER_DATA:-$HOME/.1688_chrome_debug}"

if [ ! -x "$CHROME" ]; then
  echo "未找到 Google Chrome，请先安装: https://www.google.com/chrome/"
  exit 1
fi

echo "启动 Chrome（调试端口 $PORT）"
echo "配置目录: $PROFILE"
echo ""
echo "接下来请在此 Chrome 里："
echo "  1. 打开 1688 并登录（可选）"
echo "  2. 搜索你的关键词，完成滑块"
echo "  3. 另开终端运行:"
echo "     .venv/bin/python scripts/1688_main_images.py -k 你的关键词 -n 20"
echo ""

exec "$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  "https://s.1688.com/selloffer/offer_search.htm"
