# -*- coding: utf-8 -*-
"""回归测试：延迟构建卡片内容的懒加载兜底守卫

背景：
_model_selector_card_content / _model_config_popup 由延迟构建链
（_deferred_build_cards，800ms 定时器 / P2 懒加载 pending）创建。
用户提前点击「模型选择 / 模型参数」按钮时内容仍为 None，直接访问会崩：
    AttributeError: 'NoneType' object has no attribute 'set_providers_data'

修复：
新增幂等的 _ensure_model_selector_card_content / _ensure_model_config_popup，
在 _load_model_selector_to_card / _load_model_config_to_card 入口兜底调用。

本测试验证：
1. ensure 方法幂等（重复调用只构建一次，不重复 addWidget/按钮）
2. 崩溃场景（内容为 None）下 _load_*_to_card 不再抛 AttributeError
"""

from types import MethodType
from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtWidgets import QWidget


def _qapp():
    """确保 QApplication 可用，返回实例"""
    import sys

    from PyQt5.QtWidgets import QApplication

    return QApplication.instance() or QApplication(sys.argv)


def _make_fake_window():
    """构造真实 QWidget 宿主，绑定 main_widget 的懒加载相关真实方法

    说明：OpenAIChatToolWindow.__new__ 会被 PyQt C++ 层拦截
    （super-class __init__ never called），故用 QWidget + MethodType 绑定，
    parent 参数（QWidget）合法，且能访问真实 BaseSettingsCard/内容控件。
    """
    from app.main_widget import OpenAIChatToolWindow

    class _FakeWindow(QWidget):
        pass

    inst = _FakeWindow()
    inst._window_id = "test-window"
    inst._model_selector_card_content = None
    inst._model_selector_card = None
    inst._model_config_popup = None
    inst._model_config_card = None
    inst._valid_configs = {}
    inst._current_provider_name = ""
    inst._current_model_name = ""
    inst._card_manager = MagicMock()
    inst._bottom_card_container = MagicMock()
    inst._restore_after_system_close = lambda: None
    inst.cfg = MagicMock()
    inst._on_model_selected_from_popup = lambda *a, **k: None
    inst._on_sticky_provider_changed = lambda *a, **k: None
    inst._on_add_provider_from_card = lambda: None
    inst._on_configure_providers_from_card = lambda: None
    inst._on_config_applied = lambda *a, **k: None

    # 绑定懒加载守卫相关的真实方法
    for name in (
        "_ensure_model_selector_card",
        "_ensure_model_selector_card_content",
        "_ensure_model_config_card",
        "_ensure_model_config_popup",
        "_load_model_selector_to_card",
        "_load_model_config_to_card",
        "_update_model_selector_header",
        "_ensure_thinking_fields",
    ):
        setattr(inst, name, MethodType(getattr(OpenAIChatToolWindow, name), inst))
    return inst


class TestLazyCardContentGuard:
    """懒加载卡片内容兜底守卫的回归测试"""

    @pytest.fixture(autouse=True)
    def _ensure_qapp(self):
        _qapp()

    # ── 幂等性 ─────────────────────────────────────────────

    def test_ensure_model_selector_card_content_idempotent(self):
        """_ensure_model_selector_card_content 重复调用只构建一次内容"""
        inst = _make_fake_window()
        inst._ensure_model_selector_card_content()
        first = inst._model_selector_card_content
        assert first is not None, "首次调用应构建出内容"
        assert inst._model_selector_card is not None, "应同时确保卡片框架已创建"
        # 第二次调用：幂等，不重复构建
        inst._ensure_model_selector_card_content()
        assert inst._model_selector_card_content is first, "重复调用不应重建内容"
        # 头部按钮只添加一次（add_header_button 非幂等，重复调用会累积按钮）
        assert inst._model_selector_card._extra_buttons_container.count() == 2

    def test_ensure_model_config_popup_idempotent(self):
        """_ensure_model_config_popup 重复调用只构建一次内容"""
        inst = _make_fake_window()
        inst._ensure_model_config_popup()
        first = inst._model_config_popup
        assert first is not None, "首次调用应构建出 popup"
        inst._ensure_model_config_popup()
        assert inst._model_config_popup is first, "重复调用不应重建 popup"

    # ── 崩溃场景回归（修复的核心） ───────────────────────────

    def test_load_model_selector_to_card_no_crash_when_content_none(self):
        """内容未构建（None）时调用 _load_model_selector_to_card 不崩溃且兜底构建

        对应历史报错：
        AttributeError: 'NoneType' object has no attribute 'set_providers_data'
        """
        inst = _make_fake_window()
        with (
            patch("app.main_widget.get_merged_provider_models", return_value={}),
            patch("app.main_widget.get_model_capabilities", return_value={}),
        ):
            # 模拟延迟构建尚未执行：content 为 None 直接调用加载入口
            inst._model_selector_card_content = None
            inst._load_model_selector_to_card()
        # 兜底构建生效：内容已创建且成功 set_providers_data
        assert inst._model_selector_card_content is not None

    def test_load_model_config_to_card_no_crash_when_popup_none(self):
        """popup 未构建（None）时调用 _load_model_config_to_card 不崩溃且兜底构建

        同类隐患：_model_config_popup 同样由延迟构建创建。
        """
        inst = _make_fake_window()
        with patch("app.main_widget.apply_model_defaults", side_effect=lambda c, m: c):
            inst._model_config_popup = None
            inst._load_model_config_to_card()
        assert inst._model_config_popup is not None

    # ── 延迟构建链复用同一守卫（防止双份内容） ──────────────

    def test_deferred_build_reuses_guard_no_duplicate(self):
        """延迟构建链与兜底路径共用守卫：先兜底后延迟构建不产生重复内容"""
        inst = _make_fake_window()
        # 用户提前点击 → 兜底构建
        inst._ensure_model_selector_card_content()
        first = inst._model_selector_card_content
        # 延迟构建链随后执行（原 _build_deferred_card_model_selector 的内容部分）
        inst._ensure_model_selector_card_content()
        assert inst._model_selector_card_content is first
