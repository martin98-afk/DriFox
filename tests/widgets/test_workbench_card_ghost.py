# -*- coding: utf-8 -*-
"""回归：工作台卡片 tab 投影恢复后不得残留"幽灵可见"状态

用户操作序列（2026-09-02 报告）：
1. 右侧边栏（工作台）打开可关闭的临时插件卡片（right 容器 → 动态 tab）
2. 在工作台内切到常驻页签（窗口记忆停在常驻页）
3. 切到其他对话标签页（投影摘除卡片 tab）
4. 切回来（投影恢复卡片 tab，activate=False）

修复前症状：恢复路径无条件调 widget.show_card()，而插件模板的
show_card() 末尾有 setVisible(True)（5 个 right 容器插件全同模式），
卡片被强行显示为 QStackedWidget 的"非当前页但可见"幽灵页，
被常驻页 raise 后压在背景层——与常驻页内容重叠且不可点击。
"""

from PyQt5.QtWidgets import QApplication, QWidget

import pytest

from app.widgets.tab_manager_window import TabManagerWindow
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


class _TemplateCard(QWidget):
    """模拟 ui-plugin-creator 模板生成的卡片：show_card 末尾 setVisible(True)"""

    def show_card(self):
        self.setVisible(True)


def _get_card_widget(reg, card_id):
    for win_map in reg._card_widget_instances.values():
        widget = win_map.get(card_id)
        if widget is not None:
            return widget
    return None


@pytest.fixture()
def tm(qtbot):
    tm = TabManagerWindow.create_instance()
    tm.show()
    qtbot.wait(50)
    yield tm
    QApplication.processEvents()


def test_projection_restore_keeps_card_hidden(qtbot, tm):
    """投影恢复（activate=False）路径不得把非当前页卡片强行 show 出来"""
    reg = UIPluginRegistry.get_instance()
    panel = tm.workbench_panel
    card_id = "wb-ghost-card"

    reg.register_floating_card("tp", card_id, _TemplateCard, "right", title="幽灵卡")
    # 1) 用户打开卡片：挂载 + 激活
    reg.toggle_floating_card(card_id)
    for _ in range(3):
        QApplication.processEvents()

    widget = _get_card_widget(reg, card_id)
    assert widget is not None, "卡片实例应已创建"
    assert panel.has_card_tab(card_id), "卡片 tab 应挂载"
    assert panel._stack.currentWidget() is widget, "打开后当前页应为卡片"
    assert widget.isVisible(), "打开后卡片应可见（此为正常路径）"

    # 2) 用户在工作台内切到常驻页签（写入窗口页签记忆）
    panel.set_current_tab(panel.TAB_WORKTREE, user=True)
    QApplication.processEvents()
    assert panel._stack.currentWidget() is not widget

    # 3) 切到其他对话 tab：投影摘除卡片
    reg.sync_floating_cards_to_tab("other-window-id")
    QApplication.processEvents()
    assert not panel.has_card_tab(card_id), "切走后卡片 tab 应被摘除"

    # 4) 切回：投影恢复（auto_expand=False, activate=False）。
    # 测试环境无对话窗口，toggle 时 scope 落 __global__ 兜底；
    # 从记录集合取真实 scope，等价真实环境的「切回原对话 tab」
    scope = next(iter(reg._workbench_card_scopes.keys()), "tp-fallback")
    reg.sync_floating_cards_to_tab(scope)
    QApplication.processEvents()

    assert panel.has_card_tab(card_id), "切回应恢复卡片 tab"
    assert panel._stack.indexOf(widget) >= 0, "卡片应重新挂回 stack"
    # ★ 回归断言：非当前页的卡片不得强行可见（幽灵页）
    assert panel._stack.currentWidget() is not widget, "恢复不应抢走当前页签"
    assert not widget.isVisible(), (
        "投影恢复（activate=False）不得触发 show_card 的 setVisible(True)，"
        "否则卡片残留为背景幽灵页"
    )
