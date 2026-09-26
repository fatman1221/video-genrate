#!/usr/bin/env bash
# 给 intro.html 生成整页长截图。
#
# 难点：agent-browser 只能以 deviceScaleFactor=1 截图，在 Retina 屏上文字会发虚。
# 做法：把视口放大到 1920，再用 CSS zoom=1.5 让页面仍按 1280 布局，
#       于是输出像素翻 1.5 倍而不改变版式（不会掉进窄屏媒体查询）。
#       1920 / 1.5 = 1280 —— 与设计稿宽度一致。
#
# 用法：./scripts/shot_intro.sh [缩放比] [输出路径]
#       ./scripts/shot_intro.sh 2 /tmp/intro@2x.png
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin:$PATH

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/intro.html"
SCALE="${1:-1.5}"
OUT="${2:-$ROOT/intro-full.png}"

# 用到 Pillow 打印尺寸，按可用性逐级探测（与 dev.sh 同一套顺序）：
#   $PY  >  项目内 .venv  >  已有托管环境  >  PATH 上的 python3
detect_python() {
  if [ -n "${PY:-}" ]; then printf '%s' "$PY"; return; fi
  local c
  for c in "$ROOT/.venv/bin/python" "$ROOT/backend/.venv/bin/python" \
           /Users/zhangdongke/.workbuddy/binaries/python/envs/default/bin/python; do
    [ -x "$c" ] && { printf '%s' "$c"; return; }
  done
  command -v python3
}
PY="$(detect_python)"
if ! "$PY" -c "import PIL" >/dev/null 2>&1; then
  echo "提示：当前 Python 缺少 Pillow（$PY），无法打印图片尺寸。" >&2
  echo "      装一下：$PY -m pip install Pillow" >&2
fi

# 页面按 1280 设计；视口 = 1280 * SCALE，zoom 抵消回 1280
BASE_W=1280
VIEW_W=$("$PY" -c "print(round($BASE_W * $SCALE))")

# 拷贝成临时文件名：agent-browser 对 file:// 有缓存，同名重开会拿到旧页面
TMP="/tmp/.intro_shot_$$.html"
cp "$SRC" "$TMP"
trap 'rm -f "$TMP"' EXIT

agent-browser close >/dev/null 2>&1 || true
agent-browser set viewport "$VIEW_W" 1200 >/dev/null
agent-browser open "file://$TMP" >/dev/null
sleep 3
agent-browser eval "document.documentElement.style.zoom='$SCALE'; \
  document.documentElement.scrollWidth + 'x' + document.documentElement.scrollHeight" \
  | tail -1
agent-browser screenshot --full "$OUT" >/dev/null
agent-browser close >/dev/null 2>&1 || true

echo "→ $OUT"
"$PY" - "$OUT" <<'PY'
import os, sys
from PIL import Image
im = Image.open(sys.argv[1])
w, h = im.size
print(f"   {w} x {h}  {os.path.getsize(sys.argv[1])/1024/1024:.1f} MB")
PY
