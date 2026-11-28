"""工作树和分享记录在主窗口中的入口与停靠位置。"""

from pathlib import Path

import pytest
from PyQt5.QtCore import Qt


@pytest.mark.ui
def test_branch_opens_left_and_share_history_opens_right(ui_app, qtbot):
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    tm, win = ui_app["tm"], ui_app["chat_window"]
    registry = UIPluginRegistry.get_instance()
    plugins = Path(__file__).resolve().parents[2] / "plugins"
    for name in ("worktree-manager", "share-history"):
        assert registry.ensure_plugin_loaded(name, plugins / name)

    tm.set_workbench_visible(False)
    registry.hide_floating_card_globally("worktree-manager")
    tm._tab_panel.set_collapsed(True)
    tm._on_sidebar_toggled(True, animate=False)
    try:
        # ui_app 构造时可能尚未注册插件，用标题栏同一工厂装配。
        chip = win._branch_widget or registry.get_titlebar_widget("branch").widget_factory(win)
        qtbot.mouseClick(chip, Qt.LeftButton)
        page = registry.get_floating_card_widget("worktree-manager")
        assert page is not None
        qtbot.waitUntil(lambda: page.isVisible() and tm._global_left_wrapper.width() > 60)
        assert tm._global_left_wrapper.isAncestorOf(page)
        assert page._current_project == win._current_project
        assert not tm.is_workbench_visible()
        qtbot.mouseClick(chip, Qt.LeftButton)
        assert tm._card_manager.is_card_visible("worktree-manager", tm._window_id)
        assert page._title.isVisible()
        qtbot.mouseClick(page._close_btn, Qt.LeftButton)
        qtbot.waitUntil(lambda: not page.isVisible())

        assert "share-history" not in registry.get_floating_cards()
        assert tm.open_workbench_tab("share-history")
        assert tm.workbench_panel.current_tab_id() == "share-history"
        assert not tm.workbench_panel.has_card_tab("share-history")
        share = tm.workbench_panel._plugin_widgets["share-history"]
        qtbot.waitUntil(lambda: share.isVisible() and share._refresh_btn.isEnabled())
    finally:
        tm.set_workbench_visible(False)
        registry.hide_floating_card_globally("worktree-manager")
