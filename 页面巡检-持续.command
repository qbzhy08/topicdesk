#!/bin/bash
# 持续页面巡检（开着不关）：Claude 同步新版本后自动重查，结果写进 data/ui_check/summary.json，Claude 能直接读到并接着改。
# 页面自动巡检：把主题编辑台每一类页面、用你库里现有的数据都打开一遍（电脑和手机两种宽度），自动找显示问题。
# 只打开页面，不点任何会修改数据的按钮。需要主题编辑台正在运行（先双击 start.command）。
cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || { echo "请先双击 start.command 运行一次"; read -r -p "按回车退出" _; exit 1; }
echo "【1/3】检查巡检用的浏览器组件（playwright）…"
$PY -c "import playwright" 2>/dev/null || $PY -m pip install -q playwright
echo "【2/3】准备无界面浏览器 Chromium（第一次要下载约 150MB，几分钟；以后秒过）…"
$PY -m playwright install chromium || { echo "浏览器下载失败，检查网络后再双击一次"; read -r -p "按回车退出" _; exit 1; }
echo "【3/3】持续巡检：同步新版本后自动重查；窗口别关，按 Ctrl+C 停止…"
$PY tools/ui_check.py --base "http://127.0.0.1:${PORT:-8000}" --out data/ui_check --max 6 --watch "$@"
read -r -p "按回车键退出" _
