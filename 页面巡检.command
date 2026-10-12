#!/bin/bash
# 页面自动巡检：把主题编辑台每一类页面、用你库里现有的数据都打开一遍（电脑和手机两种宽度），自动找显示问题。
# 只打开页面，不点任何会修改数据的按钮。需要主题编辑台正在运行（先双击 start.command）。
cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || { echo "请先双击 start.command 运行一次"; read -r -p "按回车退出" _; exit 1; }
$PY -c "import playwright" 2>/dev/null || $PY -m pip install -q playwright
$PY -m playwright install chromium >/dev/null 2>&1
$PY tools/ui_check.py --base "http://127.0.0.1:${PORT:-8000}" --out data/ui_check --max 6
open data/ui_check/report.html 2>/dev/null
read -r -p "按回车键退出" _
