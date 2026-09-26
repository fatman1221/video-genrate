"""本地规划引擎（fallback planner）。

职责边界说明：
- 正常生产流程中，脚本 / 分镜 / 人物都应该由 Agent（WorkBuddy / Codex）思考生成，
  然后通过 create_script / create_storyboard / create_character 写入。
- 本模块是「离线兜底」：当没有 Agent 参与时，仍能让整条链路真实跑通，
  也便于做确定性回归测试（同样输入永远得到同样分镜）。
- 它不是把业务逻辑写死进 Backend，而是可替换的规划 Provider。
"""
from __future__ import annotations

from typing import Any

# 章节模板：(标题, 摘要, 地点, 情绪, [(要点, 旁白, 画面, 镜头语言), ...])
CHAPTERS: tuple[tuple[str, str, str, str, tuple[tuple[str, str, str, str], ...]], ...] = (
    (
        "开场：为什么需要 AI Agent", "用一个真实痛点引出主题", "深夜的办公室", "好奇",
        (
            ("深夜加班的痛点", "凌晨一点，你还在重复着整理表格、写周报、回消息的机械劳动。",
             "深夜办公室，一个人趴在桌上，屏幕堆满标签页", "缓慢推近，特写疲惫的脸"),
            ("一个念头", "如果有一个助手，能自己看、自己判断、自己动手，那会怎样？",
             "角色头顶亮起一个对话气泡，里面是一个发光的小机器人", "镜头拉近气泡，光线扩散"),
            ("Agent 登场", "这就是 AI Agent：它不只是回答问题，而是替你把事情做完。",
             "紫色机器人「阿格」悬浮在画面中央，环形灯缓缓转动", "环绕运镜，仰视角度"),
            ("本期目标", "接下来的五分钟，我们把 Agent 从概念一路拆到能干活的系统。",
             "黑板上写着四个大字：看、想、做、查", "平稳横移，扫过黑板"),
            ("认识讲师", "我是小智，今天带你用漫画的方式，把 Agent 彻底讲明白。",
             "戴圆框眼镜、穿紫色卫衣的小智挥手打招呼", "中景，轻微点头动画感"),
            ("预告路线", "我们会先看原理，再看工具，最后动手搭一个真能跑的 Agent。",
             "一条路线图从左到右展开，三个节点依次点亮", "跟镜平移，节奏明快"),
        ),
    ),
    (
        "什么是 AI Agent", "把概念讲清楚，避免神秘化", "明亮的教室", "专注",
        (
            ("不是聊天框", "很多人以为 Agent 就是一个更聪明的聊天框，其实不是。",
             "画面左边是聊天窗口，右边被打上一个大大的问号", "左右分屏，疑问符号放大"),
            ("定义一句话", "Agent 是能自己设定目标、拆解任务、调用工具、并根据结果修正的智能体。",
             "四个关键词依次出现：目标、拆解、工具、修正", "镜头按顺序点选每个词"),
            ("和大模型的区别", "大模型负责『想』，Agent 还要负责『做』，并且对结果负责。",
             "两个角色对比：一个只会说话，一个动手施工", "分屏对比，中景切近景"),
            ("一个生活类比", "把它想成刚入职的实习生：给方向、给工具、给反馈，它就能独当一面。",
             "实习生坐在工位，桌上摆着工具箱和待办清单", "过肩视角落幅到工具箱"),
            ("能力三件套", "感知环境、作出决策、执行动作，三件事缺一不可。",
             "三个齿轮咬合转动，分别标注感知、决策、执行", "缓慢拉远，展示整体结构"),
            ("常见误解", "Agent 不是万能，它只是把『重复推理 + 调用工具』这件事自动化了。",
             "一个天平，左边写自动化，右边写判断权交给人", "平移镜头，天平轻微摆动"),
        ),
    ),
    (
        "Agent 的核心循环", "Plan-Execute-Observe-Correct", "白板前", "严谨",
        (
            ("循环概览", "Agent 的心跳是一个循环：规划、执行、观察、修正。",
             "环形箭头出现在白板上，四个节点依次高亮", "顺时针环绕运镜"),
            ("规划阶段", "先拆任务：把大目标切成可执行的小步骤，越具体越容易成功。",
             "一棵任务树从根节点层层展开", "由下向上推镜，展示树枝延展"),
            ("执行阶段", "然后调用工具：查资料、跑代码、发请求，一步一步落地。",
             "机械臂依次抓取不同的工具图标", "侧向跟随机械臂运动"),
            ("观察阶段", "做完不看等于没做，必须拿到真实的执行结果。",
             "屏幕上滚动着日志，一双眼睛紧盯屏幕", "眼珠特写，画面轻微缩放"),
            ("评估阶段", "结果对不对？和预期差多少？这一步决定要不要返工。",
             "一个打分表，检查项逐个打勾或打叉", "从上向下俯拍打分表"),
            ("修正阶段", "不对就改：换参数、换工具、换提示词，然后进入下一轮。",
             "角色拧动旋钮，仪表读数回到绿色区间", "特写旋钮，轻微抖动感"),
        ),
    ),
    (
        "工具调用与 Skill", "Agent 的手和脚", "机房走廊", "兴奋",
        (
            ("工具有多重要", "没有工具的 Agent，只是一个会说话的嘴替。",
             "一个人自言自语，旁边工具箱落满灰", "横移镜头，灰尘飘落"),
            ("工具的形式", "工具可以是一个 API、一段命令行、甚至一次浏览器点击。",
             "三种形态并排展示：接口、终端、浏览器", "三连切，节奏紧凑"),
            ("优先级原则", "能用 API 就别用命令行，能用命令行就别开浏览器。",
             "阶梯示意：API 在最上，浏览器在最下", "自下而上摇镜"),
            ("什么是 Skill", "把可执行能力封装成标准 Skill：有名字、有输入、有输出、有状态。",
             "一个标准化的卡片模板，字段逐个亮起", "推镜到卡片细节"),
            ("Skill 契约", "输入校验、错误信息、状态回报，缺一样都会让 Agent 抓瞎。",
             "契约文档被盖章通过，旁边是退回的红章版本", "对比分屏，落幅盖章特写"),
            ("可追踪性", "每次调用都要留下记录：谁调的、用了什么参数、结果如何。",
             "日志面板里一条条记录不断追加", "缓慢下移，展示日志流"),
        ),
    ),
    (
        "记忆与状态管理", "让 Agent 不每次都从零开始", "图书馆", "沉静",
        (
            ("为什么需要记忆", "没有记忆的 Agent，每一步都在重新认识这个世界。",
             "角色反复打开同一本书，表情越来越困惑", "循环运镜，第几次切换时加速"),
            ("短期记忆", "当前任务上下文是短期记忆，用完就丢，保持轻快。",
             "一块白板，写满又被擦掉", "快节奏擦除，白板闪回"),
            ("长期记忆", "跨任务复用的经验是长期记忆，需要沉淀和检索。",
             "书架上一本本标注好的笔记被抽出来", "推镜到被抽出的那本书"),
            ("状态外置", "把状态放到数据库里，Agent 才能在中断后接着干。",
             "Agent 与数据库之间有一条稳定的连线", "横移展示连线上的数据流"),
            ("断点恢复", "任务失败不可怕，可怕的是失败后要从头再来。",
             "进度条停在 62%，随后从 62% 继续前进", "特写进度条，数字跳动"),
            ("粒度控制", "失败重试的最小单位应该是镜头，而不是整个项目。",
             "一整条视频被切成很多小格，只有一格被替换", "俯拍网格，单格高亮"),
        ),
    ),
    (
        "实战：搭一个 Agent", "从零到能跑的最短路径", "工作台", "务实",
        (
            ("先定边界", "先想清楚：它负责哪些事，不负责哪些事。",
             "一张职责清单，被分成『做』和『不做』两栏", "平移扫过清单"),
            ("搭骨架", "项目、任务、素材、状态，四个模块先立起来。",
             "四个模块像积木一样被依次搭起", "由下向上抬镜"),
            ("接工具", "从最简单的工具开始接，先跑通再加复杂度。",
             "第一根线被插上，指示灯变绿", "特写插头与指示灯"),
            ("写循环", "把规划、执行、观察、修正写成代码里的主循环。",
             "代码在屏幕上逐行滚动，循环结构高亮", "推镜到循环结构"),
            ("加护栏", "超时、重试、人工确认，三样护栏一个都别省。",
             "三道护栏依次落下，护住中间的流程", "跟镜落下，节奏顿挫"),
            ("第一次跑通", "第一次跑通的那一刻，整个链路会给你巨大的信心。",
             "屏幕上出现第一个成功的绿色标记", "推近绿色标记，轻微光晕"),
        ),
    ),
    (
        "常见坑与调优", "把踩过的坑提前告诉你", "修理间", "谨慎",
        (
            ("坑一：范围太大", "一次让它干太多事，结果什么都做不好。",
             "一个箱子被塞满，盖子合不上", "侧拍箱子，轻微弹跳"),
            ("坑二：没有反馈", "不给它真实结果，它就会一直在猜。",
             "角色闭着眼睛摸索，撞到桌角", "跟拍碰撞，短促抖动"),
            ("坑三：不设预算", "不限制次数和成本，循环会烧到失控。",
             "仪表指针冲进红区，警报灯闪烁", "推镜到红区，红光闪烁"),
            ("坑四：忽略幂等", "重试时重复扣款、重复发送，是最常见的翻车点。",
             "两条一模一样的记录被贴上重复标签", "快速切镜，标签盖章"),
            ("调优思路", "先降低单步难度，再提高单步质量，最后提速。",
             "三个旋钮按顺序被拧动", "依次特写每个旋钮"),
            ("评测闭环", "给自己建一套评测，改动之后才知道是变好还是变坏。",
             "折线图先下滑后稳步上升", "跟随折线走势横移"),
        ),
    ),
    (
        "总结与下一步", "收束全片，给出行动清单", "清晨的城市", "明亮",
        (
            ("回顾主线", "今天我们走完了：概念、循环、工具、记忆、实战、避坑。",
             "六个节点在画面中连成一条完整路线", "快速回溯，节点依次点亮"),
            ("一句话记住", "Agent 的本质，是让模型学会用工具、看结果、再修正。",
             "这句话被放大在画面正中，其余元素虚化", "推镜至文字，背景模糊"),
            ("动手清单", "今天就挑一个重复劳动，试着把它交给 Agent。",
             "清单上第一条被郑重打勾", "特写打勾动作"),
            ("风险提醒", "涉及资金和对外发送的动作，永远保留人工确认。",
             "一个确认弹窗被慎重地点下同意", "中景，手部特写"),
            ("下期预告", "下一期，我们讲怎么给 Agent 做质量检查。",
             "预告卡片翻转，露出下一期标题", "翻转运镜"),
            ("结束语", "我是小智，我们下期见。",
             "小智挥手告别，画面缓缓变亮", "慢慢拉远，光线渐强"),
        ),
    ),
)


