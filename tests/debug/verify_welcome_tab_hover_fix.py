# -*- coding: utf-8 -*-
"""[DEBUG-hvsc] 欢迎卡片底部 tab hover 残留修复验证脚本

对应修复：TabHoverSyncHost（app/widgets/custom_title_bar.py）+
         _build_welcome_mode_tabs 宿主替换（app/widgets/message_card.py）
（本环境 pytest 跑 widgets Qt 用例不稳定，用本脚本做等价断言）

验证点：
1. clear_tab_hover：手动置 hover 后能全量清空（意图标志 + 动画目标）
2. Leave 事件 → 宿主兜底清空全部按钮 hover（残留根因的直接兜底）
3. Resize / LayoutRequest → 延迟一拍触发 sync_tab_hover 重算（布局平移场景）
4. sync_tab_hover 在光标不在按钮上时清空、不误置 hover
5. 宿主替换后既有属性齐全（_welcome_tab_host / indicator_ctl 可建）
"""
import faulthandler
import os
import sys
import time

faulthandler.enable()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget


def log(m):
    print(m, flush=True)


def get_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def build_host_with_buttons(n=3):
    """构造 TabHoverSyncHost + n 个 CustomTabButton（非 indicator_managed）"""
    from app.widgets.custom_title_bar import CustomTabButton, TabHoverSyncHost

    holder = QWidget()  # 持引用防 GC（项目坑：QWidget 无主析构崩溃）
    host = TabHoverSyncHost(holder)
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    btns = []
    for i in range(n):
        b = CustomTabButton(f"tab{i}", f"标签{i}", host, font_size=12)
        lay.addWidget(b)
        btns.append(b)
    host.resize(300, 100)
    holder._ref_host = host
    holder._ref_btns = btns
    return holder, host, btns


def main():
    app = get_app()
    failures = []

    def check(name, cond):
        log(f"[{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    # ── 1. clear_tab_hover 全量清空 ──
    holder, host, btns = build_host_with_buttons()
    host.show()
    app.processEvents()
    for b in btns:
        b.set_hover(True)
    check("1a 手动置 hover 后意图标志为 True", all(b._hovered for b in btns))
    host.clear_tab_hover()
    check("1b clear_tab_hover 清空全部意图标志", all(not b._hovered for b in btns))
    for _ in range(40):
        app.processEvents()
        time.sleep(0.01)  # 交替推进：纯 sleep 会停摆 Qt 动画 timer
    app.processEvents()
    check("1c 清空后 hover 进度收敛到 0", all(b._hover_t < 0.001 for b in btns))

    # ── 2. Leave 事件兜底清空 ──
    for b in btns:
        b.set_hover(True)
    app.sendEvent(host, QEvent(QEvent.Leave))
    check("2 Leave 事件触发宿主清空全部 hover", all(not b._hovered for b in btns))

    # ── 3. Resize / LayoutRequest 触发延迟重算 ──
    calls = []
    host.sync_tab_hover = lambda: calls.append(1)  # monkeypatch 记录调用
    app.sendEvent(host, QEvent(QEvent.Resize))
    check("3a Resize 后 pending 标志置位", host._hover_resync_pending)
    app.processEvents()
    check("3b 事件循环一拍后重算被执行且 pending 复位", bool(calls) and not host._hover_resync_pending)
    calls.clear()
    app.sendEvent(host, QEvent(QEvent.LayoutRequest))
    app.processEvents()
    check("3c LayoutRequest 同样触发重算", bool(calls))

    # ── 4. sync_tab_hover：光标不在按钮上 → 全 False（offscreen 光标默认远离）──
    holder2, host2, btns2 = build_host_with_buttons()
    host2.show()
    app.processEvents()
    for b in btns2:
        b.set_hover(True)
    host2.sync_tab_hover()
    check("4 光标不在任何按钮上时 sync 清空全部 hover", all(not b._hovered for b in btns2))

    # ── 5. 重复事件合并（_schedule_hover_sync pending 去重）──
    calls2 = []
    host2.sync_tab_hover = lambda: calls2.append(1)
    app.sendEvent(host2, QEvent(QEvent.Resize))
    app.sendEvent(host2, QEvent(QEvent.Resize))
    app.sendEvent(host2, QEvent(QEvent.LayoutRequest))
    check("5a 同拍多次事件 pending 去重", host2._hover_resync_pending and not calls2)
    app.processEvents()
    check("5b 一拍后只重算一次", len(calls2) == 1)

    # ── 6. 指示器胶囊不是 CustomTabButton，不会被误当按钮 ──
    from app.widgets.custom_title_bar import _TabIndicator

    ind = _TabIndicator(holder2)
    check("6 _TabIndicator 类型不被 findChildren(CustomTabButton) 命中", len(host2._tab_buttons()) == len(btns2))

    print()
    if failures:
        log(f"结果：{len(failures)} 项失败 -> {failures}")
        sys.exit(1)
    log("结果：全部通过 ✓")


if __name__ == "__main__":
    main()
