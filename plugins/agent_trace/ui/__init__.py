# -*- coding: utf-8 -*-
"""agent_trace UI 入口。

注册两个 UI 组件：

1. **常驻顶部 tab** (`register_titlebar_tab`)
   - tab_id = ``agent_trace``（与 full 卡 card_id 命名空间一致）
   - label = ``轨迹``
   - on_click → ``UIPluginRegistry.toggle_floating_card("agent_trace")``
     （**不能**用 ``card_manager.show_card``：它不创建实例，首次点击会静默失败）

2. **full 容器浮动卡** (`register_floating_card`)
   - card_id = ``agent_trace``
   - container = ``full``
   - default_visible = False
   - 自动注册 ``/agent_trace`` 命令

热重载兼容：清理 ``ui_plugin_agent_trace.*`` 旧子模块。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from loguru import logger

# 卡片 ID：与 register_floating_card / register_titlebar_tab 共用同一命名空间
CARD_ID = "agent_trace"


def _plugin_icons_dir() -> str:
    """返回 ``icons/`` 资源目录的绝对字符串（主程序读取 SVG 用）。"""
    here = Path(__file__).resolve().parent
    return str(here.parent / "icons")


def _resolve_active_main_widgets() -> List[object]:
    """返回所有已注册到 ``UIPluginRegistry._window_main_widgets`` 的 main_widget。

    单窗口场景下只有一个；多窗口场景下返回多个 — 调用方对每个独立 show_card。
    """
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        widgets = list(UIPluginRegistry.get_instance()._window_main_widgets.values())
    except Exception:  # noqa: BLE001 — 兼容尚未初始化的极早启动顺序
        widgets = []
    return [w for w in widgets if w is not None]


def _resolve_global_host():
    """取卡片宿主：Tab 模式为 TabManagerWindow，单窗口模式回退 main_widget。

    与 ``UIPluginRegistry._show_floating_card`` 的宿主解析保持一致 —— 全屏卡片
    在 Tab 模式下挂在 **TabManagerWindow** 上（不是 MainWidget），
    用 MainWidget 的 card_manager 操作会找不到卡片。
    """
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
    except Exception:  # noqa: BLE001
        return None, None, None
    try:
        host = reg._resolve_global_host()
    except Exception:  # noqa: BLE001
        host = None
    if host is None:
        for mw in _resolve_active_main_widgets():
            if getattr(mw, "_card_manager", None) is not None:
                host = mw
                break
    if host is None:
        return None, None, None
    return host, getattr(host, "_card_manager", None), getattr(host, "_window_id", None)


def _is_trace_visible() -> bool:
    """轨迹卡当前是否已可见。"""
    _host, cm, wid = _resolve_global_host()
    if cm is None or not wid:
        return False
    try:
        return bool(cm.is_card_visible(CARD_ID, wid))
    except Exception:  # noqa: BLE001
        return False


def _on_trace_tab_clicked() -> None:
    """标题栏「轨迹」常驻 tab 点击回调 — 显示轨迹 full 卡片。

    注意：必须走 ``UIPluginRegistry.toggle_floating_card``，不能直接用
    ``card_manager.show_card``：后者只显示**已存在**的 widget 实例，而插件卡片的
    实例是懒创建的（``_show_floating_card`` 里 ``card_info.widget_class(...)`` +
    ``container.add_card`` + ``card_manager.register_card``），首次点击时实例还
    不存在 → ``show_card`` 在 ``card_widget is None`` 处静默 return，表现就是
    "点了没反应"。

    Tab 模式宿主是 TabManagerWindow：``toggle_floating_card`` 内部
    ``_resolve_global_host()`` 已处理，故无需自己遍历 main_widget。
    """
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
    except Exception as e:  # noqa: BLE001
        logger.error(f"[agent_trace] 无法获取 UIPluginRegistry: {e}")
        return

    # 已可见 → 不动作（toggle 会把它关掉，而点 tab 语义是"切到该 tab"）
    if _is_trace_visible():
        logger.debug("[agent_trace] 轨迹卡已可见，忽略重复点击")
        return

    try:
        reg.toggle_floating_card(CARD_ID)
        logger.info("[agent_trace] 已切换显示轨迹卡")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[agent_trace] toggle_floating_card 失败: {e}")


def _footer_avg_throughput(ctx) -> dict | None:
    """页脚信息项回调：流式期间显示当前流实时吞吐量，回合落定后显示会话平均。

    平均口径对齐 detail_panel 统计页：总时长 = elapsed_ms（缺实时回填时反推），
    首 token 延迟 = ttft_ms（chat_worker 落盘），生成 = 总时长 − 首 token，
    吞吐量 = 输出 token ÷ 生成秒；轮次级 Σ/Σ 聚合即加权平均。
    流式期间（ctx["streaming"]）直接用 live 实时值（live_tokens ÷ live_gen_s），
    不并入平均。
    """
    try:
        from .trace_collector import TraceCollectorHub
        from .trace_models import EntryKind

        # 流式跟随：live 数据全在卡片侧 context，不依赖 collector ——
        # 放在 collector_for 之前，collector 建不出来也不影响实时显示
        live_tokens = int(ctx.get("live_tokens") or 0)
        live_gen_s = float(ctx.get("live_gen_s") or 0.0)
        if bool(ctx.get("streaming")):
            if live_tokens <= 0 or live_gen_s <= 0.5:
                return None
            tps = live_tokens / live_gen_s
            return {
                "text": f"{_fmt_tps(tps)} tok/s",
                "color": "#2ea043",
                "tooltip": "当前流实时吞吐量（输出字符÷4 估算）",
            }

        mw = ctx.get("main_widget")
        if mw is None:
            return None
        hub = _footer_hub(TraceCollectorHub)
        collector = hub.collector_for(mw)
        if collector is None:
            return None

        # 回合落定 / 历史加载：会话平均吞吐量（Σ输出 token ÷ Σ生成秒）
        total_tokens = 0
        total_gen_ms = 0
        rounds = 0
        for rec in collector.records:
            if rec.kind != EntryKind.ASSISTANT or rec.is_pending:
                continue
            tokens = rec.tokens
            if tokens <= 0:
                continue
            total_ms = rec.duration_ms if rec.duration_ms > 0 else int(rec.meta.get("elapsed_ms") or 0)
            ttft = rec.meta.get("ttft_ms")
            ttft_ms = int(ttft) if isinstance(ttft, (int, float)) and ttft > 0 else 0
            gen_ms = total_ms - ttft_ms
            # 生成段 <200ms 的轮次不参与（首字延迟吃掉几乎全部时长时 tps 虚高）
            if gen_ms <= 200:
                continue
            total_tokens += tokens
            total_gen_ms += gen_ms
            rounds += 1
        if total_gen_ms <= 0 or total_tokens <= 0:
            # collector 尚未投影到本轮（backend 信号顺序不定，落定刷新可能先于
            # 投影完成）→ 用落定 context 兜底算本轮单值，投影就绪后下次刷新
            # 自然切换为会话平均。
            tu = ctx.get("token_usage") or {}
            out = tu.get("output") or 0
            el = ctx.get("elapsed")
            ttft_ms = tu.get("ttft_ms") or 0
            if isinstance(out, (int, float)) and out > 0 and isinstance(el, (int, float)) and el > 0:
                gen_s = float(el) - float(ttft_ms) / 1000.0
                if gen_s > 0.2:
                    return {
                        "text": f"{_fmt_tps(out / gen_s)} tok/s",
                        "color": "#2ea043",
                        "tooltip": "本轮吞吐量（会话统计就绪前的单轮值）",
                    }
            return None
        tps = total_tokens / (total_gen_ms / 1000.0)
        return {
            "text": f"{_fmt_tps(tps)} tok/s",
            "color": "#2ea043",
            "tooltip": f"本会话 {rounds} 轮平均吞吐量（Σ输出 token ÷ Σ生成秒）",
        }
    except Exception as e:  # noqa: BLE001 — 页脚回调异常不能影响消息渲染
        logger.debug(f"[agent_trace] footer 吞吐量计算失败: {e}")
        return None


def _fmt_tps(tps: float) -> str:
    """吞吐量文本格式化：≥1000 显示 K 位，其余取整。"""
    return f"{tps / 1000:.1f}K" if tps >= 1000 else str(round(tps))


# 页脚平均吞吐量专用 hub（与轨迹卡实例解耦的模块级单例；
# 轨迹卡未打开也能对任意窗口惰性建 collector 并重投影落盘消息）
_FOOTER_HUB = None


def _footer_hub(hub_cls):
    global _FOOTER_HUB
    if _FOOTER_HUB is None:
        from PyQt5.QtCore import QCoreApplication

        _FOOTER_HUB = hub_cls(QCoreApplication.instance())
    return _FOOTER_HUB


def register_ui(registry) -> None:
    """注册 agent_trace 的 UI 组件。"""
    # 热重载兼容：清理旧子模块缓存（避免 Python 用旧 sys.modules 引用）
    prefix = "ui_plugin_agent_trace."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    # 延迟 import — card widget 依赖 qfluentwidgets / PyQt5，注册期不必即时加载
    from .trace_card import TraceCardWidget

    icons_dir = _plugin_icons_dir()
    icon_dark = str(Path(icons_dir) / "icon.svg")
    icon_light = str(Path(icons_dir) / "icon_light.svg")

    # ── full 容器浮动卡（标题栏「轨迹」tab 触发显示，× 关闭回到对话区）──
    registry.register_floating_card(
        plugin_name="agent_trace",
        card_id=CARD_ID,
        widget_class=TraceCardWidget,
        container="full",
        title="轨迹",
        default_visible=False,
        metadata={
            "icon_dark": icon_dark,
            "icon_light": icon_light,
            # 全窗口卡（与已有设置卡同款：可被标题栏 full tab 接管；
            # 「常驻 tab」自身由 register_titlebar_tab 注册）。
            "full_card": True,
            # 不进 Tab 侧边栏插件列表：入口只有标题栏「轨迹」常驻 tab，
            # 侧边栏再列一份是冗余（tab_panel.py 按此键过滤）。
            "hide_sidebar": True,
        },
    )

    # ── 常驻标题栏 tab（「轨迹」，无 ×，点开即触发 on_click）──
    # 不传 icon_path：CustomTabButton 用 QIcon(path).pixmap 渲染单一路径，
    # **无 icon_light_path 主题感知**（不像 register_input_button），深色主题下
    # 深色描边图标会不可见。纯文字更安全。
    registry.register_titlebar_tab(
        plugin_name="agent_trace",
        tab_id=CARD_ID,
        label="轨迹",
        on_click=_on_trace_tab_clicked,
        priority=0,
    )

    # ── 消息卡片页脚信息项：会话平均吞吐量（流式期间跟随刷新）──
    registry.register_footer_stat(
        plugin_name="agent_trace",
        stat_id="agent_trace:avg_tps",
        provider=_footer_avg_throughput,
        priority=0,
        metadata={"label": "会话平均吞吐量"},
    )

    logger.info(
        "[agent_trace] UI 组件已注册：titlebar_tab(agent_trace) + floating_card(agent_trace/full)"
        " + footer_stat(avg_tps)"
    )
