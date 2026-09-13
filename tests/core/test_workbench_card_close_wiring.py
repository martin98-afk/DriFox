# -*- coding: utf-8 -*-
"""工作台卡片 tab × 关闭接线回归测试（同步 pyside6 ad524790）。

回归背景：接线幂等标志原挂在 registry 单例上且藏在 if widget is None 分支内，
第一个 panel 接上线之后，后续问世的 panel 全部接不上：× 照常 emit、无人接收，
表现为「点关闭钮没反应且日志无报错」。修复：标志挂 panel 实例、接线移出分支。
"""
from PyQt5.QtWidgets import QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.workbench_panel import WorkbenchPanel


class _RealHost(QWidget):
    """最小全局宿主桩：绕开 TabManagerWindow 单例依赖"""

    def __init__(self):
        super().__init__()
        self._window_id = "probe-win"
        self.workbench_panel = None

    def is_workbench_visible(self):
        return True

    def set_workbench_visible(self, visible):
        pass


def _make_host_panel():
    host = _RealHost()
    panel = WorkbenchPanel(host)
    host.workbench_panel = panel
    return host, panel


def test_close_signal_wired_per_panel(qapp):
    """回归：tab × 的关闭信号必须对**每个** panel 都接线。

    旧实现标志挂 registry 单例：第一个 panel 接线后标志永久为 True，
    第二个 panel 的 × emit 无人接收（点关闭钮没反应且日志无报错）。
    """
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    card_id = "probe-close:card"
    hosts = []
    panels = []
    try:
        reg.register_floating_card(
            plugin_name="probe-close",
            card_id=card_id,
            widget_class=QWidget,
            container="right",
            title="探针卡",
        )
        card_info = reg.get_floating_cards()[card_id]
        for i in range(2):
            host, panel = _make_host_panel()
            hosts.append(host)  # 持宿主引用：host 被 GC 会级联删 panel，finally 探活就踩空
            panels.append(panel)
            reg._resolve_global_host = lambda h=host: h
            reg._show_floating_card_in_workbench(card_info, panel, host)
            assert panel.has_card_tab(card_id), f"前置：第 {i} 个 panel 应已挂载卡片页签"
            # 面板 emit 关闭请求：registry 必须收到并摘 tab（接线失效则本断言挂）
            panel.card_tab_close_requested.emit(card_id)
            assert not panel.has_card_tab(card_id), f"第 {i} 个 panel 的 × 未接上关闭信号"
    finally:
        reg.reset()
        for panel in panels:
            try:
                panel.deleteLater()
            except RuntimeError:
                pass


def test_close_workbench_card_tab_missing_panel_logs_warning():
    """拿不到工作台面板时必须 warning 留痕（不得静默 return 掩盖「点了没反应」）。"""
    from app.plugins.registries import ui_plugin_registry as mod

    reg = UIPluginRegistry.get_instance()
    reg.reset()
    records = []
    token = mod.logger.add(records.append, level="WARNING")
    try:
        reg._resolve_global_host = lambda: None
        reg._close_workbench_card_tab("probe-missing:card")  # 修复前：静默吞掉
        assert any("拿不到工作台面板" in str(r) for r in records)
    finally:
        mod.logger.remove(token)
        reg.reset()
