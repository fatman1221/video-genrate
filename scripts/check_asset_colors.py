"""
角色资产色偏量化验收 —— 门禁脚本。

背景：出图"看起来颜色不对"是主观的，容易被误判成抽卡运气问题。
本脚本把 spec 里的硬性色彩指标量化成可复现的判据，生成前先跑
`--baseline` 记录基线，生成后跑 `--check` 比对，色偏超阈值即判 FAIL。

核心思路：
  · 头发：取头部区域**最暗的 25% 像素**（即真实发丝，避开背景与发饰）
    判据 = 明度 V 不能过低（发黑）+ R-B 差值（暖调栗红）+ 色相在 10–35° 区间
  · 尾巴：取左右两侧中部（扇形尾区）
    判据 = 饱和度 S 必须低（象牙白是低饱和），S > 15% 即偏米黄/暖黄
  · 亮度：全图灰度均值，防欠曝/过曝（本组基准约 147）

用法：
  python scripts/check_asset_colors.py --baseline <目录>   # 记录基线
  python scripts/check_asset_colors.py --check <目录>      # 比对并判 PASS/FAIL
  python scripts/check_asset_colors.py --dir <目录>        # 只打印实测值
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# ---- 验收阈值（按 spec 写死，非经验值）----
HAIR_MIN_V = 24.0        # 发丝明度下限（低于此= 压死的黑）
HAIR_MIN_WARMTH = 38.0   # R-B 差下限（低于此= 冷调黑棕）
HAIR_HUE_RANGE = (8.0, 40.0)   # 栗红/红棕区间
TAIL_MAX_S = 15.0        # 尾巴饱和度上限（超过= 偏米黄/暖黄）
LUMA_MIN, LUMA_MAX = 110.0, 190.0   # 全图亮度合理区间

COLORS = ["01", "02", "03", "04", "05", "06", "07", "08", "09"]


def _hsv(rgb: np.ndarray) -> tuple[float, float, float]:
    import colorsys
    r, g, b = [float(c) / 255.0 for c in rgb]
    return colorsys.rgb_to_hsv(r, g, b)


def measure(path: Path) -> dict:
    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(float)
    h, w, _ = a.shape

    # ⚠️ 构图无关的统计：不能用固定相对 ROI（背视图/半身/hero 构图下
    # 固定框会落到背景、肩膀或深色衣服上，产生误判）。
    # 关键：以「全图最暗 10% 像素」作为发丝采样层——在任何构图里
    # 发丝必然属于画面最暗的一档，而背景（浅灰）、朱红裙、金饰、
    # 象牙白狐尾都显著更亮，因此这个分层对 9 种构图都成立。
    # ⚠️ 绝不能用「暖差 > 阈值」来筛发丝：近黑发的 R-B 很小，
    # 会被筛掉，导致黑发版反而测出更高的暖差（指标反向）。
    flat = a.reshape(-1, 3)
    lum = flat @ [0.299, 0.587, 0.114]
    hairpx = flat[lum <= np.percentile(lum, 10)]
    hm = hairpx.mean(0)
    hh, hs, hv = _hsv(hm)

    # 尾巴：全图高亮低饱和像素（象牙白毛发的特征签名）
    hi = flat[(lum > np.percentile(lum, 88)) & ((flat[:, 0] - flat[:, 2]) > 8)]
    tm = hi.mean(0) if len(hi) > 200 else flat[lum >= np.percentile(lum, 85)].mean(0)
    th, ts, tv = _hsv(tm)

    luma = float(lum.mean())
    return {
        "file": path.name,
        "hair_rgb": [round(float(x), 1) for x in hm],
        "hair_hue": round(hh * 360, 1),
        "hair_sat": round(hs * 100, 1),
        "hair_val": round(hv * 100, 1),
        "hair_warmth": round(float(hm[0] - hm[2]), 1),
        "hair_px": int(len(hairpx)),
        "tail_rgb": [round(float(x), 1) for x in tm],
        "tail_sat": round(ts * 100, 1),
        "tail_val": round(tv * 100, 1),
        "luma": round(luma, 1),
    }


def judge(m: dict) -> list[str]:
    errs = []
    if m["hair_val"] < HAIR_MIN_V:
        errs.append(f"发丝明度 {m['hair_val']}% < {HAIR_MIN_V}%（发黑，spec 要栗色）")
    if m["hair_warmth"] < HAIR_MIN_WARMTH:
        errs.append(f"发丝 R-B 差 {m['hair_warmth']} < {HAIR_MIN_WARMTH}（偏冷调黑棕）")
    if not (HAIR_HUE_RANGE[0] <= m["hair_hue"] <= HAIR_HUE_RANGE[1]):
        errs.append(f"发色相 {m['hair_hue']}deg 不在 {HAIR_HUE_RANGE} 区间（应为栗红/红棕）")
    if m["tail_sat"] > TAIL_MAX_S:
        errs.append(f"尾巴饱和度 {m['tail_sat']}% > {TAIL_MAX_S}%（偏米黄/暖黄，spec 要象牙白）")
    if not (LUMA_MIN <= m["luma"] <= LUMA_MAX):
        errs.append(f"全图亮度 {m['luma']} 不在 {LUMA_MIN}~{LUMA_MAX}（欠曝/过曝）")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="直接打印实测值")
    ap.add_argument("--baseline", help="记录基线到 baseline.json")
    ap.add_argument("--check", help="与 baseline.json 比对")
    ap.add_argument("--out", default="scripts/_color_baseline.json")
    a = ap.parse_args()

    if not (a.dir or a.baseline or a.check):
        ap.print_help()
        return 2
    d = Path(a.dir or a.baseline or a.check).resolve()
    files = sorted(p for p in d.glob("0*.png") if "_ref" not in p.name)
    if not files:
        print(f"目录下没有 0*.png: {d}", file=sys.stderr)
        return 2

    results = [measure(p) for p in files]

    if a.baseline:
        Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"基线已写入 {a.out}（{len(results)} 张）")
    elif a.check:
        bp = Path(a.out)
        if not bp.exists():
            print(f"没有基线文件 {bp}，先跑 --baseline", file=sys.stderr)
            return 2
        base = {b["file"]: b for b in json.loads(bp.read_text(encoding="utf-8"))}
        print(f"{'文件':<24}{'发丝V':>7}{'发丝暖差':>9}{'发色相':>8}"
              f"{'尾饱和':>8}{'亮度':>7}  判定")
        fails = 0
        for m in results:
            errs = judge(m)
            b = base.get(m["file"])
            mark = "PASS" if not errs else "FAIL"
            if errs:
                fails += 1
            drift = ""
            if b:
                d_w = round(m["hair_warmth"] - b["hair_warmth"], 1)
                d_t = round(m["tail_sat"] - b["tail_sat"], 1)
                drift = f"  (暖差{d_w:+}, 尾饱和{d_t:+})"
            print(f"{m['file']:<24}{m['hair_val']:>7}{m['hair_warmth']:>9}{m['hair_hue']:>8}"
                  f"{m['tail_sat']:>8}{m['luma']:>7}  {mark}{drift}")
            for e in errs:
                print(f"    - {e}")
        print(f"\n{len(results)-fails}/{len(results)} 通过")
        return 1 if fails else 0
    else:
        for m in results:
            print(f"{m['file']:<24} 发丝 V={m['hair_val']}% 暖差={m['hair_warmth']} "
                  f"色相={m['hair_hue']}deg | 尾饱和={m['tail_sat']}% | 亮度={m['luma']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
