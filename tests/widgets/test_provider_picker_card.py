# -*- coding: utf-8 -*-
"""P1-7 回归：服务商预置卡片墙

背景
----
「添加服务商」原先直接进 831 行手写表单，用户要先在下拉里翻找服务商、再手工
填 URL / 认证方式 / 模型名。卡片墙把「选哪个服务商」前置成一眼可辨的一步：
每张卡 = 一个插件声明的服务商（图标 + 名称 + 一句话），点击即以预置参数进入
表单，用户只补 API_KEY；末尾固定「自定义服务商」入口走原手工表单。

本组覆盖：
1. 分组渲染：带 login_hook 的 def 归 OAuth 组（复用 _group_of）
2. 空注册表不崩（只出「自定义」入口）
3. 「自定义」入口发 customPicked 信号（不走 providerPicked）
4. 预置摘要 preset_provider_summary 取插件声明值（URL / 认证方式 / 默认模型）
5. ProviderEditCard(preset_provider=...) 锁定服务商 + 预填 + URL 只读
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt5.QtWidgets import QApplication

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="module")
def _qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    return app


@pytest.fixture()
def registry(monkeypatch):
    """隔离注册表"""
    reg = ProviderRegistry()
    monkeypatch.setattr(ProviderRegistry, "_instance", reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


# ══════════════════════════════════════════════════════════════════
# 卡片墙渲染
# ══════════════════════════════════════════════════════════════════


class TestPickerRendering:
    def test_tiles_rendered_per_provider(self, _qapp, registry):
        """每个已声明服务商一张卡 + 固定「自定义」入口"""
        from app.widgets.cards.settings.provider_picker_card import CUSTOM_ENTRY, ProviderPickerCard

        registry.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
        registry.register(ProviderDef(name="CodeBuddy", api_url="https://copilot.tencent.com"), source="plugin:test")

        card = ProviderPickerCard()
        names = card.provider_names()
        assert "DeepSeek" in names
        assert "CodeBuddy" in names
        assert names[-1] == CUSTOM_ENTRY, "自定义入口固定末位"

    def test_login_hook_provider_in_oauth_group(self, _qapp, registry):
        """带 login_hook 的 def 归 OAuth 组（复用 _group_of 判据，零硬编码）"""
        from PyQt5.QtWidgets import QLabel

        from app.widgets.cards.settings.provider_picker_card import ProviderPickerCard
        from app.widgets.cards.settings.provider_setting_card import _group_of

        registry.register(
            ProviderDef(name="CodeBuddy", capabilities={"login_hook": lambda: None}),
            source="plugin:test",
        )
        card = ProviderPickerCard()
        assert _group_of("CodeBuddy", {}) == "OAuth"
        # 组头文本出现在页面上（OAuth（1））
        labels = [lb.text() for lb in card.findChildren(QLabel)]
        assert any("OAuth" in t for t in labels), f"应有 OAuth 组头，实际标签: {labels}"

    def test_empty_registry_only_custom_entry(self, _qapp, monkeypatch):
        """空注册表不崩，只出「自定义」入口"""
        from app.widgets.cards.settings import provider_picker_card as mod

        # 注意：注册表 all() 会触发 ensure_loaded 扫描真实插件目录（12 家内置），
        # 要构造「真空」必须连 all() 一起打桩（仅换 _instance 不够）。
        monkeypatch.setattr(
            ProviderRegistry,
            "get_instance",
            classmethod(lambda cls: SimpleNamespace(all=lambda: [])),
        )
        card = mod.ProviderPickerCard()
        assert card.provider_names() == [mod.CUSTOM_ENTRY]

    def test_registry_failure_does_not_crash(self, _qapp, monkeypatch):
        """注册表抛异常 → 卡片墙降级为空（只留自定义入口），不崩"""
        from app.widgets.cards.settings import provider_picker_card as mod

        monkeypatch.setattr(
            ProviderRegistry,
            "get_instance",
            classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("boom"))),
        )
        card = mod.ProviderPickerCard()
        assert card.provider_names() == [mod.CUSTOM_ENTRY]


class TestTileFixedWidth:
    """用户裁决（第三轮）：限制的是卡片本体宽度——tile setFixedSize 固定，
    容器多宽 tile 都不跟（系统配置卡家族 setFixedWidth 同款写法）"""

    def test_tile_size_constant(self, _qapp, registry):
        """tile 固定 176×68，构造即定，无 width 参数"""
        from app.widgets.cards.settings.provider_picker_card import _CARD_H, _CARD_W, ProviderPickerCard

        registry.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
        card = ProviderPickerCard()
        for tile in card.tiles():
            assert (tile.width(), tile.height()) == (_CARD_W, _CARD_H), "tile 尺寸应为固定常量"
            assert tile.maximumWidth() == _CARD_W, "tile 必须有宽度上限约束（setFixedSize 副作用）"

    def test_tile_width_invariant_under_container_resize(self, _qapp, registry):
        """红线：容器 resize（含 1920 宽）后 tile 宽度恒定不变"""
        from app.widgets.cards.settings.provider_picker_card import _CARD_W, ProviderPickerCard

        registry.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
        card = ProviderPickerCard()
        card.resize(700, 400)
        _qapp.processEvents()
        card.resize(1280, 400)
        _qapp.processEvents()
        card.resize(1920, 400)
        _qapp.processEvents()
        for tile in card.tiles():
            assert tile.width() == _CARD_W, f"容器拉宽到 1920 后 tile 宽度不得变（实际 {tile.width()}）"


# ══════════════════════════════════════════════════════════════════
# 信号路由
# ══════════════════════════════════════════════════════════════════


class TestPickerSignals:
    def test_preset_tile_emits_provider_picked(self, _qapp, registry):
        """点预置卡 → providerPicked(name)，不发 customPicked"""
        from app.widgets.cards.settings.provider_picker_card import ProviderPickerCard

        registry.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
        card = ProviderPickerCard()

        picked, custom = [], []
        card.providerPicked.connect(picked.append)
        card.customPicked.connect(lambda: custom.append(1))

        tile = next(t for t in card.tiles() if t.provider_name == "DeepSeek")
        tile.picked.emit("DeepSeek")

        assert picked == ["DeepSeek"]
        assert custom == [], "预置卡不得触发自定义路径"

    def test_custom_tile_emits_custom_picked(self, _qapp, registry):
        """点自定义卡 → customPicked()，不发 providerPicked"""
        from app.widgets.cards.settings.provider_picker_card import CUSTOM_ENTRY, ProviderPickerCard

        card = ProviderPickerCard()
        picked, custom = [], []
        card.providerPicked.connect(picked.append)
        card.customPicked.connect(lambda: custom.append(1))

        tile = next(t for t in card.tiles() if t.provider_name == CUSTOM_ENTRY)
        tile.picked.emit(CUSTOM_ENTRY)

        assert custom == [1]
        assert picked == [], "自定义卡不得走预置路径"


# ══════════════════════════════════════════════════════════════════
# 预置摘要
# ══════════════════════════════════════════════════════════════════


class TestPresetSummary:
    def test_summary_from_declaration(self, _qapp, registry):
        """摘要取插件声明值：preset_urls 首位 / auth_type / default_model"""
        from app.widgets.cards.settings.provider_picker_card import preset_provider_summary

        registry.register(
            ProviderDef(
                name="百度千帆",
                api_url="https://qianfan.baidubce.com/v2",
                preset_urls=["https://qianfan.baidubce.com/v2", "https://qianfan.baidubce.com/v2/chat"],
                auth_type="bce",
                default_model="ernie-4.0",
            ),
            source="plugin:test",
        )
        s = preset_provider_summary("百度千帆")
        assert s is not None
        assert s["API_URL"] == "https://qianfan.baidubce.com/v2"
        assert s["认证方式"] == "bce", "认证方式必须走 get_auth_type（插件声明优先）"
        assert s["模型名称"] == "ernie-4.0"

    def test_summary_none_for_unknown(self, _qapp, registry):
        from app.widgets.cards.settings.provider_picker_card import preset_provider_summary

        assert preset_provider_summary("不存在") is None


# ══════════════════════════════════════════════════════════════════
# ProviderEditCard 预置态
# ══════════════════════════════════════════════════════════════════


class TestEditCardPreset:
    def test_preset_locks_provider_and_prefills(self, _qapp, registry):
        """预置态：服务商改静态展示（combo 隐藏）+ URL/模型预填 + URL 只读 + 仅 API_KEY 可编辑"""
        from PyQt5.QtWidgets import QLabel

        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        registry.register(
            ProviderDef(
                name="百度千帆",
                api_url="https://qianfan.baidubce.com/v2",
                auth_type="bce",
                default_model="ernie-4.0",
            ),
            source="plugin:test",
        )
        card = ProviderEditCard(provider_name="", provider_info={}, is_new=True, preset_provider="百度千帆")

        assert card.nameCombo.currentText() == "百度千帆", "combo 仍持值（保存链读它）"
        assert card.nameCombo.isHidden(), "预置态 combo 应隐藏（改静态展示行）"
        # 静态行：加粗服务商名 + 「预置服务商」小标
        labels = [lb.text() for lb in card.findChildren(QLabel)]
        assert "百度千帆" in labels, "静态行应显示服务商名"
        assert "预置服务商" in labels, "静态行应带「预置服务商」小标"
        assert card.apiUrlCombo.currentText() == "https://qianfan.baidubce.com/v2", "URL 应预填"
        assert not card.apiUrlCombo.isEnabled(), "预置态 URL 应只读"
        assert card.modelListEditor.getDefaultModel() == "ernie-4.0", "默认模型应预填"
        assert card.apiKeyEdit.isEnabled(), "API_KEY 必须可编辑（唯一需用户填的）"

    def test_preset_static_row_has_icon(self, _qapp, registry):
        """静态行含服务商图标（用户反馈：禁用下拉无 icon）"""
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard
        from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget

        registry.register(ProviderDef(name="百度千帆", api_url="https://q.example.com/v2"), source="plugin:test")
        card = ProviderEditCard(provider_name="", provider_info={}, is_new=True, preset_provider="百度千帆")

        icons = [w for w in card.findChildren(ProviderIconWidget) if w.provider_name == "百度千帆"]
        assert icons, "静态行应有该服务商的图标控件"

    def test_preset_does_not_wash_prefill(self, _qapp, registry):
        """关键回归：setCurrentIndex 的 _on_provider_changed 不得洗掉预填

        坑：nameCombo.setCurrentIndex 会触发 _on_provider_changed，它内部会重写
        配置名并重载预设 URL。若不加 blockSignals，预填的 URL / 模型会被洗成
        下拉首项的值（本项最大翻车点）。
        """
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        registry.register(
            ProviderDef(
                name="阿里云 (DashScope)",
                api_url="https://token-plan.example.com/v1",
                preset_urls=["https://token-plan.example.com/v1"],
                default_model="qwen3.5-plus",
            ),
            source="plugin:test",
        )
        card = ProviderEditCard(provider_name="", provider_info={}, is_new=True, preset_provider="阿里云 (DashScope)")
        assert card.apiUrlCombo.currentText() == "https://token-plan.example.com/v1"
        assert card.modelListEditor.getDefaultModel() == "qwen3.5-plus"

    def test_no_preset_keeps_manual_flow(self, _qapp, registry):
        """无预置（自定义路径）：下拉可编辑，走原手工流程"""
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        registry.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
        card = ProviderEditCard(provider_name="", provider_info={}, is_new=True, preset_provider="")

        assert card.preset_provider == ""
        assert card.nameCombo.isEnabled(), "自定义路径下拉必须可编辑"
        assert card.apiUrlCombo.isEnabled(), "自定义路径 URL 必须可编辑"

    def test_edit_mode_unaffected_by_preset_param(self, _qapp, registry):
        """编辑既有配置：preset_provider 不生效（编辑路径不受影响）"""
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        registry.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
        info = {"provider_name": "DeepSeek", "API_URL": "https://api.deepseek.com", "API_KEY": "sk-old"}
        card = ProviderEditCard(provider_name="DeepSeek", provider_info=info, is_new=False, preset_provider="其他家")

        assert card.apiUrlCombo.currentText() == "https://api.deepseek.com"
        assert card.apiKeyEdit.text() == "sk-old"
        assert card.apiUrlCombo.isEnabled(), "编辑态 URL 必须可编辑（预置只读逻辑不得外溢）"


class TestLocalGroup:
    """R6-1：「本地」组此前永不命中（传空 info 时判据拿不到 auth_type/url）"""

    def test_local_provider_lands_in_local_group(self, _qapp, monkeypatch):
        """认证方式 none 的服务商（Ollama 类）应落「本地」组"""
        from PyQt5.QtWidgets import QLabel

        from app.widgets.cards.settings.provider_picker_card import ProviderPickerCard

        reg = ProviderRegistry()
        reg.register(
            ProviderDef(name="Ollama", api_url="http://localhost:11434/v1", auth_type="none"),
            source="plugin:test",
        )
        reg.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

        card = ProviderPickerCard()
        labels = [lb.text() for lb in card.findChildren(QLabel)]
        assert "本地（1）" in labels, f"本地组应出现且计 1，实际组头: {[t for t in labels if '（' in t]}"

    def test_localhost_url_lands_in_local_group(self, _qapp, monkeypatch):
        """api_url 含 localhost（即使 auth_type 非 none）也应落「本地」组"""
        from PyQt5.QtWidgets import QLabel

        from app.widgets.cards.settings.provider_picker_card import ProviderPickerCard

        reg = ProviderRegistry()
        reg.register(
            ProviderDef(name="LM Studio", api_url="http://localhost:1234/v1", auth_type="bearer"),
            source="plugin:test",
        )
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

        card = ProviderPickerCard()
        labels = [lb.text() for lb in card.findChildren(QLabel)]
        assert "本地（1）" in labels, f"localhost URL 应落本地组，实际: {labels}"
