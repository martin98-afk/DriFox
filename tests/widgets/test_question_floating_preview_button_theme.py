# -*- coding: utf-8 -*-
"""QuestionFloatingWidget 预览按钮（_preview_btn）主题色适配回归测试

== 问题描述 ==
权限提问卡片右下角的「预览参数」按钮原本硬编码
``color: rgba(255,255,255,0.72)`` / ``color: rgba(255,255,255,0.95)``（白字）。
切到浅色主题（如 crema：``realtime_bg: rgba(244, 234, 212, 248)`` 浅奶 + 浅黄
REALTIME_TAG_BG）后，白字落在浅黄底上几乎不可见。

== 修复 ==
将按钮 ``color`` 改为 ``Colors.REALTIME_TEXT``，随主题切换：
- 深色主题：浅色文字 → 暗底可读
- 浅色主题：深色文字 → 浅底可读

== 回归测试 ==
直接构造 QuestionFloatingWidget（不依赖 DriFox 主题加载），调用 ``_apply_card_style``
后断言按钮的 QSS 满足两个不变量：
1. 不再含任何 ``rgba(255,255,255,...`` 硬编码白字
2. 包含 ``Colors.REALTIME_TEXT``（保证可读性跟主题走）
"""

import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

Qt.AA_ShareOpenGLContexts = Qt.AA_ShareOpenGLContexts
try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings  # noqa: F401
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except Exception:
    pass

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.utils.design_tokens import Colors  # noqa: E402


def _setup_app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv)


def test_preview_button_no_hardcoded_white_text():
    """预览按钮 QSS 不得含任何 rgba(255,255,255,*) 硬编码白字（浅色主题会不可读）"""
    _setup_app()
    from app.widgets.cards.floating.question_floating_widget import (
        QuestionFloatingWidget,
    )

    widget = QuestionFloatingWidget()
    # 触发一次样式刷新，确保 _preview_btn 拿到当前主题的 QSS
    widget._apply_card_style()

    qss = widget._preview_btn.styleSheet()
    assert qss, "预览按钮样式不应为空"
    # 防御性检查：白字+alpha 模式（任何 alpha 值）都不应再出现
    assert not re.search(
        r"rgba\(\s*255\s*,\s*255\s*,\s*255\s*,",
        qss,
    ), f"预览按钮 QSS 仍含硬编码白字 rgba(255,255,255,...)，浅色主题会不可读: {qss!r}"


def test_preview_button_text_uses_realtime_text_token():
    """预览按钮 QSS 必须使用 Colors.REALTIME_TEXT 作为前景色（跟主题）"""
    _setup_app()
    from app.utils.theme_manager import theme_manager  # noqa: F401  触发 Colors 懒加载
    from app.widgets.cards.floating.question_floating_widget import (
        QuestionFloatingWidget,
    )

    # 保证 Colors.refresh() 至少跑过一次（模块已 _apply_card_style 触发过即可）
    Colors.refresh()

    widget = QuestionFloatingWidget()
    widget._apply_card_style()

    qss = widget._preview_btn.styleSheet()
    # 期望 QSS 内的 color 值与 Colors.REALTIME_TEXT 同步
    expected = Colors.REALTIME_TEXT
    # 在 QSS 里 ``color: <value>`` 出现至少一次
    color_matches = re.findall(r"color\s*:\s*([^;}\n]+)", qss)
    assert color_matches, f"预览按钮 QSS 中未找到 color 声明: {qss!r}"
    # 所有 color 声明都必须等于当前主题的 REALTIME_TEXT（不允许出现裸 #ffffff 之类）
    for c in color_matches:
        c_clean = c.strip()
        assert c_clean == expected, (
            f"预览按钮 QSS 出现非主题色前景 {c_clean!r}（期望 {expected!r}），浅色主题下将不可读: {qss!r}"
        )


if __name__ == "__main__":
    test_preview_button_no_hardcoded_white_text()
    test_preview_button_text_uses_realtime_text_token()
    print("[preview-button-theme] all passed")
