# -*- coding: utf-8 -*-
"""提问卡片「自定义输入」多选取消选择回归测试

== 问题描述 ==
多选问题里点「输入自己的答案」自定义输入框后，再点无法取消勾选，一旦选中即锁死。

== 根因 ==
1. `_CustomInputCard.mousePressEvent` 只有「未激活 -> 激活」分支，缺少取消分支；
   `set_active(False)` 仅由单选点其它选项 / 恢复答案触发，多选没有取消路径。
2. `eventFilter` 把 `FocusIn` 当选中信号：自动聚焦（窗口首帧、切回前台）会把
   已取消的勾选默默复活。

== 修复 ==
1. 多选再次点击卡片（输入框以外区域）取消选中；单选不取消（互斥语义由点
   其它选项完成），且取消分支刻意不调 setFocus，避免 FocusIn 复活。
2. 选中语义改为「点击输入框内部或打字」：viewport 鼠标事件 + textChanged；
   聚焦本身不再算选中。`_setting_content` 放过恢复草稿的程序化写入。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PyQt5.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QApplication

# 与 tests/widgets 现有用例一致：offscreen 下先声明共享 GL 上下文并预热 QWebEngine。
Qt.AA_ShareOpenGLContexts = Qt.AA_ShareOpenGLContexts
try:
    from PyQt5.QtWebEngineWidgets import (  # noqa: F401
        QWebEnginePage,
        QWebEngineSettings,
        QWebEngineView,
    )
except Exception:
    pass

# 注意：不在模块顶层 import app.* 模块。收集期导入早于 conftest 的
# AA_ShareOpenGLContexts fixture，会让后续 QApplication 创建 access violation。
# app 模块一律在函数内延迟导入。


_APP = None


def _setup():
    # QApplication 必须由模块级变量持有：PyQt5 下丢失 Python 引用会销毁 C++
    # 对象，后续构造 widget 直接 access violation（0xC0000005，表现为无输出崩溃）。
    global _APP
    # 时序要求：先 import app 模块链，再创建 QApplication。
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget  # noqa: F401

    _APP = QApplication.instance() or QApplication(sys.argv)
    return _APP


def _pump(ms: int = 60):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _click(widget):
    ev = QMouseEvent(
        QEvent.MouseButtonPress, QPointF(5, 5), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
    )
    widget.mousePressEvent(ev)
    QApplication.processEvents()


def _click_viewport(card):
    """点击输入框内部（鼠标事件走 viewport，不经卡片 mousePressEvent）"""
    ev = QMouseEvent(
        QEvent.MouseButtonPress, QPointF(5, 5), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
    )
    QApplication.sendEvent(card._text_edit.viewport(), ev)
    QApplication.processEvents()


def _make(multiple: bool):
    from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

    w = QuestionFloatingWidget()
    w.show_question(
        [
            {
                "question": "请选择你的答案？",
                "options": [
                    {"label": "选项A", "description": ""},
                    {"label": "选项B", "description": ""},
                ],
                "multiple": multiple,
            }
        ]
    )
    w.resize(420, 600)
    w.show()
    _pump(120)
    return w


def test_multiple_custom_input_can_be_deselected():
    """多选：激活自定义输入后再次点击卡片应取消，且已勾选项保留、不计入答案"""
    _setup()
    w = _make(True)
    card = w._custom_input_widget
    assert card is not None
    opt_a = w._option_widgets[0]

    _click(opt_a)
    _click(card)
    assert card._active is True, "首次点击应激活自定义输入"
    assert card._icon.text() == "☑"

    card._text_edit.setPlainText("临时答案")
    QApplication.processEvents()

    _click(card)
    assert card._active is False, "多选下再次点击卡片必须取消自定义输入"
    assert card._icon.text() == "□"
    assert w._get_custom_input_text() == "", "取消后自定义文本不得计入答案"
    assert [o["label"] for o in w._get_selected_options()] == ["选项A"], "取消自定义不应影响已勾选项"

    w._save_current_answer()
    saved = w._answers.get(0)
    assert saved is not None
    assert "临时答案" not in saved["text"], "取消后提交答案不得包含自定义文本"
    assert saved["custom"] is False


def test_multiple_custom_input_toggle_is_reversible():
    """多选 toggle 可反复：取消后再次点击又能激活"""
    _setup()
    w = _make(True)
    card = w._custom_input_widget

    _click(card)
    assert card._active is True
    _click(card)
    assert card._active is False
    _click(card)
    assert card._active is True


def test_single_custom_input_keeps_mutex_semantics():
    """单选：点击卡片保持选中（不 toggle），点选项才取消自定义"""
    _setup()
    w = _make(False)
    card = w._custom_input_widget
    opt_a = w._option_widgets[0]

    _click(card)
    assert card._active is True
    _click(card)
    assert card._active is True, "单选下重复点击不应取消（取消由点其它选项完成）"

    _click(opt_a)
    assert card._active is False, "单选点选项后自定义输入必须取消"
    assert opt_a._selected is True


def test_single_click_inside_text_edit_selects_and_clears_options():
    """单选：点击输入框内部区域应选中自定义并取消其它选项"""
    _setup()
    w = _make(False)
    card = w._custom_input_widget
    opt_a = w._option_widgets[0]

    _click(opt_a)
    assert opt_a._selected is True

    _click_viewport(card)
    assert card._active is True, "点击输入框内部应选中自定义输入"
    assert opt_a._selected is False, "单选下选中自定义应取消其它选项"


def test_focus_alone_does_not_activate():
    """聚焦本身不算选中：自动聚焦/切回前台不得把已取消的勾选复活"""
    _setup()
    w = _make(True)
    card = w._custom_input_widget

    _click(card)
    assert card._active is True
    _click(card)
    assert card._active is False

    w._next_btn.setFocus()
    QApplication.processEvents()
    card._text_edit.setFocus()
    _pump(30)
    assert card._active is False, "FocusIn 不得自动激活自定义输入"


def test_typing_after_cancel_reactivates():
    """取消后在输入框继续打字 -> 重新视为选用自定义答案"""
    _setup()
    w = _make(True)
    card = w._custom_input_widget

    _click(card)
    card._text_edit.setPlainText("第一版")
    QApplication.processEvents()
    _click(card)
    assert card._active is False

    card._text_edit.setPlainText("第二版")
    QApplication.processEvents()
    assert card._active is True, "取消后打字应重新激活，避免「有文字但未勾选」的隐性状态"
    assert w._get_custom_input_text() == "第二版"


def test_restore_draft_does_not_activate():
    """恢复草稿（程序化写入）不得触发自动激活"""
    _setup()
    w = _make(True)
    card = w._custom_input_widget

    _click(card)
    _click(card)
    assert card._active is False

    card.set_content("历史草稿")
    QApplication.processEvents()
    assert card._active is False, "set_content 回填草稿不应自动激活"


if __name__ == "__main__":
    for name, fn in [
        ("1: 多选取消自定义", test_multiple_custom_input_can_be_deselected),
        ("2: 多选 toggle 可反复", test_multiple_custom_input_toggle_is_reversible),
        ("3: 单选互斥语义保持", test_single_custom_input_keeps_mutex_semantics),
        ("4: 点击输入框内部选中", test_single_click_inside_text_edit_selects_and_clears_options),
        ("5: 聚焦不激活", test_focus_alone_does_not_activate),
        ("6: 取消后打字重激活", test_typing_after_cancel_reactivates),
        ("7: 恢复草稿不激活", test_restore_draft_does_not_activate),
    ]:
        try:
            fn()
            print(f"✅ {name}")
        except AssertionError as e:
            print(f"❌ {name}: {e}")
        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"⚠️ {name}: {e}")
