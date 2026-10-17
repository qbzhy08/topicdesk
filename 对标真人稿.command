#!/bin/bash
# 对标真人稿：系统按 bench/cases.json 里的主题（外加自己从近几天热点里选的 2 个）检索、写文章，
# 再和同主题真人写的文章逐项对比“像不像人写的”。结果：data/style_bench/<时间>/report.md；
# 汇总写进 data/style_bench/summary.json，Claude 能直接读到并接着改。
# 会调用大模型和搜索 API（每个主题大约相当于一次检索 + 一次写作的花费）。不需要先开 start.command。
cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || { echo "请先双击 start.command 运行一次"; read -r -p "按回车退出" _; exit 1; }
echo "对标真人稿：先跑清单里的主题，再让系统自己选 2 个。窗口别关，大约每个主题 3-8 分钟。"
$PY tools/style_bench.py --auto 2 "$@"
echo
echo "报告在 data/style_bench 里最新的文件夹（report.md）。回到对话里跟 Claude 说“跑完了”就行。"
read -r -p "按回车键退出" _
