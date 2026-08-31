# -*- coding: utf-8 -*-
"""回归测试：短消息（含长 URL）气泡被钉死 MAX_HEIGHT 的高度异常。

Bug 复现时序（fuzz seed=24 最小化）
----------------------------------
1. 短文本使气泡收缩到窄宽度（viewer w=80，viewport≈58-64px）
2. `set_text(含长 URL 的短消息)` —— 内部 `doc.setTextWidth(vp_width)` 把文档
   按窄宽排版，长 URL 不可断 token 硬折行 → 文档高度膨胀（404px）
3. 10ms 后 `_update_height`：`doc.setTextWidth(cap)` 不触发同步重排，
   `doc.size()` 读到旧宽排版缓存 404 > 3×lineSpacing → 误判长内容走 WIDE 分支
   → setFixedSize(cap, MAX_HEIGHT) 且写入 `_tall_cap` 污染"必然超高"缓存
4. 之后所有重算被 tall_cached O(1) 短路 → 气泡永久 300px，下方一大块空白

修复：测量改用独立 QTextDocument 副本（同步布局，不受 QTextEdit 异步
layoutTimer 干扰）；并移除 set_text/append_chunk 对显示文档的手动窄宽排版。
"""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget

from app.widgets.message_card import MessageCard, PlainTextViewer

_URL_MSG = "看这个 https://example.com/long/path/that/is/very/long?q=1 谢谢"


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _flush(qapp, ms=80):
    """跑事件循环让 _schedule_update_height 的 10ms singleShot 落地。"""
    loop = QTimer()
    loop.setSingleShot(True)
    loop.start(ms)
    while loop.isActive():
        qapp.processEvents()


def _need_height(viewer: PlainTextViewer) -> float:
    """按当前气泡宽度用独立文档同步测量内容需求高度。"""
    doc = viewer.text_edit.document().clone()
    doc.setDefaultFont(viewer.text_edit.document().defaultFont())
    doc.setDocumentMargin(viewer.text_edit.document().documentMargin())
    doc.setPlainText(viewer._text)
    doc.setTextWidth(max(20, viewer.width() - 16))
    h = doc.size().height() + 12
    return max(40, min(h, viewer.MAX_HEIGHT))


def _make_shown_viewer(qapp) -> tuple:
    """AlignRight 布局：复刻真实 chat_layout 中用户卡片按 sizeHint 宽度布局、
    viewport 停留在收缩宽的状态（复现必要条件）。"""
    host = QWidget()
    host.resize(1200, 600)
    viewer = PlainTextViewer()
    QVBoxLayout(host).addWidget(viewer, 0, Qt.AlignRight)
    host.show()
    qapp.processEvents()
    return host, viewer


def test_short_url_msg_not_pinned_to_max_height(qapp):
    """窄气泡态下 set_text(含 URL 短消息) 后，气泡高度必须跟随真实内容。"""
    host, viewer = _make_shown_viewer(qapp)

    # 1) 先建立"窄气泡收缩"状态
    viewer.set_text("你好")
    viewer.set_width_cap(999)
    _flush(qapp)
    assert viewer.width() < 200, f"前置：短文本气泡应收缩，实际 w={viewer.width()}"

    # 2) 窄态下替换为含长 URL 的短消息，随后宽度上限同步到 999（cap 不变则手动触发）
    viewer.set_text(_URL_MSG)
    viewer.set_width_cap(999)
    _flush(qapp)

    need = _need_height(viewer)
    assert viewer.height() <= need + 8, (
        f"短消息气泡被钉死在 {viewer.height()}px（内容需求仅 {need:.0f}px）——"
        f"_tall_cap={viewer._tall_cap} 污染或 size() 过期缓存误判 WIDE"
    )
    assert viewer._tall_cap == 0, f"短消息不应写入'必然超高'缓存，tall_cap={viewer._tall_cap}"

    host.hide()
    viewer.cleanup()


