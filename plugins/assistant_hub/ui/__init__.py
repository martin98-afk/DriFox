# -*- coding: utf-8 -*-
"""assistant_hub UI 入口。

注册组件（参考 agent_trace 同款模式）：

1. **常驻标题栏 tab**（``register_titlebar_tab``）
   - tab_id = ``assistant_hub``
   - label = ``助手``（放在「轨迹」右侧）
   - on_click → ``UIPluginRegistry.toggle_floating_card("assistant_hub")``

2. **full 容器浮动卡**（``register_floating_card``）
   - card_id = ``assistant_hub``
   - container = ``full``
   - widget_class = ``AssistantCardWidget``（左列表 + 右 Tab 编辑器）

3. **Gitee 同步内容注册**（``register_sync_content_provider``）
   - provider_id = ``assistant_hub``
   - 同步整个 <app_data>/assistant_hub/ 目录（助手信息 + 记忆），跨设备同步。

热重载兼容：清理 ``ui_plugin_assistant_hub.*`` 旧子模块。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import List

from loguru import logger

CARD_ID = "assistant_hub"

# ── 共享 manager 模块 ──────────────────────────────────────────
# ui 子模块与 hooks/inject_assistant.py 都通过 importlib 按文件路径加载
# assistant_manager.py（模块名固定 assistant_hub_manager 缓存到 sys.modules）。
# 保证两处拿到同一个 AssistantManager 类 → 单例一致，不会出现 UI 建助手、
# hook 读不到的双实例问题。
_SHARED_MANAGER_MODULE = "assistant_hub_manager"


def _ensure_shared_manager_module() -> None:
    if _SHARED_MANAGER_MODULE in sys.modules:
        return
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        _SHARED_MANAGER_MODULE,
        str(root / "assistant_manager.py"),
    )
    if spec is None or spec.loader is None:
        logger.error("[assistant_hub] 无法创建 shared manager spec")
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SHARED_MANAGER_MODULE] = module
    try:
        spec.loader.exec_module(module)
        logger.debug("[assistant_hub] shared assistant_hub_manager 已加载")
    except Exception as e:
        logger.error(f"[assistant_hub] shared manager 加载失败: {e}")


_ensure_shared_manager_module()


def _plugin_icons_dir() -> str:
    here = Path(__file__).resolve().parent
    return str(here.parent / "icons")


def _resolve_active_main_widgets() -> List[object]:
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        widgets = list(UIPluginRegistry.get_instance()._window_main_widgets.values())
    except Exception:
        widgets = []
    return [w for w in widgets if w is not None]


def _resolve_global_host():
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
    except Exception:
        return None, None, None
    try:
        host = reg._resolve_global_host()
    except Exception:
        host = None
    if host is None:
        for mw in _resolve_active_main_widgets():
            if getattr(mw, "_card_manager", None) is not None:
                host = mw
                break
    if host is None:
        return None, None, None
    return host, getattr(host, "_card_manager", None), getattr(host, "_window_id", None)


def _is_card_visible() -> bool:
    _host, cm, wid = _resolve_global_host()
    if cm is None or not wid:
        return False
    try:
        return bool(cm.is_card_visible(CARD_ID, wid))
    except Exception:
        return False


def _on_tab_clicked() -> None:
    """标题栏「助手」tab 点击 → 显示助手中心 full 卡片。"""
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
    except Exception as e:
        logger.error(f"[assistant_hub] 无法获取 UIPluginRegistry: {e}")
        return

    if _is_card_visible():
        logger.debug("[assistant_hub] 卡片已可见，忽略重复点击")
        return

    try:
        reg.toggle_floating_card(CARD_ID)
        logger.info("[assistant_hub] 已切换显示助手中心卡片")
    except Exception as e:
        logger.error(f"[assistant_hub] toggle_floating_card 失败: {e}")


def _register_sync_provider() -> None:
    """注册 Gitee 同步内容：助手信息 + 记忆跨设备同步。

    与 ConfigSyncService 的 register_sync_content_provider 对接：
    - 本地目录 = AssistantManager.root（<app_data>/assistant_hub）
    - 远端路径 = drifox/ext/assistant_hub.zip
    - 绑定 Gitee 后自动上传/下载；目录变更 watch 到自动上传。
    """
    try:
        from app.core.config_sync import register_sync_content_provider

        from assistant_hub_manager import AssistantManager

        mgr = AssistantManager.get_instance()
        register_sync_content_provider(
            provider_id="assistant_hub",
            label="助手信息与记忆",
            local_dir=str(mgr.root),
            remote_path="drifox/ext/assistant_hub.zip",
            enabled=True,
        )
        logger.info(f"[assistant_hub] 已注册 Gitee 同步内容: {mgr.root}")
    except Exception as e:
        logger.warning(f"[assistant_hub] 注册 Gitee 同步内容失败: {e}")


def _promote_build_system_prompt_hook() -> None:
    """确保 assistant_hub 的 BuildSystemPrompt hook 先于系统 inject_agent_identity 执行。

    系统插件（plugins/system/hooks/hooks.json）里的 builtin_inject_agent_identity
    会读取 context["agent_identity_content"]。assistant_hub 的 hook 需要**先**改
    context 才能实现"替换注入"（否则系统 hook 先输出原智能体提示词，我们的
    修改只变成追加）。注册顺序 = 执行顺序，把我们的 rule 提前到列表头部。
    """
    try:
        from app.core.hook_manager import HookManager

        hm = HookManager.get_instance()
        rules = getattr(hm, "_hooks", {}).get("BuildSystemPrompt", [])
        # 找 assistant_hub 的 rule
        for i, rule in enumerate(rules):
            if getattr(rule, "skill_name", "") == "assistant_hub":
                if i > 0:
                    rule_obj = rules.pop(i)
                    rules.insert(0, rule_obj)
                break
        logger.debug("[assistant_hub] BuildSystemPrompt hook 已提升到最前（先于系统身份注入）")
    except Exception as e:
        logger.warning(f"[assistant_hub] 提升 hook 顺序失败: {e}")


def _mood_sections(content: str) -> List[tuple]:
    """解析 mood 块「键：值」行 → [(key, value)]；无键行 key 为空串。"""
    sections: List[tuple] = []
    for raw in content.strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        if "：" in line:
            key, _, val = line.partition("：")
            sections.append((key.strip(), val.strip()))
        elif ":" in line:
            key, _, val = line.partition(":")
            sections.append((key.strip(), val.strip()))
        else:
            sections.append(("", line))
    return sections


# mood 卡点缀色（玫瑰灰，明暗主题下均可读）
_MOOD_ACCENT = "#c9767e"


def _render_mood_card(content: str, ctx: dict) -> str:
    """<mood> 内心独白卡渲染器（hanako 人格 MOOD 块协议）。

    纯函数：可在后台渲染线程调用，禁止触碰 Qt widget。输出为单格
    <table> 结构 HTML，双端兼容 QLabel 富文本（QTextDocument）与
    QWebEngineView。文字色继承消息卡片主题色，仅点缀用固定暖色。

    ctx: {tag, completed, compact}；流式未闭合（completed=False）渲染
    单行占位，避免逐 chunk 闪大卡。
    """
    import html as _html

    if not bool(ctx.get("completed", True)):
        return (
            '<table width="100%" style="margin:4px 0; border-collapse:collapse;">'
            f'<tr><td style="border-left:3px solid {_MOOD_ACCENT}; padding:4px 10px;">'
            f'<span style="color:{_MOOD_ACCENT}; font-size:12px;">&#9829; mood</span>'
            '<span style="font-size:12px;"> · 内心独白中&#8230;</span>'
            "</td></tr></table>"
        )

    rows: List[str] = []
    for key, val in _mood_sections(content):
        key_esc = _html.escape(key)
        val_esc = _html.escape(val)
        if key:
            rows.append(
                '<tr><td style="padding:1px 10px;">'
                f'<span style="color:{_MOOD_ACCENT}; font-size:12px;">{key_esc}</span>'
                f'<span style="font-size:13px;">&nbsp;&nbsp;{val_esc}</span>'
                "</td></tr>"
            )
        else:
            rows.append(f'<tr><td style="padding:1px 10px;"><span style="font-size:13px;">{val_esc}</span></td></tr>')
    header = (
        f'<tr><td style="border-left:3px solid {_MOOD_ACCENT}; padding:5px 10px 2px;">'
        f'<span style="color:{_MOOD_ACCENT}; font-size:12px; font-weight:bold;">&#9829; MOOD</span>'
        '<span style="font-size:11px;"> · 内心独白</span>'
        "</td></tr>"
    )
    return f'<table width="100%" style="margin:4px 0; border-collapse:collapse;">{header}{"".join(rows)}</table>'


def register_ui(registry) -> None:
    """注册 assistant_hub 的 UI 组件。"""
    # 热重载兼容
    prefix = "ui_plugin_assistant_hub."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    # 延迟 import — 卡片依赖 qfluentwidgets / PyQt5
    from .assistant_card import AssistantCardWidget

    icons_dir = _plugin_icons_dir()
    icon_dark = str(Path(icons_dir) / "icon.svg")
    icon_light = str(Path(icons_dir) / "icon_light.svg")

    # ── full 容器浮动卡 ──
    registry.register_floating_card(
        plugin_name="assistant_hub",
        card_id=CARD_ID,
        widget_class=AssistantCardWidget,
        container="full",
        title="助手中心",
        default_visible=False,
        metadata={
            "icon_dark": icon_dark,
            "icon_light": icon_light,
            "full_card": True,
            "hide_sidebar": True,
        },
    )

    # ── 常驻标题栏 tab（「助手」，位于「轨迹」之后）──
    registry.register_titlebar_tab(
        plugin_name="assistant_hub",
        tab_id=CARD_ID,
        label="助手",
        on_click=_on_tab_clicked,
        priority=10,
    )

    # ── <mood> 内心独白卡（hanako 人格 MOOD 块协议）──
    registry.register_tag_renderer(
        plugin_name="assistant_hub",
        tag_name="mood",
        render_func=_render_mood_card,
        priority=10,
    )

    # ── Gitee 同步内容：助手信息 + 记忆 ──
    _register_sync_provider()

    # ── BuildSystemPrompt hook 顺序提升（先于系统身份注入）──
    _promote_build_system_prompt_hook()

    logger.info("[assistant_hub] UI 组件已注册：titlebar_tab(助手) + floating_card(assistant_hub/full) + gitee sync")
