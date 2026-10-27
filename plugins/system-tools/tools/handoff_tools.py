# -*- coding: utf-8 -*-
"""
系统工具插件 — 会话交接文档（handoff）

上下文接近上限时，主智能体在关键节点把当前工作状态写成一份交接文档，
落盘到 `<project_root>/.drifox/handoff/<session_id>.md`（无工作目录时回退
`<app_data_dir>/handoff/`）。下一个新会话由 SessionStart hook 读取并注入，
实现「换会话不断档」。

frontmatter 由本工具生成与维护，只存四项元数据：
- session_id / project_root / created_at / updated_at / injected_into
`injected_into` 记录已注入过的新会话 ID（保留最近 5 个），避免布尔 consumed
在「新会话未落库被丢弃」时把交接误标为已消费。

只依赖 tool_ctx（workdir / session_id / env.app_data_dir），不 import 主程序模块。
"""

import json
import time
from pathlib import Path
from typing import Dict, Optional

from app.plugins.sdk import ToolResult

GROUP_CONTEXT = "上下文与记忆"

_FRONT_KEYS = ("session_id", "project_root", "created_at", "updated_at", "injected_into")
_MAX_INJECTED = 5

_UPDATE_HANDOFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_handoff",
        "description": (
            "更新当前会话的交接文档，供下一个新会话无缝接手。"
            "在关键节点调用：功能落地、方案定案、遇到阻塞、准备切换任务前、上下文接近上限前。"
            "文档必须包含六节：## 任务目标 / ## 已完成 / ## 关键上下文 / ## 未决问题 / "
            "## 剩余任务与下一步 / ## 用户偏好与约束。"
            "「已完成」每条要带文件路径或函数名；「剩余任务」要写成下一任可直接执行的步骤。"
            "只写交接文档本身，不要复述对话过程、不要写元描述。全文不超过 1500 字。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "交接文档正文（Markdown，六节结构，不含 frontmatter）",
                },
                "switch": {
                    "type": "boolean",
                    "description": (
                        "写完这份交接后是否立即开一个新会话继续。"
                        "任务到了可交接的边界（一段工作收尾、要换阶段）时设为 true；"
                        "只是想暂存进度、本轮还会继续干活时不要传。"
                    ),
                },
            },
            "required": ["content"],
        },
    },
}


def _handoff_dir(tool_ctx: Dict) -> Path:
    """交接目录：有工作目录放项目内，否则回退应用数据目录"""
    workdir = str((tool_ctx or {}).get("workdir") or "").strip()
    if workdir:
        return Path(workdir) / ".drifox" / "handoff"
    env_dir = ((tool_ctx or {}).get("env") or {}).get("app_data_dir")
    base = Path(env_dir) if env_dir else Path.home() / ".drifox"
    return base / "handoff"


def _parse_front(text: str):
    """解析 frontmatter（值统一按 JSON 解析，失败则保留原字符串）"""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: Dict = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        try:
            meta[key] = json.loads(raw)
        except Exception:
            meta[key] = raw
    return meta, text[end + 5 :]


def _dump_front(meta: Dict) -> str:
    """序列化 frontmatter（值统一 JSON 编码，避免路径含冒号时解析歧义）"""
    lines = ["---"]
    for key in _FRONT_KEYS:
        if key not in meta:
            continue
        lines.append(f"{key}: {json.dumps(meta[key], ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _update_handoff_impl(content: str = "", switch: bool = False, tool_ctx: Optional[Dict] = None) -> ToolResult:
    """写入/覆盖当前会话的交接文档

    Args:
        content: 交接文档正文（Markdown，六节结构）
        switch: 写完是否请求切新会话（经 ToolResult.data 回传主程序）
        tool_ctx: 工具执行上下文（workdir / session_id / env）

    Returns:
        ToolResult，content 为落盘路径与字数
    """
    text = (content or "").strip()
    if not text:
        return ToolResult(False, error="交接内容为空：content 必须是非空 Markdown 正文")

    ctx = tool_ctx or {}
    session_id = str(ctx.get("session_id") or "").strip()
    if not session_id:
        return ToolResult(False, error="无法确定当前会话 ID，交接文档未写入")

    path = _handoff_dir(ctx) / f"{session_id}.md"

    old_meta: Dict = {}
    if path.exists():
        try:
            old_meta, _ = _parse_front(path.read_text(encoding="utf-8"))
        except Exception:
            old_meta = {}

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    injected = old_meta.get("injected_into")
    if not isinstance(injected, list):
        injected = []
    meta = {
        "session_id": session_id,
        "project_root": str(ctx.get("workdir") or ""),
        "created_at": old_meta.get("created_at") or now,
        "updated_at": now,
        "injected_into": injected[-_MAX_INJECTED:],
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_dump_front(meta))
            f.write("\n")
            f.write(text)
            f.write("\n")
    except Exception as e:
        return ToolResult(False, error=f"交接文档写入失败: {e}")

    return ToolResult(
        True,
        content=f"交接文档已更新：{path}（{len(text)} 字）"
        + ("；本轮结束后将开新会话继续" if switch else ""),
        data={"handoff_switch": True, "handoff_path": str(path)} if switch else None,
    )


def register(registry):
    registry.register(
        "update_handoff",
        _UPDATE_HANDOFF_SCHEMA,
        impl=_update_handoff_impl,
        danger="safe",
        icon="编辑",
        cn_name="更新交接",
        group=GROUP_CONTEXT,
        description="更新会话交接文档，供新会话无缝接手",
        aliases=["UpdateHandoff", "updateHandoff", "handoff"],
        metadata={"no_offload": True, "no_prune": True},
    )
