"""精选卡片重排及精选页搜索入口的界面回归。"""

from pathlib import Path

from PyQt5.QtWidgets import QApplication

MARKETPLACE = Path(__file__).resolve().parents[2] / "plugins" / "plugin-marketplace"


def test_featured_reflow_preserves_download_in_progress(monkeypatch):
    monkeypatch.syspath_prepend(str(MARKETPLACE))
    from ui.cards import _ExploreCard, _ExploreGridSection

    monkeypatch.setattr(_ExploreCard, "_resolve_local_path", lambda self: None)
    section = _ExploreGridSection("工具")
    for i in range(4):
        meta = {"name": f"demo-{i}", "description": "插件说明", "version": "1.0"}
        section.add_card(meta["name"], _ExploreCard(meta, installed=False, parent=section))
    card = section.cards()[2][1]
    card.set_downloading()
    card.set_progress(0.4, "下载中")
    section.show()
    for width, columns in ((960, 2), (716, 2), (715, 1), (480, 1), (960, 2)):
        section.resize(width, section.height())
        QApplication.processEvents()
        item = section._card_layout.itemAtPosition(*divmod(2, columns))
        assert item.widget() is card
        assert card._btn.text() == "取消" and card._progress.value() == 40
    section.close()


def test_featured_search_opens_unfiltered_catalogue(monkeypatch):
    monkeypatch.syspath_prepend(str(MARKETPLACE))
    from ui.cards import MarketplaceCard

    card = MarketplaceCard()
    monkeypatch.setattr(card, "_build_local_extra_plugins", lambda: [])
    card._plugin_data = [{"name": f"plugin-m-{i:03d}", "categories": ["工具"]} for i in range(3)]
    card._render_plugins(card._plugin_data)
    card._filter_bar.setCurrentItem("featured")
    card._active_tags = {"旧分类"}
    card._source_filter = "旧来源"
    card._search_edit.setText("plugin-m-002")
    card._search_edit.searchButton.click()
    assert card._current_filter == "all"
    assert [p["name"] for p in card._matched] == ["plugin-m-002"]
    assert not card._active_tags and not card._source_filter
    assert card._filter_panel.isHidden()
    card._filter_toggle.click()
    assert not card._filter_panel.isHidden()
    card._filter_toggle.click()
    assert card._filter_panel.isHidden()
    card.close()


def test_catalog_reflows_filtered_cards_and_footer(monkeypatch):
    monkeypatch.syspath_prepend(str(MARKETPLACE))
    from ui.cards import MarketplaceCard

    card = MarketplaceCard()
    monkeypatch.setattr(card, "_build_local_extra_plugins", lambda: [])
    card._plugin_data = [{"name": f"grid-test-{i:02d}"} for i in range(19)]
    card.resize(900, 600)
    card.show()
    card._filter_bar.setCurrentItem("all")
    card._reveal_rows()
    first, second = list(card._row_map.values())[:2]
    assert first.y() == second.y() and first.x() < second.x()
    assert len(card._row_map) == 16
    assert card._load_more_btn.text() == "加载更多 (3 个)"
    assert card._load_more_btn.width() > first.width()
    card._on_load_more()
    card._reveal_rows()
    last = card._row_map["grid-test-18"]
    assert last.width() == 2 * first.width() + card._content_layout.horizontalSpacing()
    assert card._load_more_btn is None
    card.resize(550, 600)
    QApplication.processEvents()
    assert first.x() == second.x() and first.y() < second.y()
    card._search_edit.setText("grid-test-01")
    card._filter_plugins()
    card._reveal_rows()
    assert card._content_layout.itemAtPosition(0, 0).widget() is second
    assert card._load_more_btn is None
    card.close()


def test_disabled_mark_tracks_changes_in_both_views(monkeypatch):
    monkeypatch.syspath_prepend(str(MARKETPLACE))
    from ui.cards import _ExploreCard, _PluginRow

    for card_type in (_PluginRow, _ExploreCard):
        monkeypatch.setattr(card_type, "_resolve_local_path", lambda self: None)
        card = card_type({"name": "mark-test", "version": "1"}, installed=True, status="enabled")
        for status in ("disabled", "enabled", "builtin_disabled", "builtin_enabled"):
            card.apply_state(True, False, "1", status)
            assert not card._installed_mark.isHidden()
            assert card._installed_mark.text() == ("×" if "disabled" in status else "✓")
            if isinstance(card, _PluginRow):
                assert "已启用" not in card._meta_label._full_text and "已禁用" not in card._meta_label._full_text
        card.apply_state(False, False, None)
        assert card._installed_mark.isHidden()
        card.close()
