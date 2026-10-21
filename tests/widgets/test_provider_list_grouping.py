# -*- coding: utf-8 -*-
"""服务商列表分组渲染回归测试（P0-2）

覆盖：
1. 分组判据 _group_of：OAuth 优先于 Coding Plan；Coding Plan 走 coding_plan_fetcher
   或名称含 "-coding"；本地走 auth_type=none / localhost；其余归 API。
2. 列表渲染顺序：按 OAuth → Coding Plan → 本地 → API 出组头，组内保持配置插入序。
3. 无 provider 声明时判据退化到 info 字段（不抛异常）。
"""

import pytest
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QLabel

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
        # qconfig 全局实例未指向测试配置项，只拦截这两个 item，其余（qfluentwidgets
        # 自己的 themeColor 等）必须透传——否则样式表渲染拿到 str 直接 AttributeError
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


def _headers(card) -> list[str]:
    return [label.text() for label in card._group_headers]


class TestGroupOf:
    """分组判据纯函数"""

    def test_oauth_beats_coding_plan(self, card_factory):
        """login_hook 存在 → OAuth（即使同时声明了 coding_plan_fetcher）"""
        from app.widgets.cards.settings.provider_setting_card import _group_of

        card_factory(
            {},
            [
                ProviderDef(
                    name="CodeBuddy",
                    capabilities={"login_hook": lambda: None},
                    coding_plan_fetcher=lambda c: None,
                )
            ],
        )
        assert _group_of("CodeBuddy", {}) == "OAuth"

    def test_coding_plan_by_fetcher(self, card_factory):
        """coding_plan_fetcher 非空 → Coding Plan"""
        from app.widgets.cards.settings.provider_setting_card import _group_of

        card_factory({}, [ProviderDef(name="智谱AI", coding_plan_fetcher=lambda c: None)])
        assert _group_of("智谱AI", {}) == "Coding Plan"

    def test_coding_plan_by_name_suffix(self, card_factory):
        """未注册插件但名称含 -coding → Coding Plan（自定义命名兜底）"""
        from app.widgets.cards.settings.provider_setting_card import _group_of

        card_factory({}, [])
        assert _group_of("my-coding-plan", {}) == "Coding Plan"
        assert _group_of("My-Coding", {}) == "Coding Plan"

    def test_local_by_auth_type_and_url(self, card_factory):
        """auth_type=none / api_url 含 localhost → 本地"""
        from app.widgets.cards.settings.provider_setting_card import _group_of

        card_factory({}, [])
        assert _group_of("Ollama", {"认证方式": "none"}) == "本地"
        assert _group_of("自建", {"API_URL": "http://localhost:11434/v1"}) == "本地"

    def test_default_is_api(self, card_factory):
        """其余归 API"""
        from app.widgets.cards.settings.provider_setting_card import _group_of

        card_factory({}, [ProviderDef(name="DeepSeek")])
        assert _group_of("DeepSeek", {"API_URL": "https://api.deepseek.com"}) == "API"


class TestGroupedRendering:
    """列表按组渲染"""

    def _providers(self) -> dict:
        return {
            "cid_ds": {"provider_name": "DeepSeek", "API_URL": "https://api.deepseek.com"},
            "cid_cb": {"provider_name": "CodeBuddy", "API_URL": "https://api.codebuddy.com"},
            "cid_zp": {"provider_name": "智谱AI", "API_URL": "https://open.bigmodel.cn"},
            "cid_ollama": {"provider_name": "Ollama", "API_URL": "http://localhost:11434/v1"},
        }

    def test_groups_rendered_in_order(self, card_factory):
        """组头按 OAuth → Coding Plan → 本地 → API 出现，且数量正确"""
        card = card_factory(
            self._providers(),
            [
                ProviderDef(name="CodeBuddy", capabilities={"login_hook": lambda: None}),
                ProviderDef(name="智谱AI", coding_plan_fetcher=lambda c: None),
                ProviderDef(name="DeepSeek"),
                ProviderDef(name="Ollama"),
            ],
        )
        assert _headers(card) == ["OAuth（1）", "Coding Plan（1）", "本地（1）", "API（1）"]

    def test_group_without_members_is_skipped(self, card_factory):
        """空组不出组头"""
        card = card_factory(
            {"cid_ds": {"provider_name": "DeepSeek"}},
            [ProviderDef(name="DeepSeek")],
        )
        assert _headers(card) == ["API（1）"]

    def test_items_after_their_group_header(self, card_factory):
        """每个服务商行必须紧跟在自己组头之后（渲染顺序正确）"""
        from app.widgets.cards.settings.provider_setting_card import ProviderItem

        card = card_factory(
            self._providers(),
            [
                ProviderDef(name="CodeBuddy", capabilities={"login_hook": lambda: None}),
                ProviderDef(name="智谱AI", coding_plan_fetcher=lambda c: None),
                ProviderDef(name="DeepSeek"),
                ProviderDef(name="Ollama"),
            ],
        )
        seq = []
        for i in range(card.viewLayout.count()):
            w = card.viewLayout.itemAt(i).widget()
            if w is None:
                continue
            if isinstance(w, ProviderItem):
                seq.append(w.provider_name)
                continue
            labels = w.findChildren(QLabel)
            if labels and labels[0] in card._group_headers:
                seq.append(f"[{labels[0].text()}]")
        assert seq == [
            "[OAuth（1）]",
            "CodeBuddy",
            "[Coding Plan（1）]",
            "智谱AI",
            "[本地（1）]",
            "Ollama",
            "[API（1）]",
            "DeepSeek",
        ]

    def test_unregistered_provider_falls_back_to_api(self, card_factory):
        """未在插件注册表中登记的服务商（自定义）不抛异常，归 API"""
        card = card_factory(
            {"cid_x": {"provider_name": "我的自建服务", "API_URL": "https://x.example.com/v1"}},
            [],
        )
        assert _headers(card) == ["API（1）"]
