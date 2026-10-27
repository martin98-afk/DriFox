# -*- coding: utf-8 -*-
"""系统插件 — 会话交接工具（update_handoff）

写交接文档到 `<project_root>/.drifox/handoff/<session_id>.md`（无工作目录回退
`<app_data_dir>/handoff/`），`switch=true` 时把「停流 → 新建会话 → 发继续指令」
三步登记到主程序的动作队列——工具跑在 worker 线程，同步停流会杀掉自己所在的
worker，只有入队才是安全时序。

只依赖 tool_ctx（workdir / session_id）与能力池，不 import 主程序内部模块。
"""

import time
from pathlib import Path
from typing import Dict, Optional

from app.plugins.sdk import ToolResult, caps_for

GROUP_CONTEXT = "上下文与记忆"

_FRONT_KEYS = ("session_id", "project_root", "created_at", "updated_at", "injected_into")
_MAX_INJECTED = 5

_CONTINUE_PROMPT = (
    "根据交接文档继续未完成的工作。"
    "从「剩余任务与下一步」的第一项开始执行，不要重做「已完成」里的内容，不要复述交接。"
    "如果交接里没有可执行的任务，直接说明情况并停下等我指示。"
)


def _continue_prompt(source_title: str) -> str:
    """自动继续文案：带上原会话标题，新会话里一眼看出在接哪段活"""
    title = (source_title or "").strip()
    if not title or title == "新对话":
        return _CONTINUE_PROMPT
    return f"继续完成「{title}」会话的交接任务。\n" + _CONTINUE_PROMPT

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
                        "写完是否立即开新会话继续，默认为 true：写完就切，新会话读交接接着干。"
                        "仅当本轮还要继续干活、只想暂存进度时传 false。"
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
    import json

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
    import json

    lines = ["---"]
    for key in _FRONT_KEYS:
        if key not in meta:
            continue
        lines.append(f"{key}: {json.dumps(meta[key], ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _source_session_title() -> str:
    """当前（源）会话的标题，拿不到返回空串"""
    read = caps_for("session.read")
    if read is None:
        return ""
    try:
        return str((read() or {}).get("name") or "")
    except Exception:
        return ""


def _arm_handoff(tool_ctx: Dict, path: Path, body: str) -> None:
    """登记待注入内容 + 入队三连（停流 → 新建 → 发继续指令）"""
    pending = {
        "path": str(path),
        "content": body,
        "source_session_id": str((tool_ctx or {}).get("session_id") or ""),
        "ts": time.time(),
    }
    kv = caps_for("state.kv")
    if kv:
        try:
            kv["set"]("handoff_pending", pending)
            kv["set"]("handoff_active", {"ts": pending["ts"]})
        except Exception:
            pass

    steps = (
        ("session.interrupt", {}),
        ("session.new", {}),
        # 自动继续是用户可见的一条新消息，带上原会话标题方便回溯
        ("session.send", {"text": _continue_prompt(_source_session_title())}),
    )
    for cap_id, kwargs in steps:
        fn = caps_for(cap_id)
        if fn is None:
            return
        try:
            fn(**kwargs)
        except Exception:
            return


def _update_handoff_impl(content: str = "", switch: bool = True, tool_ctx: Optional[Dict] = None) -> ToolResult:
    """写入/覆盖当前会话的交接文档

    Args:
        content: 交接文档正文（Markdown，六节结构）
        switch: 写完是否请求切新会话（登记到动作队列）
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

    if switch:
        _arm_handoff(ctx, path, text)

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
