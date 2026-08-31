# -*- coding: utf-8 -*-
"""[DEBUG-bubble-height] 回放失败种子，逐步打印 (height, need, tall_cap, cap, textlen)。"""

import os
import random
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout

from app.widgets.message_card import MessageCard, PlainTextViewer

_WORDS = ["你好", "hello world", "测试消息", "🔥emoji👍", "https://example.com/long/path?q=1", "全角字符ＡＢＣ", "line1\nline2", "a" * 50]

SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 24


def _flush(qapp, ms=60):
    loop = QTimer()
    loop.setSingleShot(True)
    loop.start(ms)
    while loop.isActive():
        qapp.processEvents()


def _need_height(viewer) -> float:
    viewer.text_edit.document().setTextWidth(max(20, viewer.width() - 16))
    h = viewer.text_edit.document().size().height() + 12
    return max(40, min(h, viewer.MAX_HEIGHT))


def replay(qapp, seed):
    rng = random.Random(seed)
    host = QWidget()
    host.resize(rng.randint(500, 1100), 600)
    lay = QVBoxLayout(host)
    card = MessageCard(role="user")
    lay.addWidget(card, 0)
    card.sync_width()
    host.show()
    _flush(qapp, 20)

    def state(tag):
        v = card.viewer
        if v is None:
            print(f"  [{tag}] viewer=None")
            return
        print(
            f"  [{tag}] h={v.height()} need={_need_height(v):.0f} tall_cap={v._tall_cap} "
            f"cap={v._width_cap} textlen={len(v._text)}"
        )

    for i in range(20):
        op = rng.choice(
            [
                "sync", "sync_force", "preview_on", "preview_off", "append", "set_text",
                "resize_host", "update_height", "theme", "finish", "hide", "show", "flush",
            ]
        )
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
        state(f"{i:02d}-{op}")

    _flush(qapp)
    state("FINAL")
    host.hide()
    card.cleanup()
    host.deleteLater()


# [DEBUG-bubble-height] 插桩 _update_height，打印测量细节
_orig_update_height = PlainTextViewer._update_height


def _patched_update_height(self):
    cap = self._width_cap
    dtc = self._definitely_tall_chars()
    tall_cached = bool(self._tall_cap) and cap <= self._tall_cap
    print(f"    ENTER: cap={cap} tall_cap={self._tall_cap} tall_cached={tall_cached} len={len(self._text)} dtc={dtc}")
    if not (tall_cached or len(self._text) >= dtc):
        from PyQt5.QtGui import QFontMetrics

        doc = self.text_edit.document()
        fm = QFontMetrics(self.text_edit.font())
        vp = self.text_edit.viewport().width()
        print(
            f"    PRE : cap={cap} vp={vp} te_w={self.text_edit.width()} doc.textWidth={doc.textWidth()} "
            f"size={doc.size().width():.0f}x{doc.size().height():.0f}"
        )
        doc.setTextWidth(cap if cap < 100000 else 4000)
        h_full = doc.size().height()
        lsp = fm.lineSpacing()
        print(f"    MEASURE@cap: doc_h={h_full:.1f} lineSpacing={lsp} thr={3.0 * lsp:.1f} branch={'WIDE' if h_full > 3.0 * lsp else 'SHRINK'}")
        from PyQt5.QtCore import QCoreApplication

        QCoreApplication.processEvents()
        print(f"    POST-flush: doc_h={doc.size().height():.1f} textWidth={doc.textWidth()} size={doc.size().width():.0f}x{doc.size().height():.0f}")
        if h_full <= 3.0 * lsp:
            longest = 0.0
            block = doc.begin()
            while block.isValid():
                layout = block.layout()
                if layout is not None:
                    for i in range(layout.lineCount()):
                        longest = max(longest, layout.lineAt(i).naturalTextWidth())
                block = block.next()
            bubble_w = max(80, min(int(__import__("math").ceil(longest)) + 40, cap))
            doc.setTextWidth(bubble_w - 16)
            print(f"    MEASURE@bubble: bubble_w={bubble_w} doc_h={doc.size().height():.1f} vp={self.text_edit.viewport().width()}")
    _orig_update_height(self)
    print(f"    EXIT: h={self.height()} w={self.width()} tall_cap={self._tall_cap}")


PlainTextViewer._update_height = _patched_update_height

if __name__ == "__main__":
    qapp = QApplication.instance() or QApplication(sys.argv)
    replay(qapp, SEED)
