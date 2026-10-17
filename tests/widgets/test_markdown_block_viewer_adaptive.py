# -*- coding: utf-8 -*-
"""[X1 饿死守卫] 无边界快速流式下自适应派发拍必须能到期。

回归背景：_schedule_render 无边界分支曾无条件 _render_timer.start()——
QTimer.start() 对激活中的单发定时器重置倒计时，chunk 间隔（8ms）小于档位
（150/300/500ms）时每 chunk 重启 → 倒计时永不到期 → 纯代码流/无句号英文段
格式化渲染饿死至 finish_streaming。修复为 isActive 守卫：激活中不重启。

本用例模拟 8ms 间隔无边界 chunk 流 ~2s，断言 _perform_update 至少触发一次。
"""

import time

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest


def test_adaptive_dispatch_not_starved_on_boundaryless_stream(qapp):
    from app.widgets.markdown_block_viewer import MarkdownBlockViewer

    viewer = MarkdownBlockViewer()
    # 计数挂 _reconcile：_perform_update 内部为运行时属性查找，实例级替换可被捕获
    # （直接 wrap _perform_update 无效——timeout.connect 在构造期已绑定原方法对象）
    perform_calls = {"n": 0}
    orig = viewer._reconcile

    def counting_reconcile(force=False):
        perform_calls["n"] += 1
        orig(force)

    viewer._reconcile = counting_reconcile

    # 无边界文本：无空行、无句号、无代码 fence——三种边界永不命中
    unit = "lorem ipsum dolor sit amet consectetur "
    chunk = unit * 2  # 80 字符/chunk
    sent = 0
    total_chunks = 250  # 8ms × 250 ≈ 2s

    def feed():
        nonlocal sent
        if sent >= total_chunks:
            feed_timer.stop()
            return
        sent += 1
        viewer.append_chunk(chunk)

    feed_timer = QTimer()
    feed_timer.setInterval(8)
    feed_timer.timeout.connect(feed)
    feed_timer.start()

    deadline = time.monotonic() + 5.0
    # 流式中途（约 1s 处）断言：饿死场景下此刻派发拍被 chunk 持续重置，
    # reconcile 零触发；修复后 ~150ms 内首拍落地
    while sent < total_chunks // 2 and time.monotonic() < deadline:
        QTest.qWait(20)
    assert perform_calls["n"] >= 1, (
        "无边界快速流式进行中 _perform_update 零触发：自适应派发拍被 chunk 重启饿死"
    )
    while sent < total_chunks and time.monotonic() < deadline:
        QTest.qWait(20)
    feed_timer.stop()
    QTest.qWait(600)  # 等最后一拍派发落地

    assert sent == total_chunks, f"投递未完成 {sent}/{total_chunks}"
    assert perform_calls["n"] >= 1, (
        "无边界快速流式 2s 内 _perform_update 零触发：自适应派发拍被 chunk 重启饿死"
    )
