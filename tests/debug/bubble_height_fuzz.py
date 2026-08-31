# -*- coding: utf-8 -*-
"""[DEBUG-bubble-height] fuzz：随机时序交错轰炸用户气泡高度链，抓"高空白"失败种子。

每个轮次：随机 20 步操作（sync_width / preview / update_content / set_text /
resize / update_height / refresh_theme / finish_streaming / show-hide），结束后
断言 viewer.height() ≤ need + 8。失败则打印完整操作种子。
"""

import os
import random
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout

from app.widgets.message_card import MessageCard, PlainTextViewer

_WORDS = [
    "你好",
    "hello world",
    "测试消息",
    "🔥emoji👍",
    "https://example.com/long/path?q=1",
    "全角字符ＡＢＣ",
    "line1\nline2",
    "a" * 50,
]


def _flush(qapp, ms=60):
    loop = QTimer()
    loop.setSingleShot(True)
    loop.start(ms)
    while loop.isActive():
        qapp.processEvents()


def _need_height(viewer: PlainTextViewer) -> float:
    viewer.text_edit.document().setTextWidth(max(20, viewer.width() - 16))
    h = viewer.text_edit.document().size().height() + 12
    return max(40, min(h, viewer.MAX_HEIGHT))


def run(qapp, rounds=300, seed0=0):
    failures = []
    for r in range(rounds):
        rng = random.Random(seed0 + r)
        host = QWidget()
        host.resize(rng.randint(500, 1100), 600)
        lay = QVBoxLayout(host)
        card = MessageCard(role="user")
        lay.addWidget(card, 0)
        card.sync_width()
        host.show()
        _flush(qapp, 20)

        ops = []
        for _ in range(20):
            op = rng.choice(
                [
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
            )
            ops.append(op)
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
            # 随机让定时器落地一半概率
            if rng.random() < 0.4:
                _flush(qapp, 12)

        _flush(qapp)
        viewer = card.viewer
        if viewer is None:
            host.hide()
            card.cleanup()
            host.deleteLater()
            continue
        need = _need_height(viewer)
        if viewer.height() > need + 8 or viewer.height() < 40:
            failures.append(
                (seed0 + r, ops, viewer.height(), need, viewer._text[:60], viewer._width_cap, viewer._tall_cap)
            )
            print(
                f"❌ seed={seed0 + r} viewer_h={viewer.height()} need={need:.0f} cap={viewer._width_cap} tall_cap={viewer._tall_cap}"
            )
            print(f"   text={viewer._text[:60]!r}")
            print(f"   ops={ops}")
        host.hide()
        card.cleanup()
        host.deleteLater()

    print(f"\n{rounds} 轮完成，失败 {len(failures)}")
    return failures


if __name__ == "__main__":
    qapp = QApplication.instance() or QApplication(sys.argv)
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    s = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    run(qapp, rounds=n, seed0=s)
