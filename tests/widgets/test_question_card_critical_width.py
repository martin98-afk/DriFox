# -*- coding: utf-8 -*-
"""QuestionFloatingWidget 换行临界宽度下的自激循环回归测试

== 根因 ==
``CardContainer._follow_content_natural_h()`` 早期用「容器宽」作为
``card.heightForWidth()`` 的测量宽度，而卡片真实排版宽度 = 容器宽 - 容器左右
margins（``BottomCardContainer`` 各 8px，合计 16px）。

文本换行数一旦落在这 16px 窗口内，容器算出的高度就比卡片真实需要**少一行**：

    容器宽 1098 → hfw = 362（少一行）
    内容宽 1082 → hfw = 384（真实所需）

→ 容器锁死的高度低于卡片布局最小需求 → Qt 布局无解 → 重排 → Resize →
heightChanged → ``_do_expand``（仍用错宽度）→ 无限循环。

观感：提问卡片出现时卡内元素（标题/选项/底栏）每帧上下位移（实测
``_do_expand`` 323 次、底栏 y 在 327↔349 往复 22px）；窗口 resize 跳出
该宽度区间后循环断开，抖动立即停止。

== 断言 ==
1. 场景可达：存在「旧基准算出的高度 ≠ 真实排版高度」的宽度，否则 skip；
2. 这些宽度下跑出现流程：``_do_expand`` 次数收敛（<= 30）；
3. 30 帧后卡内元素位置完全静止（无往复位移）。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt

Qt.AA_ShareOpenGLContexts = Qt.AA_ShareOpenGLContexts

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PyQt5.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

# 临界文本：描述末尾贴近一行边界，16px 宽度差即可改变换行数
CRITICAL_QUESTION = [
    {
        "question": "请选择实现方案：这里是一段长度经过设计的标题文字，用于逼近换行临界点",
        "options": [
            {
                "label": "方案A",
                "description": "使用状态机重构内部实现逻辑，保持现有对外接口完全不变，"
                "同时补齐单元测试与集成测试覆盖率，并在文档中记录迁移步骤",
            },
            {
                "label": "方案B",
                "description": "在现有回调函数中增加判断分支，改动量最小，风险较低，"
                "但可维护性下降，后续每次扩展都需要重新审视这段核心代码",
            },
        ],
        "multiple": False,
    }
]

SCAN_RANGE = range(1000, 1400)
MAX_CRITICAL_CASES = 6
FRAMES = 36
STABLE_FROM = 24  # 第 24 帧后必须完全静止

_WID_SEQ = [0]


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class _Host:
    """真机结构：QVBoxLayout(对话区[拉伸], BottomCardContainer)

    对齐 app/main_widget.py 的装配（容器宽度由父布局分配，与高度解耦）。
    """

    def __init__(self, width: int, height: int = 800):
        _WID_SEQ[0] += 1
        self.wid = f"crit-{_WID_SEQ[0]}"
        self.win = QWidget()
        self.win.setFixedSize(width, height)
        outer = QVBoxLayout(self.win)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        chat = QWidget()
        chat.setStyleSheet("background:#333;")

        from app.widgets.cards.card_container import BottomCardContainer
        from app.widgets.cards.card_manager import CardManager, ContainerType
        from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

        self.container = BottomCardContainer()
        outer.addWidget(chat, 1)
        outer.addWidget(self.container)

        self.card = QuestionFloatingWidget(self.container)
        self.card.setVisible(False)
        self.container.add_card("question", self.card)

        self.manager = CardManager.get_instance()
        self.manager.register_window(self.wid)
        self.container.bind_card_manager(self.manager, self.wid)
        self.manager.register_card(self.wid, ContainerType.BOTTOM, "question", self.card)

        self.expand_count = 0
        orig = self.container._do_expand

        def traced():
            self.expand_count += 1
            orig()

        self.container._do_expand = traced

        self.win.show()
        _pump(50)

    def measurement_basis(self):
        """返回 (旧基准高度, 真实排版高度, 容器内容区宽, 卡片宽)"""
        margins = self.container.layout().contentsMargins()
        content_w = self.container.width() - margins.left() - margins.right()
        old_basis = self.card.heightForWidth(self.container.width()) if self.container.width() > 0 else -1
        real_basis = self.card.heightForWidth(content_w) if content_w > 0 else -1
        return old_basis, real_basis, content_w, self.card.width()

    def ask(self):
        self.card.setUpdatesEnabled(False)
        self.card.show_question(CRITICAL_QUESTION)
        self.card.setUpdatesEnabled(True)
        lay = self.card.layout()
        if lay is not None:
            lay.invalidate()
        self.card.updateGeometry()
        self.manager.show_card("question", self.wid)
        QTimer.singleShot(200, self.container._do_expand)

    def positions(self):
        card = self.card
        return (
            card._question_label.mapTo(card, card._question_label.rect().topLeft()).y(),
            card._options_container.mapTo(card, card._options_container.rect().topLeft()).y(),
            card._footer_widget.mapTo(card, card._footer_widget.rect().topLeft()).y()
            if card._footer_widget.isVisible()
            else -1,
        )

    def run_frames(self, n: int = FRAMES, interval: int = 16):
        rows = []

        def tick():
            QApplication.processEvents()
            rows.append(self.positions())
            if len(rows) < n:
                QTimer.singleShot(interval, tick)

        QTimer.singleShot(interval, tick)
        _pump(interval * n + 250)
        return rows


# CardManager 的容器枚举（顶部导入会拖慢收集，改在 _Host 内延迟引用）

def _find_critical_widths(limit: int = MAX_CRITICAL_CASES):
    """扫描窗口宽度，找出旧基准与真实排版高度不一致的临界宽度

    返回 [(win_width, 旧基准高, 真实高), ...]
    """
    found = []
    host = _Host(SCAN_RANGE.start)
    host.ask()
    _pump(150)
    try:
        for win_w in SCAN_RANGE:
            host.win.setFixedSize(win_w, 800)
            _pump(12)
            old_basis, real_basis, _content_w, _card_w = host.measurement_basis()
            if old_basis > 0 and real_basis > 0 and old_basis != real_basis:
                found.append((win_w, old_basis, real_basis))
                if len(found) >= limit:
                    break
    finally:
        host.win.close()
    return found


def _jitter_reason(rows):
    """返回抖动原因字符串，无抖动返回空串"""
    for name, idx in (("问题标题 y", 0), ("选项区 y", 1), ("底栏 y", 2)):
        series = [r[idx] for r in rows]
        if idx == 2 and all(v < 0 for v in series):
            continue
        diffs = [b - a for a, b in zip(series, series[1:]) if abs(b - a) > 1]
        turns = sum(1 for i in range(1, len(diffs)) if (diffs[i] > 0) != (diffs[i - 1] > 0))
        if turns >= 2:
            return f"{name} 方向交替 {turns} 次（diffs={diffs[:10]}）"
    for name, idx in (("问题标题 y", 0), ("选项区 y", 1), ("底栏 y", 2)):
        values = {r[idx] for r in rows[STABLE_FROM:]}
        if len(values) > 1:
            return f"{name} {STABLE_FROM} 帧后仍在变化 {sorted(values)}"
    return ""


def test_measurement_basis_matches_card_layout_width():
    """高度测量基准必须等于卡片真实排版宽度（容器内容区宽度）"""
    app = QApplication.instance() or QApplication(sys.argv)
    host = _Host(1200)
    host.ask()
    _pump(250)
    try:
        _old, real_basis, content_w, card_w = host.measurement_basis()
        assert content_w > 0, "容器内容区宽度未分配，场景无效"
        assert card_w == content_w, (
            f"卡片宽({card_w}) 应等于容器内容区宽({content_w})，"
            "否则说明容器 margins 之外还有额外宽度损耗，测量基准需重新对齐"
        )
        natural_h = host.container._follow_content_natural_h()
        margins = host.container.layout().contentsMargins()
        expected = real_basis + margins.top() + margins.bottom()
        assert abs(natural_h - expected) <= 1, (
            f"容器 natural_h({natural_h}) 应等于卡片真实内容高({real_basis}) + 容器上下 margins"
            f"({margins.top()}+{margins.bottom()}) = {expected}"
        )
    finally:
        host.win.close()


def test_critical_wrap_width_does_not_livelock():
    """换行临界宽度下，容器展开收敛且卡内元素静止"""
    app = QApplication.instance() or QApplication(sys.argv)
    critical = _find_critical_widths()
    if not critical:
        pytest.skip("未找到换行临界宽度（文本未贴近行边界），需调整测试文本")

    print(f"\n[临界宽度] {critical}")
    failures = []
    for win_w, old_basis, real_basis in critical:
        host = _Host(win_w)
        host.ask()
        rows = host.run_frames()
        try:
            if host.expand_count > 30:
                failures.append(f"win={win_w}: _do_expand {host.expand_count} 次（阈值 30）")
            reason = _jitter_reason(rows)
            if reason:
                failures.append(f"win={win_w}: {reason}")
        finally:
            host.win.close()

    # 证据：这些宽度下旧基准确实少算了高度
    assert failures == [], (
        "换行临界宽度下出现布局自激循环（测量基准与真实排版宽度不一致）：\n  "
        + "\n  ".join(failures)
        + f"\n  临界宽度（旧基准高 vs 真实高）: {critical}"
    )


if __name__ == "__main__":
    print("=" * 70)
    print("QuestionFloatingWidget 换行临界宽度自激循环回归测试")
    print("=" * 70)
    ok = True
    for fn in (test_measurement_basis_matches_card_layout_width, test_critical_wrap_width_does_not_livelock):
        try:
            fn()
            print(f"✅ {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            print(f"❌ {fn.__name__}: {exc}")
            ok = False
    sys.exit(0 if ok else 1)
