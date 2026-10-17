#!/bin/bash
# 上传到 GitHub：第一次运行会用 topicdesk.bundle 恢复完整的提交历史（89 次提交），再推到你给的仓库地址。
# 以后改了代码再双击一次，就会把改动提交并推上去。data/（数据库、API key）和 .venv 不会上传。
cd "$(dirname "$0")" || exit 1
command -v git >/dev/null || { echo "这台 Mac 还没有 git：在弹出的窗口里点“安装”（Xcode 命令行工具），装好后再双击一次。"; xcode-select --install; read -r -p "按回车退出" _; exit 1; }
if [ ! -d .git ]; then
  [ -f topicdesk.bundle ] || { echo "找不到 topicdesk.bundle"; read -r -p "按回车退出" _; exit 1; }
  git init -q -b main
  git fetch -q topicdesk.bundle "master:refs/remotes/bundle/main" || { echo "恢复提交历史失败"; read -r -p "按回车退出" _; exit 1; }
  git reset -q bundle/main
  echo "已恢复提交历史：$(git rev-list --count HEAD) 次提交"
fi
git config user.email >/dev/null || git config user.email "qbzhy08@gmail.com"
git config user.name >/dev/null || git config user.name "topicdesk"
git add -A
MSG_FILE=".提交说明.txt"   # 有写好的提交说明就用它（用完删掉），没有就用默认说明
if [ -f "$MSG_FILE" ]; then
  git reset -q -- "$MSG_FILE" 2>/dev/null
  git diff --cached --quiet || git commit -q -F "$MSG_FILE"
  rm -f "$MSG_FILE"
else
  git diff --cached --quiet || git commit -qm "同步本机改动"
fi
ask_url() {
  read -r -p "粘贴 GitHub 仓库地址（仓库页面浏览器地址栏里的就行，如 https://github.com/你的用户名/topicdesk）：" RAW
  # 不管粘的是仓库页、某次提交页还是 .git 地址，都只取“用户名/仓库名”
  OWNER_REPO=$(echo "$RAW" | sed -E 's#^(https?://)?(www\.)?github\.com[/:]##; s#\.git$##' | cut -d/ -f1-2 | sed -E 's#[?\#].*$##')
  case "$OWNER_REPO" in */*) ;; *) echo "看不懂这个地址：$RAW"; ask_url; return;; esac
  URL="https://github.com/$OWNER_REPO.git"
  git remote remove origin 2>/dev/null
  git remote add origin "$URL"
  echo "仓库地址：$URL"
}
if ! git remote get-url origin >/dev/null 2>&1; then
  ask_url
else
  echo "当前仓库地址：$(git remote get-url origin)"
  read -r -p "直接回车继续；要换地址输入 n：" CH
  [ "$CH" = "n" ] || [ "$CH" = "N" ] && ask_url
fi
echo "开始上传。如果提示输入：Username 填 GitHub 用户名，Password 填 Personal Access Token（不是登录密码）。"
if git push -u origin main; then
  echo "上传成功：$(git remote get-url origin)"
else
  echo ""
  echo "被拒绝了：GitHub 上的仓库不是空的（多半是建仓库时勾了 Add a README）。"
  read -r -p "如果里面只有 GitHub 自动生成的 README，输入 y 用本机的版本覆盖它（本机已有更完整的 README）：" OK
  if [ "$OK" = "y" ] || [ "$OK" = "Y" ]; then
    git push -u --force origin main && echo "上传成功：$(git remote get-url origin)"
  else
    echo "没有覆盖。可以把那个 README 在 GitHub 网页上删掉后再双击一次。"
  fi
fi
read -r -p "按回车键退出" _
