"""通过外观模式入口切换，验证已存在的控件和滚动内容底色。"""

from importlib import import_module
from pathlib import Path

import pytest
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QLineEdit
from qfluentwidgets import isDarkTheme

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry, WindowInfo, WorkbenchTabInfo
from app.utils import theme_mode
from app.utils.design_tokens import Colors, card_container_surface_color, make_card_content_opaque, opaque_surface_color
from app.utils.theme_manager import theme_manager
from app.utils.theme_refresh import ThemeRefreshCoordinator
from app.widgets.plugin_window import PluginWindow
from tools.ui_driver import place_offscreen

pytestmark = pytest.mark.ui


def test_open_widgets_follow_mode_in_both_directions(ui_app, qtbot):
    host = ui_app["chat_window"]
    tm = ui_app["tm"]
    qtbot.waitUntil(lambda: not host._initialization_in_progress, timeout=10000)
    host._open_settings_popup()
    settings = host._settings_popup
    settings._set_active_page("common")
    sidebar = tm._tab_panel
    info = WindowInfo(
        plugin_name="shortcut-manager",
        window_id="theme-test",
        widget_class=import_module("plugins.shortcut-manager.ui.cards").ShortcutManagerCard,
        context_provider=host._build_ui_context,
    )
    plugin = PluginWindow(info)
    plugin._content._search.setText("保留筛选")
    make_card_content_opaque(plugin._content, 246)
    qtbot.addWidget(plugin)
    place_offscreen(plugin)
    original_mode = theme_mode.current_mode()
    try:
        for mode in ("dark", "light", "dark"):
            epoch = ThemeRefreshCoordinator.get_appearance_epoch()
            theme_mode.set_mode(mode, save=False)
            qtbot.waitUntil(lambda: ThemeRefreshCoordinator.get_appearance_epoch() > epoch)
            assert isDarkTheme() == (mode == "dark")
            assert theme_manager.is_light_theme() == (mode == "light")
            assert theme_manager.get_current_colors()["text_primary"] in plugin._content._search.styleSheet()
            assert plugin._content._search.text() == "保留筛选"
            assert Colors.CONTENT_BG in plugin.styleSheet()
            assert plugin._content.palette().window().color() == card_container_surface_color(246)
            assert Colors.DIVIDER_COLOR in settings.singleInstanceCard.styleSheet()
            assert Colors.BORDER_ACCENT in settings.giteeCard._bind_btn.styleSheet()
            assert Colors.TEXT_MUTED in settings.llmProviderCard._empty_label.styleSheet()
            assert Colors.TEXT_MUTED in tm.workbench_panel._empty_page._hint.styleSheet()
            assert Colors.TEXT_PRIMARY in host._file_mention_card._crumb_labels[-1].styleSheet()

            for widget, alpha in (
                (sidebar._list_widget, 150),
                (sidebar._tree_widget, 150),
                (settings._nav_scroll.widget(), 230),
                (settings._page_widgets["provider"].widget(), 230),
            ):
                expected = opaque_surface_color(Colors.CARD_BG.format(alpha=255), alpha, Colors.CONTENT_BG)
                if widget in (sidebar._list_widget, sidebar._tree_widget) and sidebar._region_backdrop_visible():
                    expected = QColor(0, 0, 0, 0)
                actual = widget.palette().color(widget.backgroundRole())
                assert actual == expected, (mode, widget.objectName(), alpha, actual.getRgb(), expected.getRgb())
    finally:
        settings._on_close()
        epoch = ThemeRefreshCoordinator.get_appearance_epoch()
        theme_mode.set_mode(original_mode, save=False)
        qtbot.waitUntil(lambda: ThemeRefreshCoordinator.get_appearance_epoch() > epoch)


def test_open_file_tree_and_workspace_update_in_place(ui_app, qtbot, monkeypatch):
    tm = ui_app["tm"]
    panel = tm.workbench_panel
    registry = UIPluginRegistry.get_instance()
    workspace = tm._workspace_page_host
    original_mode = theme_mode.current_mode()
    original_pages = registry.get_workbench_tabs()
    original_widget = tm._content_area.currentWidget()
    was_visible = tm.is_workbench_visible()
    file_tree_class = import_module("plugins.file-tree.ui.cards").FileTreeCard
    tab = WorkbenchTabInfo("file-tree", "file-tree", "文件树", file_tree_class)
    monkeypatch.setattr(registry, "get_workbench_tabs", lambda: [tab])
    monkeypatch.setattr(registry, "_workspace_pages", dict(registry._workspace_pages))

    class WorkspaceEditor(QLineEdit):
        def __init__(self, parent, context):
            super().__init__(parent)
            self.context = context

        def apply_theme(self):
            self.setStyleSheet(f"color: {self.context['colors']['text_primary']};")

    try:
        registry.register_workspace_page("test", "theme-editor", "编辑器", WorkspaceEditor)
        workspace.show_page("theme-editor")
        editor = tm._content_area.currentWidget()
        editor.setText("保留草稿")
        panel.sync_plugin_pages([tab])
        tm.set_workbench_visible(True, animate=False)
        assert panel.set_current_tab_by_id("file-tree")
        tree = panel._plugin_widgets["file-tree"]
        qtbot.waitUntil(lambda: tree.isVisible() and editor.isVisible())
        path = str(Path(__file__).resolve())
        tree._open_preview(path)
        assert tree._open_global_preview(path)
        preview = tree._global_preview.page()
        scans = []
        monkeypatch.setattr(tree, "_async_load_tree", lambda *args: scans.append(args))
        for mode in ("light", "dark"):
            epoch = ThemeRefreshCoordinator.get_appearance_epoch()
            theme_mode.set_mode(mode, save=False)
            qtbot.waitUntil(lambda: ThemeRefreshCoordinator.get_appearance_epoch() > epoch)
            text = theme_manager.get_current_colors()["text_primary"]
            assert editor.styleSheet() == f"color: {text};"
            assert editor.text() == "保留草稿"
            assert tree._colors["is_dark"] == (mode == "dark")
            assert tree._colors["text"].name() in tree._tree_view.styleSheet()
            assert preview._text_color == tree._colors["text"]
            assert tree._colors["card_bg"].name() in tree._global_preview.widget.styleSheet()
            assert preview._is_dark == tree._preview_page._is_dark == (mode == "dark")
            assert preview.current_path() == tree._preview_page.current_path() == path
        assert not scans
    finally:
        if tree := panel._plugin_widgets.get("file-tree"):
            tree._close_global_preview()
        tm._content_area.setCurrentWidget(original_widget)
        workspace._destroy_page("theme-editor")
        tm.set_workbench_visible(was_visible, animate=False)
        panel.sync_plugin_pages(original_pages)
        epoch = ThemeRefreshCoordinator.get_appearance_epoch()
        theme_mode.set_mode(original_mode, save=False)
        qtbot.waitUntil(lambda: ThemeRefreshCoordinator.get_appearance_epoch() > epoch)
