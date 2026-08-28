# -*- coding: utf-8 -*-
"""
界面字号刻度条（FontSizeStepperCard / _FontStepTrack）+ 档位 delta 化迁移测试

- 挡位范围 -5px..+10px（16 挡，点击节点直达）
- 旧档位键（small/medium/large/superlarge）自动迁移到 delta 键
- scale_font_size 按 delta 生效
"""

from types import SimpleNamespace

from qfluentwidgets import FluentIcon, OptionsConfigItem, OptionsValidator

from app.utils import design_tokens
from app.utils.config import Settings
from app.widgets.cards.settings.llm_settings_card import FontSizeStepperCard, _font_step_text


class _FakeCfg:
    """最小 cfg 假体：ui_font_size 真实 OptionsConfigItem（不落盘）+ set 直写"""

    def __init__(self, initial="2"):
        self.ui_font_size = OptionsConfigItem(
            "UI", "FontSize", initial, OptionsValidator([str(d) for d in range(-5, 11)])
        )
        self.set_calls = []

    def set(self, item, value, save=True):
        self.set_calls.append((item, value))
        item.value = value


def _make_card(qapp, initial="2"):
    cfg = _FakeCfg(initial)
    card = FontSizeStepperCard(FluentIcon.FONT, "界面字号", "统一调整界面与对话内容字号", cfg)
    return card, cfg


def test_stepper_has_16_steps(qapp):
    """挡位范围 -5..+10 共 16 挡，显示文本正数带 + 前缀"""
    card, _ = _make_card(qapp)
    assert card._track._steps == [str(d) for d in range(-5, 11)]
    assert _font_step_text("-3") == "-3"
    assert _font_step_text("0") == "0"
    assert _font_step_text("7") == "+7"
    assert _font_step_text("10") == "+10"


def test_stepper_click_sets_delta(qapp):
    """点击节点 → cfg.set(delta 键) + 节点高亮/当前值同步"""
    card, cfg = _make_card(qapp, initial="0")
    card._track.stepClicked.emit("3")
    assert cfg.set_calls == [(cfg.ui_font_size, "3")]
    assert cfg.ui_font_size.value == "3"
    # valueChanged 链驱动高亮与 header 当前值
    assert card._value_label.text() == "+3 px"
    assert card._track._current == "3"


def test_stepper_click_same_step_noop(qapp):
    """点击当前档位 → 不写配置"""
    card, cfg = _make_card(qapp, initial="2")
    card._track.stepClicked.emit("2")
    assert cfg.set_calls == []


def test_stepper_index_at_maps_nearest(qapp):
    """x 坐标 → 最近节点索引（点击命中）"""
    card, _ = _make_card(qapp)
    card._track.resize(400, 38)
    idx = card._track._index_at(18)  # 首节点位置
    assert idx == 0
    idx = card._track._index_at(382)  # 末节点位置（400-18）
    assert idx == 15
    # 中点 → 最近节点（15 挡跨度中点 ≈ 7.5 → round 8）
    assert card._track._index_at(200) in (7, 8)


def test_stepper_legacy_value_mapped(qapp):
    """旧档位键（large）→ 显示 +2 高亮（读取兜底映射，不写盘）

    OptionsConfigItem 的 value setter 全程走 validator.correct，运行时不会
    持有旧键；此处替换读值源验证 _sync_current 的兜底映射分支（双保险）。
    """
    card, cfg = _make_card(qapp, initial="0")
    assert card._value_label.text() == "0 px"
    cfg.ui_font_size = SimpleNamespace(value="large")
    card._sync_current()
    assert card._value_label.text() == "+2 px"
    assert card._track._current == "2"


def test_font_size_options_delta_mapping():
    """design_tokens：旧键迁移映射 + 默认档位 + delta 生效"""
    assert design_tokens._LEGACY_FONT_SIZE_KEYS == {
        "small": "-1",
        "medium": "0",
        "large": "2",
        "superlarge": "4",
    }
    assert design_tokens._DEFAULT_FONT_SIZE_KEY == "2"
    opts = design_tokens.FONT_SIZE_OPTIONS
    assert opts["0"]["delta"] == 0 and opts["0"]["base"] == 14
    assert opts["-5"]["delta"] == -5
    assert opts["10"]["delta"] == 10


def test_scale_font_size_follows_delta(monkeypatch):
    """scale_font_size 按当前档位 delta 缩放（superlarge → +4）"""
    monkeypatch.setattr(design_tokens, "_cached_font_size_key", None)
    fake = SimpleNamespace(ui_font_size=SimpleNamespace(value="superlarge"))
    monkeypatch.setattr("app.utils.config.Settings.get_instance", lambda: fake)
    assert design_tokens.scale_font_size(14) == 18  # superlarge → delta +4
    assert design_tokens.get_ui_font_size_key() == "4"
    # 缓存清理，避免影响其他用例
    monkeypatch.setattr(design_tokens, "_cached_font_size_key", None)


