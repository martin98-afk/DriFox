# -*- coding: utf-8 -*-
"""真实 WebEngine 历史卡片开关基准，不访问用户配置与会话。

运行：.venv/Scripts/python benchmarks/bench_card_lifecycle.py --rounds 8 --cards 4
测量：构建/渲染/关闭耗时、主/子进程私有内存、弱引用存活数、复用池容量。
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
import time
import weakref
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(rounds: int, card_count: int, native_delete: bool) -> None:
    isolation = tempfile.TemporaryDirectory(prefix="drifox_card_lifecycle_")
    os.environ["DRIFOX_DATA_DIR"] = isolation.name
    os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
    os.environ["QT_QPA_PLATFORM"] = "windows"

    import psutil
    from loguru import logger
    from PyQt5.QtCore import QCoreApplication, QEvent, Qt
    from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    from PyQt5 import QtWebEngineWidgets  # noqa: F401

    app = QApplication([])
    logger.remove()
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.widgets.card_viewers import _dialog_event_filter
    from app.widgets.message_card import MessageCard
    from app.widgets.webview_pool import WebViewPool

    init_shared_web_profile(app)
    proc = psutil.Process()
    tracked_cards = weakref.WeakSet()
    tracked_viewers = weakref.WeakSet()
    pool = WebViewPool.get_instance()
    content = [
        {"type": "text", "text": ("历史正文含 **加粗** 和 `行内代码`。\n\n" * 80)},
        {"type": "reasoning", "content": "历史思考过程。" * 100},
        {"type": "text", "text": "```python\nvalue = sum(range(100))\n```\n\n" + "结束正文。" * 100},
    ]

    def settle(milliseconds):
        end = time.perf_counter() + milliseconds / 1000
        while time.perf_counter() < end:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            time.sleep(0.005)

    def snapshot(label, **extra):
        gc.collect()
        children = []
        for child in proc.children(recursive=True):
            try:
                if "QtWebEngine" in child.name():
                    children.append(child.memory_info())
            except psutil.Error:
                pass
        mem = proc.memory_info()
        row = {
            "stage": label,
            "private_mb": round(getattr(mem, "private", mem.rss) / 2**20, 2),
            "rss_mb": round(mem.rss / 2**20, 2),
            "web_private_mb": round(sum(getattr(m, "private", m.rss) for m in children) / 2**20, 2),
            "web_processes": len(children),
            "cards": len(tracked_cards),
            "viewers": len(tracked_viewers),
            "filter_viewers": len(_dialog_event_filter._viewers),
            "idle": pool.size(),
            **extra,
        }
        print(json.dumps(row, ensure_ascii=False), flush=True)

    snapshot("initial")
    for index in range(rounds):
        host = QWidget()
        host.resize(900, 700)
        layout = QVBoxLayout(host)
        host.show()
        start = time.perf_counter()
        for _ in range(card_count):
            card = MessageCard("assistant", parent=host)
            tracked_cards.add(card)
            card.set_content(content)
            layout.addWidget(card)
            card.ensure_rendered()
            if card.viewer is not None:
                tracked_viewers.add(card.viewer)
        built = time.perf_counter()
        settle(800)
        snapshot(f"open-{index + 1}", build_ms=round((built - start) * 1000, 2))
        start = time.perf_counter()
        for position in reversed(range(layout.count())):
            widget = layout.takeAt(position).widget()
            if not native_delete:
                widget.cleanup()
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        del card, widget
        host.hide()
        host.deleteLater()
        del layout, host
        settle(150)
        snapshot(f"closed-{index + 1}", close_ms=round((time.perf_counter() - start) * 1000, 2))
    pool.clear()
    settle(300)
    snapshot("pool-cleared")
    app.closeAllWindows()
    # 共享 profile 必须晚于所有 page 销毁。
    settle(100)
    app.quit()
    del app
    gc.collect()
    try:
        isolation.cleanup()
    except OSError:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测量真实历史卡片开关的资源增长")
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--cards", type=int, default=4)
    parser.add_argument("--native-delete", action="store_true", help="对照组：直接删除，不显式 cleanup")
    args = parser.parse_args()
    run(args.rounds, args.cards, args.native_delete)
