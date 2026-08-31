# -*- coding: utf-8 -*-
"""[DEBUG-bubble-height] 临时排查脚本：用户短消息气泡高度偶发异常复现矩阵。

症状：用户消息很短，但气泡很高，下方一大块空白。

复现矩阵（offscreen Qt，量 viewer 实际高度 vs 文档需求高度）：
  S1 真实时序：MessageCard → update_content → finish_streaming → addWidget → sync_width
  S2 先 flush 再 sync_width（10ms 定时器在 cap 注入前落地）
  S3 resize preview 进出竞态
  S4 显示前 set 文本，显示后不重算
  S5 特殊文本（多行/emoji/超长 URL/全角）
  S6 addWidget 后窗口 resize（50ms 防抖链）
  S7 二次 update_content（预览更新/重发场景）

判定：viewer.height() > need + 30 即视为"高空白"异常。
运行：QT_QPA_PLATFORM=offscreen python tests/debug/bubble_height_repro.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout

from app.widgets.message_card import MessageCard, PlainTextViewer


def _flush(qapp, ms=120):
    loop = QTimer()
    loop.setSingleShot(True)
    loop.start(ms)
    while loop.isActive():
        qapp.processEvents()


def _need_height(viewer: PlainTextViewer) -> float:
    """按当前气泡宽度测量文档需求高度（与 _update_height 同法）。"""
    viewer.text_edit.document().setTextWidth(viewer.width() - 16)
    h = viewer.text_edit.document().size().height() + 12
    return max(40, min(h, viewer.MAX_HEIGHT))


def _report(tag: str, viewer: PlainTextViewer, anomalies: list):
    need = _need_height(viewer)
    actual = viewer.height()
    bad = actual > need + 30
    mark = "❌ 异常" if bad else "OK"
    print(f"[{tag}] viewer_h={actual} need={need:.0f} cap={viewer._width_cap} tall_cap={viewer._tall_cap} {mark}")
    if bad:
        anomalies.append((tag, actual, need))


def make_host():
    host = QWidget()
    host.resize(900, 600)
    lay = QVBoxLayout(host)
    host._lay = lay
    return host


def run(qapp):
    anomalies: list = []
    texts = {
        "short": "你好",
        "multiline": "第一行\n第二行",
        "emoji": "👍🔥 测试 emoji 👌",
        "url": "看这个 https://example.com/a/very/long/path/that/keeps/going/on/and/on/forever?q=1",
        "fullwidth": "全角字符测试：＄＃＠％％，中文标点！！！",
    }

    # ── S1 真实时序 ──
    for name, txt in texts.items():
        host = make_host()
        card = MessageCard(role="user")
        host._lay.addWidget(card)
        host.show()
        card.sync_width()
        _flush(qapp, 30)
        card.update_content(txt)
        card.finish_streaming()
        _flush(qapp)
        _report(f"S1-{name}", card.viewer, anomalies)
        host.hide()
        card.cleanup()

    # ── S2 先 flush 再 sync（内容定时器先于 cap 注入落地）──
    for name, txt in texts.items():
        host = make_host()
        card = MessageCard(role="user")
        host._lay.addWidget(card)
        host.show()
        card.update_content(txt)
        card.finish_streaming()
        _flush(qapp, 30)  # 10ms 定时器先落地：cap 仍 16777215
        card.sync_width()
        _flush(qapp)
        _report(f"S2-{name}", card.viewer, anomalies)
        host.hide()
        card.cleanup()

    # ── S3 preview 竞态：preview(True) → 内容 → preview(False) ──
    host = make_host()
    card = MessageCard(role="user")
    host._lay.addWidget(card)
    host.show()
    card.sync_width()
    _flush(qapp, 30)
    card.set_resize_preview_mode(True)
    card.update_content("你好")
    card.finish_streaming()
    _flush(qapp, 30)
    card.set_resize_preview_mode(False)
    _flush(qapp)
    _report("S3-preview", card.viewer, anomalies)
    # 再触发一次无关高度事件，观察是否自愈
    card.viewer.update_height()
    _flush(qapp)
    _report("S3-preview-heal", card.viewer, anomalies)
    host.hide()
    card.cleanup()

    # ── S4 未显示先建卡（真实 _append_user_message 顺序：先内容后 addWidget）──
    for name, txt in texts.items():
        host = make_host()
        card = MessageCard(role="user")
        card.update_content(txt)  # 未 addWidget 未 show
        card.finish_streaming()
        host._lay.addWidget(card)
        host.show()
        card.sync_width()
        _flush(qapp)
        _report(f"S4-{name}", card.viewer, anomalies)
        host.hide()
        card.cleanup()

    # ── S6 addWidget 后 host resize（防抖 50ms 链 + sync_width 变化）──
    host = make_host()
    card = MessageCard(role="user")
    host._lay.addWidget(card)
    host.show()
    card.sync_width()
    card.update_content("你好")
    card.finish_streaming()
    _flush(qapp)
    host.resize(420, 600)
    card.sync_width()
    _flush(qapp)
    _report("S6-shrink", card.viewer, anomalies)
    host.resize(1200, 600)
    card.sync_width()
    _flush(qapp)
    _report("S6-grow", card.viewer, anomalies)
    host.hide()
    card.cleanup()

    # ── S7 二次 update_content ──
    host = make_host()
    card = MessageCard(role="user")
    host._lay.addWidget(card)
    host.show()
    card.sync_width()
    card.update_content("第一版内容")
    card.finish_streaming()
    _flush(qapp)
    card.update_content("第二版更长的内容，多几个字看看高度会不会保持异常")
    card.finish_streaming()
    _flush(qapp)
    _report("S7-twice", card.viewer, anomalies)
    host.hide()
    card.cleanup()

    print()
    if anomalies:
        print(f"共 {len(anomalies)} 个异常场景：")
        for tag, actual, need in anomalies:
            print(f"  {tag}: actual={actual} need={need:.0f}")
    else:
        print("所有场景高度正常，未复现。")


if __name__ == "__main__":
    qapp = QApplication.instance() or QApplication(sys.argv)
    run(qapp)
