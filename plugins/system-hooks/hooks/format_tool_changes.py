# -*- coding: utf-8 -*-
"""
PluginChanged Hook 函数 — 工具增删改后向对话注入变更 + 完整 tool calling schema

context 由 PluginChanged 触发链注入（backend.emit_plugin_changed /
plugin_manager / mcp_tools），含：
    - action: installed/updated/uninstalled/enabled/disabled/...
    - sub_actions: tools_added/tools_removed/tools_updated/...
    - diff: {"tools_added": [], "tools_removed": [], "tools_updated": [],
             "mcp_added": [], "mcp_removed": []}

注入语义（打消模型对可用性的怀疑）：
- 新增/变更工具：注入与 API 请求一致的完整 schema，并明确声明
  「已加入当前工具列表，本轮可直接调用」（工具列表每轮动态构建，
  插件启停后下一轮请求即生效——注入即真实可用）。
- 移除工具：明确声明「已从工具列表移除，请勿再调用」。
- 无工具/MCP 层变化 → 空输出（不注入，避免噪音）。
"""

import json

_ACTION_NAMES = {
    "installed": "插件已安装",
    "updated": "插件已更新",
    "uninstalled": "插件已卸载",
    "enabled": "插件已启用",
    "disabled": "插件已禁用",
}

# 与 API 请求同构的 schema 声明前缀（新增/变更工具）
_AVAILABLE_NOTE = "已加入当前请求的工具列表，本轮起可直接调用，无需再探测其是否存在"
_REMOVED_NOTE = "已从工具列表移除，后续请求不可用，请勿再调用（如需同类能力请说明缺口，不要重试）"


def _get_full_schema(name: str) -> dict:
    """取单个工具的完整 tool calling schema（与 API 请求同构）

    从 ToolRegistry 现查（触发时工具已注册完成）。
    查不到（MCP 工具 mcp__ 前缀不在 registry / 已被覆盖移除）返回 {}。
    """
    try:
        from app.tools.registry import ToolRegistry

        reg = ToolRegistry.get_instance().get(name)
        if reg is None:
            return {}
        return reg.schema or {}
    except Exception:
        return {}


def hook(event: str, context: dict) -> str:
    """格式化工具/MCP 变更注入（完整 schema + 可用性声明）

    Args:
        event: 事件名称（PluginChanged）
        context: 触发上下文（见模块 docstring）

    Returns:
        变更说明；无工具/MCP 层变化时返回空串（不注入）
    """
    diff = context.get("diff") or {}
    tools_added = diff.get("tools_added") or []
    tools_removed = diff.get("tools_removed") or []
    tools_updated = diff.get("tools_updated") or []
    mcp_added = diff.get("mcp_added") or []
    mcp_removed = diff.get("mcp_removed") or []

    # 无工具/MCP 层变化（如仅 ui/theme 组件重载）→ 空输出静默
    if not (tools_added or tools_removed or tools_updated or mcp_added or mcp_removed):
        return ""

    action = context.get("action", "")
    action_name = _ACTION_NAMES.get(action, action or "插件变更")
    target = context.get("plugin_name") or context.get("server_name") or ""
    lines = [f"{action_name}{'：' + target if target else ''}"]

    if tools_added:
        lines.append(f"🟢 新增工具（{_AVAILABLE_NOTE}）：")
        for name in tools_added:
            schema = _get_full_schema(name)
            if schema:
                lines.append(f"  - {name} 完整 schema：")
                lines.append("    " + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
            else:
                # MCP 工具：registry 查不到（schema 由 MCP 动态发现），列名即可用
                lines.append(f"  - {name}（MCP 工具，随 MCP 会话动态提供，可直接调用）")
    if tools_updated:
        lines.append(f"🟡 变更工具（{_AVAILABLE_NOTE}，以下为最新 schema）：")
        for name in tools_updated:
            schema = _get_full_schema(name)
            if schema:
                lines.append(f"  - {name} 最新 schema：")
                lines.append("    " + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
            else:
                lines.append(f"  - {name}（schema 经 MCP 动态提供）")
    if tools_removed:
        lines.append(f"🔴 移除工具（{_REMOVED_NOTE}：{', '.join(tools_removed)}）")
    if mcp_added:
        lines.append("🟢 新增 MCP 服务器（其工具随连接就绪后可用）：" + ", ".join(mcp_added))
    if mcp_removed:
        lines.append("🔴 移除 MCP 服务器（其 mcp__ 前缀工具一并失效，请勿再调用）：" + ", ".join(mcp_removed))

    return "\n".join(lines)
