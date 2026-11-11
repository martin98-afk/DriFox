# -*- coding: utf-8 -*-
"""事件类型混淆守卫（qfluentwidgets 的 Resize / Wheel 消费点）

## 症状与根因

同一类错位事件已二次复发：

- 2026-11-02，两连发。ScrollBar.eventFilter（scroll_bar.py:405）在
  ``e.type() == QEvent.Resize`` 后调 ``e.size()`` 崩：
  ``AttributeError: 'QWheelEvent' object has no attribute 'size'``
- 2026-11-05，ExpandBorderWidget.eventFilter（expand_setting_card.py:170）
  同一条判断、同一个崩法：
  ``AttributeError: 'QMouseEvent' object has no attribute 'size'``

即事件的 Python 包装类型与 ``type()`` 字段来自不同对象。实测（
tmp 复现脚本）机制是**悬垂 wrapper**：Python 侧长期持有某个事件对象后，
C++ 事件在 ``notify`` 返回时即销毁，内存被后续事件复用；sip 的包装类型
在创建时定死（仍是 QMouseEvent），``type()`` 却读到复用者的字段。此时
既没有 ``size()`` 也没有 ``setType``。若错位值恰好是 14（Resize），就
骗过判断一路走到 ``e.size()``。

Qt 自身只信 type 字段、对 Resize 盲转 ``QResizeEvent*``——同样的错位
对象若进 C++ 内部是段错误，Python 侧先崩反而留了现场。

## 修法（防御，不赌根因）

两层，一层兜全部、一层兜自己：

1. **应用级过滤器**（主防线）：包装是 ``QInputEvent``（鼠标/滚轮/键盘/
   悬停）但 ``type() == Resize`` 的事件在下发前直接吞掉。控件侧
   eventFilter 一根都收不到，qfluentwidgets 全部 ``e.size()`` 点一次覆盖——
   ``expand_setting_card.ExpandBorderWidget``、``line_edit.EditLayer``、
   ``info_bar``（两处）、``mask_dialog_base``、``expand_layout``、
   ``splash_screen``，以及宿主与插件将来写的同类判断。
   判据用「输入事件包装」而非「非 QResizeEvent」：后者会误吞宿主自建的
   ``QEvent(QEvent.Resize)``（实测打到
   tests/widgets/test_welcome_card_tab_bar.py）。真 resize 事件 4/4 均为
   QResizeEvent 包装；应用级过滤器先于目标控件 eventFilter 生效，拦下后
   控件确实收不到。
2. **ScrollBar / SmoothScrollDelegate 类级补丁**（2026-11-02 的旧防线，
   保留）：应用级过滤器只覆盖经过 ``QApplication::notify`` 的分发路径，
   monkeypatch 兜住旁路调用与直接实例调用的场景。

两处放行都是「少重定位/少滚动一次」，下一个真事件补回，交互代价为零。
"""

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QInputEvent, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import QApplication
from qfluentwidgets.components.widgets.scroll_bar import ScrollBar, SmoothScrollDelegate


class _ResizeTypeGuard(QObject):
    """应用级守卫：吞掉「type=Resize 但包装是输入事件」的错位事件。

    判据用 ``QInputEvent`` 而非「非 QResizeEvent」：后者会把
    ``QEvent(QEvent.Resize)`` 这种合法替身（宿主自建、单测里常见）一并
    吞掉（实测误伤 tests/widgets/test_welcome_card_tab_bar.py）。输入事件
    的 type 字段不可能是 Resize——出现即错位，判据精确且零误伤。
    覆盖 qfluentwidgets 全部 ``e.size()`` 消费点，见模块 docstring。
    """

    def eventFilter(self, obj, e):  # noqa: N802 - Qt 回调
        # 应用级过滤器每个事件都会过一遍，快路径先做整型比较再判包装类型
        if e.type() == QEvent.Resize and isinstance(e, QInputEvent):
            return True
        return False

def _patch_scrollbar_filter(cls) -> None:
    if getattr(cls.eventFilter, "_drifox_guarded", False):
        return

    original = cls.eventFilter

    def _safe_eventFilter(self, obj, e):
        # 类型混淆：type 称 Resize 但包装不是 QResizeEvent → 跳过 e.size()
        # 重定位。原实现对非 Resize 事件的出口是 super().eventFilter ≈ False。
        if (
            obj is self.parent()
            and e.type() == QEvent.Resize
            and not isinstance(e, QResizeEvent)
        ):
            return False
        return original(self, obj, e)

    _safe_eventFilter._drifox_guarded = True
    cls.eventFilter = _safe_eventFilter


def _patch_delegate_filter(cls) -> None:
    if getattr(cls.eventFilter, "_drifox_guarded", False):
        return

    original = cls.eventFilter

    def _safe_eventFilter(self, obj, e):
        # 同类混淆打到 Wheel 分支时不喂 angleDelta，原样放行走默认滚动
        if e.type() == QEvent.Wheel and not isinstance(e, QWheelEvent):
            return False
        return original(self, obj, e)

    _safe_eventFilter._drifox_guarded = True
    cls.eventFilter = _safe_eventFilter


def install_guard() -> None:
    """安装事件类型守卫（幂等）

    应用级过滤器 + 类级补丁。方法查找在调用期进行；应用级过滤器需
    QApplication 已创建，故在 main.py 里 QApp 就绪后立刻调用。
    """
    app = QApplication.instance()
    if app is not None and getattr(app, "_drifox_resize_guard", None) is None:
        guard = _ResizeTypeGuard(app)
        app.installEventFilter(guard)
        app._drifox_resize_guard = guard  # 挂引用，防 GC 后过滤器失效

    _patch_scrollbar_filter(ScrollBar)
    _patch_delegate_filter(SmoothScrollDelegate)