def test_genuine_long_content_still_capped(qapp):
    """修复不得破坏正常限高：真长内容仍应撞 MAX_HEIGHT 并写 tall_cap。"""
    host, viewer = _make_shown_viewer(qapp)
    viewer.set_width_cap(400)
    viewer.set_text("长" * 4000)
    _flush(qapp)

    assert viewer.height() == PlainTextViewer.MAX_HEIGHT, f"长内容应限高 MAX_HEIGHT，实际 {viewer.height()}"
    assert viewer._tall_cap >= 400, f"长内容撞限后应记录 tall_cap，实际 {viewer._tall_cap}"

    host.hide()
    viewer.cleanup()


def test_height_consistent_across_repeated_updates(qapp):
    """重复 update_height 不应出现高度漂移（缓存一致性）。"""
    host, viewer = _make_shown_viewer(qapp)
    viewer.set_text(_URL_MSG)
    viewer.set_width_cap(600)
    _flush(qapp)
    h1 = viewer.height()

    for _ in range(3):
        viewer.update_height()
        _flush(qapp, 30)
        assert viewer.height() == h1, f"高度漂移：{h1} → {viewer.height()}"

    host.hide()
    viewer.cleanup()


# ── fuzz 确定性回放：固定种子重放随机时序交错（含已知失败种子 24/58）──

_WORDS = ["你好", "hello world", "测试消息", "🔥emoji👍", _URL_MSG, "全角字符ＡＢＣ", "line1\nline2", "a" * 50]

_FUZZ_OPS = [
    "sync",
    "sync_force",
    "preview_on",
    "preview_off",
    "append",
    "set_text",
    "resize_host",
    "update_height",
    "theme",
    "finish",
    "hide",
    "show",
    "flush",
]


def _replay_fuzz(qapp, rng_seed: int) -> None:
    import random

    rng = random.Random(rng_seed)
    host = QWidget()
    host.resize(rng.randint(500, 1100), 600)
    card = MessageCard(role="user")
    QVBoxLayout(host).addWidget(card)
    card.sync_width()
    host.show()
    _flush(qapp, 20)

    for _ in range(20):
        op = rng.choice(_FUZZ_OPS)
        if op == "sync":
            card.sync_width(target_width=rng.randint(320, 1100))
        elif op == "sync_force":
            card.sync_width(force=True, target_width=rng.randint(320, 1100))
        elif op == "preview_on":
            card.set_resize_preview_mode(True)
        elif op == "preview_off":
            card.set_resize_preview_mode(False)
        elif op == "append":
            card.update_content(rng.choice(_WORDS))
        elif op == "set_text":
            card.set_content(rng.choice(_WORDS))
        elif op == "resize_host":
            host.resize(rng.randint(400, 1200), 600)
        elif op == "update_height":
            if card.viewer:
                card.viewer.update_height()
        elif op == "theme":
            if card.viewer:
                card.viewer.refresh_theme()
        elif op == "finish":
            card.finish_streaming()
        elif op == "hide":
            host.hide()
        elif op == "show":
            host.show()
        elif op == "flush":
            _flush(qapp, 15)
        if rng.random() < 0.4:
            _flush(qapp, 12)

    _flush(qapp)
    if card.viewer is not None:
        viewer = card.viewer
        doc = viewer.text_edit.document().clone()
        doc.setDefaultFont(viewer.text_edit.document().defaultFont())
        doc.setDocumentMargin(viewer.text_edit.document().documentMargin())
        doc.setPlainText(viewer._text)
        doc.setTextWidth(max(20, viewer.width() - 16))
        need = max(40, min(doc.size().height() + 12, viewer.MAX_HEIGHT))
        assert viewer.height() <= need + 8, (
            f"seed={rng_seed}: 气泡 h={viewer.height()} 超过内容需求 {need:.0f}（tall_cap={viewer._tall_cap} 污染钉死）"
        )
    host.hide()
    card.cleanup()
    host.deleteLater()


def test_fuzz_seed_regression(qapp, tmp_path):
    """fuzz 种子回归：seed=24/58 曾把短消息气泡钉死 300px（_tall_cap 污染）。

    重放种子 0-79（覆盖已知失败种子），断言无高度钉死。
    """
    for seed in range(80):
        _replay_fuzz(qapp, seed)
