# -*- coding: utf-8 -*-
"""插件 SDK — 插件层唯一允许依赖的主程序共享面（静态型）。

分层（详见 docs/plugins/capabilities.md）：

- **静态型**（本文件）：常量 / 枚举 / dataclass / 纯函数 / 可继承 QWidget 基类。
  无宿主运行时依赖，import 期即可使用。
- **能力型**（``ctx["capabilities"]``）：需要宿主运行时状态、有副作用的动作。
- **注册型**（``register_*`` 扩展点）：插件加载期向宿主登记。

收录判定（三条全中才收）：
1. 无宿主运行时依赖（不需要窗口 / session / 全局配置实例）
2. 是常量、枚举、dataclass、纯函数，或可直接继承的 QWidget 基类
3. 至少有一个插件实际在用

**稳定性分级**
--------------
收录符号默认**稳定**：主程序保证它存在于本模块、签名与语义不变；要改就得
先改这里并在 CHANGELOG 记破坏性变更。插件只应依赖 ``app.plugins.sdk`` 与
``ctx["capabilities"]``，不 import 主程序内部模块——那样主程序一重构插件就炸，
而 sdk 是显式契约面，破坏前有地方拦。

未列入 ``_STABLE`` 的符号标为 **provisional（临时）**：签名可能随主程序内部
实现调整，插件可用但别写进长期逻辑；主程序侧要收敛它们。

自检：

```python
from app.plugins.sdk import sdk_stability
print(sdk_stability("ToolResult"))   # 'stable'
```

**按需加载**：本模块用 PEP 562 模块级 ``__getattr__`` 延迟取符号。只用到
纯函数（如 ``estimate_tokens``）的插件不会被迫拉起 Qt 栈——实测
``import app.plugins.sdk`` 仅 +1 个模块，取纯值符号仍不导入 QtWidgets。

非法 import（由 tools/plugin_import_guard.py 检查）：
- 只允许 app.plugins.contracts / app.plugins.registries /
  app.plugins.managers.plugin_config_store / app.plugins.sdk
"""

from __future__ import annotations

import importlib
from typing import Any

