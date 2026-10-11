# -*- coding: utf-8 -*-
"""排队消息卡片高度跟随回归测试（多条时容器高度坍缩）

== 问题描述 ==
队列 1 条时容器已展开（88px）。追加第 2 条后容器高度不跟随 →
卡片被裁到只剩第 1 行（截图：标题栏「排队消息 2 条」+ 第 1 行可见，
第 2 行不可见，容器边框紧贴第 1 行底边）。

== 根因 ==
排队卡声明了 FOLLOW_CONTENT_PROP，但容器 _follow_content_natural_h()
对未实现 heightForWidth 的卡片退化为读 card.sizeHint()，且容器感知
「内容变化」的两条链路都不可靠：
1. sizeHint 是 Qt 布局缓存：set_entries 同 tick 内是残值（实测 44，
   真值 109），容器若在此刻重算会锁到远小于内容的高度；
2. 触发重算依赖卡片 Resize 事件 → _schedule_expand(source="resize")，
   而等待展开/折叠动画跑完的窗口内 Resize 会被守卫丢弃（防自激），
   窗口过后卡片几何不再变化 → 再无任何触发源 → 容器永久锁死旧高度。

== 修复 ==
排队卡补齐与 CommandCard 同款的两件套：
1. hasHeightForWidth/heightForWidth 返回按行数计算的精确高度，
   不经过 Qt 布局缓存；
2. set_entries 末尾 emit heightChanged，容器重算不再依赖 Resize 中介。
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import Qt

Qt.AA_ShareOpenGLContexts = Qt.AA_ShareOpenGLContexts
try:
    from PyQt5.QtWebEngineWidgets import (  # noqa: F401
        QWebEnginePage,
        QWebEngineSettings,
        QWebEngineView,
    )
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PyQt5.QtCore import QEventLoop, QTimer
from PyQt5.QtWidgets import QSplitter, QVBoxLayout, QWidget


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _entries(n: int):
    return [{"id": f"q{i}", "text": f"消息内容 {i}"} for i in range(1, n + 1)]


def _make_scene(n0: int = 1):
    """构造 窗口 + vdock splitter + BottomCardContainer + QueueMessageCard

    先以 n0 条展开到稳态（模拟「队列已有 1 条」的现场）。
    """
    from app.widgets.cards.card_container import BottomCardContainer
    from app.widgets.cards.floating.queue_message_card import QueueMessageCard

    win = QWidget()
    win.resize(900, 800)
    outer = QVBoxLayout(win)
    chat = QWidget()
    chat.setMinimumHeight(400)
    splitter = QSplitter()
    splitter.setOrientation(Qt.Vertical)
    splitter.addWidget(chat)
    splitter.setStretchFactor(0, 1)
    container = BottomCardContainer()
    splitter.addWidget(container)
    splitter.setStretchFactor(1, 0)
    outer.addWidget(splitter)
    win.show()
    container.enable_dock_mode(splitter)

    card = QueueMessageCard(container)
    container.add_card("message_queue", card)
    card.set_entries(_entries(n0))
    card.setVisible(True)
    container._on_card_shown("message_queue")
    _pump(400)  # 等展开动画（200ms）与布局收敛
    return win, container, card


def test_height_for_width_returns_exact_height(qapp):
    """heightForWidth 按行数返回精确高度（容器 follow_content 分支的直接输入）"""
    win, container, card = _make_scene(1)

    assert card.hasHeightForWidth(), "排队卡必须实现 hasHeightForWidth（容器精确取高前提）"
    for n in (1, 2, 3, 5):
        card.set_entries(_entries(n))
        _pump(60)
        hfw = card.heightForWidth(800)
        hint = card.sizeHint().height()
        assert hfw == hint, f"{n} 条: heightForWidth={hfw} 与 sizeHint={hint} 不一致"
    win.close()


def test_container_follows_after_entries_change_without_resize_events(qapp):
    """追加条目时容器跟随高度，且不依赖卡片 Resize 事件送达

    模拟真实坍缩窗口：Resize 链路中断（removeEventFilter，等价于动画守卫
    丢弃 + 其后几何无变化）后追加第 2 条，容器必须仍能重算到新高度。
    """
    win, container, card = _make_scene(1)
    h1 = container.height()
    assert h1 > 0, "1 条时容器应已展开"

    # 断开容器对卡片的事件监视：卡片 Resize 不再触发 _schedule_expand
    card.removeEventFilter(container)

    card.set_entries(_entries(2))
    _pump(400)  # 等容器动画

    need = card.sizeHint().height()
    assert container.height() >= need, (
        f"2 条时容器 {container.height()}px 未跟随卡片内容 {need}px（坍缩：第 2 行被裁切）"
    )
    assert card.height() >= need, f"卡片 {card.height()}px 被容器裁到不足 {need}px"
    win.close()


def test_container_follows_when_entries_change_during_expand_animation(qapp):
    """展开动画进行中追加条目：其后容器仍跟随（不因 Resize 被守卫吞掉而锁死）"""
    from app.widgets.cards.card_container import BottomCardContainer
    from app.widgets.cards.floating.queue_message_card import QueueMessageCard

    win = QWidget()
    win.resize(900, 800)
    outer = QVBoxLayout(win)
    chat = QWidget()
    chat.setMinimumHeight(400)
    splitter = QSplitter()
    splitter.setOrientation(Qt.Vertical)
    splitter.addWidget(chat)
    splitter.setStretchFactor(0, 1)
    container = BottomCardContainer()
    splitter.addWidget(container)
    splitter.setStretchFactor(1, 0)
    outer.addWidget(splitter)
    win.show()
    container.enable_dock_mode(splitter)
    card = QueueMessageCard(container)
    container.add_card("message_queue", card)

    # 1 条入队并立即 show → 容器开始 0→88 展开动画
    card.set_entries(_entries(1))
    card.setVisible(True)
    container._on_card_shown("message_queue")
    _pump(30)  # 动画进行中
    # 动画窗口内追加第 2 条：此刻 Resize 会被动画守卫丢弃
    card.set_entries(_entries(2))
    _pump(600)

    need = card.sizeHint().height()
    assert container.height() >= need, f"动画中追加后容器 {container.height()}px 未跟随内容 {need}px（坍缩）"
    win.close()


if __name__ == "__main__":
    print("QueueMessageCard 高度跟随回归测试")
    for fn in (
        test_height_for_width_returns_exact_height,
        test_container_follows_after_entries_change_without_resize_events,
        test_container_follows_when_entries_change_during_expand_animation,
    ):
        try:
            fn()
            print(f"✅ {fn.__name__}")
        except Exception:
            import traceback

            traceback.print_exc()
            print(f"❌ {fn.__name__}")
