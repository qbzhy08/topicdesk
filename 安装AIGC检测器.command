#!/bin/bash
# 安装本地 AIGC 检测器（HC3 中文检测模型 Hello-SimpleAI/chatgpt-detector-roberta-chinese）
# 在访达里双击运行即可。会装到主题编辑台自己的 Python 环境（.venv）里，不影响系统里的其他东西。
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "还没有 .venv：请先双击 start.command 运行一次主题编辑台，再运行本脚本。"
  read -r -p "按回车键退出" _; exit 1
fi
PY=.venv/bin/python
echo "== 第 1 步：安装 torch 和 transformers（约 300MB，几分钟）"
if ! $PY -m pip install -q torch transformers; then
  echo "官方源安装失败，改用清华镜像…"
  $PY -m pip install -q torch transformers -i https://pypi.tuna.tsinghua.edu.cn/simple || {
    read -r -p "安装失败（把上面的报错截图发给我），按回车退出" _; exit 1; }
fi
echo "== 第 2 步：下载检测模型（约 400MB）并试运行"
$PY - <<'PYEOF'
import os, sys, json
from pathlib import Path
sys.path.insert(0, ".")
from app import aigc
s = aigc.settings()
def try_load(endpoint):
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint
    aigc._PIPE.clear()
    return aigc.detect("这是一段用来测试检测器能否正常运行的中文文字。它没有特别的意义，只是为了确认模型已经下载并且可以使用。")
for ep in ([s.get("hf_endpoint")] if s.get("hf_endpoint") else []) + ["", "https://hf-mirror.com"]:
    try:
        print("尝试下载地址：", ep or "huggingface.co（官方）")
        r = try_load(ep)
        if ep:
            aigc.save_settings(hf_endpoint=ep)
        print("成功：测试文本的 AI 概率 =", r["ai_prob"])
        sys.exit(0)
    except Exception as e:
        print("这个地址不行：", str(e)[:200])
print("模型下载失败。可以换个网络再试，或者用外部检测器手动录入分数。")
sys.exit(1)
PYEOF
if [ $? -eq 0 ]; then
  echo ""
  echo "== 安装完成。请重启主题编辑台（双击 start.command），然后在实验报告里点「用本地检测器检测全部」。"
fi
read -r -p "按回车键退出" _