# 符号 → (来源模块, 模块内符号名)
_SYMBOLS: dict[str, tuple[str, str]] = {
    # ── 常量 / 纯函数（无 Qt） ──
    "PARAM_SCHEMA": ("app.constants", "PARAM_SCHEMA"),
    "provider_quota_exclude_keys": ("app.constants", "provider_quota_exclude_keys"),
    "LINE_ESTIMATE_STEP": ("app.core.tools.tool_arg_lines", "LINE_ESTIMATE_STEP"),
    "build_progress_payload": ("app.core.tools.tool_arg_lines", "build_progress_payload"),
    "extract_partial_path": ("app.core.tools.tool_arg_lines", "extract_partial_path"),
    "should_emit_progress": ("app.core.tools.tool_arg_lines", "should_emit_progress"),
    "append_text_block": ("app.core.conversation.message_content", "append_text_block"),
    "create_agent_manager": ("app.core.conversation.agent", "create_agent_manager"),
    "get_model_capabilities": ("app.core.modelmeta.model_capabilities", "get_model_capabilities"),
    "normalize_reasoning_effort": (
        "app.core.modelmeta.model_capabilities",
        "normalize_reasoning_effort",
    ),
    "resolve_model_capabilities": (
        "app.core.modelmeta.model_capabilities",
        "resolve_model_capabilities",
    ),
    "get_provider_profile": ("app.core.modelmeta.provider_profile", "get_provider_profile"),
    "detect_provider_family": ("app.core.modelmeta.provider_profile", "detect_provider_family"),
    "resolve_context_limit": ("app.core.modelmeta.model_capabilities", "resolve_context_limit"),
    "resolve_max_output_tokens": ("app.core.modelmeta.model_capabilities", "resolve_max_output_tokens"),
    "estimate_tokens": ("app.core.infra.token_estimator", "estimate_tokens"),
    "count_messages_tokens": ("app.core.infra.token_estimator", "count_messages_tokens"),
    "clear_cache": ("app.core.infra.message_identity", "clear_cache"),
    "build_openai_client": ("app.utils.http_client", "build_openai_client"),
    "format_relative_time": ("app.utils.session_preview", "format_relative_time"),
    "retarget": ("app.utils.motion", "retarget"),
    # ── 设计 token ──
    "Colors": ("app.utils.design_tokens", "Colors"),
    "font_size_css": ("app.utils.design_tokens", "font_size_css"),
    "get_unified_scrollbar_style": ("app.utils.design_tokens", "get_unified_scrollbar_style"),
    "make_card_content_opaque": ("app.utils.design_tokens", "make_card_content_opaque"),
    "card_container_surface_color": ("app.utils.design_tokens", "card_container_surface_color"),
    "scale_font_size": ("app.utils.design_tokens", "scale_font_size"),
    "get_ui_font_size": ("app.utils.design_tokens", "get_ui_font_size"),
    "apply_font_size_to_widget": ("app.utils.design_tokens", "apply_font_size_to_widget"),
    "scale_icon_size": ("app.utils.design_tokens", "scale_icon_size"),
    "Animations": ("app.utils.design_tokens", "Animations"),
    "Shadows": ("app.utils.design_tokens", "Shadows"),
    "BorderRadius": ("app.utils.design_tokens", "BorderRadius"),
    # ── 数据契约 / 纯工具 ──
    "ToolResult": ("app.tools.result", "ToolResult"),
    "GitWorktreeDetector": ("app.utils.git_worktree", "GitWorktreeDetector"),
    "WorktreeInfo": ("app.utils.git_worktree", "WorktreeInfo"),
    "get_project_color": ("app.utils.project_meta", "get_project_color"),
    "extract_project_initials": ("app.utils.project_meta", "extract_project_initials"),
    "get_message_preview": ("app.utils.session_preview", "get_message_preview"),
    "alpha_tint": ("app.utils.project_meta", "_alpha_tint"),
    "bind_theme_qss": ("app.utils.theme_style", "bind_theme_qss"),
    "replay_theme_qss": ("app.utils.theme_style", "replay_theme_qss"),
    # ── 宿主工具函数（导入 utils 会拉 Qt，按需加载） ──
    "get_app_data_dir": ("app.utils.utils", "get_app_data_dir"),
    "get_font_family_css": ("app.utils.utils", "get_font_family_css"),
    "get_icon": ("app.utils.utils", "get_icon"),
    "get_unified_font": ("app.utils.utils", "get_unified_font"),
    # ── 可继承 UI 基类 ──
    "ElidedLabel": ("app.widgets.elided_label", "_ElidedLabel"),
    "SquareAvatar": ("app.widgets.square_avatar", "_SquareAvatar"),
    "EmptyHint": ("app.widgets._workbench_helpers", "_EmptyHint"),
    "SectionHeader": ("app.widgets._workbench_helpers", "_SectionHeader"),
    "SingleInputDialog": ("app.widgets.common_dialogs", "SingleInputDialog"),
    "CustomTabButton": ("app.widgets.custom_title_bar", "CustomTabButton"),
    "style_if_changed": ("app.widgets.hover_style_guard", "style_if_changed"),
    "ConfirmDialog": ("app.widgets.common_dialogs", "ConfirmDialog"),
    "ContainerType": ("app.widgets.cards.card_manager", "ContainerType"),
    # ── 工具注册表访问门面（经能力池，不直连 app.tools.registry） ──
    "tool_registry_api": ("app.plugins.sdk_tool_registry", "tool_registry_api"),
    "RegistryProxy": ("app.plugins.sdk_tool_registry", "RegistryProxy"),
    # ── 纯函数：工具摘要工厂（无依赖） ──
    "make_summarize_from_preview": ("app.tools.registry", "make_summarize_from_preview"),
    # ── 工具危险分级（纯函数 + 常量，无宿主依赖） ──
    "classify_tool_danger": ("app.tools.tool_classifier", "classify_tool_danger"),
    "classify_command": ("app.tools.command_safety", "classify_command"),
    "needs_shell": ("app.tools.command_safety", "needs_shell"),
    "run_safe": ("app.tools.command_safety", "run_safe"),
    "run_with_shell": ("app.tools.command_safety", "run_with_shell"),
    "DANGER_SAFE": ("app.tools.tool_classifier", "DANGER_SAFE"),
    # ── 能力池解析入口（无 ctx 的后台线程 / 工具里取能力） ──
    "caps_for": ("app.plugins.capabilities.resolve", "caps_for"),
    "pool_for": ("app.plugins.capabilities.resolve", "pool_for"),
    "declared_for": ("app.plugins.capabilities.resolve", "declared_for"),
    # ── 渲染辅助（HTML 转义 / 全局字体 / diff 摘要，纯函数无宿主依赖） ──
    "html_escape": ("html", "escape"),
    "global_font_family": ("app.utils.design_tokens", "_get_global_font"),
    "unescape_newlines": ("app.widgets.render_helpers", "_unescape_newlines"),
    "summarize_diff": ("app.widgets.render_helpers", "_summarize_diff"),
    "render_diff_preview": ("app.widgets.render_helpers", "_render_diff_preview"),
    # ── 上下文阈值配置读取（纯函数读配置，不持有 Settings 实例） ──
    "cfg_ratio": ("app.core.context.config", "cfg_ratio"),
    "offload_skip_tools": ("app.core.context.config", "offload_skip_tools"),
    "prune_tool_result": ("app.core.context.tool_prune", "prune_tool_result"),
    "resolve_tool_result_max_len": ("app.core.context.tool_prune", "resolve_tool_result_max_len"),
    "prune_skip_tools": ("app.core.context.config", "prune_skip_tools"),
    "cfg_int": ("app.core.context.config", "cfg_int"),
    # ── Skill 元数据查询（纯函数，不触发 Skill 加载） ──
    "load_skill": ("app.utils.utils", "load_skill"),
    "get_local_skills": ("app.utils.utils", "get_local_skills"),
    "get_skill_by_name": ("app.utils.utils", "get_skill_by_name"),
    # ── 插件贡献注册表（能力池：动态技能源/命令/智能体） ──
    "contribution_registry": (
        "app.plugins.registries.contribution_registry",
        "ContributionRegistry",
    ),
}

