# -*- coding: utf-8 -*-
"""ProviderIconWidget 字母提取与字号适配回归测试

覆盖需求：
1. 字母回退：取每个 part 的首个字母/汉字字符，
   跳过 #/&/数字 等非字母字符，避免出现 "C#"/"A&" 之类难看的首字母
2. 字号：内置图标在 paintEvent 中按 width * 0.45 计算并 scale_font_size 适配系统字号
3. _init_icon 在设置自定义图标时清空 _text，确保图标模式走 super().paintEvent
"""

import sys

import pytest
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    app = QApplication.instance()
    if app is None:
        from PyQt5.QtCore import Qt as _Qt

        QApplication.setAttribute(_Qt.AA_ShareOpenGLContexts)
        app = QApplication(sys.argv)
    return app


class TestProviderIconWidgetLetters:
    """ProviderIconWidget 字母回退测试（使用自定义服务商名，走 fallback 分支）"""

    def _make_widget(self, name, size=32):
        """直接构造 ProviderIconWidget（不挂父窗体）"""
        _ensure_qapp()
        from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget

        return ProviderIconWidget(name, size)

    def test_single_part_ascii(self):
        """单个 ASCII part: 'MyProvider' -> 'M'"""
        w = self._make_widget("MyProvider")
        assert w._text == "M"

    def test_single_part_compound(self):
        """单个 part 也只取首字母: 'MyAI' -> 'M'"""
        w = self._make_widget("MyAI")
        assert w._text == "M"

    def test_multi_part_takes_first_letter_per_part(self):
        """多 part: 'Foo Bar' -> 'FB'"""
        w = self._make_widget("Foo Bar")
        assert w._text == "FB"

    def test_skip_hash_suffix(self):
        """跳过 # 后缀: 'My Provider #2' -> 'MP'（不应该是 'MP#'）"""
        w = self._make_widget("My Provider #2")
        assert w._text == "MP"

    def test_skip_ampersand(self):
        """跳过 &: 'Custom & AI' -> 'CA'"""
        w = self._make_widget("Custom & AI")
        assert w._text == "CA"

    def test_chinese_part(self):
        """中文 part 取首个汉字: '我的智能服务' -> '我'"""
        w = self._make_widget("我的智能服务")
        assert w._text == "我"

    def test_chinese_paren_skipped_outer_letter_taken(self):
        """中文括号内不计入 part，外层英文字符仍正常取值: 'MyAI (备用)' -> 'MB'"""
        w = self._make_widget("MyAI (备用)")
        # "MyAI" -> "M", "(备用)" 被跳过括号过滤，对应 part 为 "备用"，跳过，停止
        # 实际会因为 letters 已有一个字母即停
        # wait——逻辑是 letters >= 2 break；这里 letters = "M" 后继续找第二个 part
        # part = "备用", ch='备' isalpha → letters = "M备"
        assert w._text == "M备"

    def test_only_punctuation_part(self):
        """纯数字 part 跳过: '123 Provider' -> 'P'"""
        w = self._make_widget("123 Provider")
        assert w._text == "P"

    def test_max_two_letters(self):
        """累计到 2 个字母即停: 'Foo Bar Baz' -> 'FB'（不取到 Bz 的 B）"""
        w = self._make_widget("Foo Bar Baz")
        assert w._text == "FB"

    def test_chinese_with_space_paren(self):
        """英文 + 中文括号: 'MyFlow (我的硅基)' -> 'M我'"""
        w = self._make_widget("MyFlow (我的硅基)")
        # part = "MyFlow", letters = "M"；part = "(我的硅基)", 跳过括号；
        # 括号丢弃后实际 part 是 "我的硅基"，取首个汉字 "我"
        assert w._text == "M我"


class TestProviderIconWidgetInitIconClearText:
    """当 provider 在 PROVIDER_ICONS 中能查到时，_text 必须被清空（避免与图标叠加）"""

    def test_known_provider_uses_icon_not_text(self, monkeypatch):
        """DeepSeek 是内置服务商 → _text 应当为空，调用 _init_icon 走图标分支"""
        from PyQt5.QtGui import QIcon, QPixmap

        from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget

        _ensure_qapp()

        captured = {"called": False}

        def fake_get_icon(name):
            captured["called"] = True
            pix = QPixmap(32, 32)
            pix.fill()
            return QIcon(pix)

        monkeypatch.setattr(
            "app.widgets.cards.settings.provider_setting_card.get_icon",
            fake_get_icon,
        )

        w = ProviderIconWidget("DeepSeek", 32)
        assert captured["called"], "内置服务商应当调用 get_icon 走图标分支"
        assert w._text == "", f"内置图标应清空 _text，实际 {w._text!r}"


class TestProviderIconWidgetFontScale:
    """paintEvent 中的字号应随系统字号缩放"""

    def test_paint_event_font_size_scales(self, monkeypatch, qtbot):
        """paintEvent 中调用 scale_font_size 时应跟随系统字号缩放

        通过 monkeypatch 替换 scale_font_size 验证 paintEvent 内确实调用了它
        """
        from PyQt5.QtGui import QFont, QPixmap

        from app.utils import design_tokens
        from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget

        _ensure_qapp()

        # 重定向 scale_font_size 以捕获真实调用
        original = design_tokens.scale_font_size
        calls = []

        def spy_scale(size):
            calls.append(size)
            return max(8, int(size))

        monkeypatch.setattr(design_tokens, "scale_font_size", spy_scale)
        # 也覆盖 widgets/cards/settings/provider_setting_card 内的引用
        from app.widgets.cards.settings import provider_setting_card as mod

        monkeypatch.setattr(mod, "scale_font_size", spy_scale)

        w = ProviderIconWidget("MyCustom", 20)
        # 强制分配一个 20x20 区域以触发 setFixedSize 后 width=20
        w.setFixedSize(20, 20)

        # 把 widget 渲染到 pixmap 触发 paintEvent
        pix = QPixmap(20, 20)
        w.render(pix)
        QApplication.processEvents()

        assert calls, "paintEvent 必须调用 scale_font_size 计算字号"
        # 第一次调用应该是 0.45 * 20 = 9
        assert calls[0] == 9, f"字号基数应为 int(width * 0.45) = 9，实际 {calls[0]}"


class TestProviderIconWidgetRefreshStyle:
    """refresh_style 在主题变更时不应崩"""

    def test_refresh_style_no_crash(self, qtbot):
        from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget

        _ensure_qapp()
        w = ProviderIconWidget("Custom Name", 24)
        w.refresh_style()  # 不抛异常
        assert w.width() > 0
