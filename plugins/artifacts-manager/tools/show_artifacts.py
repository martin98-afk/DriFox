# -*- coding: utf-8 -*-
"""artifacts-manager — 成果展示工具（show_artifacts）

把已存在的文件放入当前助手消息卡片的成果栏。只做路径校验与条目拼装，
UI 与落库全走宿主通用链路：``ToolResult.artifacts`` → chat_worker 落库 tool
消息 → 宿主投递到本插件的成果栏面板 → 重开会话由 history_loader 重建。

不读文件内容、不复制、不移动、不生成文件。
"""
import os
from pathlib import Path
from typing import Optional

from app.plugins.sdk import ToolResult

GROUP_READ = "文件读取"
MAX_PATHS = 8

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "show_artifacts",
        "description": (
            "把已存在的文件放入当前消息卡片的成果栏，供用户预览/打开/拖拽导出。"
            "用于交付成品：图表、报告、导出文件、截图。"
            "相对路径按会话工作目录解析，工作目录外的文件必须给绝对路径。"
            "不读内容、不生成、不复制、不移动文件。一次最多 8 个。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "文件路径列表（1~8 个）",
                },
            },
            "required": ["paths"],
        },
    },
}


def _resolve(workdir: Optional[Path], path: str) -> Path:
    """绝对路径直用；环境变量/~ 展开；相对路径基于会话工作目录"""
    p = Path(os.path.expandvars(path)).expanduser()
    if p.is_absolute():
        return p
    return (workdir or Path.cwd()) / p


def _show_impl(tool_ctx, **kwargs):
    workdir = Path(tool_ctx["workdir"]) if tool_ctx.get("workdir") else None
    raw = kwargs.get("paths")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ToolResult(False, error="paths 必须是文件路径列表")
    paths = [str(p).strip() for p in raw if str(p or "").strip()]
    if not paths:
        return ToolResult(False, error="paths 为空：至少给一个文件路径")
    truncated = len(paths) > MAX_PATHS
    paths = paths[:MAX_PATHS]

    items = []
    skipped = []
    for p in paths:
        try:
            full = _resolve(workdir, p)
        except (OSError, ValueError):
            skipped.append(p)
            continue
        if not full.is_file():
            skipped.append(p)
            continue
        try:
            size = full.stat().st_size
        except OSError:
            size = None
        item = {"path": str(full)}
        if size:
            item["size"] = size
        items.append(item)

    if not items:
        return ToolResult(False, error=f"没有可展示的文件（不存在或不是普通文件）：{', '.join(skipped)}")

    lines = [f"已放入成果栏（{len(items)} 项）："]
    lines.extend(f"- {it['path']}" for it in items)
    if skipped:
        lines.append(f"跳过 {len(skipped)} 项（不存在或不是文件）：{', '.join(skipped)}")
    if truncated:
        lines.append(f"超出上限，仅展示前 {MAX_PATHS} 项")
    return ToolResult(True, content="\n".join(lines), artifacts=items)


def register(registry):
    registry.register(
        "show_artifacts",
        _SCHEMA,
        impl=_show_impl,
        danger="safe",
        icon="folder",
        cn_name="展示成果",
        group=GROUP_READ,
        description="把文件放入成果栏",
        aliases=["present_files", "show_files"],
        metadata={"permission_arg": "paths"},
    )
