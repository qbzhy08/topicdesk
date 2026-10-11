#!/bin/bash
# 记忆验证演示：离线回放模式（模拟网站 + 独立数据库 data/replay.db，不影响真实数据）
# 双击运行；浏览器会打开“记忆验证演示”页。用 8001 端口，可以和正常服务同时开。
cd "$(dirname "$0")" || exit 1
export PORT="${PORT:-8001}"
export OPEN_PATH="#/memory/demo"
exec bash start.command --replay
