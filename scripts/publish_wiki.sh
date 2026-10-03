#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 把 wiki/ 目录发布到 GitHub Wiki。
#
#   ./scripts/publish_wiki.sh              推送（首次会提示先初始化）
#   NO_PROXY_GIT=1 ./scripts/publish_wiki.sh   绕过本机失效的 git 代理
#   WIKI_REPO=<url> ./scripts/publish_wiki.sh  指定 wiki 仓库地址
#
# 背景：GitHub 的 wiki 后端是一个独立的 git 仓库（<repo>.wiki.git），
# 但它**只有在网页端创建过第一个页面后才会生成**。没有 REST/GraphQL API
# 可以建 wiki 页，所以首次必须手工点一下，之后的更新都可以用本脚本推。
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/wiki"

[ -d "$SRC" ] || { echo "✗ 找不到 wiki 源目录：$SRC" >&2; exit 1; }

# --- 推导 wiki 仓库地址 ------------------------------------------------------
if [ -n "${WIKI_REPO:-}" ]; then
  WIKI_URL="$WIKI_REPO"
else
  ORIGIN="$(git -C "$ROOT" remote get-url origin 2>/dev/null || true)"
  [ -n "$ORIGIN" ] || { echo "✗ 本仓库没有 origin，请用 WIKI_REPO=<url> 指定" >&2; exit 1; }
  WIKI_URL="${ORIGIN%.git}.wiki.git"
fi

# 可选的代理绕过（本机 ~/.gitconfig 可能指向未启动的代理客户端）
GIT_C=()
[ "${NO_PROXY_GIT:-0}" = "1" ] && GIT_C=(-c http.proxy= -c https.proxy=)

echo "→ 源目录 : $SRC"
echo "→ wiki 仓库: $WIKI_URL"
echo

# --- 克隆（工作副本放临时目录）----------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if ! GIT_TERMINAL_PROMPT=0 git "${GIT_C[@]}" clone --quiet "$WIKI_URL" "$WORK/repo" 2>"$WORK/err"; then
  if grep -qi "not found" "$WORK/err"; then
    WEB="${WIKI_URL%.wiki.git}/wiki"
    cat >&2 <<MSG
✗ wiki 仓库还不存在：$WEB

  GitHub 只在「网页端保存过第一个页面」之后才创建 wiki 的 git 后端，
  没有任何 REST/GraphQL API 可以代劳 —— 实测直接 push 会返回
  "Repository not found"，git 并不会替你把仓库建出来。

  请手工点一次（约 10 秒）：

    1. 打开 $WEB/_new
    2. 标题填 Home，正文随便写一个字，点 Save
       （内容会在下一步被本脚本整体覆盖，不用在意）
    3. 回来重新运行本脚本

MSG
    exit 2
  fi
  echo "✗ 克隆失败：" >&2
  cat "$WORK/err" >&2
  echo >&2
  echo "  若报的是代理相关错误，试：NO_PROXY_GIT=1 $0" >&2
  exit 1
fi

# --- 同步内容（删除多余页面，保证 wiki 与源目录一致）------------------------
cd "$WORK/repo"
find . -maxdepth 1 -name '*.md' -not -name '_*' -delete
find . -maxdepth 1 -name '_*.md' -delete
cp "$SRC"/*.md .

# 若上游已有内容（首次初始化时创建的空页），会被上面的删除覆盖；
# 出现非 Home 的遗留页时提醒一次。
LEFT="$(find . -maxdepth 1 -name '*.md' | sed 's|^\./||' | sort)"
echo "→ 将发布以下页面："
echo "$LEFT" | sed 's/^/    /'
echo

git add -A
if git diff --cached --quiet; then
  echo "✓ 内容无变化，无需推送。"
  exit 0
fi

COUNT="$(echo "$LEFT" | wc -l | tr -d ' ')"
git -c user.name="$(git -C "$ROOT" config user.name || echo wiki-bot)" \
    -c user.email="$(git -C "$ROOT" config user.email || echo wiki-bot@local)" \
    commit --quiet -m "docs(wiki): 同步 ${COUNT} 个页面

源目录：wiki/
由 scripts/publish_wiki.sh 自动发布。"

GIT_TERMINAL_PROMPT=0 git "${GIT_C[@]}" push --quiet origin HEAD:master 2>/dev/null \
  || GIT_TERMINAL_PROMPT=0 git "${GIT_C[@]}" push --quiet origin HEAD:main

echo "✓ 已发布 ${COUNT} 个页面 → $(echo "$WIKI_URL" | sed 's/\.wiki\.git$//')/wiki"
