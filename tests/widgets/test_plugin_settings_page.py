# -*- coding: utf-8 -*-
"""插件设置主从页（QApplication 离屏）：列表来源、排序、筛选、搜索、详情三页签。

回归背景（2026-11-07）：插件设置页原来只列「注册了设置卡的插件」，没有
config_schema 的插件（本机 46 个插件里的 39 个）在设置里完全不可见，用户
搜不到、也看不到它们的权限与关于信息。本页数据源改为 PluginManager 全量清单。
"""

from pathlib import Path

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication, QWidget

from app.plugins.contracts.plugin_config import parse_config_schema
from app.plugins.managers.plugin_manager import PluginInfo
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
from app.widgets.cards.settings import plugin_settings_page as page_mod


class _FakePM:
    """最小 PluginManager 替身：页面只用到 is_initialized / list_plugins / is_enabled"""

    def __init__(self, plugins, enabled):
        self._plugins = plugins
        self._enabled = set(enabled)

    def is_initialized(self) -> bool:
        return True

    def list_plugins(self):
        return list(self._plugins)

    def is_enabled(self, name: str) -> bool:
        return name in self._enabled


def _info(tmp_path: Path, name: str, **manifest) -> PluginInfo:
    base = {"name": name, "version": "1.0.0", "description": f"{name} 描述"}
    base.update(manifest)
    return PluginInfo(name=name, manifest=base, path=tmp_path / name, plugin_type="user")


@pytest.fixture()
def env(tmp_path, monkeypatch, qapp):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    reg = PluginConfigRegistry.get_instance()
    reg.register(
        parse_config_schema(
            "voice-reply",
            {
                "title": "语音播报配置",
                "fields": [
                    {"key": "api_key", "label": "在线 Key", "type": "password", "required": True},
                    {"key": "engine", "label": "引擎", "type": "text", "default": "edge", "description": "合成语音使用的引擎"},
                ],
            },
        )
    )
    reg.register(
        parse_config_schema(
            "git-panel",
            {
                "title": "Git 提交描述生成",
                "fields": [{"key": "prompt", "label": "提示词", "type": "textarea", "default": "写清楚"}],
            },
        )
    )
    plugins = [
        _info(tmp_path, "browser", capabilities=["ui.notify"], components={"ui": True, "mcp": True}),
        _info(tmp_path, "git-panel", components={"skills": True}),
        _info(tmp_path, "voice-reply", capabilities=["model.config"], components={"tools": True}),
    ]
    fake = _FakePM(plugins, enabled={"browser", "git-panel"})
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        classmethod(lambda cls: fake),
    )
    yield fake
    reg.unregister_plugin("voice-reply")
    reg.unregister_plugin("git-panel")


def _pump(app, times: int = 8) -> None:
    for _ in range(times):
        app.processEvents()


def test_lists_all_plugins_including_without_schema(env, qapp):
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    names = [e.info.name for e in page._entries]
    assert set(names) == {"browser", "git-panel", "voice-reply"}
    # 无 config_schema 的 browser 也在列表里
    assert any(e.info.name == "browser" and not e.has_schema for e in page._entries)
    assert len(page._rows) == 3


def test_pending_required_sorts_first(env, qapp):
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    assert page._entries[0].info.name == "voice-reply"
    assert page._entries[0].state == "missing"
    assert page._entries[0].missing == ["在线 Key"]


def test_search_by_plugin_name_and_field_label(env, qapp):
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    page._search.setText("在线 Key")
    page._apply_filter()
    _pump(qapp)
    visible = [r._entry.info.name for r in page._rows if not r.isHidden()]
    assert visible == ["voice-reply"]
    # 默认筛选只看「有配置」，搜无配置插件前先切到「全部」
    page._on_chip_clicked("all")
    page._search.setText("browser")
    page._apply_filter()
    visible = [r._entry.info.name for r in page._rows if not r.isHidden()]
    assert visible == ["browser"]


def test_filter_chip_pending(env, qapp):
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    page._on_chip_clicked("todo")
    visible = [r._entry.info.name for r in page._rows if not r.isHidden()]
    assert visible == ["voice-reply"]