__all__ = sorted(_SYMBOLS)

# ── 稳定性分级 ──────────────────────────────────────────────
# 稳定面：主程序承诺不改签名/语义，破坏前必须在 CHANGELOG 记破坏性变更。
# 未列入的符号为 provisional（临时收录，签名可能随内部实现调整）。
_STABLE: frozenset[str] = frozenset(
    {
        # 数据契约（跨进程/跨插件序列化依赖它们）
        "ToolResult",
        "PARAM_SCHEMA",
        "ContainerType",
        # 纯函数：工具参数 / 模型能力 / token 估算
        "LINE_ESTIMATE_STEP",
        "build_progress_payload",
        "extract_partial_path",
        "should_emit_progress",
        "get_model_capabilities",
        "normalize_reasoning_effort",
        "resolve_model_capabilities",
        "get_provider_profile",
        "estimate_tokens",
        "count_messages_tokens",
        "make_summarize_from_preview",
        "classify_tool_danger",
        "DANGER_SAFE",
        # 设计 token（UI 一致性依赖）
        "Colors",
        "font_size_css",
        "get_unified_scrollbar_style",
        "make_card_content_opaque",
        "card_container_surface_color",
        "scale_font_size",
        "scale_icon_size",
        "Animations",
        "Shadows",
        "BorderRadius",
        # 路径 / 时间 / 项目元数据
        "get_app_data_dir",
        "get_project_color",
        "alpha_tint",
        "format_relative_time",
        "bind_theme_qss",
        "retarget",
        # 工具注册表门面（能力池投影，签名由 tool.read 契约定）
        "tool_registry_api",
        "RegistryProxy",
        # 能力池解析入口
        "caps_for",
        "pool_for",
        "declared_for",
        # 渲染辅助
        "html_escape",
        "global_font_family",
        "unescape_newlines",
        "summarize_diff",
        "render_diff_preview",
        # 上下文阈值 / Skill 查询
        "cfg_ratio",
        "cfg_int",
        "offload_skip_tools",
        "prune_skip_tools",
        "prune_tool_result",
        "resolve_tool_result_max_len",
        "load_skill",
        "get_local_skills",
        "get_skill_by_name",
        # 模型能力补充面
        "detect_provider_family",
        "resolve_context_limit",
        "resolve_max_output_tokens",
        # 智能体工厂（单例，签名稳定）
        "create_agent_manager",
        # 命令安全（纯函数）
        "classify_command",
        "needs_shell",
        "run_safe",
        "run_with_shell",
        # 字体 / 主题 / worktree / 消息预览
        "get_ui_font_size",
        "apply_font_size_to_widget",
        "replay_theme_qss",
        "GitWorktreeDetector",
        "WorktreeInfo",
        "get_message_preview",
        "extract_project_initials",
        "append_text_block",
        "build_openai_client",
        "clear_cache",
        "provider_quota_exclude_keys",
    }
)


def sdk_stability(name: str) -> str:
    """符号的稳定性等级：``stable`` / ``provisional`` / ``unknown``。"""
    if name not in _SYMBOLS:
        return "unknown"
    return "stable" if name in _STABLE else "provisional"


def __getattr__(name: str) -> Any:
    """PEP 562：首次访问时导入并缓存（后续走 globals() 直接命中）。"""
    entry = _SYMBOLS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol = entry
    value = getattr(importlib.import_module(module_name), symbol)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals()) + __all__)
