#!/usr/bin/env python
"""妲己 / 赤焰狐妃｜国漫3D角色标准资产 9 图（Qwen-Image 2.1 + Qwen-Image-Edit 2511）。

与 v1（gen_daji_assets.py，8 图）的区别：这一版严格按用户本次给的 9 图 spec 重写，
  - 01 不再依赖外部剪贴板参考图，**纯文生图**立基准（Qwen-Image 2.1 / qwen_image_character）
  - 02–09 全部以 01 为唯一参考图派生（Qwen-Image-Edit 2511 / qwen_edit_scene）
  - 角色设定按 spec 逐项写死：琥珀红眼 / 栗红色（非纯黑）长发 / 朱红绛红+香槟金 /
    露肩收腰高开衩长裙 / **九条**象牙白狐尾（尾尖暖金渐变）
  - 新增 09 Hero Master Reference

依赖顺序（脚本存在的理由）：01 必须先生成并通过人眼核验，后面 8 张才有意义。
已存在的图会跳过 → 中断可续跑。

参数策略（依据 comfyui-api-batch-gen skill §22.8 / §23.2）：
  - cfg 必须 >2.5，否则「向左旋转90度」「完全背对镜头」这类空间指令基本不生效
  - ⚠️【2026-10-06 实证修正】**采样器必须用 euler + simple**！
    本脚本原先把采样器覆盖成 `dpmpp_2m` + `karras`，这会让 `qwen_image_2.1_int8_convrot`
    的**纯文生图**输出彻底崩坏：绿色斑块 + 纵向细密拉丝 + 人物半透明鬼影 + 脸糊。
    对照实验（同 prompt / 同 seed / 同 28 步）：
      dpmpp_2m+karras+cfg4.0 → 崩；  euler+simple+cfg2.5 → 干净；  euler+simple+cfg4.0 → 干净
    → 崩坏与 cfg 无关，是 **采样器/调度器组合** 的问题。现默认已改回 euler/simple。
  - 低分辨率出母版（视角服从度好、墙钟短）→ 之后用 scripts/upscale_stills.py 超分到 2K

用法：
  python scripts/gen_daji_assets9.py --only 01          # 先只出基准，人工核验
  python scripts/gen_daji_assets9.py --only 02,03,04,05,06,07,08,09 --pipeline
  python scripts/gen_daji_assets9.py --sheet             # 全齐后拼审阅版式图
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
from gen_daji_assets import make_sheet, prep_upper_crop, wait_robust  # noqa: E402

# ============================================================ 角色锁定（spec 逐项）
# 反复实测过：**服装不能只写「主色」**，模型会就地改款。这里把版型/层次/材质全部写死。
COSTUME = (
    "服装主色必须是朱红色与绛红色，整条主裙是浓郁的朱红偏绛红，"
    "绝不是白色、米色、淡粉色或灰白色，裙子不能是白色布料："
    "上身是修身剪裁的朱红色紧身衣，露肩设计，露出锁骨与肩部，"
    "肩臂配有一对半透明的朱红色轻纱广袖；"
    "腰部明显收紧，纤细的腰线上系着香槟金的腰饰；"
    "下身是浓郁的朱红色高开衩长裙，露出部分修长腿部，"
    "裙摆层叠飘逸，边缘缀有半透明的朱红色薄纱；"
    "衣身有精致的香槟金刺绣、金色云纹与东方宫廷缠枝纹样。"
    # 【2026-10-06】实测：「金色狐纹」会被模型实现成裙子上趴着实体狐狸（A2 版最明显）。
    # 用词层面避开「狐」字，并显式限定为平面织物纹样。
    "衣身上的云纹与缠枝纹必须是绣在布料上的平面织绣纹样，"
    "绝对不得画成真实的狐狸、动物实体、动物躯体或立体摆件。"
    "材质为高级丝绸与细腻刺绣，垂坠感强。"
    # 原句写「必须与参考图完全一致」——但 01 是纯文生图、没有参考图，这句会让模型
    # 找不到锚点。改成中性表述，动笔链与文生图都成立。
    "服装的版型、配色与装饰必须始终保持完全一致，不得改款、不得改配色、不得简化或重组设计。"
)

FACE = (
    "成年东方女性，成熟妩媚、高贵神秘、带有危险感的九尾狐妖气质。"
    "精致东方女性面孔，鹅蛋脸，细长的柳叶眉，狭长而明亮的琥珀红色眼眸，眼尾微微上扬，"
    "长睫毛，精致挺拔的鼻梁，朱红色饱满唇形。"
    "深栗红色长发（chestnut auburn），通体是暖调的栗棕偏红棕色，"
    "在光线下能看出明显的红棕色反光与栗色层次，绝不是黑色、绝不是乌黑、绝不是冷调深棕；"
    "柔顺有光泽，长度超过腰部，部分长发自然垂落在身体两侧和胸前。"
    # 【2026-10-06】原写「带有狐尾造型」——实测被模型实现成头顶两只向上翘的
    # 尖角/兽耳状凸起（每版都有）。去掉「狐尾造型」，改成祥云纹+花叶纹+垂珠流苏，
    # 并显式封死一切向上的尖角。
    "佩戴固定的东方宫廷风香槟金冠式发饰：贴合头骨轮廓的金冠，"
    "以祥云纹、缠枝花叶纹与垂珠流苏为主体造型，造型精致华丽、佩戴端正。"
    "发饰的顶部轮廓平缓圆润、紧贴头顶，绝对不得出现任何向上竖直或弯曲的尖角、"
    "角状凸起、犄角、兽角、兽耳或狐狸耳朵形状。"
    "身材修长纤细而成熟，拥有自然明显的女性曲线，肩颈优雅，腰部纤细，腿部修长，身体比例统一。"
)

TAILS = (
    "身后有九条蓬松的象牙白色狐尾，尾巴毛发细腻柔软，具有真实的3D毛发质感。"
    "九条狐尾的根部全部收束在腰部与臀部后方的同一小块区域，"
    "从那里呈扇形向外与向后展开，尾巴整体向镜头后方纵深延伸。"
    # 【2026-10-06】实测：只说「狐尾」模型会画成细长弯曲的羽毛/新月薄片（三版都这样）。
    # 对策：把「兽尾」的物理形态写死 —— 圆柱体 + 根部粗 + 粗细均匀 + 末端圆钝。
    "每条狐尾都是一根粗壮饱满的圆柱形兽尾：根部明显粗壮，整条尾身保持均匀而扎实的粗度，"
    "尾身的粗度非常明显、比人物的手臂更粗，末端圆钝、收成一个蓬松的圆形，"
    "形态像被放大数倍的狐狸尾巴。"
    "每一条尾巴都是厚实、蓬松、柔软的毛绒兽尾：尾巴的边缘是毛茸茸、不规则、蓬乱的"
    "毛发轮廓，能看出一簇簇翘起分开的毛发与清晰的毛流走向，毛发根根分明、蓬松有厚度。"
    "绝不是光滑齐整、边缘规整的羽片，也绝不是细长条状、柳叶状或月牙状的薄片，"
    "尾尖绝不是尖锐的、向上钩起的羽尖。"
    "狐尾具有明确的体积感、厚度、毛发透光与投影层次，"
    "不得半透明、不得像纱幔或羽毛那般轻飘扁平。"
    "所有狐尾都严格保持在人物身体轮廓线的后方，绝不遮挡人物的面部、肩膀、手臂、"
    "腰线与裙摆，人物全身轮廓完整可见，身体与狐尾之间留有可见的背景缝隙。"
    "狐尾通体是冷调纯净的象牙白，白得很干净、很清透，接近纯白，"
    "只在每条尾巴最末端约五分之一的尖端部分，才有一道非常淡的暖金色渐变，"
    "尾巴其余五分之四的长度全部保持纯净的象牙白，不带任何黄色、米色、奶油色或暖黄调。"
    "九条狐尾彼此清楚地分开，每两条尾巴之间都有清晰可见的背景缝隙，"
    "每条尾巴都有完整可辨的独立轮廓与根部和尖端，绝不融合成一团白雾。"
    "九条狐尾的大小、长度、根部位置与整体形态保持一致，数量必须恰好是九条，可以逐条数出来。"
)

# 反面清单要跟着改：把上一版实际翻车的形态明确写进去
NEG_BASE = (
    "低质量，模糊，变形，多手多指，肢体错乱，手部畸形，文字，字幕，水印，logo，签名，"
    "杂乱背景，山水风景，树林，云海，宫殿，建筑，亭台，家具，道具，景深虚化背景，"
    "背景中出现其他人物，多个人物，第二个角色，多重人影，残影，重影，克隆，镜像复制，"
    "过曝，噪点，塑料感，过度磨皮，皮肤蜡像感，脸部模糊，五官崩坏，"
    "不同于参考图，换脸，年龄变化，幼态，娃娃脸，Q版，卡通比例，二次元平涂，日系2D，"
    "真人照片，写实摄影，欧美画风，现代礼服，晚礼服，夜店服装，"
    # 上一版实际翻车的形态，逐条写死
    "白色主裙，白色裙子，米色裙子，淡粉色裙子，灰白色裙子，裙子褪色成白色，"
    "整体泛白，曝光过度导致没有颜色，低饱和度，雾蒙蒙，白雾，朦胧，"
    "脸上一块绿色，绿色斑块，绿色污渍，脸上有花纹，额间彩绘，"
    "尾巴融合成一团，尾巴粘连成白雾，尾巴互相重叠成一片，尾巴数量无法分辨，"
    "尾巴数量错误，多余的尾巴，只有六条尾巴，只有七条尾巴，只有八条尾巴，只有一条尾巴，"
    # 【v3-min 2026-10-06】只加 5 项针对本次两个问题，**不要**再扩负向词表：
    # 实测把负向词堆到 90+ 条会直接把画面打崩（与遮挡无关的另一回事）。
    "狐尾遮挡身体，狐尾挡在人物身前，九尾像翅膀，孔雀开屏，金黄色眼睛，"
    # 【2026-10-06】连续三版实测：狐尾都被画成羽毛状。补 5 条羽状类负向词
    #（等量增补、不再扩表；负向词堆到 90+ 条会把画面打崩）
    "尾巴变成手臂，尾巴变成翅膀，尾巴变成布料，"
    "羽毛，羽状尾巴，细长的羽毛，扁平的羽毛片，弯月形薄片，"
    "纯黑色头发，乌黑头发，冷调黑色头发，深棕黑色头发，黑色短发，"
    "纯白色头发，银白色头发，绿色眼睛，蓝色眼睛，紫色头发，短发，"
    "米黄色尾巴，暖黄色尾巴，奶油色尾巴，金黄色尾巴，杏色尾巴，"
    "整条尾巴都是暖黄渐变，尾巴通体偏黄，尾巴泛黄，尾巴脏黄色，"
    "人物出画，人物被裁切，头顶被裁掉，脚被裁掉，"
    "改款，换装，不同的服装设计，改变服装配色，简化服装，丢失刺绣，"
    "铠甲，盔甲，鳞甲，劲装，战斗服，制服，铠甲长裙，金属护甲，"
    # 【2026-10-06】羽状类负向词：换掉低价值的立绘类 4 条（净 +1）
    "尖细的尾尖，向上钩起的尖锐尾尖，光滑齐整的羽片边缘，规则排列的羽毛，羽轴，"
    "狐狸耳朵，兽耳，动物耳朵，尖耳朵，猫耳，狐耳长在头顶，人形耳朵，"
    "去掉香槟金装饰，腰饰改款，服装与参考图不一致，"
    # 【2026-10-06】发饰尖角/兽耳：每版都出现，补负向词
    "头顶尖角，犄角，兽角，角状凸起，耳朵状发饰，"
)

STYLE = (
    "高品质中国国漫3D动画风格，电影级3D CG角色渲染，高预算国产3D动画电影质感，"
    "精致人物建模，细腻皮肤材质，真实丝绸材质，细腻毛发，蓬松的狐尾，"
    "高级金属材质，电影级灯光，高级角色设计，高细节，高质量渲染，色彩浓郁饱和。"
)

NEG_FULL = NEG_BASE + "，特写，半身，半身像，胸像，近景，只有头部，只有上半身，腰以上构图"
NEG_HALF = NEG_BASE + "，全身，远景，画面裁掉头部，腿部，鞋子，背影，背对镜头，侧脸，侧身，第二个身体，两个人物，暗淡，欠曝，逆光剪影，阴影中的脸"
NEG_HERO = NEG_BASE + "，特写，半身像，胸像，近景，平面构图"


# 【2026-10-06】颜色锁。实测：TAILS 段里「象牙白」出现次数太多，模型会把这个白
# 扩散到整条长裙上（输出全白裙，与 spec 的朱红/绛红完全相反），而把白色写进负向词
# 几乎无效（负向词对颜色不敏感）。对策：在 prompt **末尾**（编码器最后读到的位置）
# 再正面锁一次主色，并明确「象牙白只属于狐尾」。
COLOR_LOCK = (
    "最后再次确认配色：整条长裙的主色是浓郁的朱红色与绛红色，"
    "绝不是白色、象牙白色、米白色或灰白色的裙子，裙身不得出现大面积白色；"
    "象牙白色只属于身后的九条狐尾。"
)


def view(head: str, *, bg: str, neg_key: str = "full", half: bool = False,
         extra_tail: str = "", neg_extra: str = "") -> tuple[str, str]:
    """组装 (prompt, neg)。half=True 时不注入全身构图段。"""
    # 【2026-10-06 修复】原先这里漏了 COSTUME —— 整段服装描述（刺绣/腰饰/广袖/
    # 高开衩/宫廷纹样）**从未进入 prompt**，模型只拿到一句颜色锁，
    # 所以前三版都退化成「一条现代感红长裙」。补上。
    body = head + COSTUME + FACE + TAILS + extra_tail + STYLE + bg
    if not half:
        body += ("镜头为远景全身，人物居中竖直站立，"
                 "从头顶到脚底完整入画、绝不允许裁切，人物高度约占画面 85%，"
                 "头顶与脚下各保留少量背景空间，鞋子完整可见。")
    body += COLOR_LOCK
    neg = {"full": NEG_FULL, "half": NEG_HALF, "hero": NEG_HERO}[neg_key]
    if neg_extra:
        neg = neg + "，" + neg_extra
    return body, neg


BG_PLAIN = "背景为干净的纯浅灰色背景，画面中只有这一个角色，没有具体场景、建筑或道具。"
BG_HERO = ("背景为简洁干净的浅灰到淡墨色渐变，带有轻微东方幻想氛围，"
           "不得出现具体场景、建筑、山水或道具，画面中只有妲己一个人。")

def row(code: str, name: str, label: str, head: str, **kw) -> tuple[str, str, str, str, str]:
    """把 view() 的 (prompt, neg) 摊平成 VIEWS 需要的 5 元组。"""
    prompt, neg = view(head, **kw)
    return (code, name, label, prompt, neg)


# (code, 文件名, 中文标签, prompt, neg)
VIEWS: list[tuple[str, str, str, str, str]] = [
    # ---------------------------------------------------------- 01 主参考（文生图）
    row("01", "01_front_full.png", "正面全身 Master Reference",
        "生成妲己的【正面全身标准角色资产图】，这是整个角色资产库的最高优先级人物参考图。"
        "人物完全正面对镜头，自然站立，身体略微呈优雅的S形曲线，一条腿略微向前迈出，"
        "双肩自然放松，双手采用自然优雅的姿势垂放身侧。"
        "双眼直视镜头，嘴角带着非常轻微的妩媚微笑。"
        "完整展示脸部、发型、发饰、锁骨、完整服装、纤细腰线、双手、长腿、鞋子，"
        "以及九条象牙白狐尾在身体后方自然展开。"
        "九条狐尾在身后左右两侧各展开约四十五条，尾尖向外上方翘起，"
        "中间留出明显的空隙可以看到背景，人物的身体与服装完全不被尾巴遮挡。"
        "如果底图带有山水、云雾、亭台楼阁等场景背景，必须彻底替换为纯浅灰色背景。"
        "如果底图是四分之三侧身或半身构图，必须改为完全正面对镜头的全身站姿，"
        "补全从头顶到脚底的全部身体部分（含双脚与鞋子），取景拉远到能容纳完整全身与九尾。",
        bg=BG_PLAIN, neg_key="full"),

    # ---------------------------------------------------------- 02–09（以 01 为参考）
    row("02", "02_left_profile.png", "左侧面 90°",
        "生成完全相同的妲己的【左侧90度全身视图】，这是角色资产库的标准侧面图。"
        "人物严格旋转90度呈正左侧面，身体与头部保持同一朝向，不要回望镜头。"
        "⚠️ 服装必须是参考图里那一条朱红绛红色的东方幻想长裙：修身露肩上身、"
        "香槟金腰饰、高开衩飘逸长裙。绝对不能改成铠甲、盔甲、劲装、制服、"
        "铠甲裙、鳞甲、战斗服或任何其他服装，只能是同一条红裙。"
        "脸型、眼睛、鼻子、嘴唇、发型、发饰、身体比例、服装与腰线必须与参考图完全相同。"
        "重点展示鼻梁的侧面轮廓、下颌线、颈部线条、肩部结构、胸肩轮廓、纤细腰部、"
        "臀部轮廓、修长腿部与裙摆的侧面结构。九条狐尾从背部自然延伸出去。",
        bg=BG_PLAIN, neg_key="full"),

    row("03", "03_back_full.png", "背面",
        "生成完全相同的妲己的【正后方全身视图】，这是角色资产库的标准背面图。"
        "人物完全背对镜头，不要回头。"
        "重点展示栗红色长发的背面结构、香槟金发饰的背面、服装背面、裸露的肩背线条、"
        "纤细腰线、腰部金色装饰、长裙与九条象牙白狐尾。服装背面设计华丽而大胆。"
        "九条狐尾从背部和腰部自然展开，必须清晰可数、互不融合。",
        bg=BG_PLAIN, neg_key="full"),

    row("04", "04_front45.png", "45° 正面",
        "生成完全相同的妲己的【45度正面全身图】。"
        "人物身体向左前方旋转45度，头部略微转向镜头，眼神妩媚而自信。"
        "一只手轻轻触碰自己的长发，另一只手自然放在腰侧，身体形成优雅自然的S形曲线。"
        "重点突出东方女性面部、锁骨、肩颈、纤细腰部、修长腿部、朱红长裙、"
        "香槟金装饰与九条象牙白狐尾。",
        bg=BG_PLAIN, neg_key="full"),

    row("05", "05_back45.png", "45° 背面（回眸）",
        "生成完全相同的妲己的【45度背面全身图】。"
        "⚠️ 本图必须是背面角度：人物的身体明显背对镜头，主要看到的是后背、"
        "后脑的栗红色长发与背部的香槟金发饰，肩背线条正对镜头之外。"
        "绝不是正面或侧身构图。只有头部轻轻回眸转向镜头，"
        "能看到四分之一侧脸与一只眼睛，眼神神秘妩媚。"
        "重点展示长发、香槟金发饰、肩背、腰线、服装背部、裙摆与九条象牙白狐尾。"
        "九条狐尾形成巨大的象牙白扇形，部分尾巴向人物两侧展开。",
        bg=BG_PLAIN, neg_key="full"),

    row("06", "06_bust_portrait.png", "电影级半身肖像",
        "生成完全相同的妲己的【电影级半身肖像】，构图从头顶到腰部，画面只保留上半身。"
        "妲己**正面朝向镜头**，脸部完整清晰地位于画面正中，双眼睁开直视镜头，绝不侧身、绝不背对镜头、"
        "绝不让长发或狐尾遮住面部。头部微微低下，双眼抬起直视镜头，"
        "嘴角带着若有若无的妩媚微笑。"
        "⚠️ 双眼必须是睁开的、瞳孔琥珀红色、视线明确看向镜头，"
        "严禁闭眼、低头、垂眼、闭着嘴或眼神看向别处。"
        "本图只画一个人物，画面中不得出现第二个身体、不得出现背影、不得出现侧脸。"
        "重点展示东方女性面孔、琥珀红色的眼睛、长睫毛、朱红色嘴唇、栗红色长发、"
        "香槟金发饰、锁骨、肩部与朱红色服装。皮肤细腻自然，眼睛具有真实的高光，"
        "头发具有细腻光泽。电影级国漫3D人物肖像。"
        "画面影调必须明亮通透：光线充足、面部曝光正常偏亮、肤色白皙透亮、"
        "浅灰色背景明亮干净，不要阴暗、不要逆光剪影、不要让头发糊成一片黑。"
        "九尾只需在人物身后隐约露出，不占据画面主体。",
        bg=BG_PLAIN, neg_key="half", half=True,
        extra_tail="本图只画一个人物，画面中不得出现第二个身体、不得出现背影、不得出现侧脸。"
                   "服装必须与主参考图完全一致：整件是纯正的朱红色长裙，"
                   "不得出现黑色衣料、不得出现黑红撞色拼接、不得出现白色拼接面料。"
                   # 【2026-10-06】实测：42% 裁切的参考图里带了胸口那道白色镶边，
                   # 模型把它当成"撞色拼接"→ 半身像的裙子变成黑红拼接，与 01 不一致。
                   # 对策：裁切收紧到头部+肩部 + 负向明写黑色/撞色。
                   "脸型、五官、发型、发色必须与主参考图完全是同一个人，不得换脸。",
        neg_extra="黑色裙子，黑色服装，黑红拼接，黑红撞色，撞色拼接裙，"
                  "白色拼接衣料，白色胸衣，服装改款，与参考图不同的服装设计"),

    row("07", "07_dance_pose.png", "东方古典舞动态",
        "生成完全相同的妲己的【全身动态角色图】，用于后续图生视频的动作参考。"
        "妲己正在跳优雅的东方古典舞：身体轻微旋转，一条腿向前迈出，腰部自然扭转，"
        "一只手抬起，另一只手拉起长袖。朱红色的长袖与裙摆随动作飘动，"
        "栗红色长发向后飞扬。九条象牙白狐尾同时完全展开，形成具有层次感的巨大扇形——"
        "部分尾巴向上、部分向两侧、部分向下。动作优雅、性感、高贵，具有视觉冲击力。",
        bg=BG_PLAIN, neg_key="full",
        extra_tail="九尾必须形成有上下层次的三维扇形，而不是平铺的一排。"),

    row("08", "08_battle_state.png", "战斗状态",
        "生成完全相同的妲己的【战斗状态全身角色图】。"
        "人物身体向前倾，一只手向前伸出，另一只手位于身体侧后方。"
        "朱红色长袖与半透明薄纱随气流向后飘动，栗红色长发向后飞扬，"
        "九条象牙白狐尾全部完全展开，形成强烈的空间层次感。"
        "眼神从妩媚转为冷静、危险、凌厉，双眼带有微弱的赤金色妖力光芒，"
        "香槟金饰品反射着光线。服装、脸型与九尾数量必须与参考图完全一致，不得改变。"
        "整体效果像高预算中国3D动画电影中的九尾狐妖战斗镜头。",
        bg=BG_PLAIN, neg_key="full",
        extra_tail="九尾必须形成强烈的纵深层次感。"),

    row("09", "09_hero_master.png", "最终 Hero Master Reference",
        "生成妲己的【最终角色 Hero 图】，这是整个角色资产库的 Master Reference，"
        "将作为后续所有分镜、角色一致性、图生视频与角色编辑的最高优先级参考图。"
        "妲己站在画面中央，身体呈45度，头部回眸看向镜头，栗红色长发自然飘动。"
        "完整展示朱红与绛红色的东方幻想服装、香槟金饰品、纤细腰部与修长双腿。"
        "九条象牙白狐尾在身后完全展开，形成巨大的优雅扇形，尾尖带有极淡的暖金色渐变。"
        "妲己拥有成熟、妩媚、高贵、神秘、危险的东方狐妖气质，琥珀红色的眼睛，"
        "朱红色的嘴唇，面带若有若无的自信微笑。"
        "整体色彩只使用朱红、绛红、香槟金、象牙白与栗红色。"
        "整体视觉为高预算国产3D动画电影的中国国漫3D东方幻想角色设计，"
        "电影级CG，精致人物建模，细腻皮肤，真实丝绸，细腻毛发，蓬松狐尾，高级金属材质，"
        "电影级灯光，体积光，边缘光，高级材质，高细节，电影海报级构图。"
        "画面中不得出现其他人物。",
        bg=BG_HERO, neg_key="hero",
        extra_tail="九尾必须形成巨大、优雅、对称的扇形，具有海报级的视觉冲击力。"),
]

RAW_SUB = "raw"


# ============================================================ 造型预设（2026-10-06）
# 用户反馈：① 腿露得太少 ② 主色要改白（"还是白色吧"）。
# 实现方式：不动 VIEWS 字面量，而是在 argparse 之后对**已组装好的 prompt/neg**
# 做定向替换。每个替换对写成 (源, 目标, 是否必须命中)。
# ⚠️ 硬约束：标记为必须命中的替换对若一处都没命中 → 直接报错退出。
#    （教训：静默没改到 = 白烧一轮 GPU，而且会误判成"改了也没用"。）
# ⚠️ 不要在负向词表里堆料：实测 90+ 条会把画面打崩。这里只做**等量替换**，
#    白裙禁令换成红裙禁令，净条数不变。
P_SLIT_RED = "下身是浓郁的朱红色高开衩长裙，露出部分修长腿部，"
P_SLIT_WHITE = "下身是月白色的高开衩长裙，"

LOOK_PRESETS = {
    "white": {
        "pos": [
            ("服装主色必须是朱红色与绛红色，整条主裙是浓郁的朱红偏绛红，"
             "绝不是白色、米色、淡粉色或灰白色，裙子不能是白色布料：",
             "服装主色必须是月白色与银白色，整条主裙是清爽的月白偏银白，"
             "绝不是朱红、绛红、正红或任何大面积饱和暖红色，裙子不能是红裙：", True),
            ("上身是修身剪裁的朱红色紧身衣，露肩设计，露出锁骨与肩部，",
             "上身是修身剪裁的月白色丝质紧身衣，露肩设计，露出锁骨与肩部，", True),
            ("肩臂配有一对半透明的朱红色轻纱广袖；",
             "肩臂配有一对半透明的月白色轻纱广袖；", True),
            ("裙摆层叠飘逸，边缘缀有半透明的朱红色薄纱；",
             "裙摆层叠飘逸，边缘缀有半透明的月白色薄纱；", True),
            # ⚠️ 开衩句必须由 LOOK 层先换色，LEGS 层才能按白裙源串接上。
            #    顺序依赖：pos_pairs 是 LOOK → LEGS → TAIL，改顺序会静默退回红裙。
            ("下身是浓郁的朱红色高开衩长裙，露出部分修长腿部，",
             "下身是月白色的高开衩长裙，", True),
            ("衣身有精致的香槟金刺绣、金色云纹与东方宫廷缠枝纹样。",
             "衣身满布精致的香槟金刺绣、金色云纹与东方宫廷缠枝纹样，"
             "领口、腰封与袖口各缀一道正红色滚边 —— 正红是全身上下唯一的暖色点睛。", True),
            ("朱红色饱满唇形。", "正红色饱满唇形。", True),
            # ---- 颜色锁（prompt 最末尾那句，必须整句换掉。只追加新锁会与旧锁打架）----
            ("最后再次确认配色：整条长裙的主色是浓郁的朱红色与绛红色，"
             "绝不是白色、象牙白色、米白色或灰白色的裙子，裙身不得出现大面积白色；"
             "象牙白色只属于身后的九条狐尾。",
             "最后再次确认配色：整条长裙的主色是清爽的月白色与银白色，"
             "绝不是朱红、绛红、正红等饱和暖色，正红只以极细的滚边点缀出现；"
             "身后的九条狐尾是冷调象牙白，狐尾与月白长裙之间必须有清晰可辨的边界、"
             "投影与明暗反差，绝不糊成一片白色。", True),
            # ---- 02–09 视图里零散写着的「朱红」，不换掉会与主色冲突 ----
            ("朱红绛红色的东方幻想长裙", "月白银白色的东方幻想长裙", True),
            ("只能是同一条红裙。", "只能是同一条白裙。", True),
            ("修长腿部、朱红长裙、", "修长腿部、月白长裙、", True),
            ("香槟金发饰、锁骨、肩部与朱红色服装。",
             "香槟金发饰、锁骨、肩部与月白色服装。", True),
            ("服装必须与主参考图完全一致：整件是纯正的朱红色长裙，"
             "不得出现黑色衣料、不得出现黑红撞色拼接、不得出现白色拼接面料。",
             "服装必须与主参考图完全一致：整件是纯正的月白色长裙，"
             "不得出现黑色衣料、不得出现红色撞色拼接、不得出现大红拼接面料。", True),
            ("朱红色的长袖与裙摆随动作飘动，", "月白色的长袖与裙摆随动作飘动，", True),
            ("朱红色长袖与半透明薄纱随气流向后飘动，",
             "月白色长袖与半透明薄纱随气流向后飘动，", True),
            ("完整展示朱红与绛红色的东方幻想服装、",
             "完整展示月白与银白色的东方幻想服装、", True),
            ("整体色彩只使用朱红、绛红、香槟金、象牙白与栗红色。",
             "整体色彩只使用月白、银白、香槟金、正红点睛、象牙白与栗红色。", True),
        ],
        "neg": [
            ("白色主裙，白色裙子，米色裙子，淡粉色裙子，灰白色裙子，裙子褪色成白色，",
             "朱红主裙，大红主裙，绛红色裙子，高饱和红色主色，整条裙子变成红色，", True),
            ("白色拼接衣料，白色胸衣，",
             "红色拼接衣料，大红胸衣，", True),
        ],
        # 白色锁已经整句替换了 COLOR_LOCK，这里不再追加，避免两套配色锁互相打架
        "pos_add": [],
        "neg_add": [],
    },
}

LEGS_PRESETS = {
    "high": {
        "pos": [
            (P_SLIT_RED,
             "下身是浓郁的朱红色超高开衩长裙，腰侧的开衩一路开到大腿根部、开衩极高，"
             "将整条大腿、膝盖与小腿大面积完整露出并被光线打亮，", False),
            (P_SLIT_WHITE,
             "下身是月白色的超高开衩长裙，腰侧的开衩一路开到大腿根部、开衩极高，"
             "将整条大腿、膝盖与小腿大面积完整露出并被光线打亮，", False),
        ],
        "neg": [],
        "pos_add": [
            "构图必须强调双腿：大腿上半段、膝盖、小腿与脚踝全部直接可见并被光线打亮，"
            "长裙只在身体后方与两侧自然垂落，绝不用裙摆遮盖双腿。",
        ],
        "neg_add": [
            "裙摆盖住大腿，长裙遮住膝盖，整条腿被裙子包住，看不到腿，腿部被布料覆盖",
        ],
    },
    # 【2026-10-06】用户追加要求"把大腿露出来"。high 档实测只露一条腿，
    # 且两片布料仍在身体前方合拢 → 再加一档：双片分离、双腿全露。
    "max": {
        "pos": [
            # 01 原写「一条腿略微向前迈出」——与"双腿都露出来"冲突，一并改掉
            ("自然站立，身体略微呈优雅的S形曲线，一条腿略微向前迈出，",
             "自然站立，双腿自然微微分开、重心均匀，身体略微呈优雅的S形曲线，", False),
            (P_SLIT_RED,
             "下身是浓郁的朱红色前后分离双片长裙：前片从腰线起就向两侧分开，"
             "开衩高到大腿根部以上，后片自然垂落；两条腿从大腿根部一直到脚踝全部裸露在外，"
             "完全不被任何布料遮挡、覆盖或透过纱料遮蔽，", False),
            (P_SLIT_WHITE,
             "下身是月白色的前后分离双片长裙：前片从腰线起就向两侧分开，"
             "开衩高到大腿根部以上，后片自然垂落；两条腿从大腿根部一直到脚踝全部裸露在外，"
             "完全不被任何布料遮挡、覆盖或透过纱料遮蔽，", False),
        ],
        "neg": [],
        "pos_add": [
            "构图必须把两条腿完整露出来：站姿为双腿自然微微分开、重心均匀，"
            "左腿与右腿都完整可见并被光线均匀打亮；裙摆只从腰部两侧与身体后方垂下，"
            "绝不在身体前方合拢，布料不得覆盖或半遮任何一条腿，"
            "大腿内侧与外侧的皮肤都必须直接可见。",
        ],
        "neg_add": [
            "布料遮住大腿，纱料覆盖大腿，半透明纱料遮住腿，腿被裙摆挡住，"
            "只有一条腿可见，另一条腿被挡住，双腿被布料包裹，裙摆在身体前方合拢，"
            "裙片在身前交叉，大腿被衣料遮盖",
        ],
    },
}

TAIL_PRESETS = {
    "clean": {
        "pos": [
            ("只在每条尾巴最末端约五分之一的尖端部分，才有一道非常淡的暖金色渐变，",
             "九条尾巴通体是完全一致的冷调纯白色，尾尖同样保持纯白，"
             "不允许出现任何金色、米色或暖黄色渐变，", True),
        ],
        "neg": [
            ("米黄色尾巴，暖黄色尾巴，奶油色尾巴，金黄色尾巴，杏色尾巴，",
             "金色的尾巴尖，暖金色尾尖，尾尖泛黄，尾巴尖带金色，", True),
        ],
        "pos_add": [],
        "neg_add": [],
    },
}


def _sub(text: str, pairs, hits: dict) -> str:
    for src, dst, _req in pairs:
        n = text.count(src)
        if n:
            hits[src] = hits.get(src, 0) + n
            text = text.replace(src, dst)
    return text


def apply_look(look: str, legs: str, tail: str) -> None:
    """就地把 VIEWS 的 prompt/neg 换成指定造型。未命中的必填替换对 → 报错退出。"""
    global VIEWS
    pos_pairs, neg_pairs, pos_add, neg_add = [], [], [], []
    for store, key in ((LOOK_PRESETS, look), (LEGS_PRESETS, legs), (TAIL_PRESETS, tail)):
        cfg = store.get(key)
        if not cfg:
            continue
        pos_pairs += cfg["pos"]
        neg_pairs += cfg["neg"]
        pos_add += cfg["pos_add"]
        neg_add += cfg["neg_add"]

    if not (pos_pairs or neg_pairs or pos_add or neg_add):
        return

    hits: dict[str, int] = {}
    out = []
    for code, name, label, prompt, neg in VIEWS:
        prompt = _sub(prompt, pos_pairs, hits)
        neg = _sub(neg, neg_pairs, hits)
        if pos_add:
            prompt += "".join(pos_add)
        if neg_add:
            neg = neg + "，" + "，".join(neg_add)
        out.append((code, name, label, prompt, neg))
    VIEWS = out

    missing = [src for src, _d, req in pos_pairs if req and src not in hits]
    missing += [src for src, _d, req in neg_pairs if req and src not in hits]
    if missing:
        for m in missing:
            print(f"[预设] ⚠️ 必填替换对未命中: {m[:70]}...", file=sys.stderr)
        raise SystemExit("[预设] 有必填替换对没命中 —— 拒绝继续生成（防止静默没改到）")

    # legs 档位的开衩源串是「二选一」（随主色而变），至少要命中一条
    if legs in LEGS_PRESETS and not any(src in hits for src, _d, _r in LEGS_PRESETS[legs]["pos"]):
        raise SystemExit(f"[预设] legs={legs} 的开衩替换一处都没命中 —— 拒绝继续生成")

    print(f"[预设] look={look} · legs={legs} · tail={tail} · 命中替换 {sum(hits.values())} 处")


def main() -> int:
    ap = argparse.ArgumentParser(description="妲己 9 图标准资产生成（Qwen-Image 2.1）")
    ap.add_argument("--out-dir", default="backend/storage/temp/stills/daji_assets9")
    ap.add_argument("--base-ref", default="",
                    help="01 的画质锚点参考图（走编辑链）。给了就用编辑链，"
                         "画质明显优于纯文生图；不给则纯文生图。")
    ap.add_argument("--sheet", nargs="?", const="backend/storage/temp/stills/daji_assets9_sheet.png",
                    default="", help="拼版输出路径（不带值用默认）")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1536,
                    help="全身图建议 1024x1536（2:3 竖构），半身图同样尺寸即可")
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--cfg", type=float, default=4.0,
                    help="必须 >2.5，否则旋转/背面这类空间指令不生效（skill §22.8）")
    ap.add_argument("--sampler", default="euler",
                    help="⚠️ 必须 euler。dpmpp_2m 会让本 int8 模型的纯文生图崩坏（绿斑+纵向拉丝）")
    ap.add_argument("--scheduler", default="simple",
                    help="⚠️ 必须 simple。karras 会与 dpmpp_2m 一起触发崩坏；single 已实证干净")
    ap.add_argument("--seed", type=int, default=20261004)
    ap.add_argument("--host", default="http://127.0.0.1:8188")
    ap.add_argument("--only", default="", help="只跑指定序号，逗号分隔")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--retries", type=int, default=12)
    ap.add_argument("--retry-wait", type=int, default=15)
    ap.add_argument("--per-try", type=int, default=1500)
    ap.add_argument("--pipeline", action="store_true",
                    help="一次性把全部任务压进队列再统一取回（避免主模型反复换入换出）")
    ap.add_argument("--crop06", action="store_true", default=True,
                    help="06 用 01 的上半身裁切图当参考（半身构图更稳）")
    ap.add_argument("--crop06-ratio", type=float, default=0.28,
                    help="06 参考图的裁切比例（取 01 顶部多少比例）。"
                         "0.42 会把胸口那道白镶边带进去 → 模型误判成撞色拼接，"
                         "半身像裙子变黑红拼接。收紧到 0.28（头部+肩部）身份更稳。")
    ap.add_argument("--look", choices=["red", "white"], default="red",
                    help="主色：red=朱红绛红（原 spec）/ white=月白银白+正红点睛")
    ap.add_argument("--legs", choices=["normal", "high", "max"], default="normal",
                    help="露腿程度：normal=原高开衩 / high=超高超开衩 / max=双片分离、双腿全露")
    ap.add_argument("--tail", choices=["gold", "clean"], default="gold",
                    help="尾尖：gold=末端极淡暖金渐变 / clean=通体冷调纯白无金")
    args = ap.parse_args()

    apply_look(args.look, args.legs, args.tail)

    base = args.host.rstrip("/")
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    raw_dir = out_dir / RAW_SUB
    raw_dir.mkdir(parents=True, exist_ok=True)

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    targets = [v for v in VIEWS if not only or v[0] in only]
    ref01 = raw_dir / "01_front_full.png"

    base_ref = Path(args.base_ref) if args.base_ref else None
    if base_ref is not None:
        if not base_ref.is_absolute():
            base_ref = (ROOT / base_ref).resolve()
        if not base_ref.exists():
            print(f"画质锚点参考图不存在: {base_ref}", file=sys.stderr)
            return 2
        print(f"[画质锚点] {base_ref.name}  (01 走编辑链)")

    print(f"[母版] {raw_dir}")
    print(f"[规格] {args.width}x{args.height} · {len(targets)} 张 · {args.steps}步 · "
          f"cfg={args.cfg} · {args.sampler}/{args.scheduler}")
    print(f"[超分] python scripts/upscale_stills.py --in-dir {RAW_SUB}\n")

    upload_cache: dict[str, str] = {}
    done, failed, pending = [], [], []

    def collect(job: dict) -> None:
        code, label, out, wf = job["code"], job["label"], job["out"], job["wf"]
        last_err = ""
        for attempt in range(1, args.retries + 1):
            try:
                if job.get("pid") is None:
                    job["pid"] = submit(base, wf, uuid.uuid4().hex)
                    print(f"        [{code}] 重投 {job['pid'][:8]} ...", flush=True)
                imgs = wait_robust(base, job["pid"], args.per_try, f"{code} {label}")
                saved = download(base, imgs[0], out)
                print(f"        -> {saved.name} ({saved.stat().st_size/1024:.0f} KB)", flush=True)
                done.append(saved)
                return
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                job["pid"] = None
                from gen_daji_assets import JobLost
                retryable = isinstance(e, JobLost) or "interrupted" in last_err
                if retryable and attempt < args.retries:
                    why = "被清出队列" if isinstance(e, JobLost) else "被外部中断"
                    print(f"        [{code}] {why}，{args.retry_wait}s 后重投 "
                          f"({attempt}/{args.retries})", flush=True)
                    time.sleep(args.retry_wait)
                    continue
                print(f"        失败: {last_err[:300]}", file=sys.stderr, flush=True)
                break
        failed.append((code, label, last_err))

    for idx, (code, name, label, prompt, neg) in enumerate(targets, 1):
        out = raw_dir / name
        if out.exists() and out.stat().st_size > 1024 and not args.force:
            print(f"[{idx}/{len(targets)}] {code} {label} — 已存在，跳过")
            done.append(out)
            continue

        # 01 默认走编辑链（有画质锚点时），否则文生图。
        # ⚠️ 实测：纯文生图的 01 会出「绿色斑块 + 纵向条纹 + 脸被糊住」的伪影，
        #    而同一提示词走编辑链（以高画质的既有妲己图为底）画质明显更好。
        if base_ref:
            key = "01"
            if key not in upload_cache:
                ref_use = prep_reference(base_ref, args.width, args.height, "pad")
                upload_cache[key] = upload_image(base, ref_use)
            wf = build_workflow("qwen_edit_scene.json", {
                "{{prompt}}": prompt, "{{negative_prompt}}": neg,
                "{{width}}": args.width, "{{height}}": args.height,
                "{{seed}}": args.seed,
                "{{reference_image}}": upload_cache[key],
            })
        elif code == "01":
            wf = build_workflow("qwen_image_character.json", {
                "{{prompt}}": prompt, "{{negative_prompt}}": neg,
                "{{width}}": args.width, "{{height}}": args.height,
                "{{seed}}": args.seed,
            })
        else:
            if not ref01.exists():
                print(f"[{idx}/{len(targets)}] {code} {label} — 跳过：主参考图 01 还不存在",
                      file=sys.stderr)
                failed.append((code, label, "缺少 01 主参考图"))
                continue
            ref_src = ref01
            key = f"{code}"
            if key not in upload_cache:
                ref_use = ref_src
                if code == "06" and args.crop06:
                    # 裁到腰部以上：太低会带入大量尾巴，参考图里尾巴占比一大，
                    # 输出就跟着变成「尾巴为主体的小图」（实测构图跑偏的主因）。
                    ref_use = prep_upper_crop(ref_src, raw_dir / "_ref06_upper.png",
                                              args.crop06_ratio, args.width, args.height)
                ref_use = prep_reference(ref_use, args.width, args.height, "pad")
                upload_cache[key] = upload_image(base, ref_use)
            wf = build_workflow("qwen_edit_scene.json", {
                "{{prompt}}": prompt, "{{negative_prompt}}": neg,
                "{{width}}": args.width, "{{height}}": args.height,
                "{{seed}}": args.seed + int(code),
                "{{reference_image}}": upload_cache[key],
            })

        apply_overrides(wf, steps=args.steps, cfg=args.cfg,
                        sampler=args.sampler, scheduler=args.scheduler)
        for node in wf.values():
            if node.get("class_type") == "SaveImage":
                node["inputs"]["filename_prefix"] = f"daji9_{code}"

        job = {"code": code, "label": label, "out": out, "wf": wf, "pid": None}
        if args.pipeline:
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
        print(f"\n[取回] 队列中 {len(pending)} 个任务 ...")
        for job in pending:
            collect(job)

    print(f"\n[完成] 成功 {len(done)} / 失败 {len(failed)}")
    for c, l, e in failed:
        print(f"   x {c} {l}: {e[:200]}")

    if args.sheet and len(done) == len(VIEWS):
        import gen_daji_assets as g
        g.VIEWS = VIEWS          # 拼版按 9 图的新文件名/标签走
        g.RAW_SUB = RAW_SUB
        sheet = Path(args.sheet)
        if not sheet.is_absolute():
            sheet = (ROOT / sheet).resolve()
        make_sheet(out_dir, sheet)
    elif args.sheet:
        print(f"\n[拼版] 跳过 —— 需要 9 张齐全，当前 {len(done)} 张")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
