# -*- coding: utf-8 -*-
"""[DEBUG-qcard-blank] 复现「提问卡片下方空白反复出现/消失」。

症状：提问卡片（followContent）可见期间，卡片下方不停出现/消失一块空白区域。

机制假设 H1（状态卡共存 → follow_content 失效 → 30% 占比地板）：
  BOTTOM 是共存容器，question 强制清场对 L2 状态卡豁免
  （card_manager._hide_same_container_cards 默认 exempt_stackable=True）。
  → sub_agent_compact / message_queue / undo_delete 与 question 同容器共存时
    _visible_cards_follow_content() = False（要求 ALL 可见卡都声明 followContent）
  → _do_expand 走 dock 通用分支：target = max(mem, 30%×对话区高)
  → 容器槽位 ≫ 卡片内容高度 → 卡片下方空白
  → 状态卡 visible_when 谓词翻转（refresh_layer）→ follow/非 follow 分支交替
  → 空白反复出现/消失

场景：
  F1  question + 状态卡共存（静止）→ 空白应出现（容器槽位 - 卡片底 > 阈值）
  F2  状态卡谓词每 400ms 翻转 → 空白应反复出没（方向交替）
  F3  F1 态下检查 _dock_card_sizes 是否被程序化 setSizes 污染（splitterMoved）
运行：QT_QPA_PLATFORM=offscreen python tests/debug/question_card_blank_repro.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QLabel, QSplitter, QVBoxLayout, QWidget

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


class _StatusCard(QWidget):
    """模拟 L2 状态卡（sub_agent_compact 等）：定高、随容器宽度自适应"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(QLabel("状态卡占位（模拟 sub_agent_compact）"))


