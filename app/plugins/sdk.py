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

非法 import（由 tools/plugin_import_guard.py 检查）：
- 只允许 app.plugins.contracts / app.plugins.registries /
  app.plugins.managers.plugin_config_store / app.plugins.sdk
"""

from app.constants import PARAM_SCHEMA
from app.constants import provider_quota_exclude_keys
from app.core.conversation.message_content import append_text_block
from app.core.infra.message_identity import clear_cache
from app.core.infra.token_estimator import count_messages_tokens, estimate_tokens
from app.core.modelmeta.model_capabilities import (
    get_model_capabilities,
    normalize_reasoning_effort,
    resolve_model_capabilities,
)
from app.core.modelmeta.provider_profile import get_provider_profile
from app.core.tools.tool_arg_lines import (
    LINE_ESTIMATE_STEP,
    build_progress_payload,
    extract_partial_path,
    should_emit_progress,
)
from app.tools.result import ToolResult
from app.utils.design_tokens import (
    Animations,
    BorderRadius,
    Colors,
    Shadows,
    font_size_css,
    get_unified_scrollbar_style,
    make_card_content_opaque,
    scale_font_size,
    scale_icon_size,
)
from app.utils.git_worktree import GitWorktreeDetector
from app.utils.http_client import build_openai_client
from app.utils.motion import retarget
from app.utils.project_meta import _alpha_tint as alpha_tint
from app.utils.project_meta import get_project_color
from app.utils.session_preview import format_relative_time
from app.utils.theme_style import bind_theme_qss
from app.utils.utils import get_app_data_dir, get_font_family_css, get_icon, get_unified_font
from app.widgets._workbench_helpers import _EmptyHint as EmptyHint
from app.widgets._workbench_helpers import _SectionHeader as SectionHeader
from app.widgets.cards.card_manager import ContainerType
from app.widgets.common_dialogs import ConfirmDialog, SingleInputDialog
from app.widgets.elided_label import _ElidedLabel as ElidedLabel
from app.widgets.square_avatar import _SquareAvatar as SquareAvatar

__all__ = [
    "Animations",
    "BorderRadius",
    "Colors",
    "ConfirmDialog",
    "ContainerType",
    "ElidedLabel",
    "EmptyHint",
    "GitWorktreeDetector",
    "LINE_ESTIMATE_STEP",
    "PARAM_SCHEMA",
    "SectionHeader",
    "Shadows",
    "SingleInputDialog",
    "SquareAvatar",
    "ToolResult",
    "alpha_tint",
    "append_text_block",
    "bind_theme_qss",
    "build_openai_client",
    "build_progress_payload",
    "clear_cache",
    "count_messages_tokens",
    "estimate_tokens",
    "extract_partial_path",
    "font_size_css",
    "format_relative_time",
    "get_app_data_dir",
    "get_font_family_css",
    "get_icon",
    "get_model_capabilities",
    "get_project_color",
    "get_provider_profile",
    "get_unified_font",
    "get_unified_scrollbar_style",
    "make_card_content_opaque",
    "normalize_reasoning_effort",
    "provider_quota_exclude_keys",
    "resolve_model_capabilities",
    "retarget",
    "scale_font_size",
    "scale_icon_size",
    "should_emit_progress",
]
