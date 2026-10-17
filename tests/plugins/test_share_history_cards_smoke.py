# -*- coding: utf-8 -*-
"""插件冒烟测试：share-history 卡片 emoji 替换后功能完整性

覆盖：
- 模块导入 / 图标常量类型
- _RecordItem 三态按钮构建（有文件 / 有链接 / 缺失）
- 删除按钮类型为 TransparentToolButton
- 主题刷新不抛异常（浅色 + 深色）
"""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "share-history"


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture(scope="module", autouse=True)
def _sys_path():
    sys.path.insert(0, str(PLUGIN_DIR))
    yield
    sys.path.remove(str(PLUGIN_DIR))


def _theme():
    return {
        "tc": "rgba(0,0,0,0.85)",
        "tcs": "rgba(0,0,0,0.45)",
        "border_c": "rgba(128,128,128,0.15)",
        "card_bg_dim": "rgba(128,128,128,0.06)",
        "hover_bg": "rgba(128,128,128,0.10)",
        "badge_bg": "rgba(128,128,128,0.10)",
        "btn_bg": "rgba(128,128,128,0.08)",
        "btn_border": "rgba(128,128,128,0.15)",
        "btn_disabled": "rgba(128,128,128,0.4)",
        "ff": "Microsoft YaHei",
        "fs": 14,
    }


def test_type_icons_are_fluent_icons(qt_app):
    """类型图标应为 FluentIcon，不再是 emoji 字符串"""
    from qfluentwidgets import FluentIcon

    from ui.cards import _TYPE_ICONS, _TYPE_ICON_FALLBACK

    assert set(_TYPE_ICONS) == {"session", "project"}
    for icon in _TYPE_ICONS.values():
        assert isinstance(icon, FluentIcon)
    assert isinstance(_TYPE_ICON_FALLBACK, FluentIcon)


def test_no_emoji_in_source():
    """源码中不应残留 emoji（省略号/箭头/破折号等排版字符除外）"""
    import re

    src = (PLUGIN_DIR / "ui" / "cards.py").read_text(encoding="utf-8")
    emoji = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff]")
    hits = emoji.findall(src)
    assert hits == [], f"残留 emoji: {hits}"


def test_record_item_with_local_file(qt_app, tmp_path):
    """有本地文件时应构建打开按钮 + 复制链接按钮"""
    from qfluentwidgets import FluentIcon, TransparentPushButton, TransparentToolButton

    from ui.cards import _RecordItem

    f = tmp_path / "a.json"
    f.write_text("{}", encoding="utf-8")
    rec = {
        "id": 1,
        "type": "session",
        "title": "t",
        "format": "json",
        "file_path": str(f),
        "upload_url": "https://example.com/a.json",
        "extra_info": {"msg_count": 3, "project": "P"},
        "created_at": "2026-01-01 00:00:00",
    }
    item = _RecordItem(rec, theme=_theme())

    assert item._open_btn is not None
    assert isinstance(item._open_btn, TransparentPushButton)
    assert item._open_btn.text() == "打开"
    assert item._open_btn.icon().isNull() is False
    assert item._copy_btn is not None and item._copy_btn.text() == "复制链接"
    assert item._download_btn is None
    assert item._missing_btn is None
    # 删除按钮改为图标按钮
    assert isinstance(item._delete_btn, TransparentToolButton)
    assert not item._delete_btn.icon().isNull()
    # 类型图标为 IconWidget
    from qfluentwidgets import IconWidget

    assert isinstance(item._icon_lb, IconWidget)
    assert item._icon_lb.width() == 16


def test_record_item_url_only(qt_app):
    """仅有链接时应构建下载按钮"""
    from ui.cards import _RecordItem

    rec = {
        "id": 2,
        "type": "session",
        "title": "t",
        "format": "md",
        "file_path": "",
        "upload_url": "https://example.com/a.md",
        "extra_info": {},
        "created_at": "2026-01-01 00:00:00",
    }
    item = _RecordItem(rec, theme=_theme())
    assert item._download_btn is not None
    assert item._download_btn.text() == "下载"
    assert not item._download_btn.icon().isNull()
    assert item._open_btn is None


def test_record_item_missing_file(qt_app):
    """文件与链接都无时应构建禁用的缺失按钮"""
    from ui.cards import _RecordItem

    rec = {
        "id": 3,
        "type": "project",
        "title": "t",
        "format": "drifox_project",
        "file_path": "",
        "upload_url": "",
        "extra_info": {"session_count": 2},
        "created_at": "2026-01-01 00:00:00",
    }
    item = _RecordItem(rec, theme=_theme())
    assert item._missing_btn is not None
    assert item._missing_btn.text() == "文件缺失"
    assert not item._missing_btn.isEnabled()


def test_project_info_has_no_emoji_prefix(qt_app, tmp_path):
    """project 信息不应再带 emoji 前缀"""
    from ui.cards import _RecordItem

    f = tmp_path / "a.json"
    f.write_text("{}", encoding="utf-8")
    rec = {
        "id": 4,
        "type": "session",
        "title": "t",
        "format": "json",
        "file_path": str(f),
        "upload_url": "",
        "extra_info": {"msg_count": 5, "project": "DriFox"},
        "created_at": "2026-01-01 00:00:00",
    }
    item = _RecordItem(rec, theme=_theme())
    text = item._info_lb.text()
    assert "DriFox" in text
    assert "\U0001f4c1" not in text


def test_refresh_theme_light_dark(qt_app):
    """主题刷新（浅/深）不应抛异常"""
    from qfluentwidgets import Theme, setTheme

    from ui.cards import _RecordItem

    rec = {
        "id": 5,
        "type": "session",
        "title": "t",
        "format": "json",
        "file_path": "",
        "upload_url": "https://example.com/a.json",
        "extra_info": {},
        "created_at": "2026-01-01 00:00:00",
    }
    item = _RecordItem(rec, theme=_theme())
    for th in (Theme.LIGHT, Theme.DARK):
        setTheme(th)
        item.refresh_theme(_theme())
    setTheme(Theme.LIGHT)


def test_full_card_renders(qt_app):
    """完整卡片渲染 + 搜索图标为 IconWidget"""
    from qfluentwidgets import IconWidget

    from ui.cards import ShareHistoryCard

    card = ShareHistoryCard()
    card.set_context_provider(
        lambda: {
            "font_family": "Microsoft YaHei",
            "font_size": 14,
            "colors": {},
        }
    )
    card._apply_latest_theme()
    card._all_records = []
    card._render_records([], update_search=False)
    assert isinstance(card._search_icon, IconWidget)
    assert isinstance(card._header_icon, IconWidget)
