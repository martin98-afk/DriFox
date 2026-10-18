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
    "scale_font_size": ("app.utils.design_tokens", "scale_font_size"),
    "scale_icon_size": ("app.utils.design_tokens", "scale_icon_size"),
    "Animations": ("app.utils.design_tokens", "Animations"),
    "Shadows": ("app.utils.design_tokens", "Shadows"),
    "BorderRadius": ("app.utils.design_tokens", "BorderRadius"),
    # ── 数据契约 / 纯工具 ──
    "ToolResult": ("app.tools.result", "ToolResult"),
    "GitWorktreeDetector": ("app.utils.git_worktree", "GitWorktreeDetector"),
    "get_project_color": ("app.utils.project_meta", "get_project_color"),
    "alpha_tint": ("app.utils.project_meta", "_alpha_tint"),
    "bind_theme_qss": ("app.utils.theme_style", "bind_theme_qss"),
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
    "ConfirmDialog": ("app.widgets.common_dialogs", "ConfirmDialog"),
    "ContainerType": ("app.widgets.cards.card_manager", "ContainerType"),
    # ── 工具注册表访问门面（经能力池，不直连 app.tools.registry） ──
    "tool_registry_api": ("app.plugins.sdk_tool_registry", "tool_registry_api"),
    "RegistryProxy": ("app.plugins.sdk_tool_registry", "RegistryProxy"),
    # ── 纯函数：工具摘要工厂（无依赖） ──
    "make_summarize_from_preview": ("app.tools.registry", "make_summarize_from_preview"),
    # ── 工具危险分级（纯函数 + 常量，无宿主依赖） ──
    "classify_tool_danger": ("app.tools.tool_classifier", "classify_tool_danger"),
    "DANGER_SAFE": ("app.tools.tool_classifier", "DANGER_SAFE"),
}

__all__ = sorted(_SYMBOLS)


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
