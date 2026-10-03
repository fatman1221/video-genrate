#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 把 wiki/ 目录发布到 GitHub Wiki。
#
#   ./scripts/publish_wiki.sh                 推送（首次会提示先初始化）
#   ./scripts/publish_wiki.sh --wait          首次用：脚本在这儿等你点完网页端，自动接上发布
#   ./scripts/publish_wiki.sh --wait 3600     自定等待上限（秒，默认 1800）
#   NO_PROXY_GIT=1 ./scripts/publish_wiki.sh  绕过本机失效的 git 代理
#   WIKI_REPO=<url> ./scripts/publish_wiki.sh 指定 wiki 仓库地址
#
# 背景：GitHub 的 wiki 后端是一个独立的 git 仓库（<repo>.wiki.git），
# 但它**只有在网页端创建过第一个页面之后才会生成**。既没有 REST/GraphQL API
# 可以建 wiki 页，直接 push 也不会把仓库建出来。所以首次必须手工点一下。
# 首次推荐 `--wait`：终端把链接打给你，你点完 Save，这边自己接上发布，
# 不用来回通知。之后的所有更新都可以直接跑，不再需要手工介入。
#
# 网络容错：脚本会区分「还没初始化」和「瞬时网络错误」——
#   前者 → 按 --wait 等待；
#   后者 → 指数退避重试（连续 6 次才放弃），不会因为一次抖动就中断。
#   同时强制 HTTP/1.1，规避部分网络下的 "Error in the HTTP2 framing layer"。
# ---------------------------------------------------------------------------
set -euo pipefail

# --- 参数 -------------------------------------------------------------------
WAIT_SECS=0
MAX_TRANSIENT=6

case "${1:-}" in
  "")        ;;
  --wait)    WAIT_SECS="${2:-1800}" ;;
  --wait=*)  WAIT_SECS="${1#--wait=}" ;;
  -h|--help)
    sed -n '/^#   \.\/scripts\/publish_wiki.sh/p;/^#   NO_PROXY_GIT/p;/^#   WIKI_REPO/p' "$0" \
      | sed 's/^#   //'
    exit 0 ;;
  *)
    echo "未知参数：$1（可用：--wait [秒]）" >&2
    exit 64 ;;
esac
case "$WAIT_SECS" in
  ''|*[!0-9]*) echo "✗ --wait 需要整数秒，收到：$WAIT_SECS" >&2; exit 64 ;;
esac

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

# 网页端地址（用于提示用户去哪儿点）
WEB="${WIKI_URL%.wiki.git}/wiki"

# -c 覆盖项：可选绕过本机失效的代理；强制 HTTP/1.1 规避 HTTP/2 framing 抖动
GIT_C=()
[ "${NO_PROXY_GIT:-0}" = "1" ] && GIT_C=(-c http.proxy= -c https.proxy=)
GIT_C+=(-c http.version=HTTP/1.1)

echo "→ 源目录   : $SRC"
echo "→ wiki 仓库: $WIKI_URL"
echo

# --- 错误分类：missing（还没初始化）/ transient（网络抖动）/ fatal ------------
classify() {
  if grep -qi "not found" "$1"; then
    echo missing
  elif grep -qiE "HTTP2 framing|HTTP/2|Couldn't connect|Could not resolve|Connection (reset|refused|timed out)|SSL_ERROR|SSL_connect|GnuTLS|TLS|early EOF|RPC failed|Empty reply|unexpected disconnect|Operation timed out" "$1"; then
    echo transient
  else
    echo fatal
  fi
}

# --- 克隆（工作副本放临时目录）----------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ATTEMPT=0
TRANSIENT=0
REPO=""
DEADLINE=$(( $(date +%s) + WAIT_SECS ))
GUIDED=0
LAST_REPORT=0

