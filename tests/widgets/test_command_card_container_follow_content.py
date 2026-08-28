# -*- coding: utf-8 -*-
"""CommandCard 过滤后容器高度收缩回归测试

== 问题描述 ==
命令卡片输入文字过滤后，卡片高度已按过滤结果缩小，
但容器（BottomCardContainer, dock 模式）高度不跟随收缩，
最后一行下方出现大段空白。

== 根因 ==
1. 命令卡片未声明 FOLLOW_CONTENT_PROP：dock 容器首次展开后
   _do_expand 走"早退分支"（card_container.py），只锁 min 不收缩
   → 过滤后卡片变矮、容器保持旧高度 → 留白。
2. _lock_to_content 的 min 兜底用 _DOCK_MIN_V(80)：过滤剩 1 项
   （36px）时容器被 80px 下限撑高 → 44px 留白。

== 修复 ==
1. CommandCard 声明 FOLLOW_CONTENT_PROP + 提供 heightForWidth
   （返回精确目标高度），容器 follow_content 分支严格跟随卡片高度。
2. follow_content 分支 min 锁严格 = natural_h（不套 _dock_min 下限）。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

Qt.AA_ShareOpenGLContexts = Qt.AA_ShareOpenGLContexts
try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings  # noqa: F401
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except Exception:
    pass

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QSplitter, QVBoxLayout, QWidget


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _setup_app():
    return QApplication.instance() or QApplication(sys.argv)


def _make_scene():
    """构造 窗口 + vdock splitter + BottomCardContainer + CommandCard"""
    from app.widgets.cards.card_container import BottomCardContainer
    from app.widgets.cards.floating.command_card import CommandCard

    win = QWidget()
    win.resize(900, 800)
    outer = QVBoxLayout(win)
    chat = QWidget()
    chat.setMinimumHeight(400)
    splitter = QSplitter()
    splitter.setOrientation(1)  # Vertical
    splitter.addWidget(chat)
    splitter.setStretchFactor(0, 1)
    container = BottomCardContainer()
    splitter.addWidget(container)
    splitter.setStretchFactor(1, 0)
    outer.addWidget(splitter)
    win.show()

    container.enable_dock_mode(splitter)
    card = CommandCard(container)
    container.add_card("command", card)
    # 3 条 cmd-* + 7 条 xxx-*：过滤可精确命中 1 项/3 项
    card._all_items = [
        {"name": f"cmd-{chr(97 + i)}", "type": "command", "subtype": "", "description": f"desc {i}"} for i in range(3)
    ] + [{"name": f"xxx-{i:02d}", "type": "command", "subtype": "", "description": f"desc {i}"} for i in range(7)]
    # 注：不调 show_card（其 _refresh_data 会覆盖注入的 _all_items）
    card.load_items("", incremental=False)
    card._visible = True
    card.setVisible(True)
    _pump(60)
    container._do_expand()
    _pump(80)
    return win, container, card


def _filter_and_check(container, card, query, expect_items):
    card.load_items(query, incremental=False)
    _pump(40)
    container._do_expand()
    _pump(80)
    assert len(card._filtered_items) == expect_items, (
        f"query={query!r} 过滤结果 {len(card._filtered_items)} != {expect_items}"
    )
    gap = container.height() - card.height()
    assert gap <= 4, (
        f"query={query!r}: 容器 {container.height()}px vs 卡片 {card.height()}px, 留白 {gap}px（应为 ≤4px）"
    )


def test_container_follows_card_height_after_filter():
    """过滤后容器高度严格跟随卡片（全量 288 → 1 项 36 → 3 项 108）"""
    _setup_app()
    win, container, card = _make_scene()

    # 全量 10 项 → 8 可见 = 288
    assert container.height() == card.height() == 8 * 36, (
        f"初始高度异常: container={container.height()} card={card.height()}"
    )

    # 过滤剩 1 项 → 36px，容器必须收缩到 36（不得被 80px dock 下限撑高）
    _filter_and_check(container, card, "cmd-a", 1)
    assert card.height() == 36

    # 过滤剩 3 项 → 108px
    _filter_and_check(container, card, "cmd-", 3)
    assert card.height() == 108

    # 恢复全量 → 288px
    _filter_and_check(container, card, "", 10)
    assert card.height() == 288

    win.close()


def test_height_for_width_returns_target():
    """heightForWidth 返回精确目标高度（供容器 follow_content 分支锁定）"""
    _setup_app()
    win, container, card = _make_scene()
    assert card.hasHeightForWidth()
    assert card.heightForWidth(600) == 288
    card.load_items("cmd-a", incremental=False)
    _pump(40)
    assert card.heightForWidth(600) == 36
    win.close()


if __name__ == "__main__":
    print("CommandCard 容器高度收缩回归测试")
    for fn in (test_container_follows_card_height_after_filter, test_height_for_width_returns_target):
        try:
            fn()
            print(f"✅ {fn.__name__}")
        except Exception:
            import traceback

            traceback.print_exc()
            print(f"❌ {fn.__name__}")
