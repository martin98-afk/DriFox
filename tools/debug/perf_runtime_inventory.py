# -*- coding: utf-8 -*-
"""由 Python 3.14 sys.remote_exec 单次采集运行现场，仅输出数量和体积，不输出会话内容。"""


def _collect_runtime_inventory():
    import collections
    import gc
    import json
    import os
    import sys
    import time
    from pathlib import Path

    from PyQt5.QtWidgets import QApplication, QWidget

    started = time.perf_counter()
    widgets = QApplication.allWidgets()
    classes = collections.Counter(type(widget).__name__ for widget in widgets)
    windows = []
    for widget in widgets:
        if type(widget).__name__ != "OpenAIChatToolWindow":
            continue
        state = vars(widget)
        backend = state.get("backend")
        manager = getattr(backend, "session_manager", None) if backend else None
        session = manager.get_current_session() if manager else None
        windows.append(
            {
                "window_id": state.get("_window_id"),
                "visible": widget.isVisible(),
                "children": len(widget.findChildren(QWidget)),
                "rendered": state.get("_rendered_card_count"),
                "batches": len(state.get("_batch_cards") or []),
                "live_batches": sum(bool(batch) for batch in (state.get("_batch_cards") or [])),
                "pending_lazy": len(state.get("_pending_lazy_cards") or []),
                "messages": len(getattr(session, "messages", []) or []),
                "streaming": state.get("_is_streaming"),
            }
        )
    objects = gc.get_objects()
    object_classes = collections.Counter(type(obj).__name__ for obj in objects)
    containers = []
    for obj in objects:
        if type(obj) not in (dict, list, tuple, set):
            continue
        size = sys.getsizeof(obj)
        if size < 256 * 1024:
            continue
        containers.append({"type": type(obj).__name__, "entries": len(obj), "shallow_kb": round(size / 1024, 1)})
    result = {
        "pid": os.getpid(),
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "widget_total": len(widgets),
        "widget_classes": classes.most_common(35),
        "windows": windows,
        "gc_object_total": len(objects),
        "gc_classes": object_classes.most_common(35),
        "large_containers": sorted(containers, key=lambda item: item["shallow_kb"], reverse=True)[:30],
    }
    output = Path("D:/work/DriFox/.perf_out/runtime-inventory.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


try:
    _collect_runtime_inventory()
except Exception:
    import traceback
    from pathlib import Path

    Path("D:/work/DriFox/.perf_out/runtime-inventory-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
