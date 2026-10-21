# -*- coding: utf-8 -*-
"""服务商列表组头可见性回归测试

根因背景：_make_group_header 曾不显式 show() 组头。首开设置卡时 showEvent 的
_refresh_items 重建行发生在卡片树首帧布局前，组头停在 hidden 态——QLayout.sizeHint()
会剔除 hidden 项，紧随其后的自动展开（_expand_page_cards → _expanded_height）按
缺组头的高度定格，列表底部被裁掉一块（3 组头 × 27px）。折叠再展开时组头已被布局
周期 show 出来，量高完整，表现为「再次打开或折叠展开后恢复正常」。
"""

import pytest
from PyQt5.QtGui import QIcon

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


@pytest.fixture()
def card_factory(monkeypatch, qapp):
    """构造 ProviderListSettingCard，注册表与配置项均为隔离替身"""
    from qfluentwidgets import ConfigItem

    from app.widgets.cards.settings import provider_setting_card as mod

    def _make(providers: dict, declared: list) -> "mod.ProviderListSettingCard":
        reg = ProviderRegistry()
        for p in declared:
            reg.register(p, source="plugin:test")
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

        cfg_item = ConfigItem("Test", "SavedProviders", {})
        default_item = ConfigItem("Test", "SelectedModel", "")
        orig_get = mod.qconfig.get
        orig_set = mod.qconfig.set

        def _fake_get(item):
            if item is cfg_item:
                return providers
            if item is default_item:
                return ""
            return orig_get(item)

        monkeypatch.setattr(mod.qconfig, "get", _fake_get)
        monkeypatch.setattr(mod.qconfig, "set", lambda *a, **k: None)
        card = mod.ProviderListSettingCard(
            icon=QIcon(),
            configItem=cfg_item,
            defaultProviderItem=default_item,
            title="已保存的服务商",
        )
        return card

    return _make


def test_group_headers_not_hidden_after_rebuild(card_factory):
    """重建行后组头不得处于 hidden 态（hidden 项会被 QLayout.sizeHint 剔除）"""
    providers = {
        "p1": {"provider_name": "Alpha", "API_URL": "https://a.example.com", "模型列表": ["m1"]},
        "p2": {"provider_name": "Beta", "API_URL": "https://b.example.com", "模型列表": ["m1"]},
    }
    card = card_factory(
        providers,
        [
            ProviderDef(name="Alpha"),
            ProviderDef(name="Beta"),
        ],
    )
    card._refresh_items()
    # 对齐真实首开时序：showEvent 里的 _refresh_items 发生在卡片可见后，
    # 此时新增子控件默认 hidden（必须显式 show）；卡片未显示时子控件
    # 默认「随父显示」不触发该问题，测不出根因
    card.show()
    card._refresh_items()
    headers = card._group_headers
    # Alpha/Beta 判据一致（无 login_hook / coding_plan_fetcher，URL 非 localhost）→ 同归 API 组
    assert len(headers) == 1
    for label in headers:
        assert not label.parentWidget().isHidden(), f"组头 hidden 会使其被 sizeHint 剔除: {label.text()}"
    # 展开高度量测把组头计入：viewLayout.sizeHint 不小于「组头 + 行 + 搜索框」之和
    hint = card.viewLayout.sizeHint().height()
    items_sum = sum(
        card.viewLayout.itemAt(i).widget().sizeHint().height()
        for i in range(card.viewLayout.count())
        if card.viewLayout.itemAt(i).widget() is not None and not card.viewLayout.itemAt(i).widget().isHidden()
    )
    assert hint >= items_sum
