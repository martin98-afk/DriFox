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
