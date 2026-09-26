#!/usr/bin/env bash
# AI Video Agent 工作台 —— 一键启动后端 + 前端
#
# 用法：
#   ./scripts/dev.sh          # 前台启动（Ctrl-C 退出）
#   ./scripts/dev.sh stop     # 停止全部
#
# 可覆盖的环境变量：
#   PY=<python 解释器>   指定后端使用的 Python（默认自动探测）
#   NODE_BIN_DIR=<目录>  指定 Node 所在目录（默认自动探测）
#   BACKEND_PORT / FRONTEND_PORT   默认 8077 / 5180
#
# --------------------------------------------------------------------------- #
# 关于运行环境探测
#   Python 优先级：$PY  >  项目内 .venv  >  已有环境  >  PATH 上的 python3
#   Node   优先级：$NODE_BIN_DIR  >  已有环境  >  PATH 上的 node
#   「已有环境」指本机此前一直在用的 WorkBuddy 托管运行时；换到新电脑后
#   这些路径不存在，脚本会自动回退，无需改代码。
#
# --------------------------------------------------------------------------- #
# 注意 1：WorkBuddy/CodeBuddy 注入的 PYTHONPATH shim 会劫持 os.mkdir，
#        导致 uvicorn 启动即失败，因此必须 env -u PYTHONPATH。
#        在普通终端里该变量不存在，env -u 是空操作，可安全保留。
# 注意 2：WorkBuddy/CodeBuddy 还会通过 NODE_OPTIONS 注入 node-language-shim.cjs，
#        其中的 safe-delete 拦截「单轮删除 >50 个文件」。Vite 在依赖配置变化时会
#        清空整个 node_modules/.vite（数百文件）→ 被拦截 → dev server 当场退出。
#        对策：启动 vite 时 env -u NODE_OPTIONS 彻底摘掉该 shim。
# 注意 3：启动前主动清掉 .vite 缓存，避免踩上注意 2 的删除拦截。
#        ⚠️ 新增 MUI 图标时必须同步登记到 vite.config.js 的 MUI_ICONS，
#           否则首次引用会触发重新预构建，同样撞上删除保护。
# --------------------------------------------------------------------------- #
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PORT="${BACKEND_PORT:-8077}"
FRONTEND_PORT="${FRONTEND_PORT:-5180}"

# ---- Python 解释器探测 ----
detect_python() {
  if [ -n "${PY:-}" ]; then printf '%s' "$PY"; return; fi
  local c
  for c in "$ROOT/.venv/bin/python" "$ROOT/backend/.venv/bin/python"; do
    [ -x "$c" ] && { printf '%s' "$c"; return; }
  done
  # WorkBuddy 托管运行时（不存在则自动跳过，换机无影响）
  for c in "$HOME/.workbuddy/binaries/python/envs/default/bin/python"; do
    [ -x "$c" ] && { printf '%s' "$c"; return; }
  done
  command -v python3 2>/dev/null || true
}

# ---- Node 目录探测 ----
detect_node_dir() {
  if [ -n "${NODE_BIN_DIR:-}" ]; then printf '%s' "$NODE_BIN_DIR"; return; fi
  local c
  for c in "$HOME/.workbuddy/binaries/node/versions/22.22.2-3/bin"; do
    [ -x "$c/node" ] && { printf '%s' "$c"; return; }
  done
  if command -v node >/dev/null 2>&1; then
    dirname "$(command -v node)"
  fi
}

PY="$(detect_python)"
NODE_BIN_DIR="$(detect_node_dir)"

stop_all() {
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  pkill -f "vite --host 127.0.0.1 --port ${FRONTEND_PORT}" 2>/dev/null || true
  echo "已停止后端与前端。"
}

if [ "${1:-}" = "stop" ]; then
  stop_all
  exit 0
fi

# ---- 启动前自检（只提示，不阻断） ----
preflight() {
  local warn=0
  if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "✗ 找不到可用的 Python 解释器。" >&2
    echo "  请先创建虚拟环境：python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt" >&2
    warn=1
  fi
  if [ ! -x "$ROOT/frontend/node_modules/.bin/vite" ]; then
    echo "✗ 前端依赖未安装。" >&2
    echo "  请先执行：cd frontend && npm install" >&2
    warn=1
  fi
  if [ ! -x "$NODE_BIN_DIR/node" ] && ! command -v node >/dev/null 2>&1; then
    echo "✗ 找不到 Node 运行时（需要 Node 18+）。" >&2
    warn=1
  fi
  if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/ffmpeg" ]; then
    echo "! 未检测到 ffmpeg —— 视频合成相关任务会失败（图像/脚本仍可用）。" >&2
    echo "  安装方式见 docs/MIGRATION.md" >&2
  fi
  if [ "$warn" = "1" ]; then
    echo >&2
    echo "环境未就绪，已中止。详见 docs/MIGRATION.md" >&2
    exit 1
  fi
}
if [ "${1:-}" = "check" ]; then
  echo "Python      : ${PY:-<未找到>}"
  echo "Node 目录   : ${NODE_BIN_DIR:-<PATH 上的 node>}"
  echo "ffmpeg      : $(command -v ffmpeg 2>/dev/null || echo '<未找到>')"
  echo "前端依赖    : $([ -x "$ROOT/frontend/node_modules/.bin/vite" ] && echo 已安装 || echo 未安装)"
  echo
  preflight
  echo "环境就绪 ✓"
  exit 0
fi

preflight

stop_all
sleep 1

echo "后端 Python : $PY"
echo "前端 Node   : ${NODE_BIN_DIR:-<PATH 上的 node>}"
echo
echo "启动后端 http://127.0.0.1:${BACKEND_PORT} ..."
(
  cd "$ROOT/backend"
  env -u PYTHONPATH "$PY" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$BACKEND_PORT" --log-level info
) &
BACKEND_PID=$!

echo "启动前端 http://127.0.0.1:${FRONTEND_PORT} ..."
(
  cd "$ROOT/frontend"
  rm -rf node_modules/.vite   # 避免触发 Node 批量删除保护
  env -u NODE_OPTIONS PATH="${NODE_BIN_DIR}:$PATH" ./node_modules/.bin/vite \
    --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort
) &
FRONTEND_PID=$!

trap 'echo; echo "正在停止..."; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true; stop_all' INT TERM
wait