def test_detail_shows_config_form_for_schema_plugin(env, qapp):
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    page._select("voice-reply", force=True)
    _pump(qapp)
    detail = page._detail
    assert detail._form is not None
    assert detail._form._rows["api_key"] is not None
    # 必填缺失在配置页顶部提示
    from qfluentwidgets import CaptionLabel

    texts = [w.text() for w in detail._pages["config"].widget().findChildren(CaptionLabel)]
    assert any("在线 Key" in t for t in texts)


def test_detail_without_schema_has_permissions_and_about(env, qapp):
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    page._select("browser", force=True)
    _pump(qapp)
    detail = page._detail
    assert detail._form is None  # 无 schema 不建表单
    config_body = detail._pages["config"].widget()
    assert config_body is not None
    perms_body = detail._pages["perms"].widget()
    assert perms_body is not None
    about_body = detail._pages["about"].widget()
    assert about_body is not None
    # 权限页渲染能力 id（browser 声明 ui.notify）
    texts = [w.text() for w in perms_body.findChildren(page_mod.BodyLabel)]
    assert "ui.notify" in texts


@pytest.fixture()
def fresh_ui_registry(monkeypatch):
    """每用例独立 UIPluginRegistry（防单例状态污染）"""
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


def test_detail_renders_plugin_custom_settings_card(env, qapp, fresh_ui_registry, tmp_path):
    """插件自带设置卡（register_settings_card 非自动卡）在详情页配置 tab 实例化

    回归：git-panel / voice-reply 这类插件用自定义卡覆盖自动表单；插件页改主从后
    必须继续渲染它们，否则自定义配置入口丢失。
    """
    built = {}

    class _CustomCard(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            built["parent"] = parent

    fresh_ui_registry.register_settings_card("browser", "browser-config", "浏览器自定义配置", _CustomCard)
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    entry = next(e for e in page._entries if e.info.name == "browser")
    assert [c.card_id for c in entry.custom_cards] == ["browser-config"]

    page._select("browser", force=True)
    _pump(qapp)
    assert "parent" in built  # widget_class 被实例化
    config_body = page._detail._pages["config"].widget()
    assert config_body.findChild(_CustomCard) is not None


def test_auto_config_card_not_rendered_twice(env, qapp, fresh_ui_registry):
    """config_schema 自动卡（metadata.auto_config_card）不进 custom_cards

    自动卡由 PluginConfigForm 渲染；若也当自定义卡实例化，会出现两份同样的配置面。
    """
    from app.widgets.cards.settings.plugin_config_card import make_card_class

    fresh_ui_registry.register_settings_card(
        "voice-reply",
        "voice-reply-config",
        "语音播报配置",
        make_card_class("voice-reply"),
        metadata={"auto_config_card": True},
    )
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    entry = next(e for e in page._entries if e.info.name == "voice-reply")
    assert entry.custom_cards == []
    assert entry.has_schema is True


def test_segments_follow_plugin_capabilities(env, qapp, tmp_path):
    """页签按实际能力出现：无配置无权限只留关于；无配置有权限不留配置页"""
    env._plugins.append(_info(tmp_path, "plain-plugin"))  # 无 schema、无 capabilities
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)

    page._select("plain-plugin", force=True)
    _pump(qapp)
    assert set(page._detail._seg.items.keys()) == {"about"}

    page._select("git-panel", force=True)  # 有 config_schema、无 capabilities
    _pump(qapp)
    assert set(page._detail._seg.items.keys()) == {"config", "about"}

    page._select("browser", force=True)  # 无 config_schema、有 capabilities
    _pump(qapp)
    assert set(page._detail._seg.items.keys()) == {"perms", "about"}

    page._select("voice-reply", force=True)  # 有 config_schema、有 capabilities
    _pump(qapp)
    assert set(page._detail._seg.items.keys()) == {"config", "perms", "about"}


def test_row_and_detail_font_scaled(env, qapp):
    """列表名与详情标题在全局字号基础上有层级偏移（样式表生效，未被异常吞掉）"""
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    row = page._rows[0]
    assert "font-size" in row._name.styleSheet()
    page._select("voice-reply", force=True)
    _pump(qapp)
    assert "font-size" in page._detail._title.styleSheet()
    assert "font-size" in page._detail._meta.styleSheet()


