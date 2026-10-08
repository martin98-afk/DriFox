# -*- coding: utf-8 -*-
"""T22 回归：设置弹窗隐藏白刷门控

背景
----
main_widget 5a 弹窗段对 LLMSettingsCard（0.7s 级重型卡）在**弹窗不可见**时
仍全量刷新 12 命名卡 + frames + sep + 本体 = 纯白刷。门控后：弹窗已构建且
不可见且纯主题 scope → 置 `_theme_needs_refresh=True` 跳过弹窗自身刷新；
显示时 showEvent 补刷自愈（`_refresh_appearance_from_config`）。

安全边界：scope 含 font_family/font_size 时**不门控**（showEvent 补刷入口
不含字号路径，门控会造成字号残留）。

测试策略：源码断言。`_apply_runtime_ui_settings` 是巨方法，MagicMock 宿主
跑全方法会因 mock 值穿透 Qt 边界在 C 层崩溃（0xC0000409，与改动无关的
测试环境限制），无法轻量行为验证；改以源码断言锁定门控三要素、置脏语句、
门控边界（只包弹窗段）与 showEvent 补刷三步。
"""

import inspect

import app.widgets.cards.settings.llm_settings_card as llm_mod
from app.main_widget import OpenAIChatToolWindow


def _gate_source() -> str:
    return inspect.getsource(OpenAIChatToolWindow._apply_runtime_ui_settings)


def test_gate_conditions_exist():
    """a) 门控三要素齐全：红线 is not None 写法 + isVisible + 纯主题 scope"""

    src = _gate_source()
    assert "_popup_hidden_skip = (" in src, "缺少门控条件块"
    assert "self._settings_popup is not None" in src, "门控必须用 is not None 红线写法（勿改 hasattr）"
    assert "not self._settings_popup.isVisible()" in src, "门控必须检查弹窗可见性"
    assert "and not is_font" in src, "门控必须排除 font scope（字号无补刷路径）"


def test_gate_marks_dirty_and_skips_popup_block():
    """b) 隐藏态：置脏语句存在，且弹窗自身刷新全部包进门控 else 块"""

    src = _gate_source()
    assert "if _popup_hidden_skip:" in src
    assert "self._settings_popup._theme_needs_refresh = True" in src, "隐藏态应置脏待 showEvent 补刷"
    # 门控边界：弹窗本体刷新在 else 块内（缩进 16），命名卡循环亦然
    assert "\n                if self._settings_popup:" in src, "弹窗 frames/命名卡刷新应包进 else 门控块"
    assert "\n                self._safe_refresh(self._settings_popup)" in src, "弹窗本体刷新应包进 else 门控块"


def test_gate_does_not_block_downstream_refresh():
    """d') 门控只包弹窗段：窗口内 BaseSettingsCard 循环在门控块之外（缩进 12，
    无条件执行）——_share_card/_history_questions_card/_model_config_card/
    _model_selector_card 是窗口可见卡，不随弹窗可见性跳过"""

    src = _gate_source()
    pos_skip = src.find("_popup_hidden_skip = (")
    pos_loop = src.find("for card in _base_settings:")
    assert pos_loop != -1, "找不到 BaseSettingsCard 窗口卡刷新循环"
    line_start = src.rfind("\n", 0, pos_loop) + 1
    indent = pos_loop - line_start
    assert indent == 12, (
        f"BaseSettingsCard 循环缩进应为 12（方法体无条件块），实际 {indent}——"
        "门控不得波及窗口可见卡刷新"
    )
    assert pos_skip < pos_loop, "门控块应位于 BaseSettingsCard 循环之前"


def test_showevent_heals_dirty_flag_by_source():
    """c) showEvent 补刷：源码断言「检查标记 → 清标记 → 调补刷入口」三步存在
    且位于方法体头部（llmProviderCard 刷新之前）。

    LLMSettingsCard 构造 0.7s 级且 showEvent 末尾 super().showEvent 依赖完整
    C++ 对象，无法轻量实例化，故以源码断言锁定补刷语义。
    """
    src = inspect.getsource(llm_mod.LLMSettingsCard.showEvent)
    pos_flag = src.find("_theme_needs_refresh")
    pos_clear = src.find("_theme_needs_refresh = False")
    pos_heal = src.find("_refresh_appearance_from_config()")
    pos_tail = src.find("llmProviderCard")
    assert pos_flag != -1, "showEvent 缺少脏标记检查"
    assert 0 <= pos_clear < pos_heal, "应先清标记再调补刷（防重入）"
    assert pos_heal != -1, "showEvent 缺少 _refresh_appearance_from_config 补刷调用"
    assert pos_flag < pos_tail, "补刷逻辑应位于 showEvent 方法体头部"
