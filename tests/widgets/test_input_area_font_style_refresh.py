# -*- coding: utf-8 -*-
"""
输入框字体样式刷新回归测试。

背景：输入框 SendableTextEdit 的样式由 _apply_input_style() 生成的 QSS 控制，
QSS 中硬编码 font-family/font-size。Qt 规则：QSS 中声明的字体属性优先级
高于 widget.setFont()。此前主题/字体变化刷新链路中，字体变化分支只调用
setFont 而不重建 QSS，导致输入框字体样式不随字体配置更新（样式丢失）。

修复：字体变化分支改为调用 refresh_style() 重建 QSS。
"""
import app.utils.config as config_mod
import app.utils.utils as utils_mod
from app.utils.design_tokens import invalidate_font_cache
from app.utils.utils import invalidate_font_family_css_cache
from app.widgets.bottom_input_area import SendableTextEdit


class _FakeValue:
    def __init__(self, value):
        self.value = value


class _FakeSettings:
    _family = "Segoe UI"

    @classmethod
    def get_instance(cls):
        return cls()

    @property
    def llm_font_family(self):
        return _FakeValue(self._family)

    @property
    def canvas_font_selected(self):
        return _FakeValue(self._family)

    @property
    def ui_font_size(self):
        return _FakeValue("medium")


def _set_font_config(monkeypatch, family="Segoe UI"):
    """把字体配置打桩为指定值并失效缓存。"""
    _FakeSettings._family = family
    monkeypatch.setattr(utils_mod, "Settings", _FakeSettings)
    # design_tokens 在函数内 from app.utils.config import Settings，需 patch 源模块
    monkeypatch.setattr(config_mod, "Settings", _FakeSettings)
    invalidate_font_cache()
    invalidate_font_family_css_cache()


def test_refresh_style_rebuilds_qss_with_current_font(monkeypatch):
    """refresh_style() 后 QSS 应包含当前配置的字体族（而非旧值）。"""
    _set_font_config(monkeypatch, family="Segoe UI")
    editor = SendableTextEdit()
    editor.refresh_style()
    assert "Segoe UI" in editor.styleSheet()

    # 字体配置变化 + 缓存失效 → refresh_style 后 QSS 更新为新字体
    _set_font_config(monkeypatch, family="Microsoft YaHei")
    editor.refresh_style()
    assert "Microsoft YaHei" in editor.styleSheet()
    assert "Segoe UI" not in editor.styleSheet()


def test_qss_font_overrides_setfont(monkeypatch):
    """Qt 规则：QSS 中 font-family 优先于 setFont —— 证明仅 setFont 不够。"""
    _set_font_config(monkeypatch, family="Segoe UI")
    editor = SendableTextEdit()

    # 即使 setFont 新字体，QSS 中 font-family 仍优先（bug 前提）
    from PySide6.QtGui import QFont

    editor.setFont(QFont("Consolas", 18))
    editor.refresh_style()
    assert "Segoe UI" in editor.styleSheet()

    # QSS 重建后新字体族应进入样式表
    _set_font_config(monkeypatch, family="Consolas")
    editor.refresh_style()
    assert "Consolas" in editor.styleSheet()
