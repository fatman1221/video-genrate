#!/usr/bin/env python
"""角色九视图（3x3 转面设定图）—— 逐角度走 Qwen-Image-Edit 锁人物一致性，再拼版。

为什么逐角度而不是「一张图里画九格」：
  单张多格图里模型会自行发挥，九格的脸往往不是同一个人；逐角度 + 同一张参考图，
  每一格都锚在同一个 identity 上，一致性可控得多。

用法：
  python scripts/gen_turnaround.py \
      --reference backend/storage/characters/proj_4f2f0c178a37/character_dd0970bf3b.png \
      --out-dir backend/storage/temp/stills/turnaround \
      --sheet backend/storage/temp/stills/turnaround_sheet.png

已存在的单格会跳过 → 中断后可续跑。
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_still import (ROOT, apply_overrides, build_workflow, download,  # noqa: E402
                       prep_reference, submit, upload_image, wait)

# 标准转面九角度：(序号, 中文标签, 该格的视角描述)
VIEWS = [
    ("01", "正面", "同一个角色面向镜头正面站立，全身体态标准，双手自然垂放"),
    ("02", "左前 45°", "同一个人身体向左旋转 45 度，左前方四分之三侧身，"
                       "脸和身体正面同时可见，五官清晰可辨，脸部朝向左前方"),
    ("03", "正左侧 90°", "同一个人身体向左旋转 90 度，标准正侧面轮廓，脸部朝向画面左侧"),
    ("04", "左后 135°", "同一个角色身体向左旋转 135 度，呈左后方四分之三背面，能看到后脑与侧脸轮廓边缘。"
                       "镜头为远景，人物从头到脚完整入画，能看到完整的双腿与鞋子，人物在画面中较小"),
    ("05", "正背面 180°", "同一个角色完全背对镜头站立 180 度，展示后背与后脑，看不到面部五官"),
    ("06", "右后 135°", "同一个角色身体向右旋转 135 度，呈右后方四分之三背面，能看到后脑与侧脸轮廓边缘。"
                       "镜头为远景，人物从头到脚完整入画，能看到完整的双腿与鞋子，人物在画面中较小"),
    ("07", "正右侧 90°", "同一个角色身体向右旋转 90 度，呈标准正侧面轮廓，脸部朝向画面左侧"),
    ("08", "右前 45°", "同一个角色身体向右旋转 45 度，呈右前方四分之三侧身，脸部仍朝向镜头方向。"
                      "镜头为远景，人物从头到脚完整入画，能看到完整的双腿与鞋子，人物在画面中较小"),
    ("09", "头部特写", "同一个角色的头部与肩部正面特写，五官清晰，表情沉静"),
]

# 片中林野的服装（照抄《归途信号》关键帧，不要凭想象写）
CLOTHES = (
    "深灰色哑光立领拉链工装夹克，前中拉链，胸前两枚斜插袋，左臂一枚方形小袋，"
    "腰部收束，剪裁合体利落，内搭深灰高领内衬，下身同色系深灰直筒长裤与深色短靴"
)
# ⚠️ 特写格只能用「上身」服装描述 —— 把裤子/靴子写进特写提示词，模型会试图
#    把它们也画进来，导致画面崩坏（实测：出成一条扭曲融化的裤腿 + 破布斗篷）
CLOTHES_UPPER = "深灰色哑光立领拉链夹克与深灰高领内衬"
# 片中林野的面部特征（关键：这是「被三年深空值守磨过」的中年人，不是清秀青年）
FACE = (
    "35岁中国男性，短寸头，浓眉且眉峰上扬，眉心有浅竖纹，眼窝深邃，"
    "鼻梁高挺，颧骨清晰，下颌线利落，面容清瘦硬朗但不显老，"
    "皮肤有真实质感与细微纹理，眼神沉静锐利克制，精神饱满"
)
REF_KEEP = (
    "保持与参考图完全同一个人：相同的面部特征、相同发型、相同服装材质与配色，"
    "同一张脸，不得改变年龄与五官比例"
)
# 电影感布光是本版核心诉求：暗调、侧逆光、有明暗层次，而不是影棚平光
LIGHT = (
    "电影级布光：冷调侧逆光为主光，人物面部曝光准确、五官清晰明亮，"
    "一侧脸颊受光、另一侧落入柔和阴影，发梢与肩线有细窄轮廓光勾边，"
    "中深灰渐变背景，低饱和冷灰色调，浅景深，35mm胶片颗粒，高级电影质感"
)

STYLE = (
    f"远景全身角色设定图。画面中只有一个人物，完整全身入画，从头顶到脚底全部可见，"
    f"竖直站立，人物居中，镜头拉远，人物高度约占画面 85%，头顶与脚下各留少量背景空间。"
    f"{FACE}。身着{CLOTHES}。{LIGHT}。"
)
NEG_COMMON = (
    "头盔，颈环，太空头盔，护甲，面罩，不同的人，换脸，年龄变化，"
    "年轻化，娃娃脸，婴儿肥，过度磨皮，皮肤光滑无纹理，细眉，淡眉，"
    "两个人，多重人影，双重曝光，重影，克隆，镜像复制，多个人物，"
    "低质量，模糊，变形，多手多指，肢体错乱，文字，水印，logo，"
    "杂乱背景，过曝，噪点，卡通感，3D渲染感，塑料感，"
    "影棚平光，证件照，纯白背景，反光板打光，"
    "苍老，显老，老年，50岁，病态消瘦，面部凹陷，颧骨塌陷，"
    "半透明，抠图感，人物与背景融合，身体残缺，"
    "欠曝，画面过暗，全黑，看不清面部，主体淹没在背景里"
)
# ⚠️ 「特写/半身」与「全身」互斥 —— 负向词必须按格区分，
#    否则第 9 格（头部特写）会被自己的负向词按平。这是 §7 踩坑 4 的同类陷阱。
# ⚠️ 也不要负「口袋/袖袋/工装裤」—— 那些在片里是**真实存在**的服装细节。
NEG_FULL = NEG_COMMON + "，特写，半身，半身像，胸像，近景，只有头部，只有上半身，" \
                         "裁切身体，画面裁掉腿部，腰以上构图，人物占满画面"
NEG_HEAD = NEG_COMMON + "，全身，远景，双人，半身以下"


HEAD_STYLE = (
    f"角色设定图，头部与肩部正面特写，半身近景，头顶到胸口完整入画，人物居中。"
    f"{FACE}。上身穿着{CLOTHES_UPPER}。{LIGHT}。面部五官清晰完整，超精细皮肤纹理。"
)


def build_prompt(code: str, view_desc: str) -> str:
    style = HEAD_STYLE if code == "09" else STYLE
    return f"{style}{view_desc}。{REF_KEEP}"


def main() -> int:
    ap = argparse.ArgumentParser(description="角色九视图转面图")
    ap.add_argument("--reference", required=True, help="人物基准参考图（锁脸）")
    ap.add_argument("--out-dir", required=True, help="单格输出目录")
    ap.add_argument("--sheet", default="", help="拼版输出路径（留空则不拼）")
    ap.add_argument("--cell-width", type=int, default=1024)
    ap.add_argument("--cell-height", type=int, default=1280)
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--cfg", type=float, default=0.0, help="0 = 模板默认 (2.5)；低 cfg 会让构图/角度指令失效")
    ap.add_argument("--sampler", default="", help="空 = 模板默认 euler")
    ap.add_argument("--scheduler", default="", help="空 = 模板默认 simple")
    ap.add_argument("--ref-fit", default="crop", choices=["crop", "pad", "stretch"],
                    help="参考图适配目标比例的方式（默认 crop，防脸被拉伸）")
    ap.add_argument("--seed", type=int, default=20261004)
    ap.add_argument("--host", default="http://127.0.0.1:8188")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--only", default="", help="只跑指定序号，逗号分隔，如 03,05")
    args = ap.parse_args()

    base = args.host.rstrip("/")
    ref = Path(args.reference)
    if not ref.is_absolute():
        ref = (ROOT / ref).resolve()
    if not ref.exists():
        print(f"参考图不存在: {ref}", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    targets = [v for v in VIEWS if not only or v[0] in only]

    print(f"[参考图] {ref.name}")
    print(f"[输出] {out_dir}")
    print(f"[规格] 单格 {args.cell_width}x{args.cell_height} · {len(targets)} 个角度 · {args.steps} 步\n")

    ref_use = prep_reference(ref, args.cell_width, args.cell_height, args.ref_fit)
    if ref_use != ref:
        print(f"[参考图适配] {args.ref_fit} -> {ref_use.name}")
    remote_ref = upload_image(base, ref_use)
    print(f"[参考图已上传] {remote_ref}\n")

    mapping_base = {
        "{{width}}": args.cell_width,
        "{{height}}": args.cell_height,
        "{{reference_image}}": remote_ref,
    }

    done, failed = [], []
    for idx, (code, label, desc) in enumerate(targets, 1):
        out = out_dir / f"view_{code}.png"
        if out.exists() and out.stat().st_size > 1024:
            print(f"[{idx}/{len(targets)}] {code} {label} — 已存在，跳过")
            done.append(out)
            continue
        print(f"[{idx}/{len(targets)}] {code} {label} — 生成中 ...", flush=True)
        mapping = dict(mapping_base)
        mapping["{{prompt}}"] = build_prompt(code, desc)
        mapping["{{negative_prompt}}"] = NEG_HEAD if code == "09" else NEG_FULL
        mapping["{{seed}}"] = args.seed + int(code)
        wf = build_workflow("qwen_edit_scene.json", mapping)
        apply_overrides(wf, steps=args.steps, cfg=args.cfg,
                        sampler=args.sampler, scheduler=args.scheduler)
        for node in wf.values():
            if node.get("class_type") == "SaveImage":
                node["inputs"]["filename_prefix"] = f"turn_{code}"
        try:
            pid = submit(base, wf, uuid.uuid4().hex)
            imgs = wait(base, pid, args.timeout, f"{code} {label}")
            saved = download(base, imgs[0], out)
            print(f"        -> {saved.name}  ({saved.stat().st_size/1024:.0f} KB)")
            done.append(saved)
        except Exception as e:  # noqa: BLE001
            print(f"        失败: {e}", file=sys.stderr)
            failed.append((code, label, str(e)))

    print(f"\n[完成] 成功 {len(done)} / 失败 {len(failed)}")
    for c, l, e in failed:
        print(f"   ✗ {c} {l}: {e[:160]}")

    if args.sheet and len(done) == len(VIEWS):
        sheet = Path(args.sheet)
        if not sheet.is_absolute():
            sheet = (ROOT / sheet).resolve()
        print(f"\n[拼版] {sheet}")
        make_sheet(out_dir, sheet)
    elif args.sheet:
        print(f"\n[拼版] 跳过 —— 需要 9 格齐全，当前 {len(done)} 格")
    return 0 if not failed else 1


def make_sheet(out_dir: Path, sheet_path: Path) -> None:
    """把 9 个单格拼成 3x3，并在每格底部叠加中文角度标签。"""
    from PIL import Image, ImageDraw, ImageFont

    cell_w = cell_h = None
    imgs = []
    for code, label, _ in VIEWS:
        p = out_dir / f"view_{code}.png"
        im = Image.open(p).convert("RGB")
        if cell_w is None:
            cell_w, cell_h = im.size
        else:
            im = im.resize((cell_w, cell_h), Image.LANCZOS)
        imgs.append((im, label))

    bar = max(48, cell_h // 22)          # 标签条高度
    sheet = Image.new("RGB", (cell_w * 3, (cell_h + bar) * 3), (243, 242, 238))
    draw = ImageDraw.Draw(sheet)

    font = None
    for fp in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
               "C:/Windows/Fonts/simhei.ttf"):
        try:
            font = ImageFont.truetype(fp, int(bar * 0.46))
            break
        except Exception:
            continue

    for i, (im, label) in enumerate(imgs):
        r, c = divmod(i, 3)
        x, y = c * cell_w, r * (cell_h + bar)
        sheet.paste(im, (x, y))
        draw.rectangle([x, y + cell_h, x + cell_w, y + cell_h + bar], fill=(38, 38, 36))
        text = f"{VIEWS[i][0]}  {label}"
        if font:
            tb = draw.textbbox((0, 0), text, font=font)
            draw.text((x + 16, y + cell_h + (bar - (tb[3] - tb[1])) // 2 - tb[1]),
                      text, font=font, fill=(240, 238, 232))
        else:
            draw.text((x + 16, y + cell_h + 12), text, fill=(240, 238, 232))

    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(sheet_path, quality=95)
    print(f"       {sheet_path}  {sheet.size[0]}x{sheet.size[1]}  "
          f"({sheet_path.stat().st_size/1024/1024:.1f} MB)")


if __name__ == "__main__":
    sys.exit(main())
