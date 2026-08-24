# -*- coding: utf-8 -*-
"""声明式配置自动渲染卡（QApplication 离屏渲染）+ PluginManager 接线。"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication

from app.plugins.contracts.plugin_config import parse_config_schema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.widgets.cards.settings.plugin_config_card import (
    PluginConfigCard,
    make_card_class,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def schema_env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    reg = PluginConfigRegistry.get_instance()
    reg.register(
        parse_config_schema(
            "plug-ui",
            {
                "title": "UI 测试",
                "fields": [
                    {"key": "name", "label": "名称", "type": "text", "default": "abc"},
                    {"key": "secret", "label": "密钥", "type": "password", "default": "sk-1"},
                    {"key": "on", "label": "开关", "type": "bool", "default": False},
                ],
            },
        )
    )
    yield tmp_path
    reg.unregister_plugin("plug-ui")


def test_card_renders_all_field_rows(qapp, schema_env):
    card = PluginConfigCard("plug-ui")
    # 三个字段控件全部注册（折叠卡结构，去掉底部 save_btn）
    assert card._rows["name"] is not None
    assert card._rows["secret"] is not None
    assert card._rows["on"] is not None
    assert not hasattr(card, "save_btn")


def test_card_is_single_expand_card(qapp, schema_env):
    """一插件一项：整个 schema 合并为一张 ExpandSettingCard"""
    from qfluentwidgets import ExpandSettingCard

    card = PluginConfigCard("plug-ui")
    assert isinstance(card, ExpandSettingCard)
    # 卡片默认折叠（点开才看到字段）
    assert card.isExpand is False


def test_card_echoes_effective_values(qapp, schema_env):
    card = PluginConfigCard("plug-ui")
    assert card._rows["name"].text() == "abc"  # 默认值回显
    assert card._rows["secret"].text() == "sk-1"
    assert card._rows["on"].isChecked() is False


def test_text_field_persists_on_editing_finished(qapp, schema_env):
    """文本字段：editingFinished 信号触发即时保存（无需手动保存按钮）"""
    card = PluginConfigCard("plug-ui")
    card._rows["name"].setText("changed")
    # editingFinished 通常在失焦时触发，模拟信号发射
    card._rows["name"].editingFinished.emit()
    assert PluginConfigStore().get("plug-ui", "name") == "changed"


def test_bool_field_persists_on_toggle(qapp, schema_env):
    """bool 字段：checkedChanged 信号触发即时保存"""
    card = PluginConfigCard("plug-ui")
    card._rows["on"].setChecked(True)
    # checkedChanged 信号已在 setChecked 时自动发射（lambda 已连）
    assert PluginConfigStore().get("plug-ui", "on") is True


def test_password_field_uses_password_line_edit(qapp, schema_env):
    """password 字段使用 PasswordLineEdit（继承 QLineEdit.Password echoMode）"""
    from qfluentwidgets import PasswordLineEdit

    card = PluginConfigCard("plug-ui")
    assert isinstance(card._rows["secret"], PasswordLineEdit)
    # 默认 Password echo mode（不显示明文）
    from PyQt5.QtWidgets import QLineEdit

    assert card._rows["secret"].echoMode() == QLineEdit.Password


def test_clear_text_saves_empty_and_echoes_default(qapp, schema_env):
    """清空输入 → editingFinished → store 清除 → 字段回显 schema default（对齐旧语义）"""
    card = PluginConfigCard("plug-ui")
    # 先存一个临时值
    PluginConfigStore().set_values("plug-ui", {"name": "tmp"})
    card._rows["name"].setText("")
    card._rows["name"].editingFinished.emit()
    # 回退到 schema default "abc"
    assert PluginConfigStore().get("plug-ui", "name") == "abc"
    assert card._rows["name"].text() == "abc"


def test_noop_editing_finished_keeps_external_changes(qapp, schema_env):
    """聚焦→失焦但内容未改：不写盘，保留外部/其他实例对 config.json 的修改

    回归：editingFinished 在聚焦→失焦（未编辑）时也会触发；若输入框内容与
    回显基线一致（用户没真正修改），写回会把外部手动编辑的值覆盖掉。
    """
    store = PluginConfigStore()
    card = PluginConfigCard("plug-ui")
    assert card._rows["name"].text() == "abc"  # 回显基线
    # 外部修改（模拟用户手动编辑 config.json / 另一 DriFox 实例写入）
    store.set_values("plug-ui", {"name": "external"})
    # 用户点击输入框后失焦：editingFinished 触发，但输入框内容仍是旧回显 abc
    card._rows["name"].editingFinished.emit()
    # 外部修改未被覆盖
    assert store.get("plug-ui", "name") == "external"
    assert card._rows["name"].text() == "abc"  # UI 仍显示旧值，下次真正编辑才保存


def test_make_card_class_zero_arg_construction(qapp, schema_env):
    cls = make_card_class("plug-ui")
    widget = cls()  # register_settings_card 的 widget_class 约定：无参构造
    assert isinstance(widget, PluginConfigCard)
    assert widget._plugin_name == "plug-ui"


def test_card_without_schema_renders_empty(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    card = PluginConfigCard("never-registered")
    assert card._rows == {}


def test_plugin_manager_registers_config_schema(tmp_path, monkeypatch):
    """plugin.json 含 config_schema → 扫描后注册表可见 + 设置卡已注册"""
    import json

    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    plug_dir = tmp_path / "plug-cfg"
    (plug_dir / ".drifox-plugin").mkdir(parents=True)
    (plug_dir / ".drifox-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "plug-cfg",
                "version": "1.0.0",
                "config_schema": {
                    "title": "C 卡",
                    "fields": [{"key": "k", "label": "K", "type": "text"}],
                },
            }
        ),
        encoding="utf-8",
    )

    from app.plugins.managers.plugin_manager import PluginManager

    pm = PluginManager()
    pm._scan_one_plugin_dir(plug_dir, "user")

    reg = PluginConfigRegistry.get_instance()
    try:
        assert reg.get("plug-cfg") is not None
        ui = UIPluginRegistry.get_instance()
        cards = [c.card_id for c in ui.get_settings_cards()]
        assert "plug-cfg-config" in cards
    finally:
        # 清理：保留测试隔离
        reg.unregister_plugin("plug-cfg")
        # 清理自动卡注册（保持 UI registry 干净）
        UIPluginRegistry.get_instance()._settings_cards.pop("plug-cfg-config", None)


# ── L2 扩展类型：select / number / textarea ──


@pytest.fixture()
def rich_schema(tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    reg = PluginConfigRegistry.get_instance()
    reg.register(
        parse_config_schema(
            "plug-rich",
            {
                "title": "富配置",
                "fields": [
                    {
                        "key": "mode",
                        "label": "模式",
                        "type": "select",
                        "default": "a",
                        "options": {"a": "模式A", "b": "模式B"},
                    },
                    {
                        "key": "retry",
                        "label": "重试次数",
                        "type": "number",
                        "default": 3,
                        "min": 1,
                        "max": 10,
                        "step": 1,
                    },
                    {"key": "note", "label": "备注", "type": "textarea", "placeholder": "选填", "rows": 4},
                ],
            },
        )
    )
    yield tmp_path
    reg.unregister_plugin("plug-rich")


def test_select_options_parse_variants():
    """select options 三种声明形态解析一致（dict / list[str] / list[dict]）"""
    from app.plugins.contracts.plugin_config import _parse_select_options

    assert _parse_select_options({"a": "A", "b": "B"}) == (("a", "A"), ("b", "B"))
    assert _parse_select_options(["a", "b"]) == (("a", "a"), ("b", "b"))
    assert _parse_select_options([{"value": "a", "label": "A"}]) == (("a", "A"),)
    assert _parse_select_options(None) == ()
    assert _parse_select_options("bad") == ()


def test_rich_types_parse_metadata():
    """number/textarea 元数据解析（字符串数字宽容转 int）"""
    schema = parse_config_schema(
        "plug-meta",
        {
            "title": "元数据",
            "fields": [
                {"key": "n", "type": "number", "min": "1", "max": "9", "step": "2"},
                {"key": "t", "type": "textarea", "rows": "6"},
            ],
        },
    )
    f_n = schema.get_field("n")
    assert (f_n.min, f_n.max, f_n.step) == (1, 9, 2)
    assert schema.get_field("t").rows == 6


def test_select_without_options_rejects_schema():
    """select 缺 options → 整个 schema 忽略（容错，不影响插件加载）"""
    schema = parse_config_schema(
        "plug-bad",
        {"title": "坏", "fields": [{"key": "m", "label": "模式", "type": "select"}]},
    )
    assert schema is None


def test_rich_types_render(qapp, rich_schema):
    """select/number/textarea 渲染为对应控件"""
    from PyQt5.QtWidgets import QTextEdit
    from qfluentwidgets import SpinBox

    from app.widgets.cards.settings.plugin_config_card import SelectPillsRow

    card = PluginConfigCard("plug-rich")
    assert isinstance(card._rows["mode"], SelectPillsRow)
    assert isinstance(card._rows["retry"], SpinBox)
    assert isinstance(card._rows["note"], QTextEdit)
    # textarea 行高按 rows 声明（4 行）
    assert card._rows["note"].height() >= 4 * 22


def test_rich_types_echo_defaults(qapp, rich_schema):
    """回显：select 按 value 选中、number 取 int、textarea 取文本"""
    card = PluginConfigCard("plug-rich")
    assert card._rows["mode"].currentData() == "a"
    assert card._rows["retry"].value() == 3
    assert card._rows["note"].toPlainText() == ""


def test_select_persists_on_change(qapp, rich_schema):
    """select 切换 → valueChanged 即时保存存储 value（非 label）"""
    card = PluginConfigCard("plug-rich")
    card._rows["mode"]._on_pill_clicked("b")  # 模拟点击 pill（变化才发射信号）
    assert PluginConfigStore().get("plug-rich", "mode") == "b"


def test_number_persists_on_change(qapp, rich_schema):
    """number 改值 → valueChanged 即时保存（存储为 str，与 text 字段一致）"""
    card = PluginConfigCard("plug-rich")
    card._rows["retry"].setValue(7)
    assert PluginConfigStore().get("plug-rich", "retry") == "7"


def test_number_uses_schema_range(qapp, rich_schema):
    """number 应用 schema 的 min/max 范围"""
    card = PluginConfigCard("plug-rich")
    spin = card._rows["retry"]
    assert (spin.minimum(), spin.maximum()) == (1, 10)


def test_textarea_persists_on_focus_out(qapp, rich_schema):
    """textarea → editingFinished（focusOut 触发）即时保存"""
    card = PluginConfigCard("plug-rich")
    card._rows["note"].setPlainText("hello world")
    card._rows["note"].editingFinished.emit()
    assert PluginConfigStore().get("plug-rich", "note") == "hello world"


def test_echo_flag_blocks_signal_loop(qapp, rich_schema):
    """回显期间置 _echoing：setCurrentIndex/setValue 不触发写盘（防循环/误写）"""
    store = PluginConfigStore()
    store.set_values("plug-rich", {"mode": "b", "retry": "9"})
    PluginConfigCard("plug-rich")  # 构造回显不应写盘
    assert store.get("plug-rich", "mode") == "b"
    assert store.get("plug-rich", "retry") == "9"
