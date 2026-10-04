"""脚本分段：把一集脚本按幕切开，逐幕写作并保持上下文连贯。

存在意义：5 分钟片的脚本上千字，一次性生成质量难控、改一处整篇重来。
切成幕后可以「只写这一幕」，而续写时把**前面已定稿的正文**带进提示词，
生成结果自然承接前文，不会前后矛盾或重复推进剧情。

本模块同时负责把各幕正文回填成整篇 ``Script.content``，
让分镜生成等下游环节不必关心「脚本是分段写的」这件事。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.constants import ScriptSectionStatus
from ..models import Project, Script, ScriptSection
from . import agent_log, references as references_svc

#: 中文旁白语速（字/秒）。与 planner 里的估算保持一致，用于按时长反推字数。
CHARS_PER_SECOND = 4.6
#: 打包上下文时，最多带上前多少幕的**完整正文**（更早的幕只留摘要，避免上下文爆掉）
CONTEXT_FULL_SECTIONS = 3
#: 单独摘出「上一幕结尾」的字符数，用于承接语气与情节
TAIL_CHARS = 240


def list_sections(db: Session, project_id: str) -> list[ScriptSection]:
    return list(db.execute(
        select(ScriptSection)
        .where(ScriptSection.project_id == project_id)
        .order_by(ScriptSection.sequence.asc())
    ).scalars())


def get_section(db: Session, section_id: str) -> ScriptSection | None:
    return db.get(ScriptSection, section_id)


def latest_script(db: Session, project_id: str) -> Script | None:
    return db.execute(
        select(Script).where(Script.project_id == project_id)
        .order_by(Script.created_at.desc())
    ).scalars().first()


def _normalize_sequence(db: Session, project_id: str) -> None:
    """把幕序重排成连续的 1..N（删除 / 插入后调用）。"""
    rows = list_sections(db, project_id)
    for idx, section in enumerate(rows, start=1):
        if section.sequence != idx:
            section.sequence = idx
    db.flush()


def replace_sections(db: Session, project: Project,
                     items: list[dict[str, Any]], *, actor: str = "agent") -> list[ScriptSection]:
    """用一批「幕定义」整体替换该项目的分段。

    用于「重新规划分段」：只覆盖标题/要点/节拍这类结构信息，
    **已有正文按幕序对齐保留**，避免用户辛苦写的内容被规划动作清掉。
    """
    existing = list_sections(db, project.id)
    content_by_seq = {s.sequence: (s.content or "") for s in existing}
    provider_by_seq = {s.sequence: s.provider for s in existing}

    for section in existing:
        db.delete(section)
    db.flush()

    script = latest_script(db, project.id)
    created: list[ScriptSection] = []
    for idx, raw in enumerate(items, start=1):
        sequence = int(raw.get("sequence") or idx)
        content = raw.get("content") or content_by_seq.get(sequence, "")
        section = ScriptSection(
            project_id=project.id,
            script_id=script.id if script else None,
            sequence=sequence,
            code=raw.get("code") or f"第{sequence}幕",
            title=raw.get("title") or f"第 {sequence} 幕",
            summary=raw.get("summary") or "",
            beat=raw.get("beat") or "",
            content=content,
            target_duration=float(raw.get("target_duration") or 0.0),
            status=ScriptSectionStatus.READY if content.strip() else ScriptSectionStatus.DRAFT,
            provider=provider_by_seq.get(sequence) or "agent",
            parameters=raw.get("parameters") or {},
        )
        db.add(section)
        created.append(section)
    db.flush()
    _normalize_sequence(db, project.id)
    sync_script(db, project, actor=actor)
    agent_log.log_event(
        db, project_id=project.id, event="script.sections_planned", actor=actor,
        message=f"脚本按幕切分为 {len(created)} 段",
        detail={"count": len(created)},
    )
    return created


def upsert_section(db: Session, project: Project, *,
                   section_id: str = "", sequence: int | None = None,
                   fields: dict[str, Any] | None = None,
                   actor: str = "agent", touch_script: bool = True) -> ScriptSection:
    """创建或更新一幕。

    ``section_id`` 指定则更新该幕；否则按 ``sequence`` 找，找不到就新建。
    正文有内容时自动转 READY，清空则退回 DRAFT（除非显式指定 status）。
    """
    fields = dict(fields or {})
    section: ScriptSection | None = None
    if section_id:
        section = db.get(ScriptSection, section_id)
        if section is None or section.project_id != project.id:
            raise ValueError(f"脚本分段不存在: {section_id}")
    elif sequence is not None:
        section = db.execute(
            select(ScriptSection).where(
                ScriptSection.project_id == project.id,
                ScriptSection.sequence == int(sequence),
            )
        ).scalars().first()

    if section is None:
        script = latest_script(db, project.id)
        section = ScriptSection(
            project_id=project.id,
            script_id=script.id if script else None,
            sequence=int(sequence or len(list_sections(db, project.id)) + 1),
            code=fields.get("code") or f"第{int(sequence or 1)}幕",
            title=fields.get("title") or f"第 {int(sequence or 1)} 幕",
        )
        db.add(section)
        db.flush()

    for key in ("code", "title", "summary", "beat", "content", "provider"):
        if key in fields and fields[key] is not None:
            setattr(section, key, str(fields[key]))
    if "target_duration" in fields and fields["target_duration"] is not None:
        section.target_duration = float(fields["target_duration"])
    if "parameters" in fields and fields["parameters"] is not None:
        section.parameters = fields["parameters"]
    if not section.script_id:
        script = latest_script(db, project.id)
        section.script_id = script.id if script else None

    if fields.get("status"):
        section.status = fields["status"]
    else:
        section.status = (ScriptSectionStatus.READY if (section.content or "").strip()
                          else ScriptSectionStatus.DRAFT)

    db.flush()
    if touch_script:
        sync_script(db, project, actor=actor)
    return section


def delete_section(db: Session, project: Project, section_id: str,
                   *, actor: str = "agent") -> bool:
    section = db.get(ScriptSection, section_id)
    if section is None or section.project_id != project.id:
        return False
    db.delete(section)
    db.flush()
    _normalize_sequence(db, project.id)
    sync_script(db, project, actor=actor)
    return True


def compose_full_text(sections: list[ScriptSection]) -> tuple[str, str]:
    """把各幕拼成整篇正文与分章大纲。"""
    outline_lines: list[str] = []
    body_lines: list[str] = []
    for idx, section in enumerate(sections, start=1):
        content = (section.content or "").strip()
        outline_lines.append(
            f"{idx}. {section.title} —— {section.summary}"
            f"（{'已写' if content else '待写'}，{len(content)} 字）"
        )
        if content:
            body_lines.append(f"【{section.title}】")
            body_lines.append(content)
            body_lines.append("")
    return "\n".join(body_lines).strip(), "\n".join(outline_lines)


def sync_script(db: Session, project: Project, *, actor: str = "agent") -> Script | None:
    """把分段回填到整篇 Script，保证下游（分镜生成等）读到的是一致的全文。

    两条保护，避免「规划动作」把已有脚本清掉：
    - 没有任何分段时不动 Script（分段可能是被全部删掉了）
    - 有分段但**一幕正文都还没写**时也不动正文（典型场景：刚手动加了一幕空壳）
    """
    sections = list_sections(db, project.id)
    if not sections:
        return latest_script(db, project.id)

    written = [s for s in sections if (s.content or "").strip()]
    script = latest_script(db, project.id)

    if not written:
        if script is not None:
            script.parameters = {
                **(script.parameters or {}),
                "section_count": len(sections), "written_count": 0, "sectioned": True,
            }
            db.flush()
        return script

    content, outline = compose_full_text(sections)
    if script is None:
        script = Script(
            project_id=project.id, title=project.name,
            style=project.style, language=project.language,
            provider="agent", status="DRAFT",
        )
        db.add(script)
        db.flush()

    script.content = content
    script.outline = outline
    script.parameters = {
        **(script.parameters or {}),
        "section_count": len(sections),
        "written_count": len(written),
        "sectioned": True,
    }
    script.status = "READY" if len(written) == len(sections) else "DRAFT"
    db.flush()
    return script


def _target_chars(section: ScriptSection, project: Project,
                  sections: list[ScriptSection]) -> int:
    """这一幕大约该写多少字：优先用幕自己的时长，否则按全片时长均分。"""
    duration = section.target_duration
    if not duration:
        total = float(project.target_duration or 0) or 300.0
        duration = total / max(1, len(sections))
    return max(120, int(round(duration * CHARS_PER_SECOND)))


def build_generation_prompt(db: Session, project: Project, section: ScriptSection) -> dict[str, Any]:
    """为「本次要写的这一幕」打包一段可直接交给 Agent 的提示词。

    这是整套「分段 + 连续」机制的关键：提示词里带上了
    - 项目需求 / 风格 / 目标时长
    - 全片分幕安排与进度（哪些已定稿）
    - 前面若干幕的**完整正文**（更早的只留摘要）
    - 上一幕的结尾片段（承接语气）
    - 上传的参考素材（文本带节选）
    - 本幕的要点、情绪节拍、目标字数

    因此 Agent 拿到它就能续写，且不会与前文冲突。
    """
    sections = list_sections(db, project.id)
    index = next((i for i, s in enumerate(sections) if s.id == section.id), -1)
    if index < 0:
        raise ValueError("该分段不属于此项目")

    total_duration = float(project.target_duration or 0) or 300.0
    written = [s for s in sections[:index] if (s.content or "").strip()]

    lines: list[str] = [
        "你是资深中文微电影/短剧编剧。请**只**撰写下面指定这一幕的正文，",
        "不要写其它幕的内容，也不要输出标题、分幕标记或创作说明。",
        "",
        "【项目】",
        f"名称：{project.name}",
        f"原始需求：{project.requirement or project.description or project.name}",
        f"视觉风格：{project.style or '未指定'}",
        f"语言：{project.language or 'zh-CN'}",
        f"全片目标时长：{total_duration:.0f} 秒",
        "",
        "【全片分幕安排与进度】",
    ]
    for idx, s in enumerate(sections, start=1):
        state = "已定稿" if (s.content or "").strip() else "待写"
        mark = " ← 本次要写" if s.id == section.id else ""
        lines.append(
            f"{idx}. {s.title}（{state}，约 {int(s.target_duration or total_duration / len(sections))}s）"
            f"：{s.summary or '（无要点）'}{mark}"
        )

    if written:
        full = written[-CONTEXT_FULL_SECTIONS:]
        lines += ["", "【前文正文（已定稿，必须与之自然衔接）】"]
        for s in full:
            lines.append(f"--- {s.title} ---")
            lines.append((s.content or "").strip())
            lines.append("")
        earlier = written[:-CONTEXT_FULL_SECTIONS]
        if earlier:
            lines += ["【更早情节（摘要）】"]
            for s in earlier:
                tail = (s.content or "").strip().replace("\n", " ")
                lines.append(f"- {s.title}：{(s.summary or tail[:120])}")

        tail_text = (written[-1].content or "").strip()
        if tail_text:
            lines += ["", "【上一幕结尾（用于承接语气与情节）】",
                      f"…{tail_text[-TAIL_CHARS:]}"]
    else:
        if index == 0:
            lines += ["", "【说明】这是第一幕，没有前文，请把人物、场景和基调立起来。"]
        else:
            lines += ["", f"【说明】前面第 1~{index} 幕尚未写作，本次是首次落笔："
                          "请自行把人物、场景与基调立住，不要凭空承接尚未写出的剧情。"]

    refs = references_svc.list_references(db, project.id)
    digest = references_svc.asset_digest(refs)
    if digest:
        lines += ["", digest]

    chars = _target_chars(section, project, sections)
    lines += [
        "",
        "【本次要写的这一幕】",
        f"幕序：第 {index + 1} 幕 / 共 {len(sections)} 幕",
        f"标题：{section.title}",
        f"要点：{section.summary or '（未提供，请依据全片安排自行推进剧情）'}",
        f"情绪节拍：{section.beat or '（未指定）'}",
        f"目标时长：{section.target_duration or total_duration / len(sections):.0f} 秒",
        "",
        "【输出要求】",
        f"- 写成可直接朗读的旁白/对白，约 {chars} 字（中文旁白约 {CHARS_PER_SECOND} 字/秒）",
        "- 只输出正文本身，不要写「第几幕」「标题」等标记",
        "- 不要复述前文已经交代过的情节，直接向前推进",
        "- 台词要口语、可表演，避免书面语与旁白腔",
    ]

    return {
        "project_id": project.id,
        "section_id": section.id,
        "sequence": section.sequence,
        "title": section.title,
        "target_chars": chars,
        "prompt": "\n".join(lines),
        "context_sections": [s.id for s in written[-CONTEXT_FULL_SECTIONS:]],
        "reference_count": len(refs),
        "hint": (
            "把上面的提示词交给 Agent 生成正文后，调用 upsert_script_section 写回"
            f"（project_id={project.id}, section_id={section.id}）。"
        ),
    }


def build_plan_prompt(db: Session, project: Project) -> dict[str, Any]:
    """打包「请帮我规划分幕结构」的提示词（只切结构，不写正文）。

    工作台在没有分段时给出这个，用户复制给 Agent，Agent 规划后调用
    ``plan_script_sections`` 写回，分段列表就出现了。
    """
    total = float(project.target_duration or 0) or 300.0
    script = latest_script(db, project.id)
    refs = references_svc.list_references(db, project.id)

    lines: list[str] = [
        "你是资深中文微电影/短剧编剧。请把下面这个项目按幕切分，**只给结构，不要写正文**。",
        "",
        "【项目】",
        f"名称：{project.name}",
        f"原始需求：{project.requirement or project.description or project.name}",
        f"视觉风格：{project.style or '未指定'}",
        f"全片目标时长：{total:.0f} 秒",
    ]
    if script and (script.outline or "").strip() and not list_sections(db, project.id):
        # 只在「还没有分段」时把整篇大纲当输入；已分段时 Script.outline 本身就是
        # 分段产物，再拿它当参考会自我循环。
        lines += ["", "【现有大纲（请在此基础上细化，不要推翻重来）】", script.outline.strip()]
    digest = references_svc.asset_digest(refs)
    if digest:
        lines += ["", digest]
    if list_sections(db, project.id):
        current = "\n".join(f"{i}. {s.title}：{s.summary}" for i, s in
                            enumerate(list_sections(db, project.id), start=1))
        lines += ["", "【当前分段（可能不满意，允许重排）】", current]

    lines += [
        "",
        "【规划要求】",
        f"- 切 3~6 幕，每幕约 {total / 5:.0f} 秒（不要每幕都平均，按剧情需要分配）",
        "- 每幕给出四个字段：",
        "  · title —— 幕标题，形如「第三幕 · 相遇」",
        "  · summary —— 这一幕要讲什么（剧情推进 / 信息点），1~2 句",
        "  · beat —— 情绪节拍或关键转折",
        "  · target_duration —— 这一幕时长（秒）",
        "- 幕与幕之间要有推进关系，不要重复交代同一件事",
        "- 只输出结构，不要写任何正文",
        "",
        "【写回方式】规划好后调用 Skill：",
        "  plan_script_sections",
        f'  {{"project_id": "{project.id}", "sections": [{{"title": "...", "summary": "...", '
        '"beat": "...", "target_duration": 60}, ...]}}',
    ]

    return {
        "project_id": project.id,
        "section_count": len(list_sections(db, project.id)),
        "prompt": "\n".join(lines),
        "hint": "Agent 规划完成后调用 plan_script_sections 写回，工作台会自动出现分幕列表。",
    }
