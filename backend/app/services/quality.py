"""质量检查（Quality Check）引擎。

检查分为三类：
1. 硬性完整性检查（文件可解析、流存在、时长匹配、所有 Shot 完成）
2. 一致性检查（分辨率 / 帧率 / 时长）
3. 需要视觉模型的检查（人物一致性、伪影）—— 没有模型时明确返回 WARN（不假装通过），
   留给 Agent 用视觉能力补充判断。

质检失败会输出可执行的修复建议，供 Agent 走 ANALYZE → REGENERATE 闭环。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.constants import AssetType, TaskStatus
from ..models import Asset, Project, QualityCheck, Shot, Task
from ..providers import local_engine as engine
from . import agent_log


def _new_run_id() -> str:
    return f"qc_{uuid.uuid4().hex[:10]}"


def _record(
    db: Session, *, run_id: str, project_id: str, target_type: str, target_id: str,
    check_key: str, name: str, status: str, message: str = "",
    metric: dict[str, Any] | None = None, score: float = 0.0,
) -> QualityCheck:
    row = QualityCheck(
        project_id=project_id, target_type=target_type, target_id=target_id,
        check_key=check_key, name=name, status=status, message=message,
        metric=metric or {}, score=score, run_id=run_id,
    )
    db.add(row)
    return row


def check_shot(db: Session, shot: Shot, *, run_id: str) -> list[QualityCheck]:
    """单镜头质检：文件完整性 + 时长一致性。"""
    results: list[QualityCheck] = []
    asset = db.get(Asset, shot.video_asset_id) if shot.video_asset_id else None
    if asset is None:
        results.append(_record(
            db, run_id=run_id, project_id=shot.project_id, target_type="shot", target_id=shot.id,
            check_key="shot_video_exists", name=f"Shot {shot.code} 视频存在", status="FAIL",
            message="该镜头还没有视频产物，需要生成或重跑",
        ))
        return results

    info: dict[str, Any] = {}
    error = ""
    try:
        info = engine.ffprobe(asset.file_path)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)

    if error or not info:
        results.append(_record(
            db, run_id=run_id, project_id=shot.project_id, target_type="shot", target_id=shot.id,
            check_key="shot_video_playable", name=f"Shot {shot.code} 可播放", status="FAIL",
            message=f"文件无法解析：{error[:180]}", metric={"asset_id": asset.id},
        ))
        return results

    ok_stream = bool(info.get("has_video")) and info.get("size_bytes", 0) > 1024
    results.append(_record(
        db, run_id=run_id, project_id=shot.project_id, target_type="shot", target_id=shot.id,
        check_key="shot_video_playable", name=f"Shot {shot.code} 可播放", status="PASS" if ok_stream else "FAIL",
        message="" if ok_stream else "视频流缺失或文件异常偏小",
        metric={"size_bytes": info.get("size_bytes"), "has_video": info.get("has_video")},
        score=1.0 if ok_stream else 0.0,
    ))

    drift = abs(float(info.get("duration") or 0) - float(shot.duration or 0))
    duration_ok = drift <= max(0.75, float(shot.duration or 5) * 0.25)
    results.append(_record(
        db, run_id=run_id, project_id=shot.project_id, target_type="shot", target_id=shot.id,
        check_key="shot_duration", name=f"Shot {shot.code} 时长正确",
        status="PASS" if duration_ok else "WARN",
        message="" if duration_ok else f"实际 {info.get('duration'):.2f}s，预期 {shot.duration:.2f}s",
        metric={"actual": info.get("duration"), "expected": shot.duration, "drift": round(drift, 3)},
        score=1.0 if duration_ok else 0.5,
    ))

    # 需要视觉模型的检查：明确标注而非假装通过
    results.append(_record(
        db, run_id=run_id, project_id=shot.project_id, target_type="shot", target_id=shot.id,
        check_key="shot_character_consistency", name=f"Shot {shot.code} 人物一致性",
        status="WARN", message="需要视觉模型（VLM）判定，当前由 Agent 观察流程补充完成",
        metric={"requires": "vision_model", "characters": shot.character_ids or []},
        score=0.0,
    ))
    return results


def run_project_quality_check(
    db: Session, project: Project, *, run_id: str | None = None, commit: bool = True,
) -> dict[str, Any]:
    """项目级质检。返回汇总结果，供 Web UI 展示与 Agent 决策。"""
    run_id = run_id or _new_run_id()
    agent_log.log_event(db, project_id=project.id, event="quality.started", message="开始质量检测")

    shots = list(db.execute(
        select(Shot).where(Shot.project_id == project.id).order_by(Shot.sequence.asc())
    ).scalars())

    # 1) 逐镜头检查
    for shot in shots:
        check_shot(db, shot, run_id=run_id)

    # 2) 项目级检查
    final_asset = db.execute(
        select(Asset).where(Asset.project_id == project.id, Asset.type == AssetType.PROJECT_OUTPUT)
        .order_by(Asset.created_at.desc())
    ).scalars().first()

    info: dict[str, Any] = {}
    probe_error = ""
    if final_asset and final_asset.file_path:
        try:
            info = engine.ffprobe(final_asset.file_path)
        except Exception as exc:  # noqa: BLE001
            probe_error = str(exc)

    # 视频完整
    video_ok = bool(info.get("has_video")) and (info.get("duration") or 0) > 0
    _record(
        db, run_id=run_id, project_id=project.id, target_type="project", target_id=project.id,
        check_key="video_integrity", name="视频完整",
        status="PASS" if video_ok else "FAIL",
        message="" if video_ok else (probe_error or "尚未生成最终成片"),
        metric={"asset_id": final_asset.id if final_asset else None,
                "duration": info.get("duration"), "size_bytes": info.get("size_bytes")},
        score=1.0 if video_ok else 0.0,
    )

    # 音频完整
    audio_ok = bool(info.get("has_audio")) and (info.get("duration") or 0) > 0
    _record(
        db, run_id=run_id, project_id=project.id, target_type="project", target_id=project.id,
        check_key="audio_integrity", name="音频完整",
        status="PASS" if audio_ok else "WARN",
        message="" if audio_ok else "成片没有音轨（配音 / 配乐可能尚未合成）",
        metric={"has_audio": info.get("has_audio"), "sample_rate": info.get("sample_rate")},
        score=1.0 if audio_ok else 0.0,
    )

    # 字幕完整
    sub_count = db.scalar(
        select(func.count(Asset.id)).where(
            Asset.project_id == project.id, Asset.type == AssetType.SUBTITLE
        )
    ) or 0
    _record(
        db, run_id=run_id, project_id=project.id, target_type="project", target_id=project.id,
        check_key="subtitle_integrity", name="字幕完整",
        status="PASS" if sub_count else "WARN",
        message="" if sub_count else "尚未生成字幕资产",
        metric={"subtitle_assets": sub_count}, score=1.0 if sub_count else 0.0,
    )

    # 分辨率
    res_ok = bool(info) and info.get("width") == project.width and info.get("height") == project.height
    _record(
        db, run_id=run_id, project_id=project.id, target_type="project", target_id=project.id,
        check_key="resolution", name="分辨率正确",
        status="PASS" if res_ok else ("WARN" if info else "FAIL"),
        message="" if res_ok else f"实际 {info.get('width')}x{info.get('height')}，预期 {project.width}x{project.height}",
        metric={"actual": [info.get("width"), info.get("height")],
                "expected": [project.width, project.height]},
        score=1.0 if res_ok else 0.0,
    )

    # 帧率
    fps_ok = bool(info) and abs(float(info.get("fps") or 0) - float(project.fps or 24)) <= 0.6
    _record(
        db, run_id=run_id, project_id=project.id, target_type="project", target_id=project.id,
        check_key="frame_rate", name="帧率正确",
        status="PASS" if fps_ok else ("WARN" if info else "FAIL"),
        message="" if fps_ok else f"实际 {info.get('fps')}，预期 {project.fps}",
        metric={"actual": info.get("fps"), "expected": project.fps},
        score=1.0 if fps_ok else 0.0,
    )

    # 所有 Shot 完成
    undone = [s.code for s in shots if not s.video_asset_id]
    _record(
        db, run_id=run_id, project_id=project.id, target_type="project", target_id=project.id,
        check_key="all_shots_done", name="所有 Shot 已完成",
        status="PASS" if shots and not undone else "FAIL",
        message="" if (shots and not undone) else (
            "项目还没有镜头" if not shots else f"未完成镜头：{', '.join(undone[:12])}"
        ),
        metric={"total": len(shots), "pending": len(undone)},
        score=1.0 if (shots and not undone) else 0.0,
    )

    # 没有失败任务
    failed_tasks = list(db.execute(
        select(Task).where(Task.project_id == project.id, Task.status == TaskStatus.FAILED)
    ).scalars())
    _record(
        db, run_id=run_id, project_id=project.id, target_type="project", target_id=project.id,
        check_key="no_failed_tasks", name="没有失败任务",
        status="PASS" if not failed_tasks else "FAIL",
        message="" if not failed_tasks else f"{len(failed_tasks)} 个任务失败：{failed_tasks[0].type}",
        metric={"failed_tasks": [t.id for t in failed_tasks][:20]},
        score=1.0 if not failed_tasks else 0.0,
    )

    # 时长符合预期
    expected_duration = sum(float(s.duration or 0) for s in shots)
    actual_duration = float(info.get("duration") or 0)
    if expected_duration and actual_duration:
        drift = abs(actual_duration - expected_duration) / expected_duration
        dur_ok = drift <= 0.2
        _record(
            db, run_id=run_id, project_id=project.id, target_type="project", target_id=project.id,
            check_key="duration", name="时长符合预期",
            status="PASS" if dur_ok else "WARN",
            message="" if dur_ok else f"成片 {actual_duration:.1f}s，镜头合计 {expected_duration:.1f}s",
            metric={"actual": round(actual_duration, 2), "expected": round(expected_duration, 2),
                    "drift_pct": round(drift * 100, 2)},
            score=1.0 if dur_ok else 0.5,
        )
    else:
        _record(
            db, run_id=run_id, project_id=project.id, target_type="project", target_id=project.id,
            check_key="duration", name="时长符合预期", status="WARN",
            message="缺少成片或镜头时长，无法比对",
            metric={"actual": actual_duration, "expected": round(expected_duration, 2)}, score=0.0,
        )

    db.flush()
    rows = list(db.execute(select(QualityCheck).where(QualityCheck.run_id == run_id)).scalars())
    passed = sum(1 for r in rows if r.status == "PASS")
    failed = sum(1 for r in rows if r.status == "FAIL")
    warned = sum(1 for r in rows if r.status == "WARN")
    total = len(rows)
    score = round(sum(r.score for r in rows) / total * 100, 1) if total else 0.0

    repair_hints: list[str] = []
    if undone:
        repair_hints.append(f"重跑未完成镜头：{', '.join(undone[:8])}")
    if failed_tasks:
        repair_hints.append("重试失败任务（可调用 task.retry）")
    if not video_ok:
        repair_hints.append("执行 compose_video 生成最终成片")
    if not audio_ok:
        repair_hints.append("补齐配音 / 配乐后再合成")

    agent_log.log_event(
        db, project_id=project.id, event="quality.finished",
        message=f"质量检测完成：{passed}/{total} 通过（FAIL {failed} / WARN {warned}）",
        level="ERROR" if failed else ("WARN" if warned else "INFO"),
        detail={"run_id": run_id, "score": score},
    )

    if commit:
        db.commit()

    return {
        "run_id": run_id,
        "project_id": project.id,
        "total": total, "passed": passed, "failed": failed, "warned": warned,
        "score": score,
        "status": "FAIL" if failed else ("WARN" if warned else "PASS"),
        "items": [serialize_check(r) for r in rows],
        "repair_hints": repair_hints,
        "final_asset_id": final_asset.id if final_asset else None,
    }


def serialize_check(row: QualityCheck) -> dict[str, Any]:
    return {
        "id": row.id, "run_id": row.run_id, "check_key": row.check_key, "name": row.name,
        "status": row.status, "message": row.message, "metric": row.metric or {},
        "score": row.score, "target_type": row.target_type, "target_id": row.target_id,
        "created_at": row.created_at,
    }


def latest_run(db: Session, project_id: str) -> dict[str, Any] | None:
    last = db.execute(
        select(QualityCheck).where(QualityCheck.project_id == project_id)
        .order_by(QualityCheck.created_at.desc()).limit(1)
    ).scalars().first()
    if last is None:
        return None
    rows = list(db.execute(
        select(QualityCheck).where(QualityCheck.run_id == last.run_id)
        .order_by(QualityCheck.created_at.asc())
    ).scalars())
    passed = sum(1 for r in rows if r.status == "PASS")
    failed = sum(1 for r in rows if r.status == "FAIL")
    warned = sum(1 for r in rows if r.status == "WARN")
    total = len(rows)
    return {
        "run_id": last.run_id, "total": total, "passed": passed, "failed": failed, "warned": warned,
        "score": round(sum(r.score for r in rows) / total * 100, 1) if total else 0.0,
        "status": "FAIL" if failed else ("WARN" if warned else "PASS"),
        "items": [serialize_check(r) for r in rows],
        "created_at": last.created_at,
    }
