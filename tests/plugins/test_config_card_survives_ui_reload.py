# -*- coding: utf-8 -*-
"""回归：config_schema 自动设置卡在 ui 组件热重载（unload→load）中不被误杀。

2026-09-14 安装/更新插件后设置页配置卡不实时刷新：targeted 重载链
「rescan_plugin 注册卡 → ui unload_plugin 清卡」导致卡从注册表消失，
直到下一次全量扫描才回来。修复：卡注册时打 metadata.auto_config_card，
unload_plugin 保留之；清理收敛到 PluginManager._unregister_config_schema
（manifest 层职责）单一入口。

三用例：unload 保留自动卡且照清普通卡 / 显式清理方法精确移除 /
manifest 删除 schema 后历史注册被清。
"""
import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

PLUGIN = "cfg-card-regress"


def _card_ids(reg: UIPluginRegistry) -> set:
    return {info.card_id for info in reg.get_settings_cards()}


class _FakeWidget:
    """register_settings_card 只存类引用不实例化，占位即可。"""


@pytest.fixture()
def reg():
    r = UIPluginRegistry.get_instance()
    yield r
    r._settings_cards = {k: v for k, v in r._settings_cards.items() if v.plugin_name != PLUGIN}
    for region in r._regions.values():
        region["entries"] = {k: v for k, v in region["entries"].items() if v.plugin_name != PLUGIN}
    try:
        from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

        PluginConfigRegistry.get_instance().unregister_plugin(PLUGIN)
    except Exception:
        pass


def test_unload_keeps_auto_config_card(reg):
    """unload_plugin：自动卡保留（ui 重载不再误杀），普通卡照旧清理。"""
    reg.register_settings_card(
        PLUGIN, f"{PLUGIN}-config", "自动卡", _FakeWidget, metadata={"auto_config_card": True}
    )
    reg.register_settings_card(PLUGIN, f"{PLUGIN}-manual", "手动卡", _FakeWidget)
    reg.unload_plugin(PLUGIN)
    ids = _card_ids(reg)
    assert f"{PLUGIN}-config" in ids
    assert f"{PLUGIN}-manual" not in ids


def test_unregister_auto_config_cards_explicit(reg):
    """显式清理只移除自动卡，不动同插件的手动注册卡。"""
    reg.register_settings_card(
        PLUGIN, f"{PLUGIN}-config", "自动卡", _FakeWidget, metadata={"auto_config_card": True}
    )
    reg.register_settings_card(PLUGIN, f"{PLUGIN}-manual", "手动卡", _FakeWidget)
    reg.unregister_auto_config_cards(PLUGIN)
    ids = _card_ids(reg)
    assert f"{PLUGIN}-config" not in ids
    assert f"{PLUGIN}-manual" in ids


def test_register_schema_missing_clears_history(reg):
    """manifest 删除 config_schema 后，历史自动卡与 schema 注册一并被清。"""
    from app.plugins.contracts.plugin_config import parse_config_schema
    from app.plugins.managers.plugin_manager import PluginManager
    from app.plugins.registries.plugin_config_registry import PluginConfigRegistry

    pm = PluginManager.__new__(PluginManager)  # _register_config_schema 不依赖实例状态
    manifest_with = {
        "title": "t",
        "config_schema": {"title": "t", "fields": [{"key": "k", "label": "l", "type": "text"}]},
    }
    schema = parse_config_schema(PLUGIN, manifest_with["config_schema"])
    assert schema is not None

    pm._register_config_schema(PLUGIN, manifest_with)
    assert f"{PLUGIN}-config" in _card_ids(reg)
    assert PluginConfigRegistry.get_instance().get(PLUGIN) is not None

    pm._register_config_schema(PLUGIN, {"title": "t"})  # schema 已删除
    assert f"{PLUGIN}-config" not in _card_ids(reg)
    assert PluginConfigRegistry.get_instance().get(PLUGIN) is None
