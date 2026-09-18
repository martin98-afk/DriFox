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
import time
from pathlib import Path
from typing import Dict, List

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
    """页脚信息项回调：流式期间显示本条回复的实时吞吐量，回合落定后显示会话平均。

    口径对齐 detail_panel 统计页：生成秒 = 总时长 − 首 token 延迟；
    吞吐量 = 输出 token ÷ 生成秒。

    - 流式期间：token 用 ``estimate_tokens_text``（与轨迹卡 Tokens 列同源的
      tiktoken/cl100k 估算，中文约 1.2 token/字，不是 chars÷4），时间取宿主给的
      ``live_gen_s``（**累计出字时间**，已排除工具执行 / 长等待空档）→ 当前这条
      流的最近一次采样值；起步 0.3s 内不显示，避免首字抖动。
    - 回合落定 / 历史会话加载：统一从 collector 投影**逐条**聚合，
      Σ每条输出 token ÷ Σ每条生成秒（生成秒 = 该次 LLM 调用耗时 − 其 TTFT）。

      ⚠️ 不能用宿主卡片给的 ``elapsed`` + ``token_usage.output`` 直接相除：
      加载历史会话时 ``_restore_meta_from_batch`` 取的 ``elapsed`` 是**整轮墙钟**
      （含全部工具迭代），``token_usage.output`` 只是**最后一次** API 调用的输出，
      分子取末次、分母取整轮 → 实测偏差达百倍量级（真实样本 603 tok ÷ 1584 s
      = 0.38 tok/s，同期 per-call 口径是 42 tok/s），页脚长期显示 0~2 tok/s。
      per-call 的 ``elapsed_ms`` / ``ttft_ms`` 由 worker 逐条落盘在 assistant 消息上
      （历史会话同样有），collector 投影时已回填进 ``rec.meta``，才是同源数据。
    """
    try:
        from .trace_models import estimate_tokens_text

        if bool(ctx.get("streaming")):
            text = ctx.get("live_text") or ""
            gen_s = float(ctx.get("live_gen_s") or 0.0)
            if not text or gen_s < 0.3:
                return None
            tokens = estimate_tokens_text(text)
            if tokens <= 0:
                return None
            tps = tokens / gen_s
            return {
                "text": f"{_fmt_tps(tps)} tok/s",
                "color": _tps_color(tps),
                "tooltip": "当前这条回复的实时吞吐量（估算 token ÷ 累计出字秒数，已排除工具执行空档）",
            }

        agg = _aggregate_records(ctx)
        if agg is None:
            return None
        total_tokens, total_gen_s, rounds = agg
        tps = total_tokens / total_gen_s
        return {
            "text": f"{_fmt_tps(tps)} tok/s",
            "color": _tps_color(tps),
            "tooltip": f"本会话 {rounds} 次调用平均吞吐量（Σ输出 token ÷ Σ生成秒）",
        }
    except Exception as e:  # noqa: BLE001 — 页脚回调异常不能影响消息渲染
        logger.debug(f"[agent_trace] footer 吞吐量计算失败: {e}")
        return None


# 会话级轮次累加表已移除（2026-09-17）：
# 旧实现把宿主卡片的 ``elapsed``（整轮墙钟）与 ``token_usage.output``（末次调用输出）
# 当同一次调用相加，历史会话加载回填时分子分母跨调用串口径，实测偏差百倍；
# 且真实落定路径的 ``set_meta_info`` 不带 output，累加表只会装脏数据、永不被修正。
# 现统一走 ``_aggregate_records``（collector 逐条 per-call 口径）。

# 会话重投影节流表：(window_id, 目标 session_id) -> 上次尝试时刻。
# 只用于「投影落后于会话切换」这一种情况，防止每张卡片构建都触发全量投影。
_REPROJECT_GUARD: "Dict[tuple, float]" = {}
_REPROJECT_GUARD_WINDOW_S = 5.0


