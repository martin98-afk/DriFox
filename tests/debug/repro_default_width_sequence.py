# -*- coding: utf-8 -*-
"""复现：360 默认宽下限在开/关/重开/切卡全序列的实际宽度

用户反馈默认宽度没变。本脚本量各环节卡片槽位宽，定位哪条路径绕过下限。
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

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication(["repro"])

import test_left_card_sidebar_embed as T  # noqa: E402

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry  # noqa: E402


def _card_w(tm, tag):
    sp = tm._sidebar_splitter
    idx = sp.indexOf(tm._global_left_wrapper)
    print(f"[{tag}] 卡片槽位={sp.sizes()[idx] if 0 <= idx < len(sp.sizes()) else '?'} "
          f"frame={tm._splitter.sizes()[0]} active={tm._left_card_area_active()}")


def main():
    tm = T._make_tm()

    # 打点：内层 sidebarSplitter 每次setSizes 的值与调用者
    import traceback
    sp0 = tm._sidebar_splitter
    orig = sp0.setSizes

    def traced(vals):
        stk = traceback.extract_stack(limit=2)[-2]
        print(f"    setSizes({vals})  <- {stk.filename.split(chr(92))[-1]}:{stk.lineno} {stk.name}")
        return orig(vals)

    sp0.setSizes = traced

    # 打点：三个关键决策值
    from app.widgets.cards.card_container import CardContainer

    orig_rds = CardContainer._restore_dock_size

    def traced_rds(self, target):
        print(f"    [容器] _restore_dock_size(target={target}) _last_expand_target={self._last_expand_target}")
        return orig_rds(self, target)

    CardContainer._restore_dock_size = traced_rds

    tmw_cls = type(tm)
    orig_oct = tmw_cls._on_left_card_area_toggled

    def traced_oct(self, expanded):
        wrapper = getattr(self, "_global_left_wrapper", None)
        cont = getattr(wrapper, "_primary", None)
        print(f"    [宿主] _on_left_card_area_toggled({expanded}) extra="
              f"{self._left_card_frame_extra()} intent={getattr(cont, '_last_expand_target', '?')}")
        return orig_oct(self, expanded)

    tmw_cls._on_left_card_area_toggled = traced_oct

    reg = UIPluginRegistry.get_instance()
    _card_w(tm, "初始")

    # ① 首开（无记忆）
    T._show_left_card(tm, "tp-w-first")
    _card_w(tm, "①首开")
    cont = tm._global_left_container
    print(f"    容器 mem(first)={cont._dock_card_sizes.get('tp-w-first')}")
    print(f"    容器 _last_expand_target={cont._last_expand_target}")

    # ② 关闭
    reg.toggle_floating_card("tp-w-first")
    for _ in range(6):
        QApplication.processEvents()
    T._pump(400)
    _card_w(tm, "②关闭")

    # ③ 重开（有记忆）
    reg.toggle_floating_card("tp-w-first")
    for _ in range(6):
        QApplication.processEvents()
    T._pump(400)
    _card_w(tm, "③重开")
    print(f"    容器 mem(first)={cont._dock_card_sizes.get('tp-w-first')}")

    # ④ 换一张卡（互斥切卡路径）
    reg.register_floating_card("tp", "tp-w-second", QWidget, "left", title="T2")
    reg.toggle_floating_card("tp-w-second")
    for _ in range(6):
        QApplication.processEvents()
    T._pump(400)
    _card_w(tm, "④切新卡")
    print(f"    容器 mem(second)={cont._dock_card_sizes.get('tp-w-second')}")

    T._close_all_left_cards(tm)


if __name__ == "__main__":
    main()
