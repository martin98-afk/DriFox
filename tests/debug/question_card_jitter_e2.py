# -*- coding: utf-8 -*-
"""[DEBUG-qcard-jitter E2] 验证 skip_anim 误判：容器隐藏时卡片 show → 动画误启动。

链路（真实 _on_question_asked）：
  容器处于折叠态 = hide()
  card_mgr.show_card → card.setVisible(True)   # 卡片自身 WA_WState_Visible 置位，
                                               # 但父容器隐藏 → isVisible()=False
  → _on_card_shown → _schedule_expand → _do_expand
     has_visible        = isHidden() 语义 → True（意图可见）
     follow_content     = isHidden() 语义 → True
     skip_anim          = isVisible() 语义 → False ❌（父链断导致误判）
  → _animate_height 启动 200ms 动画
  → 下一轮 singleShot(0)（show_question / showEvent 的 heightChanged）
  → _do_expand 再次执行 → stop 动画 → snap
  → 视觉：出现瞬间高度多次跳变 = 内部元素快速抖动

验证点：
  E2-1 isVisible/isHidden 语义差
  E2-2 _should_skip_animation 在该时序下的返回值
  E2-3 _do_expand 是否启动动画（monkeypatch _animate_height）
运行：QT_QPA_PLATFORM=offscreen python tests/debug/question_card_jitter_e2.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QSplitter, QVBoxLayout, QWidget

from app.widgets.cards.card_container import BottomCardContainer, ContainerType
from app.widgets.cards.card_manager import CardManager
from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

WID_SEQ = [0]


def _flush(qapp, ms=120):
    loop = QTimer()
    loop.setSingleShot(True)
    loop.start(ms)
    while loop.isActive():
        qapp.processEvents()


def main(qapp):
    host = QWidget()
    host.resize(1200, 800)
    outer = QVBoxLayout(host)
    outer.setContentsMargins(0, 0, 0, 0)
    vsplitter = QSplitter(Qt.Vertical)
    vsplitter.setChildrenCollapsible(False)
    outer.addWidget(vsplitter)
    chat = QWidget()
    vsplitter.addWidget(chat)
    container = BottomCardContainer()
    vsplitter.addWidget(container)
    container.enable_dock_mode(vsplitter)

    WID_SEQ[0] += 1
    wid = f"e2-{WID_SEQ[0]}"
    card = QuestionFloatingWidget(host)
    card.setVisible(False)
    container.add_card("question", card)
    mgr = CardManager.get_instance()
    mgr.register_window(wid)
    container.bind_card_manager(mgr, wid)
    mgr.register_card(wid, ContainerType.BOTTOM, "question", card)

    # 仪表：动画启动记录
    anim_log = []
    orig_anim = container._animate_height

    def traced_anim(start_h, end_h, on_finished=None):
        anim_log.append((start_h, end_h))
        print(f"  [ANIM] 启动动画 {start_h} -> {end_h}")
        orig_anim(start_h, end_h, on_finished)

    container._animate_height = traced_anim

    host.show()
    _flush(qapp, 60)
    assert container.isHidden(), "前置：容器应处于折叠隐藏态"

    # 填充内容（卡片仍隐藏、容器仍隐藏）
    card.show_question(
        [
            {
                "question": "选择实现方案：",
                "options": [
                    {"label": "方案A", "description": "使用状态机重构，保留现有对外接口不变，内部逻辑全部重写"},
                    {"label": "方案B", "description": "最小改动，在现有回调里加判断分支，风险低但可维护性差"},
                ],
                "multiple": False,
            },
        ]
    )
    _flush(qapp, 30)

    # ── 真实时序：容器隐藏时 setVisible(True) ──
    print("\n[E2-1] 语义对照：")
    card.setVisible(True)
    print(f"  card.isVisible()  = {card.isVisible()}   （受父链影响）")
    print(f"  card.isHidden()   = {card.isHidden()}    （仅自身意图）")
    print(f"  container.isHidden() = {container.isHidden()}")

    print("\n[E2-2] _should_skip_animation（此刻）:")
    skip = container._should_skip_animation()
    print(f"  返回 {skip}  → {'❌ 误判：声明了 noContainerAnimation 却返回 False' if not skip else 'OK'}")

    print("\n[E2-3] 执行 _do_expand（对应 _on_card_shown 立即路径）:")
    container._do_expand()
    if anim_log:
        print(f"  ❌ 动画被启动 {anim_log} —— 出现瞬间将先动画、后被 singleShot 链取消转 snap，高度多轮跳变")
    else:
        print("  OK：未启动动画（snap 路径）")
    print(f"  此刻容器高度: {container.height()}, min={container.minimumHeight()}, max={container.maximumHeight()}")

    # 模拟后续 singleShot 链（show_question / showEvent 的 heightChanged）
    print("\n[E2-4] 后续 singleShot 链（heightChanged → _do_expand）:")
    card.heightChanged.emit()
    _flush(qapp, 10)
    card.heightChanged.emit()
    _flush(qapp, 10)
    print(f"  累计动画启动次数: {len(anim_log)}")
    print(f"  容器高度: {container.height()}, min={container.minimumHeight()}, max={container.maximumHeight()}")

    # 200ms 安全网
    QTimer.singleShot(200, container._do_expand)
    _flush(qapp, 300)
    print(f"\n[E2-5] 200ms 安全网后: 动画总次数={len(anim_log)}, 容器高={container.height()}")

    verdict = "❌ 复现：skip_anim 误判导致出现瞬间动画-取消-snap 多轮跳变" if anim_log else "OK：无动画误启动"
    print(f"\n判定: {verdict}")


if __name__ == "__main__":
    qapp = QApplication.instance() or QApplication(sys.argv)
    main(qapp)
