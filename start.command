#!/bin/bash
# 主题编辑台一键启动（macOS）：在访达里双击运行，或在终端执行 ./start.command
# 第一次运行会创建独立的 Python 环境并安装依赖，之后直接启动。
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "没有找到 Python 3.9 以上版本。"
  echo "请到 https://www.python.org/downloads/macos/ 下载安装 Python 3.12，然后重新运行本脚本。"
  read -r -p "按回车键退出" _; exit 1
fi
echo "使用 $($PY --version)"

if [ ! -x .venv/bin/python ]; then
  echo "首次运行：创建虚拟环境…"
  "$PY" -m venv .venv || { read -r -p "创建虚拟环境失败，按回车退出" _; exit 1; }
fi

if [ ! -f .venv/.deps-ok ] || [ requirements.txt -nt .venv/.deps-ok ]; then
  echo "安装依赖…"
  .venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1
  if ! .venv/bin/python -m pip install -q -r requirements.txt; then
    echo "官方源安装失败，改用清华镜像…"
    .venv/bin/python -m pip install -q -r requirements.txt \
      -i https://pypi.tuna.tsinghua.edu.cn/simple || { read -r -p "依赖安装失败，按回车退出" _; exit 1; }
  fi
  touch .venv/.deps-ok
fi

PORT="${PORT:-8000}"
echo "启动服务：http://127.0.0.1:$PORT  （关闭此窗口或按 Ctrl+C 停止）"
( sleep 2; open "http://127.0.0.1:$PORT" ) &
exec .venv/bin/python run.py --port "$PORT" --auto-resume "$@"