while :; do
  ATTEMPT=$((ATTEMPT + 1))
  REPO="$WORK/repo.$ATTEMPT"

  if GIT_TERMINAL_PROMPT=0 git "${GIT_C[@]}" clone --quiet "$WIKI_URL" "$REPO" 2>"$WORK/err"; then
    break
  fi

  KIND="$(classify "$WORK/err")"

  # --- 1) 真正的错误：直接退出，不要傻等 -----------------------------------
  if [ "$KIND" = fatal ]; then
    echo "✗ 克隆失败（非网络问题，不重试）：" >&2
    sed 's/^/    /' "$WORK/err" >&2
    echo >&2
    echo "  若报的是代理相关错误，试：NO_PROXY_GIT=1 $0" >&2
    exit 1
  fi

  # --- 2) 网络抖动：退避重试 -------------------------------------------------
  if [ "$KIND" = transient ]; then
    TRANSIENT=$((TRANSIENT + 1))
    if [ "$TRANSIENT" -gt "$MAX_TRANSIENT" ]; then
      echo >&2
      echo "✗ 连续 $MAX_TRANSIENT 次网络错误，放弃：" >&2
      sed 's/^/    /' "$WORK/err" >&2
      exit 1
    fi
    BACKOFF=$((TRANSIENT * 5))
    echo "  网络抖动（第 $TRANSIENT/$MAX_TRANSIENT 次），${BACKOFF}s 后重试…" >&2
    sed 's/^/      /' "$WORK/err" >&2
    sleep "$BACKOFF"
    continue
  fi

  # --- 3) wiki 尚未初始化 -----------------------------------------------------
  TRANSIENT=0

  if [ "$GUIDED" = 0 ]; then
    GUIDED=1
    cat >&2 <<MSG
✗ wiki 仓库还不存在：$WEB

  GitHub 只在「网页端保存过第一个页面」之后才创建 wiki 的 git 后端，
  没有 REST/GraphQL API 可以代劳，直接 push 也不会把仓库建出来。

  请手工点一次（约 10 秒）：

    1. 打开 $WEB/_new
    2. 标题填 Home，正文随便写一个字，点 Save
       （内容会在下一步被本脚本整体覆盖，不用在意）
MSG
  fi

  if [ "$WAIT_SECS" -le 0 ]; then
    echo >&2
    echo "  3. 回来重新运行本脚本；或改成 $0 --wait，让脚本在这儿等你点完。" >&2
    echo >&2
    exit 2
  fi

  NOW="$(date +%s)"
  if [ "$NOW" -ge "$DEADLINE" ]; then
    echo >&2
    echo "✗ 等待超时（${WAIT_SECS}s），wiki 仓库仍未出现。" >&2
    exit 2
  fi

  # 刚开始 5s 一次（点完立刻接上），之后放宽到 15s；每 30s 报一次进度
  ELAPSED=$((NOW - (DEADLINE - WAIT_SECS)))
  if [ "$ELAPSED" -lt 60 ]; then INTERVAL=5; else INTERVAL=15; fi
  if [ $(( NOW - LAST_REPORT )) -ge 30 ]; then
    LAST_REPORT="$NOW"
    echo "  等待你点 Save… 已等 ${ELAPSED}s / ${WAIT_SECS}s" >&2
  fi
  sleep "$INTERVAL"
done

if [ "$ATTEMPT" -gt 1 ]; then
  echo "→ wiki 仓库已就绪（第 ${ATTEMPT} 次探测）"
  echo
fi

# --- 同步内容（镜像式覆盖，保证 wiki 与源目录一致）--------------------------
cd "$REPO"
find . -maxdepth 1 -name '*.md' -not -name '_*' -delete
find . -maxdepth 1 -name '_*.md' -delete
cp "$SRC"/*.md .

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

PUSHED=0
for i in 1 2 3; do
  if GIT_TERMINAL_PROMPT=0 git "${GIT_C[@]}" push --quiet origin HEAD:master 2>/dev/null \
     || GIT_TERMINAL_PROMPT=0 git "${GIT_C[@]}" push --quiet origin HEAD:main 2>/dev/null; then
    PUSHED=1
    break
  fi
  echo "  推送失败（第 $i/3 次），${i}0s 后重试…" >&2
  sleep "$((i * 10))"
done
[ "$PUSHED" = 1 ] || { echo "✗ 推送连续失败 3 次，请检查网络后重跑。" >&2; exit 1; }

echo "✓ 已发布 ${COUNT} 个页面 → $WEB"