def test_about_page_sections(env, qapp):
    """关于页按分组组织：描述独立成段 + 四组标题 + 目录可复制按钮"""
    from qfluentwidgets import PushButton

    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    page._select("browser", force=True)
    _pump(qapp)
    about = page._detail._pages["about"].widget()
    titles = [w.text() for w in about.findChildren(page_mod._SectionTitle)]
    assert titles == ["基本信息", "兼容性", "本地位置"]
    buttons = [b.text() for b in about.findChildren(PushButton)]
    assert "复制路径" in buttons and "打开插件目录" in buttons


def test_theme_colors_applied(env, qapp):
    """页面文字颜色走主题色（qfluentwidgets 默认黑白不跟自定义主题）"""
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    page._select("voice-reply", force=True)
    _pump(qapp)
    detail = page._detail
    assert "color" in detail._title.styleSheet()
    assert "color" in detail._meta.styleSheet()
    # 说明文字同时保留 color 与更小字号
    hints = [w for w in detail.findChildren(page_mod.CaptionLabel) if w.objectName() == "configHint"]
    assert hints, "配置页应有说明文字"
    css = hints[0].styleSheet()
    assert "color" in css and "font-size" in css


def test_list_row_theme_colors_applied(env, qapp):
    """列表行文字也走主题色（分帧建行完成后统一补刷，深色模式不再黑字）"""
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    assert page._rows, "应已建出列表行"
    for row in page._rows:
        assert "color" in row._name.styleSheet()


def test_row_font_follows_refresh(env, qapp):
    """refresh_style 后列表行/详情标题/说明文字字号串重写（字号变更链路）"""
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    page._select("voice-reply", force=True)
    _pump(qapp)
    page.refresh_style()
    for row in page._rows:
        assert "font-size" in row._name.styleSheet()
    assert "font-size" in page._detail._title.styleSheet()
    assert "font-size" in page._detail._meta.styleSheet()
    hints = [w for w in page._detail.findChildren(page_mod.CaptionLabel) if w.objectName() == "configHint"]
    if hints:
        assert "font-size" in hints[0].styleSheet()


def test_single_line_row_structure(env, qapp):
    """列表行为单行结构：圆点+图标+名称，无副标题（对齐服务商列表样式）"""
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    row = page._rows[0]
    assert not hasattr(row, "_sub"), "副标题已移除"
    assert row._dot.x() < row._icon.x() < row._name.x(), "圆点应在图标前、图标在名称前"


def test_icons_refresh_on_theme_toggle(env, qapp, monkeypatch):
    """主题深浅切换后列表行与详情页图标按对应侧重解码"""
    from PyQt5.QtGui import QColor, QPixmap

    state = {"dark": False}

    def fake_icon(info, size):
        pix = QPixmap(8, 8)
        pix.fill(QColor("#ff0000") if not state["dark"] else QColor("#00ff00"))
        return pix

    monkeypatch.setattr(page_mod, "_plugin_icon", fake_icon)
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    page._select("browser", force=True)
    _pump(qapp)

    def _pix_color(label):
        img = label.pixmap().toImage()
        return img.pixelColor(0, 0).name()

    assert _pix_color(page._rows[0]._icon) == "#ff0000"
    assert _pix_color(page._detail._icon) == "#ff0000"

    state["dark"] = True
    page.refresh_style()
    assert _pix_color(page._rows[0]._icon) == "#00ff00"
    assert _pix_color(page._detail._icon) == "#00ff00"


def test_detail_frame_bg_refreshes_on_theme_toggle(env, qapp, monkeypatch):
    """主题切换后详情面板背景/边框重读 Colors token（不再停留在旧主题色）"""
    page = page_mod.PluginSettingsPage()
    page.reload(force=True)
    _pump(qapp)
    old_css = page._detail_frame.styleSheet()
    monkeypatch.setattr(page_mod.Colors, "CARD_BG", "rgba(9, 8, 7, {alpha})")
    monkeypatch.setattr(page_mod.Colors, "BORDER", "#010203")
    page.refresh_style()
    new_css = page._detail_frame.styleSheet()
    assert new_css != old_css
    assert "rgba(9, 8, 7, 140)" in new_css and "#010203" in new_css
