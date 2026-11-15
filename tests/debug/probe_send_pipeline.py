# -*- coding: utf-8 -*-
"""探针 v5：发送链路端到端分段（offscreen）

v4 已确认输入框侧瓶颈：doc.size() 首次全量布局 500k=4555ms / 100k=183ms。
本探针测量「点发送之后」的其余主线程段：

  A. MessageCard(role="user") 建卡 + update_content + finish_streaming
  B. sync_width（容器宽度注入 → PlainTextViewer 重算）
  C. _record_input_history 的 DB 写入（add_input_history）
  D. add_input_history → get_input_history → load_history 全链
  E. toPlainText 取文本

运行：
  $env:QT_QPA_PLATFORM="offscreen"; python -u tests\debug\probe_send_pipeline.py
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DRIFOX_NO_CODEBUDDY_REFRESH", "1")
os.environ.setdefault("DRIFOX_DATA_DIR", "D:\\work\\DriFox\\.drifox-test")

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


def make_text(n_chars: int, line_len: int = 80) -> str:
    unit = "这是一段用于测试发送卡顿的超长文本内容 abcdefghij"
    lines, total = [], 0
    while total < n_chars:
        lines.append((unit * (line_len // len(unit) + 2))[:line_len])
        total += line_len + 1
    return "\n".join(lines)


def ms(fn):
    t0 = time.perf_counter()
    r = fn()
    dt = (time.perf_counter() - t0) * 1000
    return r, dt


def probe_card(n_chars: int):
    from app.widgets.message_card import MessageCard

    print(f"\n===== 用户卡片 {n_chars} 字符 =====", flush=True)
    text = make_text(n_chars)

    t = time.perf_counter()
    card = MessageCard(parent=None, role="user")
    card.show()
    app.processEvents()
    print(f"  建卡                        {(time.perf_counter()-t)*1000:9.2f} ms", flush=True)

    _, t_upd = ms(lambda: card.update_content(text))
    print(f"  update_content              {t_upd:9.2f} ms", flush=True)

    _, t_sync = ms(lambda: card.sync_width(force=True))
    print(f"  sync_width(force)           {t_sync:9.2f} ms", flush=True)

    _, t_fin = ms(lambda: card.finish_streaming())
    print(f"  finish_streaming            {t_fin:9.2f} ms", flush=True)

    _, t_resize = ms(lambda: card.resize(900, 400))
    app.processEvents()
    print(f"  resize + processEvents      {t_resize:9.2f} ms", flush=True)
    card.close()


def probe_store(n_chars: int):
    from app.core.store.session_store import SessionStore
    from app.utils.utils import get_app_data_dir

    print(f"\n===== 输入历史落库 {n_chars} 字符 =====", flush=True)
    text = make_text(n_chars)
    db_dir = str((get_app_data_dir() / "probe_store"))
    os.makedirs(db_dir, exist_ok=True)
    store = SessionStore(db_dir)

    _, t_add = ms(lambda: store.add_input_history(text, None))
    print(f"  add_input_history           {t_add:9.2f} ms", flush=True)

    history, t_get = ms(lambda: store.get_input_history())
    print(f"  get_input_history           {t_get:9.2f} ms  (条数={len(history) if history else 0})", flush=True)

    from app.widgets.bottom_input_area import SendableTextEdit

    box = SendableTextEdit()
    box._initializing = False
    box.show()
    app.processEvents()
    _, t_load = ms(lambda: box.load_history(history or []))
    print(f"  input_area.load_history     {t_load:9.2f} ms", flush=True)
    box.close()
    try:
        store.close()
    except Exception:
        pass


if __name__ == "__main__":
    sizes = [int(x) for x in (sys.argv[1:] or ["100000", "500000"])]
    for n in sizes:
        probe_card(n)
        probe_store(n)
