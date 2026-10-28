# -*- coding: utf-8 -*-
"""_invalidate_welcome_card / _clear_chat_area 待删标记防闪退

根因（2026-10-15 13:41 / 13:42 连崩两次，源码版 python main.py）：
插件热重载（token-usage 删除后重装）→ UIPluginRegistry._refresh_welcome_cards
对 2 个窗口 _invalidate_welcome_card（hide + setParent(None) + deleteLater），
同时 rescheduled=2 → _schedule_initial_welcome 用 QTimer.singleShot 排重建
→ 回调 _show_initial_welcome → _clear_chat_area(delete_widgets=False)
→ takeAt(0) 把"已标记待删"的旧欢迎卡片取出来 → widget.hide()
→ 对已脱离父窗口树的 QWebEngineView 操作原生 HWND → ACCESS_VIOLATION。

deleteLater() 只把销毁事件排进队列，**不立即执行**，所以中间存在一个
"半死窗口期"：C++ 对象还活着、Python 引用还有效、但已无父窗口树。

修法：
1. _invalidate_welcome_card 摘除时先 chat_layout.removeWidget(cached)，
   并在 deleteLater 前打 _pending_delete 标记；
2. _clear_chat_area 收集阶段剔除带该标记的 widget，不进入 hide / 焦点摘除。

线程归属：两次崩溃线程都是主线程（Dummy-46 / tid 0x6bb8），
与跨线程无关——切勿再往"加主线程守卫"方向修。
"""

import ast
from pathlib import Path

import pytest

SRC_MAIN = Path("app/main_widget.py")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestInvalidateMarksPendingDelete:
    def test_invalidate_sets_pending_delete_flag(self):
        """_invalidate_welcome_card 必须在 deleteLater 前打 _pending_delete"""
        tree = ast.parse(_read(SRC_MAIN))
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_invalidate_welcome_card":
                fn = node
                break
        assert fn is not None, "_invalidate_welcome_card 不存在"
        src = ast.unparse(fn)
        assert "_pending_delete" in src, "_invalidate_welcome_card 必须打 _pending_delete 标记"
        assert "deleteLater" in src

    def test_invalidate_removes_from_chat_layout(self):
        """必须先 removeWidget 再 setParent(None)，否则 takeAt 仍能取到"""
        tree = ast.parse(_read(SRC_MAIN))
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_invalidate_welcome_card":
                fn = node
                break
        src = ast.unparse(fn)
        assert "chat_layout.removeWidget" in src, "必须先从 chat_layout 摘除 item"
        # removeWidget 必须早于 setParent(None)
        assert src.index("chat_layout.removeWidget") < src.index("setParent(None)")

    def test_pending_delete_set_before_deletelater(self):
        """标记必须在 deleteLater 之前（排队期间就要可被下游识别）"""
        tree = ast.parse(_read(SRC_MAIN))
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_invalidate_welcome_card":
                fn = node
                break
        src = ast.unparse(fn)
        # 只比较赋值语句与调用语句的位置，避开 docstring / 注释里的同名文本
        assign_at = src.index("cached._pending_delete = True")
        call_at = src.index("cached.deleteLater()")
        assert assign_at < call_at


class TestClearChatAreaSkipsPendingDelete:
    def test_clear_skips_pending_delete_widgets(self):
        """_clear_chat_area 必须剔除 _pending_delete widget，不进入 hide"""
        tree = ast.parse(_read(SRC_MAIN))
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_clear_chat_area":
                fn = node
                break
        assert fn is not None
        src = ast.unparse(fn)
        assert "_pending_delete" in src, "_clear_chat_area 必须跳过已标记待删的 widget"

    def test_clear_skips_before_focus_detach(self):
        """剔除必须早于 _detach_focus_from_widgets（它也会碰 widget）"""
        tree = ast.parse(_read(SRC_MAIN))
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_clear_chat_area":
                fn = node
                break
        src = ast.unparse(fn)
        assert "_pending_delete" in src and "_detach_focus_from_widgets" in src
        assert src.index("_pending_delete") < src.index("_detach_focus_from_widgets")


class TestBehavioralWithRealQt:
    """用真实 Qt 对象验证标记语义（offscreen 可跑）"""

    @pytest.fixture()
    def qapp(self):
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            app = QApplication([])
        return app

    def test_getattr_on_deleted_widget_returns_default(self, qapp):
        """对已 deleteLater 的 widget getattr(_pending_delete) 不抛异常"""
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QWidget

        w = QWidget()
        w._pending_delete = True
        w.deleteLater()
        # 先不 processEvents：对象仍活着，标记可读
        assert getattr(w, "_pending_delete", False) is True

    def test_removeWidget_empties_layout(self, qapp):
        """removeWidget 后 takeAt 取不到该 widget"""
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        host = QWidget()
        lay = QVBoxLayout(host)
        w = QWidget()
        lay.addWidget(w)
        assert lay.count() == 1
        lay.removeWidget(w)
        assert lay.count() == 0
        # takeAt 循环拿不到
        got = []
        while lay.count():
            item = lay.takeAt(0)
            if item and item.widget():
                got.append(item.widget())
        assert got == []

    def test_hide_after_setparent_none_is_the_danger(self, qapp):
        """记录危险模式：setParent(None) 后仍持有引用，hide() 是崩溃动作。

        本测试不真的触发 AV（无法在测试进程内崩溃），只断言语义：
        widget 在 setParent(None) 后 parent() 为 None，但 sip 对象未删除。
        """
        import shiboken6 as sip
        from PySide6.QtWidgets import QWidget

        parent = QWidget()
        w = QWidget(parent)
        assert w.parent() is parent
        w.setParent(None)
        assert w.parent() is None
        # 关键：此刻 shiboken6.isValid 仍为 False → 现有存活探测拦不住
        import shiboken6 as sip

        assert shiboken6.isValid(w) is False
