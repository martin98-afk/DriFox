# -*- coding: utf-8 -*-
"""[DEBUG-qcard-jitter E1] 验证核心事实：heightForWidth(w) 被卡片当前实际宽度污染。

QuestionFloatingWidget.heightForWidth(w) → QVBoxLayout.heightForWidth(w) →
子控件 sizeHint()。而 _OptionRadioCard.sizeHint() / _AutoHeightScrollArea.sizeHint()
在 Python 覆写里用 self.width() / viewport().width()（当前实际宽度）而非参数 w。

若成立：布局中间态（卡片实际宽度 ≠ 容器将要稳定的宽度）时，
CardContainer._follow_content_natural_h() 拿到的 natural_h 是错的，
锁 min/max + setSizes 后布局重排 → 宽度变化 → heightChanged → 再锁
→ 容器高度多次跳变 → 内部元素抖动。

判定：同一参数 w 下，不同"卡片实际宽度"算出的 heightForWidth(w) 差异 > 阈值
即证明污染存在。
运行：QT_QPA_PLATFORM=offscreen python tests/debug/question_card_jitter_e1.py
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


def build(qapp, width):
    host = QWidget()
    host.resize(width, 800)
    outer = QVBoxLayout(host)
    outer.setContentsMargins(0, 0, 0, 0)
    sp = QSplitter(Qt.Vertical)
    sp.setChildrenCollapsible(False)
    outer.addWidget(sp)
    chat = QWidget()
    sp.addWidget(chat)
    container = BottomCardContainer()
    sp.addWidget(container)
    container.enable_dock_mode(sp)

    WID_SEQ[0] += 1
    wid = f"e1-{WID_SEQ[0]}"
    card = QuestionFloatingWidget(host)
    card.setVisible(False)
    container.add_card("question", card)
    mgr = CardManager.get_instance()
    mgr.register_window(wid)
    container.bind_card_manager(mgr, wid)
    mgr.register_card(wid, ContainerType.BOTTOM, "question", card)
    return host, card


QUESTIONS = [
    {"question": "选择实现方案：", "options": [
        {"label": "方案A", "description": "使用状态机重构，保留现有对外接口不变，内部逻辑全部重写，测试覆盖率需要重新补齐"},
        {"label": "方案B", "description": "最小改动，在现有回调里加判断分支，风险低但可维护性差，后续每次扩展都要碰这段代码"},
        {"label": "方案C", "description": "引入第三方库直接替换，需要评估许可证兼容性和依赖体积变化，以及团队学习成本"},
    ], "multiple": False},
]


def main(qapp):
    # 卡片在窄宿主里填充内容并布局稳定（实际宽度 600）
    host1, card1 = build(qapp, 600)
    host1.show()
    card1.setVisible(True)
    card1.show_question(QUESTIONS)
    _flush(qapp, 120)
    w1 = card1.width()
    hfw_at_w1 = card1.heightForWidth(w1)

    # 同样内容在宽宿主里布局稳定（实际宽度 1180）
    host2, card2 = build(qapp, 1200)
    host2.show()
    card2.setVisible(True)
    card2.show_question(QUESTIONS)
    _flush(qapp, 120)
    w2 = card2.width()
    hfw_at_w2 = card2.heightForWidth(w2)

    print(f"窄卡片: 实际宽={w1}, heightForWidth({w1})={hfw_at_w1}")
    print(f"宽卡片: 实际宽={w2}, heightForWidth({w2})={hfw_at_w2}")
    print(f"参数=自身宽度时高度差: {abs(hfw_at_w2 - hfw_at_w1)} px（应 >0，宽度敏感内容）")
    print()

    # ── 污染检验：参数同为 w2，卡片实际宽度不同 ──
    # 在窄卡片上问"如果容器宽是 w2，你该多高"——布局中间态的典型问法
    polluted = card1.heightForWidth(w2)
    clean = card2.heightForWidth(w2)
    delta = abs(polluted - clean)
    print(f"[污染检验] 参数 w={w2}:")
    print(f"  窄卡片(实际宽{w1}) 算出: {polluted}")
    print(f"  宽卡片(实际宽{w2}) 算出: {clean}")
    print(f"  差值: {delta} px")
    verdict = "❌ 污染存在：heightForWidth 结果依赖卡片当前实际宽度，布局中间态必算错高" if delta > 2 \
        else "OK：heightForWidth 不受实际宽度污染"
    print(f"  判定: {verdict}")

    # ── 反向：宽卡片问窄宽度 ──
    polluted2 = card2.heightForWidth(w1)
    delta2 = abs(polluted2 - hfw_at_w1)
    print(f"\n[反向检验] 参数 w={w1}: 宽卡片算出 {polluted2} vs 窄卡片真值 {hfw_at_w1}, 差 {delta2} px")

    # ── 子控件级别定位：哪个子控件 sizeHint 被实际宽度污染 ──
    print("\n[子控件 sizeHint 检查] （参数无关，纯当前宽度函数）")
    for i in range(card1.layout().count()):
        it = card1.layout().itemAt(i)
        w = it.widget()
        if w is None:
            continue
        sh = w.sizeHint()
        hfw = w.heightForWidth(w1) if w.hasHeightForWidth() else None
        print(f"  item{i} {type(w).__name__}: sizeHint={sh.width()}x{sh.height()} "
              f"hfw({w1})={hfw} actual_w={w.width()}")

    # 清理
    host1.hide()
    host2.hide()


if __name__ == "__main__":
    qapp = QApplication.instance() or QApplication(sys.argv)
    main(qapp)
