"""窗口 resize 失效根因探针 v3（不修改业务代码，仅诊断）

v2 已确认：_system_buttons_left() 返回 0，导致顶边整条热区被判 HTCLIENT。
本探针追查：为什么 minBtn.x() == 0 —— 是标题栏宽度没同步，还是按钮未布局？
并实测"程序化 resize 是否真的改变窗口几何"，区分 hit-test 失效与布局冻结。
"""

import sys
import time

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


def dump_geometry(win, tag: str) -> None:
    tb = getattr(win, "titleBar", None)
    print(f"\n══ {tag} ══")
    print(f"主窗口: width={win.width()} height={win.height()} visible={win.isVisible()}")
    if tb is None:
        print("  titleBar = None ❌")
        return
    print(
        f"titleBar: x={tb.x()} y={tb.y()} w={tb.width()} h={tb.height()} "
        f"visible={tb.isVisible()} hidden={tb.isHidden()}"
    )
    print(f"  ★ 标题栏宽度是否同步到窗口宽度: {'✅' if tb.width() == win.width() else '❌ 未同步'}")

    for name in ("minBtn", "maxBtn", "closeBtn"):
        btn = getattr(tb, name, None)
        if btn is None:
            print(f"  {name}: None")
            continue
        print(
            f"  {name}: x={btn.x()} y={btn.y()} w={btn.width()} "
            f"hidden={btn.isHidden()} visible={btn.isVisible()}"
        )

    wb = getattr(tb, "_workbench_btn", None)
    if wb is not None:
        print(f"  workbench_btn: x={wb.x()} w={wb.width()} hidden={wb.isHidden()}")

    sbl = win._system_buttons_left()
    print(f"\n  → _system_buttons_left() = {sbl}")
    if sbl == 0:
        print("    ❌ 返回 0 ⇒ `x >= 0` 恒真 ⇒ 顶部整条热区全部判 HTCLIENT ⇒ 顶边 resize 失效")
    elif sbl >= win.width():
        print("    ⚠️ 返回窗口宽度 ⇒ 顶边让位逻辑未生效（minBtn 缺失/隐藏分支）")


def main() -> int:
    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_EnableHighDpiScaling, True)

    import logging

    logging.disable(logging.INFO)

    from app.widgets.tab_manager_window import TabManagerWindow

    win = TabManagerWindow.create_instance()
    win.resize(1000, 700)
    win.show()

    # 充分推进事件循环，确保布局完成
    for _ in range(60):
        app.processEvents()
    time.sleep(0.4)
    for _ in range(60):
        app.processEvents()

    dump_geometry(win, "启动后（布局完成后）")

    # ── 实测：程序化 resize 是否真的改变几何 ──
    print("\n══ 程序化 resize 实测 ══")
    w0, h0 = win.width(), win.height()
    win.resize(w0 + 120, h0 + 80)
    for _ in range(40):
        app.processEvents()
    time.sleep(0.3)
    for _ in range(40):
        app.processEvents()
    w1, h1 = win.width(), win.height()
    print(f"  resize 前: {w0}x{h0}")
    print(f"  resize 后: {w1}x{h1}")
    print(f"  {'✅ 窗口几何已改变' if (w1, h1) != (w0, h0) else '❌ 窗口几何未改变'}")
    print(f"  _resize_blocking = {getattr(win, '_resize_blocking', None)}（应回落 False）")

    tb = getattr(win, "titleBar", None)
    if tb is not None:
        print(
            f"  resize 后 titleBar.w={tb.width()} vs 窗口 w={win.width()} "
            f"{'✅ 同步' if tb.width() == win.width() else '❌ 未同步'}"
        )
        for name in ("minBtn", "maxBtn", "closeBtn"):
            btn = getattr(tb, name, None)
            if btn is not None:
                print(f"  resize 后 {name}.x = {btn.x()}")

    # 再等防抖结束
    time.sleep(0.5)
    for _ in range(40):
        app.processEvents()
    print(f"\n  静置后 _resize_blocking = {win._resize_blocking}")
    tb = getattr(win, "titleBar", None)
    if tb is not None:
        print(f"  静置后 titleBar.w={tb.width()} vs 窗口 w={win.width()}")
        print(f"  静置后 minBtn.x = {tb.minBtn.x()}")
        print(f"  静置后 _system_buttons_left() = {win._system_buttons_left()}")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
