# -*- coding: utf-8 -*-
"""验证：大模型卡片右键「复制」优先复制选中文本（QAction.triggered 坑回归）。

背景：``_show_context_menu`` 里 ``copy_action.triggered.connect(self._copy_to_clipboard)``
直连 —— QAction.triggered 带一个 bool(checked) 实参，会灌进 slot 首个形参
``copy_selection``，把默认值 True 静默覆盖成 False → 右键永远复制全文，
选区形同虚设。修复：lambda 转接固化 copy_selection=True。

⚠️ 不走 pytest：本仓 tests/widgets 下 Qt 用例在 pytest 进程内会 0xC0000409
（既有噪声区），验证改用独立脚本。
⚠️ CodeWebViewer 用 ``object.__new__`` 绕 __init__ 后 C++ 对象未初始化，
   作为 connect receiver 会被静默丢弃，故用真 QObject host 承载 slot。
"""

import sys
from unittest.mock import patch

from PyQt5.QtCore import QObject
from PyQt5.QtWidgets import QAction, QApplication

from app.widgets.message_card import CodeWebViewer

_SRC = "app/widgets/message_card.py"


def _make_viewer(selected: str, full_text: str) -> CodeWebViewer:
    """构造绕过 __init__ 的 CodeWebViewer，注入 mock page 与全文文本。"""
    viewer = CodeWebViewer.__new__(CodeWebViewer)

    class _FakePage:
        def selectedText(self):
            return selected

    viewer.page = lambda: _FakePage()
    viewer.get_plain_text = lambda: full_text
    return viewer


class _Host(QObject):
    """真 QObject 外壳：让 connect receiver 生效（复刻 self 为 QObject 的实况）。"""

    def __init__(self, viewer):
        super().__init__()
        self._viewer = viewer

    def copy_fixed(self, checked=False):
        """修复后写法：lambda 固化 copy_selection=True"""
        return self._viewer._copy_to_clipboard(copy_selection=True)

    def copy_buggy(self, copy_selection: bool = True):
        """修复前写法：直连，默认值被 checked 实参覆盖"""
        return self._viewer._copy_to_clipboard(copy_selection)


def _copy_via_menu(host: _Host, slot_name: str) -> str:
    """建 QAction 并按 slot_name 连法 trigger，返回写入剪贴板的文本。"""
    copied = []

    class _Clip:
        def setText(self, t):
            copied.append(t)

    with patch.object(QApplication, "clipboard", staticmethod(lambda: _Clip())):
        act = QAction("复制")
        act.triggered.connect(getattr(host, slot_name))
        act.trigger()
    return copied[0] if copied else ""


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    src = open(_SRC, encoding="utf-8").read()
    assert "lambda checked=False: self._copy_to_clipboard(copy_selection=True)" in src, "源码未落地修复"
    print("✓ 源码已含 lambda 转接写法")

    v1 = _make_viewer(selected="我选中的一段话", full_text="完整内容很长很长很长")
    h1 = _Host(v1)

    got = _copy_via_menu(h1, "copy_fixed")
    print(f"✓ 修复后右键复制: {got!r}")
    assert got == "我选中的一段话", f"期望选中文本，实际 {got!r}"

    got_old = _copy_via_menu(h1, "copy_buggy")
    print(f"✓ 修复前右键复制（bug 现场）: {got_old!r}")
    assert got_old == "完整内容很长很长很长", f"旧连法应复制全文，实际 {got_old!r}"

    h2 = _Host(_make_viewer(selected="", full_text="完整内容"))
    got_fb = _copy_via_menu(h2, "copy_fixed")
    print(f"✓ 无选区降级全文: {got_fb!r}")
    assert got_fb == "完整内容"

    h3 = _Host(_make_viewer(selected="第一行\u2029第二行", full_text="全文"))
    got_sep = _copy_via_menu(h3, "copy_fixed")
    print(f"✓ \\u2029 规范化为换行: {got_sep!r}")
    assert got_sep == "第一行\n第二行"

    del app
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
