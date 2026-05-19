#!/bin/bash
# 打包「亿牛广告看板」为 macOS 可执行程序（含 Python 与依赖）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DIST_DIR="$SCRIPT_DIR/dist"

echo "==> 安装打包依赖..."
python3 -m pip install -q --upgrade pip
python3 -m pip install -q -r "$PROJECT_DIR/requirements.txt"
python3 -m pip install -q pyinstaller>=6.0

echo "==> 清理旧构建..."
rm -rf "$SCRIPT_DIR/build"
rm -rf "$DIST_DIR"/* 2>/dev/null || true
mkdir -p "$DIST_DIR"

echo "==> 开始打包（约 1～3 分钟）..."
cd "$SCRIPT_DIR"
python3 -m PyInstaller --noconfirm --clean yiniu_merge.spec

echo ""
echo "=========================================="
echo "打包完成！输出目录："
echo ""
if [[ -d "$DIST_DIR/亿牛广告看板.app" ]]; then
  echo "  macOS 应用（推荐双击运行）："
  echo "  $DIST_DIR/亿牛广告看板.app"
  echo ""
fi
if [[ -d "$DIST_DIR/亿牛广告看板" ]]; then
  echo "  命令行版本："
  echo "  $DIST_DIR/亿牛广告看板/亿牛广告看板"
fi
echo ""
echo "使用说明："
echo "  1. 双击 .app 或运行上方命令行程序"
echo "  2. 浏览器会自动打开 http://127.0.0.1:8766/"
echo "  3. 上传今日推广数据与匹配表（可选昨日）后点击「输出结果」"
echo "  4. 退出：在 Dock 右键图标 → 退出"
echo ""
echo "  备用启动（若 .app 无反应可双击）："
LAUNCHER="$DIST_DIR/启动亿牛广告看板.command"
cat > "$LAUNCHER" << 'EOF'
#!/bin/bash
cd "$(dirname "$0")/亿牛广告看板"
./亿牛广告看板
EOF
chmod +x "$LAUNCHER"
echo "  $LAUNCHER"
echo "=========================================="