def _aggregate_records(ctx):
    """从 collector 投影聚合 (Σ输出 token, Σ生成秒, 调用次数)。

    逐条 assistant 取**同一次调用**的同源字段：token 用 ``rec.tokens``
    （有真实 usage 取 output，否则文本估算），耗时用 ``rec.meta["elapsed_ms"]``
    （单次 LLM 调用）减 ``rec.meta["ttft_ms"]``。两者都由 worker 逐条落盘在
    assistant 消息上（历史会话同样具备），不存在跨调用串口径。
    """
    from .trace_collector import TraceCollectorHub
    from .trace_models import EntryKind

    mw = ctx.get("main_widget")
    if mw is None:
        return None
    collector = _footer_hub(TraceCollectorHub).collector_for(mw)
    if collector is None:
        return None
    # 投影未跟上会话切换时不聚合：加载历史会话瞬间 collector.records 可能还
    # 是上一个会话的投影，聚合出来就是别的会话的均值。此处**驱动一次重投影**
    # 而不是直接放弃 —— 切会话路径（_load_session_from_record → set_current_session）
    # 不触发任何 backend 信号，collector 不会自己感知，不 refresh 的话页脚在
    # 用户下一次发消息前一直空白。重投影成功即对齐 sid，后续刷新自然跳过。
    #
    # ⚠️ 节流：refresh 内部会拿 backend 当前会话重新投影，会话拿不到时 sid 不变，
    # 不设守卫就会在「每张历史卡片构建 + 落定补刷」时各跑一次全量投影
    # （实测 200+ 消息会话单次 40~115ms，主线程）。同一 (窗口, 目标会话) 在
    # 窗口期内只试一次，失败就退化成不显示（下次节拍再试）。
    cur_sid = str(getattr(mw, "_current_session_id", "") or "")
    if cur_sid and str(getattr(collector, "_active_session_id", "") or "") != cur_sid:
        wid = str(ctx.get("window_id") or "")
        guard_key = (wid, cur_sid)
        now = time.time()
        if now - _REPROJECT_GUARD.get(guard_key, 0.0) < _REPROJECT_GUARD_WINDOW_S:
            return None
        _REPROJECT_GUARD[guard_key] = now
        if len(_REPROJECT_GUARD) > 32:  # 防御：切会话频繁时不让守卫表无限增长
            _REPROJECT_GUARD.clear()
            _REPROJECT_GUARD[guard_key] = now
        try:
            collector.refresh()
        except Exception as e:  # noqa: BLE001 — 重投影失败只能退化，不影响渲染
            logger.debug(f"[agent_trace] footer 重投影失败: {e}")
        if cur_sid and str(getattr(collector, "_active_session_id", "") or "") != cur_sid:
            return None
    total_tokens = 0
    total_gen_s = 0.0
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
        if gen_ms <= 200:
            continue
        total_tokens += tokens
        total_gen_s += gen_ms / 1000.0
        rounds += 1
    if total_gen_s <= 0 or total_tokens <= 0:
        return None
    return total_tokens, total_gen_s, rounds


def _fmt_tps(tps: float) -> str:
    """吞吐量文本格式化：≥1000 显示 K 位，其余取整。"""
    return f"{tps / 1000:.1f}K" if tps >= 1000 else str(round(tps))


# 页脚吞吐量配色阈值（tok/s）：慢 = 红，一般 = 黄，其余 = 绿
_TPS_SLOW = 30
_TPS_OK = 60
_COLOR_SLOW = "#f85149"
_COLOR_OK = "#d29922"
_COLOR_FAST = "#2ea043"


def _tps_color(tps: float) -> str:
    """吞吐量配色：<30 红 / <60 黄 / 其余绿（红绿语义对齐差异徽章的 +/-）。"""
    if tps < _TPS_SLOW:
        return _COLOR_SLOW
    if tps < _TPS_OK:
        return _COLOR_OK
    return _COLOR_FAST


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