def test_nav_btn_style_follows_font_delta(monkeypatch):
    """导航按钮 QSS 内嵌 font_size_css：档位变化后重建的样式用新字号

    （回归保护：旧 QSS 压制 setFont，字号切换须重建按钮样式才生效）
    """
    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    def _style_at(delta_key):
        monkeypatch.setattr(design_tokens, "_cached_font_size_key", None)
        fake = SimpleNamespace(ui_font_size=SimpleNamespace(value=delta_key))
        monkeypatch.setattr("app.utils.config.Settings.get_instance", lambda: fake)
        return LLMSettingsCard._nav_btn_style(True)

    assert "font-size: 12px;" in _style_at("0")  # 导航基准 12 + delta 0
    assert "font-size: 17px;" in _style_at("5")  # 12 + 5
    monkeypatch.setattr(design_tokens, "_cached_font_size_key", None)


def test_config_changed_rebuilds_nav_style_on_font_size(qapp, monkeypatch):
    """_last_change_type ∈ {font_size, font_family} → _on_config_changed 即时重建导航样式

    （回归保护：字号/字族切换即时重建导航 QSS，不等切 tab。
    注：不端到端走 valueChanged —— 骨架 __new__ 绕过 QObject.__init__，
    sender() 对未初始化 QObject 未定义，sender 判定链由生产路径保证）
    """
    from PySide6.QtWidgets import QLabel

    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    card = LLMSettingsCard.__new__(LLMSettingsCard)
    card.cfg = Settings.get_instance()
    card._nav_buttons = {"appearance": QLabel("外观样式")}

    # __new__ 骨架未初始化 QObject：类级 Signal 的绑定 emit 不可用、
    # sender() 抛 RuntimeError，均替换掉让 _on_config_changed 全流程可跑。
    # sender 队列模拟真实 valueChanged 信号路径（返回对应 ConfigItem）
    from unittest.mock import MagicMock

    monkeypatch.setattr(LLMSettingsCard, "configChanged", MagicMock())
    senders = []
    monkeypatch.setattr(LLMSettingsCard, "sender", lambda self: senders.pop(0) if senders else None)

    calls = []
    monkeypatch.setattr(LLMSettingsCard, "_update_nav_styles", lambda self: calls.append(1))
    monkeypatch.setattr(design_tokens, "_cached_font_size_key", None)
    try:
        # 字号 / 字族变更（模拟对应 item 的 valueChanged）→ 即时重建导航样式
        for item in (card.cfg.ui_font_size, card.cfg.llm_font_family):
            senders.append(item)
            card._on_config_changed()
        assert len(calls) == 2
        # 主题类变更 → 不重复重建
        senders.append(card.cfg.ui_theme_style)
        card._on_config_changed()
        assert len(calls) == 2
    finally:
        LLMSettingsCard._last_change_type = None
        monkeypatch.setattr(design_tokens, "_cached_font_size_key", None)


def test_config_changed_rebuilds_nav_style_even_if_listener_clears_flag(qapp, monkeypatch):
    """回归：emit configChanged 后即使 _last_change_type 被监听者清空也要重建导航

    生产路径：on_settings_config_changed → win._on_settings_config_changed
    会同步把 LLMSettingsCard._last_change_type 清零；emit 后必须还能判别
    是否需要重建导航 QSS，否则字号切换只能等切 tab 时触发。
    """
    from PySide6.QtWidgets import QLabel

    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    card = LLMSettingsCard.__new__(LLMSettingsCard)
    card.cfg = Settings.get_instance()
    card._nav_buttons = {"appearance": QLabel("外观样式")}

    from unittest.mock import MagicMock

    real_signal = MagicMock()

    def fake_emit(*args, **kwargs):
        # 模拟生产链路监听者：emit 后立即清空类级变量
        LLMSettingsCard._last_change_type = None

    real_signal.emit = fake_emit
    monkeypatch.setattr(LLMSettingsCard, "configChanged", real_signal)
    senders = []
    monkeypatch.setattr(LLMSettingsCard, "sender", lambda self: senders.pop(0) if senders else None)

    calls = []
    monkeypatch.setattr(LLMSettingsCard, "_update_nav_styles", lambda self: calls.append(1))
    monkeypatch.setattr(design_tokens, "_cached_font_size_key", None)
    try:
        # 字号 / 字族变更：emit 清空类级变量后，仍须重建导航样式
        for item in (card.cfg.ui_font_size, card.cfg.llm_font_family):
            senders.append(item)
            card._on_config_changed()
        assert len(calls) == 2, f"导航样式重建被 emit 期间清空 _last_change_type 影响，实际次数 {len(calls)}"
        # 主题类变更 → 不重建
        senders.append(card.cfg.ui_theme_style)
        card._on_config_changed()
        assert len(calls) == 2
    finally:
        LLMSettingsCard._last_change_type = None
        monkeypatch.setattr(design_tokens, "_cached_font_size_key", None)
