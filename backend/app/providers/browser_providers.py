"""浏览器自动化 Provider。

设计原则（对应需求第十五条）：
- 浏览器自动化不是唯一执行方式，优先级 API > CLI > 浏览器。
- 不要把流程写死在 Backend 里。Backend 只负责：登记 BrowserTask、
  生成步骤骨架、回写状态；真正的点击/上传/等待由 Agent 执行。
"""
from __future__ import annotations

from typing import Any

from .base import BrowserProvider, registry


BROWSER_PLAYBOOK: dict[str, list[dict[str, Any]]] = {
    "generic_video_platform": [
        {"step": "open", "desc": "打开目标平台创作页", "url": ""},
        {"step": "login", "desc": "使用已保存的会话登录；需要验证码时请求人工介入"},
        {"step": "navigate", "desc": "进入视频生成 / 上传入口"},
        {"step": "upload", "desc": "上传关键帧或参考素材（来自 Asset Center）"},
        {"step": "fill_prompt", "desc": "填写 Prompt 与参数（时长/比例/风格）"},
        {"step": "submit", "desc": "点击生成按钮"},
        {"step": "wait", "desc": "等待生成完成（轮询或等待下载按钮出现）"},
        {"step": "download", "desc": "下载结果文件"},
        {"step": "ingest", "desc": "调用 asset.upload 把结果写入 Asset Center"},
    ],
}


class AgentBrowserProvider(BrowserProvider):
    name = "agent_browser"
    display_name = "Agent 浏览器执行器"
    capabilities = ("playwright", "cdp", "screenshot", "download", "login_session")
    doc = (
        "Backend 不下发浏览器动作，只登记任务并给出 playbook；"
        "由 WorkBuddy / Codex 通过浏览器 Skill 执行，完成后回写状态与产物。"
    )

    @property
    def functional(self) -> bool:  # type: ignore[override]
        # 该 Provider 本身不驱动浏览器，因此永远"可用"，但需要 Agent 参与
        return True

    def execute(self, *, task_id: str, instruction: str, steps: list[dict[str, Any]],
                parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "status": "PENDING",
            "executor": "agent",
            "task_id": task_id,
            "instruction": instruction,
            "steps": steps or self.plan(instruction),
            "note": "任务已登记，等待 Agent 通过浏览器执行；执行完成后调用 browser.complete 回写结果。",
        }

    def plan(self, instruction: str, platform: str = "") -> list[dict[str, Any]]:
        base = BROWSER_PLAYBOOK.get(platform) or BROWSER_PLAYBOOK["generic_video_platform"]
        return [dict(item) for item in base]


def register() -> None:
    registry.register(AgentBrowserProvider(), default=True)
