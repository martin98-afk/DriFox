# -*- coding: utf-8 -*-
"""复现：卡片打开期点击标题栏折叠按钮的实际行为

用户报告：左侧有打开的 UI 卡片时点左上折叠按钮，卡片不隐藏，
只是宽度被压到最小。本脚本打印点击前后全部相关状态。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TESTS_WIDGETS = os.path.join(_REPO_ROOT, "tests", "widgets")
if _TESTS_WIDGETS not in sys.path:
    sys.path.insert(0, _TESTS_WIDGETS)

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication(["repro"])

# 借用回归测试文件的初始化/开卡 helper（保持同一套时序）
import test_left_card_sidebar_embed as T  # noqa: E402

from app.widgets.tab_manager_window import _COLLAPSED_PANEL_WIDTH, _FRAME_PADDING_X  # noqa: E402


def _state(tm, tag):
    panel = tm._tab_panel
    sp = tm._sidebar_splitter
    idx = sp.indexOf(tm._global_left_wrapper)
    sizes = sp.sizes()
    btn = T.TabManagerWindow.get_instance().titleBar._sidebar_btn
    print(f"--- {tag} ---")
    print(f"  panel._collapsed={panel._collapsed} squeeze={panel._collapsed_by_squeeze} "
          f"present={getattr(panel, '_left_card_present', None)}")
    print(f"  frame(main splitter)[0]={tm._splitter.sizes()[0]} "
          f"sidebar[tab={sizes[0]}, card={sizes[idx] if 0 <= idx < len(sizes) else '?'}]")
    print(f"  active={tm._left_card_area_active()} btn_tooltip={btn.toolTip()}")


def main():
    tm = T._make_tm()
    _state(tm, "初始（无卡片）")
    T._show_left_card(tm, "tp-repro-collapse")
    for _ in range(6):
        QApplication.processEvents()
    T._pump(400)
    _state(tm, "卡片打开后")

    # hook primary.show 抓谁在 hide 后重新 show
    import traceback
    from app.widgets.cards.card_container import CardContainer

    orig_show = CardContainer.show

    def traced_show(self):
        if self is tm._global_left_container:
            print("[CardContainer.show] 调用栈：")
            traceback.print_stack()
        return orig_show(self)

    CardContainer.show = traced_show

    btn = tm.titleBar._sidebar_btn
    print(f"\n>>> 点击标题栏折叠按钮 (tooltip={btn.toolTip()})")
    btn.click()
    for _ in range(6):
        QApplication.processEvents()
    T._pump(200)
    wr = tm._global_left_wrapper
    from app.widgets.tab_manager_window import _DockSideWrapper
    print(f"[+200ms] primary hidden={wr._primary.isHidden()} intent="
          f"{_DockSideWrapper._child_visible_intent(wr._primary)} | "
          f"stack hidden={wr._stack.isHidden()} intent="
          f"{_DockSideWrapper._child_visible_intent(wr._stack)} | "
          f"wrapper hidden={wr.isHidden()} slots={tm._sidebar_splitter.sizes()}")
    T._pump(600)
    print(f"[+800ms] primary hidden={wr._primary.isHidden()} intent="
          f"{_DockSideWrapper._child_visible_intent(wr._primary)} | "
          f"stack hidden={wr._stack.isHidden()} intent="
          f"{_DockSideWrapper._child_visible_intent(wr._stack)} | "
          f"slots={tm._sidebar_splitter.sizes()}")
    _state(tm, "点击折叠按钮后")

    print(f"\n预期收起宽 frame = {_COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X}")


if __name__ == "__main__":
    main()