class Harness:
    def __init__(self, qapp, width=1200, height=800):
        WID_SEQ[0] += 1
        self.wid = f"blank-win-{WID_SEQ[0]}"
        self.qapp = qapp
        self.host = QWidget()
        self.host.resize(width, height)
        outer = QVBoxLayout(self.host)
        outer.setContentsMargins(0, 0, 0, 0)

        self.vsplitter = QSplitter(Qt.Vertical)
        self.vsplitter.setChildrenCollapsible(False)
        self.vsplitter.setHandleWidth(6)
        outer.addWidget(self.vsplitter)

        self.chat_wrapper = QWidget()
        wl = QVBoxLayout(self.chat_wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(QLabel("对话区占位"), 1)
        self.input_placeholder = QWidget()
        self.input_placeholder.setFixedHeight(60)
        wl.addWidget(self.input_placeholder)

        self.container = BottomCardContainer()
        self.vsplitter.addWidget(self.chat_wrapper)
        self.vsplitter.addWidget(self.container)
        self.vsplitter.setStretchFactor(0, 1)
        self.vsplitter.setStretchFactor(1, 0)
        self.container.enable_dock_mode(self.vsplitter)

        self.card = QuestionFloatingWidget(self.host)
        self.card.setVisible(False)
        self.container.add_card("question", self.card)

        # 模拟 L2 状态卡：layer=status + stackable=True（与真实注册一致）
        self.status = _StatusCard(self.host)
        self.status.setVisible(False)
        self.mgr = CardManager.get_instance()
        self.mgr.register_window(self.wid)
        self.container.bind_card_manager(self.mgr, self.wid)
        self.mgr.mark_coexist_containers(self.wid, frozenset({ContainerType.BOTTOM}))
        self.mgr.register_card(
            self.wid,
            ContainerType.BOTTOM,
            "question",
            self.card,
        )
        self.mgr.register_card(
            self.wid,
            ContainerType.BOTTOM,
            "status_dummy",
            self.status,
            layer="status",
            stackable=True,
            order_hint=10,
            visible_when=lambda: self._status_should_show,
        )
        self.container.add_card("status_dummy", self.status)
        self._status_should_show = False

        self.samples = []

    # ── 观测 ──

    def blank_px(self):
        """卡片底部到容器槽位底部的空白高度（容器背景可见区）"""
        if self.card.isHidden():
            return -1
        card_bottom = self.card.y() + self.card.height()
        return self.container.height() - card_bottom

    def snapshot(self, tag=""):
        self.samples.append(
            {
                "tag": tag,
                "cont_h": self.container.height(),
                "card_h": self.card.height(),
                "blank": self.blank_px(),
                "follow": self.container._visible_cards_follow_content(),
                "status_visible": not self.status.isHidden(),
                "sp": list(self.vsplitter.sizes()),
                "mem_q": self.container._dock_card_sizes.get("question", 0),
                "min": self.container.minimumHeight(),
                "max": self.container.maximumHeight(),
            }
        )
        return self.samples[-1]

    def run_frames(self, n=60, interval=16):
        out = []

        def tick():
            self.qapp.processEvents()
            out.append(self.snapshot())
            if len(out) < n:
                QTimer.singleShot(interval, tick)

        QTimer.singleShot(interval, tick)
        _flush(self.qapp, interval * n + 300)
        return out

    def ask(self, questions):
        self.input_placeholder.setVisible(False)
        self.card.setUpdatesEnabled(False)
        self.card.show_question(questions)
        self.card.setUpdatesEnabled(True)
        lay = self.card.layout()
        if lay is not None:
            lay.invalidate()
        self.card.updateGeometry()
        self.mgr.show_card("question", self.wid)
        QTimer.singleShot(200, self.container._do_expand)


QS = [
    {
        "question": "SVG 改动只在主仓库 dev（4cd897ac），pyside6 worktree 落后 16 commit。你想怎么走？",
        "options": [
            {"label": "切主仓库 dev 跑验证", "description": "cmd: cd /d D:\\work\\DriFox && .venv\\Scripts\\python.exe main.py。在主仓库 dev 上看 SVG 效果，pyside6 worktree 不动"},
            {"label": "Cherry-pick 到 pyside6", "description": "在 .worktrees/pyside6 里 git cherry-pick 4cd897ac，pyside6 也有 SVG。可能有冲突（pyside6 改动多）"},
            {"label": "放弃改动", "description": "不跑了 / 还原改动 / 换个思路"},
        ],
        "multiple": False,
    },
]


def analyze(tag, samples, expect_blank=False):
    blanks = [s["blank"] for s in samples if s["blank"] >= 0]
    follow_flags = [s["follow"] for s in samples]
    max_blank = max(blanks) if blanks else 0
    blank_turns = 0
    prev_state = None
    for b in blanks:
        state = b > 8
        if prev_state is not None and state != prev_state:
            blank_turns += 1
        prev_state = state
    ok = True
    print(f"\n=== [{tag}] ===")
    print(f"  follow_content 判定序列（去重）: {_dedup(follow_flags)}")
    print(f"  空白峰值: {max_blank}px, 空白状态翻转次数: {blank_turns}")
    keys = [0, 2, 5, 10, 20, 40, len(samples) - 1]
    print("  帧 | cont_h card_h blank follow status_visible mem_q min max | splitter")
    for i in keys:
        if i < len(samples):
            s = samples[i]
            print(
                f"  {i:3d} | {s['cont_h']:6d} {s['card_h']:6d} {s['blank']:5d} {str(s['follow']):5s}"
                f" {str(s['status_visible']):5s} {s['mem_q']:5d} {s['min']:5d} {s['max']:8d} | {s['sp']}"
            )
    return ok, max_blank, blank_turns


def _dedup(seq):
    out = []
    for v in seq:
        if not out or out[-1] != v:
            out.append(v)
    return out


def main(qapp):
    # ── F1: question + 状态卡共存（静止）──
    print("=" * 70)
    print("F1: 提问卡 + L2 状态卡共存（状态卡持续可见）")
    h = Harness(qapp)
    h.host.show()
    _flush(qapp, 60)
    # 状态卡先可见（模拟 subagent 运行中提问 / 排队消息存在）
    h._status_should_show = True
    h.mgr.refresh_layer(h.wid, "status")
    _flush(qapp, 30)
    h.ask(QS)
    s1, max_blank, turns = analyze("F1-共存静止", h.run_frames(50))
    print(f"  判定: {'❌ 空白出现（' + str(max_blank) + 'px）' if max_blank > 8 else 'OK 未出现空白'}")

    # ── F2: 状态卡谓词翻转 → 空白反复出没 ──
    print("=" * 70)
    print("F2: 状态卡每 400ms 显隐翻转（模拟谓词翻转 / 队列变动）")
    h2 = Harness(qapp)
    h2.host.show()
    _flush(qapp, 60)
    h2.ask(QS)
    _flush(qapp, 300)

    def flip():
        h2._status_should_show = not h2._status_should_show
        h2.mgr.refresh_layer(h2.wid, "status")

    flip_timer = QTimer()
    flip_timer.setInterval(400)
    flip_timer.timeout.connect(flip)
    flip_timer.start()
    samples2 = h2.run_frames(70)
    flip_timer.stop()
    s2, max_blank2, turns2 = analyze("F2-谓词翻转", samples2)
    print(f"  判定: {'❌ 空白反复出没（峰值 ' + str(max_blank2) + 'px, 翻转 ' + str(turns2) + ' 次）' if max_blank2 > 8 and turns2 >= 2 else 'OK'}")

    # ── F3: F1 态下 memory 污染检查 ──
    print("=" * 70)
    print("F3: 共存态（非 follow 分支）下 question 卡的记忆尺寸是否被污染")
    mem = h.container._dock_card_sizes.get("question", 0)
    print(f"  _dock_card_sizes['question'] = {mem}  （follow 分支锁高值应不进入此表）")
    print(f"  判定: {'⚠️ 已污染' if mem > 0 else 'OK'}")


if __name__ == "__main__":
    qapp = QApplication.instance() or QApplication(sys.argv)
    main(qapp)
