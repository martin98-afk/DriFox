# -*- coding: utf-8 -*-
"""_ContentStack 状态机 fuzz 探针：随机交错 setCurrentIndex / slide_over / slide_swap /
resize / 打断收尾，校验终态不变量：
  1. _current 页可见且几何完全在容器内（归位，无画外残留）
  2. _current 页 z 序在最顶（无其他页遮挡）
  3. _current != 0 时 page0（对话页）必须 hidden；== 0 时必须 visible

「切到对话后整个对话区空白」的候选根因都落在上述不变量被破坏的某个子集。
跑法：.venv/bin/python tests/debug/probe_content_stack_fuzz.py [轮数]
"""
import random
import sys

from PyQt5.QtCore import Qt, QTimer, QCoreApplication
from PyQt5.QtWidgets import QApplication, QWidget

from app.widgets.tab_manager_window import _ContentStack

app = QApplication(sys.argv + ["-platform", "offscreen"])


def make_stack():
    s = _ContentStack()
    s.resize(800, 600)
    p0 = QWidget(s)
    p1 = QWidget(s)
    s.addWidget(p0)
    s.addWidget(p1)
    s.show()
    return s, [p0, p1]


def invariants(s, pages, tag, failures):
    cur = s.currentIndex()
    w = s.width()
    for i, p in enumerate(pages):
        geo = p.geometry()
        # 不变量 2：非当前页不得遮挡当前页（z 序）
        if i != cur and p.isVisible() and pages[cur].isVisible():
            if p.geometry().intersects(pages[cur].geometry()) and p.testAttribute(Qt.WA_UnderMouse) is False:
                # 同矩形叠放是常态（保活设计），只有非当前页**在最顶**才算遮挡
                pass
        # z 序检查：children 顺序
    # 用 z 序替代几何遮挡判断
    order = [id(c) for c in s.children() if isinstance(c, QWidget)]
    top_widget = None
    for c in s.children():
        if isinstance(c, QWidget):
            top_widget = c  # children 列表末尾是 z 序最高
    if top_widget is not None and top_widget in pages and top_widget is not pages[cur]:
        # 覆盖层页保活设计下允许垫底，但**最高层**必须是当前页
        failures.append(f"{tag}: z 顶是 page{pages.index(top_widget)} 而非 current page{cur}")
    cp = pages[cur]
    if not cp.isVisible():
        failures.append(f"{tag}: current page{cur} 不可见")
    geo = cp.geometry()
    if geo.x() > 0 or geo.x() + geo.width() < w or geo.width() != w:
        failures.append(f"{tag}: current page{cur} 几何未归位 {geo} (容器宽 {w})")
    # 不变量 3：对话页显隐与 _current 一致
    if cur != 0 and pages[0].isVisible():
        failures.append(f"{tag}: _current={cur} 但对话页仍 visible（应 hide）")
    if cur == 0 and not pages[0].isVisible():
        failures.append(f"{tag}: _current=0 但对话页 hidden")


def run_round(rng, round_id, failures, done):
    s, pages = make_stack()
    n_ops = rng.randint(3, 15)

    def op_swap():
        # 真实调用点守卫：卡间互斥切换只在覆盖层前台（idx==1）且无在途动画时发生
        d = rng.choice([-1, 1])
        if s.currentIndex() == 1:
            s.slide_swap(direction=d, swap=lambda: None)

    def op_ci_random():
        s.setCurrentIndex(rng.choice([0, 1]), direction=rng.choice([-1, 0, 1]))

    def op_over():
        # 真实调用点守卫：slide_over 仅在覆盖层前台且无在途切换动画时被调
        if s.currentIndex() == 1 and not s.slide_in_progress():
            s.slide_over(direction=rng.choice([-1, 1]))

    def op_resize():
        s.resize(rng.randint(600, 1000), 600)  # 触发 resizeEvent（在途只同步几何）

    def op_chat():
        s.setCurrentIndex(0, direction=-1)  # 模拟回对话

    def op_overlay():
        s.setCurrentIndex(1, direction=1)  # 模拟进覆盖层

    ops = [op_ci_random, op_over, op_swap, op_resize, op_chat, op_overlay]

    state = {"i": 0}
    trace = []
    snaps = []

    def snap(label):
        kids = [c for c in s.children() if isinstance(c, QWidget)]
        order = [f"p{pages.index(k)}" if k in pages else "?" for k in kids]
        snaps.append(
            f"{label} cur={s.currentIndex()} z={order} vis={[p.isVisible() for p in pages]} "
            f"g=[{','.join(str(p.geometry().x()) for p in pages)}] "
            f"anim={'Y' if s.slide_in_progress() else 'N'} W={s.width()}"
        )

    def step():
        if state["i"] >= n_ops:
            # 动画终态快进，等所有 finished 信号落地
            def finish():
                if s.slide_in_progress():
                    s._finish_slide(interrupted=False)
                if s.slide_in_progress():  # swap 段2 可能接力了新动画
                    s._finish_slide(interrupted=False)
                pre = len(failures)
                invariants(s, pages, f"R{round_id} 终态", failures)
                if len(failures) > pre:
                    print(f"R{round_id} 序列: {trace}")
                    for line in snaps:
                        print("   ", line)
                    snap("终态")
                    print("   ", snaps[-1])
                s.deleteLater()
                done()

            QTimer.singleShot(450, finish)  # > SLIDE_MS(300) 等自然收尾
            return
        try:
            op = rng.choice(ops)
            name = op.__name__ if hasattr(op, "__name__") else str(op)
            trace.append(name)
            op()
            snap(f"op{state['i']}:{name}")
        except Exception as e:
            failures.append(f"R{round_id} op{state['i']} 异常: {e!r}")
        state["i"] += 1
        QTimer.singleShot(rng.choice([0, 5, 30, 80, 320]), step)  # 覆盖在途/已收尾/已超时三种时机

    step()


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    rng = random.Random(20261028)
    failures = []
    state = {"r": 0}

    def next_round():
        if state["r"] >= rounds:
            if failures:
                print(f"FAIL {len(failures)} 条：")
                seen = set()
                for f in failures:
                    key = f.split(":")[0] + f.split(":", 1)[1][:40] if ":" in f else f
                    if key not in seen:
                        seen.add(key)
                        print(" -", f)
                sys.exit(1)
            print(f"PASS {rounds} 轮不变量校验")
            app.quit()
            return
        r = state["r"]
        state["r"] += 1
        run_round(rng, r, failures, lambda: QTimer.singleShot(0, next_round))

    QTimer.singleShot(0, next_round)
    app.exec_()


if __name__ == "__main__":
    main()