DEFAULT_CHARACTERS: tuple[dict[str, Any], ...] = (
    {
        "name": "小智", "role": "protagonist",
        "description": "本片主讲人，用漫画方式讲解 AI Agent",
        "appearance": "年轻讲师，圆框眼镜，紫色连帽卫衣，黑色短发，表情生动，常用手势讲解",
        "personality": "热情、耐心、善用比喻",
        "voice_style": "清晰温和的中文男声，语速中等",
        "reference_prompt": "comic style illustration, young male teacher, round glasses, purple hoodie, "
                            "friendly smile, half body, clean flat colors, teaching pose",
    },
    {
        "name": "阿格", "role": "supporting",
        "description": "拟人化的 AI Agent 机器人，本片的形象代言",
        "appearance": "悬浮球形机器人，白色机身，紫色环形指示灯，两只简洁的机械手臂",
        "personality": "好奇、行动力强、偶尔冒失",
        "voice_style": "轻快的电子音",
        "reference_prompt": "comic style illustration, cute floating spherical robot, white body, "
                            "purple glowing ring, minimalist arms, friendly, clean flat colors",
    },
    {
        "name": "阿码", "role": "supporting",
        "description": "工程师角色，负责演示动手环节",
        "appearance": "格纹衬衫，戴耳机，卷起袖子，手指灵活",
        "personality": "务实、话不多、动手快",
        "voice_style": "沉稳的中文男声",
        "reference_prompt": "comic style illustration, engineer with plaid shirt and headphones, "
                            "typing on laptop, clean flat colors, half body",
    },
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def plan_characters(requirement: str = "", style: str = "comic") -> list[dict[str, Any]]:
    return [dict(c) for c in DEFAULT_CHARACTERS]


def plan_script(
    *, requirement: str = "", style: str = "漫画教学风格",
    target_duration: float = 300.0, language: str = "zh-CN",
) -> dict[str, Any]:
    seconds_per_char = 4.6
    total_lines = int(target_duration / 5.2) + 1
    chosen: list[tuple[str, str, str, str]] = []
    for _title, _summary, _loc, _mood, points in CHAPTERS:
        chosen.extend(points)
    # 时长不足时按顺序截取，超长时循环补充（保持确定性）
    while len(chosen) < total_lines:
        chosen.extend(chosen[len(chosen) - len(CHAPTERS):] if len(chosen) > len(CHAPTERS) else chosen)

    outline_lines = []
    for idx, (_title, summary, _loc, _mood, points) in enumerate(CHAPTERS, start=1):
        outline_lines.append(f"{idx}. {_title} —— {summary}（{len(points)} 个镜头）")

    estimated = int(sum(len(_line) / seconds_per_char for _line in [p[1] for p in chosen]))
    content_lines = [
        f"《{requirement or 'AI Agent 教学视频'}》脚本",
        f"风格：{style}", f"目标时长：约 {int(target_duration)} 秒", "",
        "【分章大纲】", *outline_lines, "",
        "【正文旁白】",
    ]
    for idx, point in enumerate(chosen, start=1):
        content_lines.append(f"{idx:02d}. {point[1]}")

    return {
        "title": requirement or "AI Agent 教学视频",
        "outline": "\n".join(outline_lines),
        "content": "\n".join(content_lines),
        "style": style,
        "language": language,
        "estimated_duration": estimated,
        "scene_count": len(CHAPTERS),
        "shot_count": len(chosen),
        "provider": "local_planner",
        "model": "chapter-template-v1",
        "parameters": {"target_duration": target_duration, "style": style},
    }


def plan_storyboard(
    *, requirement: str = "", style: str = "漫画教学风格",
    target_duration: float = 300.0, shot_duration: float = 5.0,
    characters: list[dict[str, Any]] | None = None, language: str = "zh-CN",
) -> dict[str, Any]:
    shot_duration = _clamp(float(shot_duration or 5.0), 2.0, 20.0)
    target_shots = max(4, int(round(float(target_duration) / shot_duration)))
    characters = characters or plan_characters(requirement, style)
    char_ids_by_name = {c.get("name", ""): c.get("id") for c in characters}
    # 分镜按角色轮换出镜。**必须优先使用调用方传入的真实角色池**
    # （项目/系列里已定义的角色），否则主角会查无此人、提示词里还会残留
    # 内置演示角色名。只有在完全没有角色时才回落到内置演示角色。
    cast = [c.get("name", "") for c in characters if c.get("name")]
    if not cast:
        cast = [c.get("name", "") for c in DEFAULT_CHARACTERS if c.get("name")]

    # 1) 从章节里按顺序取要点
    pool: list[tuple[str, str, str, str, str]] = []
    for chapter_title, _summary, location, mood, points in CHAPTERS:
        for point in points:
            pool.append((*point, f"{chapter_title}|{location}|{mood}"))

    ordered: list[tuple[str, str, str, str, str]] = []
    round_no = 0
    while len(ordered) < target_shots:
        for item in pool:
            ordered.append(item)
            if len(ordered) >= target_shots:
                break
        round_no += 1
        if round_no > 20:  # 安全阀
            break

    # 2) 按章节切分到 Scene
    scene_defs: list[dict[str, Any]] = []
    shots: list[dict[str, Any]] = []
    seq = 0
    for chapter_index, (chapter_title, summary, location, mood, points) in enumerate(CHAPTERS, start=1):
        chapter_shots: list[dict[str, Any]] = []
        for point_title, voice, visual, camera in points:
            if seq >= target_shots:
                break
            seq += 1
            main_character = cast[(seq - 1) % len(cast)] if cast else ""
            shot = {
                "sequence": seq,
                "code": f"Shot {seq:03d}",
                "duration": shot_duration,
                "description": f"{point_title}：{visual}",
                "camera": camera,
                "location": location,
                "visual_style": style,
                "character_ids": [cid for cid in [char_ids_by_name.get(main_character)] if cid],
                "image_prompt": (
                    f"{style}，{visual}，角色：{main_character}，"
                    f"地点：{location}，情绪：{mood}，干净的扁平配色，高对比轮廓，教学插图"
                ),
                "video_prompt": f"{visual}；镜头语言：{camera}；{style}；连贯动作，人物保持一致",
                "negative_prompt": "低清，模糊，多余手指，文字乱码，水印",
                "voice_script": voice,
                "subtitle_text": voice,
                "extra": {"chapter": chapter_title, "point": point_title},
            }
            chapter_shots.append(shot)
            shots.append(shot)
        if chapter_shots:
            scene_defs.append({
                "sequence": chapter_index,
                "code": f"Scene {chapter_index:02d}",
                "title": chapter_title,
                "summary": summary,
                "location": location,
                "mood": mood,
                "shots": chapter_shots,
            })
        if seq >= target_shots:
            break

    return {
        "title": f"{requirement or 'AI Agent 教学视频'} · 分镜表",
        "synopsis": "以漫画教学风格，从概念到实战完整讲解 AI Agent 的构建方法。",
        "visual_style": style,
        "language": language,
        "scene_count": len(scene_defs),
        "shot_count": len(shots),
        "scenes": scene_defs,
        "provider": "local_planner",
        "model": "chapter-template-v1",
        "parameters": {"target_duration": target_duration, "shot_duration": shot_duration},
    }
