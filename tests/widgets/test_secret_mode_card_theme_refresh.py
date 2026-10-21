# -*- coding: utf-8 -*-
"""回归测试：主题切换后「API Key 加密方式」设置卡内部样式必须随主题重建

用户报告（2026-09-17）：系统配置 → API Key 加密方式，展开区那两个选项行
（系统钥匙串 / 密码加密）的样式不随主题动态刷新，深/浅色切换后仍是旧主题配色。

根因（两条叠加）：
1. `SecretModeSettingCard` 继承 `ExpandSettingCard`——它不是 `SettingCard` 子类
   （MRO: ExpandSettingCard → QScrollArea → QFrame），`main_widget` 主题刷新链的
   `findChildren(SettingCard)` 与 `SystemCardFrame._refresh_content_children` 都扫不到；
2. 该卡原先**没有 `refresh_style`**，行/标题/说明/按钮的 QSS 在构造期把
   `Colors.*` 直接写死进 f-string，构造后永不再重建。

修复：
- 抽出可重建的 QSS 工厂（`_hint_qss` / `_small_button_qss` / `_ModeRow._title_qss`）；
- 补 `_ModeRow.refresh_style` 与 `SecretModeSettingCard.refresh_style`（后者现取
  `_refresh()` 重建选中态，选中边框走 `BORDER_ACCENT`/`HOVER_BG`）；
- 挂进 `LLMSettingsCard.refresh_style` 的手风琴卡命名清单。

本测试用「改主题数据源 + Colors 缓存失效」模拟真实主题切换，断言刷新后 QSS
内出现新主题 token，且选中/未选中行的边框各自正确。
"""

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.utils import design_tokens  # noqa: E402

# 与真实主题 YAML 同结构的最小主题，颜色值刻意取原主题里不会出现的值
_NEW_THEME = {
    "border_accent": "#FF00FF",
    "border": "#00FF00",
    "text_primary": "#111111",
    "text_muted": "#222222",
    "hover_bg": "rgba(1, 2, 3, 0.5)",
    "card_bg": "rgba(9, 9, 9, 250)",
}


@pytest.fixture()
def _switched_theme(monkeypatch):
    """把主题数据源换成新主题并让 Colors 缓存失效（等价于一次主题切换）"""
    monkeypatch.setattr(design_tokens, "current_theme", lambda: _NEW_THEME)
    design_tokens.Colors._cached_theme_items = None
    yield
    design_tokens.Colors._cached_theme_items = None


@pytest.fixture()
def card(qapp, tmp_path, monkeypatch):
    """构造加密卡（隔离落盘目录，避免污染真实 app.config）"""
    from app.utils.config import Settings

    inst = Settings.get_instance()
    monkeypatch.setattr(inst, "file", tmp_path / "app.config")
    monkeypatch.setattr(inst, "save", lambda *a, **k: None)

    from app.widgets.cards.settings.secret_mode_card import SecretModeSettingCard

    return SecretModeSettingCard(None)


def test_card_has_refresh_style(card):
    """刷新链靠 hasattr 探测，方法缺失即静默跳过 —— 必须存在"""
    assert hasattr(card, "refresh_style")


def test_rows_rebuild_qss_on_theme_change(card, _switched_theme):
    """主题切换后：行边框、标题色、说明色全部换成新主题 token"""
    card.refresh_style()

    current = card._current_mode()
    selected_row = card._rows[current]
    other = next(r for m, r in card._rows.items() if m != current)

    assert "#FF00FF" in selected_row.styleSheet(), "选中行边框未换成新 BORDER_ACCENT"
    assert "#00FF00" in other.styleSheet(), "未选中行边框未换成新 BORDER"
    assert "#111111" in selected_row._title.styleSheet(), "行标题未换成新 TEXT_PRIMARY"
    assert "#222222" in selected_row._desc.styleSheet(), "行说明未换成新 TEXT_MUTED"


def test_password_row_widgets_rebuild_qss(card, _switched_theme):
    """密码子区（状态标签 + 两个按钮）同样跟着主题走"""
    card.refresh_style()

    assert "#222222" in card._pwd_status.styleSheet()
    for btn in (card._pwd_btn, card._forget_btn):
        assert "#00FF00" in btn.styleSheet(), "按钮边框未换成新 BORDER"
        assert "#111111" in btn.styleSheet(), "按钮文字未换成新 TEXT_PRIMARY"


def test_refresh_style_is_idempotent(card, _switched_theme):
    """连续刷新不抛异常（主题链路与字体链路会重复调用）"""
    for _ in range(3):
        card.refresh_style()


def test_mounted_in_theme_refresh_chain():
    """挂载点回归：主题切换链的命名清单必须含 secretModeCard

    该卡继承 ExpandSettingCard（非 SettingCard 子类），且挂在分页内——
    `findChildren(SettingCard)` 与 `SystemCardFrame._refresh_content_children`
    两条通用链都扫不到，只能靠 `main_widget._apply_runtime_ui_settings` 的
    命名清单显式调用（同 pluginToolCard / pluginAgentCard 的处境）。
    漏挂 = 回到「行样式停留旧主题」。
    """
    src = (PROJECT_ROOT / "app/main_widget.py").read_text(encoding="utf-8-sig")
    assert '"secretModeCard"' in src, "加密卡未挂进主题切换的命名刷新清单"


def test_reachable_from_settings_popup(qapp, tmp_path, monkeypatch):
    """清单靠 getattr(settings_popup, name) 取卡，属性名必须真实可达"""
    from app.utils.config import Settings

    inst = Settings.get_instance()
    monkeypatch.setattr(inst, "file", tmp_path / "app.config")
    monkeypatch.setattr(inst, "save", lambda *a, **k: None)

    from PyQt5.QtWidgets import QWidget

    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    popup = LLMSettingsCard(QWidget())
    card = getattr(popup, "secretModeCard", None)
    assert card is not None, "设置卡上取不到 secretModeCard 属性"
    assert hasattr(card, "refresh_style"), "取到的卡缺 refresh_style，刷新链会静默跳过"
