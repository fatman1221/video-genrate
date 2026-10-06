#!/usr/bin/env python
"""妲己｜国漫3D角色标准资产 8 图 —— 走 Qwen-Image-Edit（qwen_edit_scene）锁角色一致性。

依赖顺序（这是本脚本存在的理由）：
  01 正面全身是【主参考图】；02–08 全部以 01 为唯一参考图再生成。
  所以 01 必须先生成通过，后面才能跑。已存在的图会跳过 → 中断可续跑。

画质策略（用户裁定）：**低分辨率快速生成 → 本地 Real-ESRGAN 超分增强**。
  本脚本只负责「生成」，产物落在 <out-dir>/raw/（低分辨率母版）；
  增强走 `scripts/upscale_stills.py`（x4 出母版再降采样到 2K）。
  这样 GPU 生成时间大幅缩短，细节交给本地超分补。

用法：
  # 先只跑 01，人工验收构图/尾巴/背景
  python scripts/gen_daji_assets.py --only 01

  # 验收通过后跑其余
  python scripts/gen_daji_assets.py --only 02,03,04,05,06,07,08

  # 全跑 + 拼审阅版式图
  python scripts/gen_daji_assets.py --sheet
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_still import (ROOT, _get_json, apply_overrides, build_workflow,  # noqa: E402
                       download, prep_reference, submit, upload_image)

# ---------------------------------------------------------------- 角色锁定
# 8 张图共用的「同一个角色」描述。写得越具体，逐图漂移越小。
ID_LOCK = (
    "严格保持与参考图为同一个角色，不得改变年龄、身份与五官比例："
    "成年东方女性，鹅蛋脸，精致东方五官，细长眉毛，妩媚而清澈的眼睛，眼尾自然上扬，"
    "挺拔而柔和的鼻梁，精致唇形。黑色长发，柔顺丝滑，长度到腰部，前额有自然碎发，"
    "两侧长发垂落，东方古典发型与国漫3D角色设计结合。"
    "佩戴与参考图完全一致的金色狐妖发饰与白色狐耳，"
    "发饰的形状、大小、材质与佩戴位置一律照搬参考图，不得改动。"
    "身材修长优雅，成年女性自然身体比例，肩宽、腰围、腿长与参考图一致。"
    "拥有九条白色巨大狐尾，九条狐尾必须始终保持九条，"
    "尾巴颜色、长度、蓬松程度与根部位置都与参考图一致。"
)

# ⚠️ 服装必须**逐项照搬参考图**，绝不能只写「主色」。
#    实测踩坑：第一版把服装概括成「深红色、朱红色、黑色为主色」，
#    模型立刻照着这句话把参考图的白色纱质外罩换成了黑色长裙 + 圆形金腰带扣
#    —— 整件衣服被改款。服装的颜色与款式一律交给参考图，提示词只做「不许改」的约束。
COSTUME_LOCK = (
    "服装必须与参考图完全一致，不得改款、不得改配色、不得简化或重组设计："
    "沿用参考图的红色抹胸长裙，肩部与双臂覆盖白色半透明薄纱外罩与宽大飘逸的白色纱袖，"
    "腰系金色与青玉宝石组成的璎珞式腰饰并垂坠玉珠流苏，"
    "裙身带东方古典刺绣纹样与金色滚边，衣料垂坠、层叠、露肩宽袖。"
)

STYLE_LOCK = (
    "高品质中国国漫3D动画，电影级3D CG角色，高预算国产3D动画电影质感，"
    "精致人物建模，细腻皮肤材质，真实丝绸材质，细致黑色毛发，柔软蓬松白色狐尾，"
    "精致金属饰品，电影级柔和灯光，高级角色设计，高细节，高质量渲染。"
)

# 背景：8 张统一纯浅灰（08 海报允许极淡东方幻想渐变）
BG_PLAIN = "背景为干净的纯浅灰色背景，画面中只有这一个角色，没有具体场景、建筑、道具或其他人物。"

VIEW_FULL = ("镜头为远景全身，人物居中、竖直站立，人物从头顶到脚底完整入画、不允许裁切，"
             "人物高度约占画面 85%，头顶与脚下各留少量背景空间。")

# ---------------------------------------------------------------- 负向词
NEG_COMMON = (
    "低质量，模糊，变形，多手多指，肢体错乱，手部畸形，文字，字幕，水印，logo，签名，"
    "杂乱背景，具体场景，建筑，家具，背景中出现其他人物，过曝，噪点，塑料感，"
    "过度磨皮，皮肤蜡像感，脸部模糊，五官崩坏，不像参考图，不同的人，换脸，年龄变化，"
    "变年轻，娃娃脸，Q版，卡通比例，两个角色，多重人影，重影，克隆，镜像复制，"
    "尾巴数量错误，尾巴融合，尾巴粘连，多余的尾巴，只有一条尾巴，尾巴变成手臂，"
    "人物出画，人物被裁切，赤足，光脚 barefoot，裸露的脚，"
    # 服装改款相关 —— 第一版就是栽在这里，必须显式负掉
    "改款，换装，不同的服装，不同的服装设计，改变服装配色，简化服装，"
    "去掉纱质外罩，丢失外罩，换成黑色长裙，纯黑长裙，腰带改成圆形金扣，"
    "腰饰改款，丢失璎珞腰饰，丢失玉珠流苏，服装与参考图不一致"
)
# 全身图：负掉「半身/特写」，否则会被自己的负向词按成胸像
NEG_FULL = NEG_COMMON + "，特写，半身，半身像，胸像，近景，只有头部，只有上半身，腰以上构图"
# 半身图（06）：负掉「全身/远景」，与 NEG_FULL 互斥
# ⚠️ 06 实测踩坑：只写「构图从头顶到腰部」不够，模型会照着参考图（本身是 3/4 身）
#    继续往下画，产出 7-8 身像、脸只占画面高度的 1/5。必须把「下半身不入画」逐项负掉。
NEG_HALF = NEG_COMMON + (
    "，全身，七分身，三分身，远景，双人，腿部，大腿，膝盖，小腿，脚踝，赤足，鞋子，"
    "半身以下，人物过小，脸部占比小，远处的脸，画面裁掉头部，脖子以下全身入画")
# 海报（08）：允许淡淡的渐变背景，但仍不要具体场景
NEG_POSTER = NEG_COMMON + "，特写，半身，半身像，胸像，近景，只有头部，只有上半身，腰以上构图，" \
                         "写实照片风格，真人照片，欧美画风，日系2D，平涂"


def view_prompt(head: str, extra: str = "") -> str:
    return f"{head}{extra}{ID_LOCK}{COSTUME_LOCK}{VIEW_FULL}{STYLE_LOCK}{BG_PLAIN}"


# (code, 文件名, 中文标签, prompt, neg_key)
VIEWS: list[tuple[str, str, str, str, str]] = [
    ("01", "01_front_full.png", "正面全身标准图", view_prompt(
        "生成该角色的【正面全身标准角色设定图】，这是整个角色资产库的主参考图。"
        "人物正面对镜头，身体完全朝向镜头，保持自然中立站姿："
        "双脚自然分开，双腿伸直，双臂自然垂落身体两侧，双手自然张开，不做任何动作。"
        "完整展示脸部、发型、发饰、完整服装、腰部装饰、双手、双腿、鞋子与九条狐尾。"
        "九条狐尾自然展开在身体后方，确保九条尾巴清晰可数、互不融合。"
    ), "full"),

    ("02", "02_left_profile.png", "左侧面全身标准图", view_prompt(
        "生成完全相同的妲己的【左侧面全身标准角色设定图】。"
        "人物严格旋转 90 度，身体与头部保持同一朝向，展示真正的正左侧面轮廓。"
        "重点展示额头轮廓、眉眼轮廓、鼻梁、嘴唇、下颌线、长发侧面结构、肩部结构、"
        "腰部结构、服装侧面结构，以及九条狐尾从背部延伸出来的位置。保持标准中立站姿。"
    ), "full"),

    ("03", "03_back_full.png", "背面全身标准图", view_prompt(
        "生成完全相同的妲己的【背面全身标准角色设定图】。"
        "人物严格背对镜头，身体与头部保持同一朝向。"
        "重点展示黑色长发背面结构、金色发饰背面结构、服装背部完整结构、肩部结构、"
        "腰部金色装饰、服装刺绣、衣摆、鞋子，以及九条白色狐尾。"
        "九条狐尾必须完整显示，并且明确能够数出九条。"
        "狐尾根部必须连接在背部正确的身体位置，不得出现多余尾巴、融合尾巴或尾巴数量错误。"
    ), "full"),

    ("04", "04_front45.png", "45度正面角色图", view_prompt(
        "生成完全相同的妲己的【45度正面角色图】。"
        "人物身体朝向镜头左前方约 45 度，头部也自然转向相同方向。"
        "重点展示角色的三维立体结构：脸部立体感、鼻梁、下颌线、肩部、腰部、衣袖、"
        "身体曲线与狐尾体积。人物保持自然站立姿势。人物完整显示。"
    ), "full"),

    ("05", "05_back45.png", "45度背面角色图", view_prompt(
        "生成完全相同的妲己的【45度背面角色图】。"
        "人物以背部朝向镜头，绕垂直轴旋转约 45 度，呈【右后方四分之三背面】——"
        "镜头能同时看到她的后背与右侧脸的一小段轮廓边缘，而不是完全的正侧面。"
        "判断标准：双肩一高一低、能看到部分侧脸轮廓线、臀部与脚尖呈斜向排列，即为正确的 45 度背面。"
        "重点展示后脑发型、金色发饰背面结构、服装背面、肩部、腰部、衣摆，"
        "以及九条狐尾在背后的空间层次与前后遮挡关系。"
        "九条狐尾必须保持清晰独立、互不融合，不得增加或减少尾巴数量。"
    ), "back45"),

    ("06", "06_bust_portrait.png", "半身肖像 + 表情标准图", (
        "生成完全相同的妲己的【半身角色肖像】。注意：这是近景肖像，不是全身图。"
        "画面的裁切范围必须正好是【头顶到腰部】——画面下边缘落在腰部腰线的位置，"
        "腰部以下的身体（胯部、大腿、膝盖、小腿、脚踝、赤足）全部在画面之外，绝不能出现。"
        "人物脸部在画面中占比很大，脸与头部约占画面高度的三分之一以上。"
        "镜头正面、略微靠近人物，人物居中构图。"
        "重点展示脸部、眼睛、眉毛、鼻子、嘴唇、皮肤、黑色长发、金色发饰、"
        "服装领口、肩部与腰部以上的服装细节。"
        "妲己保持温柔、妩媚、自信的微笑，表情自然，不夸张，不性感化，不做夸张动漫表情。"
        "皮肤细腻自然，眼睛具有真实的玻璃质感与细微高光。"
        + ID_LOCK + COSTUME_LOCK + STYLE_LOCK + BG_PLAIN
    ), "half"),

    ("07", "07_pose_reference.png", "动作参考标准图", view_prompt(
        "生成完全相同的妲己的【全身角色动作参考图】，用于后续图生视频。"
        "人物做一个非常简单、优雅的中国古典舞姿势：身体略微侧转，一只手轻轻抬起，"
        "另一只手自然垂落，长袖自然下垂，身体保持优雅平衡，一只脚略微向前。"
        "双脚完整可见，脚上穿着金色古典绣鞋，鞋面有金色珠绣与流苏，"
        "鞋子造型与整体服装的华丽程度一致；绝对不能赤足、光脚。"
        "九条白色狐尾在身体后方自然展开，形成优雅的弧线。动作不能夸张，保持端庄。"
        "重点展示身体比例、手臂长度、腿部比例、服装运动结构、袖子的长度，"
        "以及狐尾与身体之间的空间关系。"
    ), "full"),

    ("08", "08_hero_poster.png", "角色最终标准海报图", (
        "生成妲己的【角色最终标准展示海报图】，这是角色资产库的 Hero Reference / Master Reference。"
        "妲己站在画面中央，采用 45 度正面站姿，全身完整入画、不允许裁切。"
        "完整展示脸部、黑色长发、金色狐妖发饰、深红色东方幻想服装、金色装饰、修长身材、"
        "双手、双腿、鞋子与九条白色狐尾。人物表情温柔、自信、神秘。"
        "九条狐尾自然向后展开，形成具有视觉冲击力的扇形构图。"
        + ID_LOCK + COSTUME_LOCK + VIEW_FULL +
        "整体视觉必须达到高预算国产3D动画电影角色海报的质量。"
        "东方幻想，中国古典美学，国漫3D，电影级CG，精致人物建模，真实丝绸，细腻皮肤，"
        "高质量毛发，柔软蓬松狐尾，精致金属饰品，电影级灯光，体积光，高级材质，高细节，高质量渲染。"
        "背景为非常淡的东方幻想渐变背景，但不得出现具体场景、建筑或其他人物。"
    ), "poster"),
]

# 05 背面 45°：在「转身」指令下，模型极易一路转到 90° 侧面 —— 必须显式负掉
NEG_BACK45 = NEG_COMMON + (
    "，特写，胸像，近景，只有头部，只有上半身，"
    "90度侧面，完全侧面，正侧面，人物正对镜头，可见完整正脸，人物正面朝向镜头")
NEG_MAP = {"full": NEG_FULL, "half": NEG_HALF, "poster": NEG_POSTER, "back45": NEG_BACK45}

# 生成母版落在 <out-dir>/raw/；超分后的成品放在 <out-dir>/ 根下
RAW_SUB = "raw"


class JobLost(RuntimeError):
    """任务被人从 ComfyUI 队列里删掉了（不是执行失败，是凭空消失）。

    本机实测：这台 ComfyUI 上还有别的进程在跑，会发 `POST /queue {"clear":true}`
    把别人的待跑任务一并清掉。此时任务既不在 queue 也不在 history —— 标准
    `wait()` 会一直傻等到超时（3600s），整批图就卡死了。
    所以必须主动探测「既不在队列也不在历史」= 丢失 → 立刻重投。
    """


def wait_robust(base: str, pid: str, timeout: int, label: str,
                poll: float = 6.0, missing_limit: int = 4) -> list[dict]:
    """像 gen_still.wait() 一样取回产物，但能识别「任务被清掉」并提前退出。

    判定丢失：连续 missing_limit 次轮询里，pid 既不在 queue(running/pending)，
    也不在 /history/{pid} —— 说明它被清除且永远不会执行。
    """
    t0 = time.time()
    missing = 0
    last = -1
    while True:
        if time.time() - t0 > timeout:
            raise TimeoutError(f"{label} 超时（{timeout}s）")
        try:
            hist = _get_json(base, f"/history/{pid}")
        except Exception:  # noqa: BLE001
            hist = {}
        if pid in hist:
            h = hist[pid]
            st = h.get("status", {})
            if st.get("status_str") == "error" or not st.get("completed", True):
                raise RuntimeError(f"{label} 执行出错: "
                                   f"{json.dumps(st.get('messages', []), ensure_ascii=False)[:600]}")
            outs = [img for _, n in (h.get("outputs") or {}).items()
                    for img in (n.get("images") or [])]
            if outs:
                return outs
            raise RuntimeError(f"{label} 完成但没有输出图")
        try:
            q = _get_json(base, "/queue")
        except Exception:  # noqa: BLE001
            q = {}
        ids = {it[1] for it in (q.get("queue_running") or []) + (q.get("queue_pending") or [])
               if isinstance(it, list) and len(it) > 1}
        if pid not in ids:
            missing += 1
            if missing >= missing_limit:
                raise JobLost(f"{label} 已从队列消失（被外部清空/删除）")
        else:
            missing = 0
            run = q.get("queue_running") or []
            if run and isinstance(run[0], list) and len(run[0]) > 3:
                pct = run[0][-1]
                if isinstance(pct, dict) and pct.get("value") is not None and pct.get("value") != last:
                    last = pct["value"]
                    print(f"        {label} {last}/{pct.get('max')}", flush=True)
        time.sleep(poll)


def prep_upper_crop(ref: Path, out: Path, frac: float, width: int, height: int) -> Path:
    """把参考图上方 frac 比例裁出来（头→胸），再**放大到铺满**目标尺寸。

    用途：06 是半身构图，而它的参考图（01）是全身 —— 直接喂全身图，
    编辑模型倾向沿用参考图的构图比例，容易又出一张全身。先裁到上半身再喂，
    构图指令的服从度明显更高。

    ⚠️【2026-10-06 修正】原实现是「裁剪后等比缩放到宽度对齐，再补灰底」。
    因为源图宽度（1024）已等于目标宽度（1024），``scale`` 恒为 1 →
    实际产出的是**一条居中的扁横条 + 下方 ~62% 的灰底**。后果有两个：
      1. 参考图里几乎没有可用的构图信息 → 模型回退到文本先验 → **换脸**
      2. 裁切线正好切在 01 胸口那道白色镶边上 → 模型读成"撞色拼接" →
         06 的裙子变成**黑红拼接**，与 01 的纯朱红不一致
    现改为「按高度放大到铺满 + 居中裁宽」，即"头占满画面"的正经半身参考，
    **不补任何灰底**。看真实输入的办法：直接 Read 出 <out> 那张 _ref06_upper.png。
    """
    from PIL import Image
    im = Image.open(ref).convert("RGB")
    sw, sh = im.size
    strip_h = max(1, int(sh * frac))
    im = im.crop((0, 0, sw, strip_h))
    # 按高度放大到目标高度（这才是"头占满画面"），再居中裁到目标宽度保住头部
    scale = height / im.size[1]
    nw = max(1, int(im.size[0] * scale))
    im = im.resize((nw, height), Image.LANCZOS)
    if nw > width:
        x0 = (nw - width) // 2
        im = im.crop((x0, 0, x0 + width, height))
    elif nw < width:
        canvas = Image.new("RGB", (width, height), (205, 205, 208))
        canvas.paste(im, ((width - nw) // 2, 0))
        im = canvas
    out.parent.mkdir(parents=True, exist_ok=True)
    im_size = im.size
    im.save(out)
    print(f"[06 参考预处理] 上 {frac:.0%} 裁切 + 放大铺满 -> {out.name} {im_size[0]}x{im_size[1]}")
    return out


def make_sheet(out_dir: Path, sheet_path: Path) -> None:
    """把 8 张拼成 4x2 审阅版式图，每张下方叠中文标签。

    优先用 out_dir 根下的成品（超分后的），没有就退回 raw/ 里的低分辨率母版。
    """
    from PIL import Image, ImageDraw, ImageFont

    cols, rows = 4, 2
    cell_w, cell_h = 560, 747                # 统一格子，竖图 3:4
    imgs = []
    for code, name, label, _, _ in VIEWS:
        cand = [out_dir / name, out_dir / RAW_SUB / name]
        p = next((c for c in cand if c.exists()), None)
        if p is None:
            continue
        imgs.append((Image.open(p).convert("RGB"), f"{code}  {label}"))

    if not imgs:
        print("没有可拼版的图")
        return

    bar = max(46, cell_h // 24)
    sheet = Image.new("RGB", (cell_w * cols, (cell_h + bar) * rows), (243, 242, 238))
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
        r, c = divmod(i, cols)
        x, y = c * cell_w, r * (cell_h + bar)
        # 01 是用户的方形参考图、其余是 3:4 竖图 —— 必须 contain 居中，不能硬拉伸
        s = min(cell_w / im.size[0], cell_h / im.size[1])
        nw, nh = max(1, int(im.size[0] * s)), max(1, int(im.size[1] * s))
        im = im.resize((nw, nh), Image.LANCZOS)
        draw.rectangle([x, y, x + cell_w, y + cell_h], fill=(233, 232, 236))
        sheet.paste(im, (x + (cell_w - nw) // 2, y + (cell_h - nh) // 2))
        draw.rectangle([x, y + cell_h, x + cell_w, y + cell_h + bar], fill=(38, 38, 36))
        if font:
            tb = draw.textbbox((0, 0), label, font=font)
            draw.text((x + 14, y + cell_h + (bar - (tb[3] - tb[1])) // 2 - tb[1]),
                      label, font=font, fill=(240, 238, 232))
        else:
            draw.text((x + 14, y + cell_h + 10), label, fill=(240, 238, 232))

    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(sheet_path, quality=95)
    print(f"[拼版] {sheet_path}  {sheet.size[0]}x{sheet.size[1]}  "
          f"({sheet_path.stat().st_size/1024/1024:.1f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description="妲己 8 图标准资产生成")
    ap.add_argument("--reference", default=r"C:\Users\Administrator\.workbuddy\clipboard-images"
                                          r"\clipboard-2026-10-04T12-19-57-868Z-41f29bff.jpg",
                    help="原始人物参考图（只用于生成 01）")
    ap.add_argument("--out-dir", default="backend/storage/temp/stills/daji_assets")
    ap.add_argument("--sheet", nargs="?", const="backend/storage/temp/stills/daji_assets_sheet.png",
                    default="", help="拼版输出路径（不带值用默认路径）")
    ap.add_argument("--width", type=int, default=768, help="生成宽（低分辨率快出，细节交给本地超分）")
    ap.add_argument("--height", type=int, default=1024, help="生成高")
    ap.add_argument("--steps", type=int, default=24)
    ap.add_argument("--cfg", type=float, default=0.0)
    ap.add_argument("--sampler", default="")
    ap.add_argument("--scheduler", default="")
    ap.add_argument("--ref-fit", default="pad", choices=["crop", "pad", "stretch"],
                    help="参考图适配比例方式（默认 pad：不裁掉参考图内容）")
    ap.add_argument("--seed", type=int, default=20261004)
    ap.add_argument("--host", default="http://127.0.0.1:8188")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--only", default="", help="只跑指定序号，逗号分隔，如 01 或 02,03")
    ap.add_argument("--force", action="store_true", help="忽略已存在的图，强制重跑")
    ap.add_argument("--retries", type=int, default=12,
                    help="被外部中断 / 被清队列时的重投次数（这台机器上三方抢 GPU，重投次数要给够）")
    ap.add_argument("--retry-wait", type=int, default=15, help="重投前等待秒数")
    ap.add_argument("--per-try", type=int, default=900,
                    help="单次投递的等待上限（秒）；超时即视为失败并重投")
    ap.add_argument("--pipeline", action="store_true",
                    help="把全部任务一次性压进 ComfyUI 队列后再取回（避免与他进程交替排队"
                         "导致主模型反复换入换出）")
    ap.add_argument("--crop06", action="store_true",
                    help="06 的参考图先用 01 的上半身裁切图（半身构图更稳）")
    ap.add_argument("--crop06-frac", type=float, default=0.62,
                    help="06 参考图取上方多少比例（0.62 ≈ 头顶到腰）")
    ap.add_argument("--ref-override", default="",
                    help="覆盖某几张的参考图（从 raw/ 取），形如 05:03_back_full.png。"
                         "用途：转身类视图用「最接近的已有视图」当参考，比一律用 01 更容易转对角度"
                         "（实测 05 用 01 会被转成 90 度侧面）")
    args = ap.parse_args()

    base = args.host.rstrip("/")
    user_ref = Path(args.reference)
    if not user_ref.is_absolute():
        user_ref = (ROOT / user_ref).resolve()
    if not user_ref.exists():
        print(f"参考图不存在: {user_ref}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_dir = out_dir / RAW_SUB
    raw_dir.mkdir(parents=True, exist_ok=True)

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    targets = [v for v in VIEWS if not only or v[0] in only]

    ref01 = raw_dir / "01_front_full.png"

    # 参考图覆盖：05:03_back_full.png —— 转身视图用最接近的已有视图当参考
    ref_map: dict[str, str] = {}
    for item in args.ref_override.split(","):
        if ":" in item:
            k, v = item.split(":", 1)
            if k.strip() and v.strip():
                ref_map[k.strip()] = v.strip()

    print(f"[原始参考图] {user_ref.name}")
    print(f"[生成母版]   {raw_dir}")
    print(f"[规格]       {args.width}x{args.height} · {len(targets)} 张 · {args.steps} 步 · "
          f"ref-fit={args.ref_fit}")
    print(f"[后续增强]   python scripts/upscale_stills.py --in-dir {RAW_SUB}\n")

    # 上传表：同一张参考图只上传一次
    upload_cache: dict[str, str] = {}
    done, failed = [], []
    pending: list[dict] = []          # --pipeline 模式：已提交、待取回的任务

    def collect(job: dict) -> None:
        """等待并下载一个已提交的任务，失败按需重投。"""
        code, label, out, wf = job["code"], job["label"], job["out"], job["wf"]
        last_err = ""
        for attempt in range(1, args.retries + 1):
            try:
                if job.get("pid") is None:
                    job["pid"] = submit(base, wf, uuid.uuid4().hex)
                    print(f"        [{code}] 重投 {job['pid'][:8]} ...", flush=True)
                imgs = wait_robust(base, job["pid"], args.per_try, f"{code} {label}")
                saved = download(base, imgs[0], out)
                print(f"        -> {saved.name}  ({saved.stat().st_size/1024:.0f} KB)", flush=True)
                done.append(saved)
                return
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                job["pid"] = None
                retryable = isinstance(e, JobLost) or "interrupted" in last_err
                if retryable and attempt < args.retries:
                    why = "被清出队列" if isinstance(e, JobLost) else "被外部中断"
                    print(f"        [{code}] {why}，{args.retry_wait}s 后重投 "
                          f"({attempt}/{args.retries}) ...", flush=True)
                    time.sleep(args.retry_wait)
                    continue
                print(f"        失败: {last_err[:300]}", file=sys.stderr, flush=True)
                break
        failed.append((code, label, last_err))

    for idx, (code, name, label, prompt, neg_key) in enumerate(targets, 1):
        out = raw_dir / name
        if out.exists() and out.stat().st_size > 1024 and not args.force:
            print(f"[{idx}/{len(targets)}] {code} {label} — 已存在，跳过")
            done.append(out)
            continue

        # 选参考图：01 用用户原图；02-08 一律用 01
        if code == "01":
            ref_src = user_ref
        else:
            if not ref01.exists():
                print(f"[{idx}/{len(targets)}] {code} {label} — 跳过：主参考图 01 还不存在，请先跑 01",
                      file=sys.stderr)
                failed.append((code, label, "缺少 01 主参考图"))
                continue
            ref_src = ref01
            if code in ref_map:
                cand = raw_dir / ref_map[code]
                if cand.exists():
                    ref_src = cand
                    print(f"        [参考图覆盖] {code} -> {ref_map[code]}", flush=True)
                else:
                    print(f"        [参考图覆盖] {cand.name} 不存在，回退用 01", file=sys.stderr)

        key = f"{ref_src}|{code}"
        if key in upload_cache:
            remote_ref = upload_cache[key]
        else:
            ref_use = ref_src
            if code == "06" and args.crop06:
                ref_use = prep_upper_crop(ref_src, raw_dir / "_ref06_upper.png",
                                          args.crop06_frac, args.width, args.height)
            ref_use = prep_reference(ref_use, args.width, args.height, args.ref_fit)
            if ref_use != ref_src:
                print(f"        [参考图适配] {args.ref_fit} -> {ref_use.name}")
            remote_ref = upload_image(base, ref_use)
            upload_cache[key] = remote_ref

        mapping = {
            "{{prompt}}": prompt,
            "{{negative_prompt}}": NEG_MAP[neg_key],
            "{{width}}": args.width,
            "{{height}}": args.height,
            "{{seed}}": args.seed + int(code),
            "{{reference_image}}": remote_ref,
        }
        wf = build_workflow("qwen_edit_scene.json", mapping)
        apply_overrides(wf, steps=args.steps, cfg=args.cfg,
                        sampler=args.sampler, scheduler=args.scheduler)
        for node in wf.values():
            if node.get("class_type") == "SaveImage":
                node["inputs"]["filename_prefix"] = f"daji_{code}"

        job = {"code": code, "label": label, "out": out, "wf": wf, "pid": None}

        if args.pipeline:
            # 关键：先把全部任务压进 ComfyUI 队列，让它们**连续**执行。
            # 本机实测：如果和别的进程交替排队，每一次切换都要把 11GB 主模型
            # + 8GB 文本编码器换进换出，单张墙钟从 ~80s 涨到 ~10min。
            try:
                job["pid"] = submit(base, wf, uuid.uuid4().hex)
                print(f"[{idx}/{len(targets)}] {code} {label} — 已入队 {job['pid'][:8]}")
                pending.append(job)
            except Exception as e:  # noqa: BLE001
                print(f"[{idx}/{len(targets)}] {code} {label} — 入队失败: {e}", file=sys.stderr)
                failed.append((code, label, str(e)))
        else:
            print(f"[{idx}/{len(targets)}] {code} {label} — 生成中 ...", flush=True)
            collect(job)

    if pending:
        print(f"\n[取回] 队列中 {len(pending)} 个任务，按完成顺序下载 ...")
        for job in pending:
            collect(job)

    print(f"\n[完成] 成功 {len(done)} / 失败 {len(failed)}")
    for c, l, e in failed:
        print(f"   x {c} {l}: {e[:200]}")

    if args.sheet and len(done) == len(VIEWS):
        sheet = Path(args.sheet)
        if not sheet.is_absolute():
            sheet = (ROOT / sheet).resolve()
        make_sheet(out_dir, sheet)
    elif args.sheet:
        print(f"\n[拼版] 跳过 —— 需要 8 张齐全，当前 {len(done)} 张")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
