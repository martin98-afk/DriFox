# -*- coding: utf-8 -*-
"""autoloop UI 组件入口 — 注册配置卡 / 运行卡 / 输入区按钮"""

import sys
from pathlib import Path

from loguru import logger

# 插件根加入 sys.path（自包含：core/worker 以 autoloop_core.* 导入，
# 与 gateway SDK 的 deps 模式一致）
_PLUGIN_ROOT = Path(__file__).parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))


def register_ui(registry):
    """注册 autoloop 的 UI 组件

    热重载兼容：清理 sys.modules 残留缓存，确保重新编译。
    """
    prefix = "ui_plugin_autoloop."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]
    # autoloop_core 包缓存同样清理（热重载后 worker/engine 需重新加载）
    stale_core = [k for k in sys.modules if k == "autoloop_core" or k.startswith("autoloop_core.")]
    for k in stale_core:
        del sys.modules[k]

    from .cards import AutoLoopConfigCard, AutoLoopRunningCard

    # 配置卡（full 覆盖对话区，hide_sidebar：不进侧边栏，仅经输入按钮/命令弹出）
    registry.register_floating_card(
        plugin_name="autoloop",
        card_id="config",
        widget_class=AutoLoopConfigCard,
        container="full",
        title="AutoLoop 配置",
        default_visible=False,
        metadata={"hide_sidebar": True},
    )
    # 运行卡（full 覆盖，hide_sidebar：仅由控制器在启动时弹出）
    registry.register_floating_card(
        plugin_name="autoloop",
        card_id="running",
        widget_class=AutoLoopRunningCard,
        container="full",
        title="AutoLoop 运行",
        default_visible=False,
        metadata={"hide_sidebar": True},
    )
    # 输入区按钮（替代原工具栏 auto_loop_btn；图标复用插件 manifest 无限.svg）
    _icon = _PLUGIN_ROOT / "icons" / "无限.svg"
    registry.register_input_button(
        plugin_name="autoloop",
        button_id="autoloop",
        icon_path=str(_icon) if _icon.exists() else "",
        tooltip="AutoLoop 自动循环（规划→执行→归档）",
        on_click=_on_input_button_clicked,
    )
    logger.info("[autoloop] UI components registered")


def _on_input_button_clicked(context):
    """输入区按钮点击 — 切换配置卡显示"""
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    UIPluginRegistry.get_instance().toggle_floating_card("config", main_widget=context.get("main_widget"))
