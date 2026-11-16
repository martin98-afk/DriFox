# -*- coding: utf-8 -*-
"""通过 sys.remote_exec 只读采集标签动画状态，不读取会话正文。"""


def _collect_tab_animation_state():
    import json
    import os
    import tempfile
    from pathlib import Path

    from PyQt5.QtWidgets import QApplication

    panels = []
    for panel in QApplication.allWidgets():
        if type(panel).__name__ != "TabPanel":
            continue
        timer = panel._anim_timer
        items = [
            {
                "index": index,
                "visible": item.isVisible(),
                "streaming": item._streaming,
                "error": item._stream_error,
                "question": item._question,
            }
            for index, item in enumerate(panel._items)
        ]
        panels.append(
            {
                "visible": panel.isVisible(),
                "timer_active": bool(timer and timer.isActive()),
                "counts": [panel._streaming_count, panel._question_count, panel._error_count],
                "actual": [sum(item[key] for item in items) for key in ("streaming", "question", "error")],
                "items": items,
            }
        )
    result = {"pid": os.getpid(), "panels": panels}
    output = Path(tempfile.gettempdir()) / f"drifox-tab-animation-{os.getpid()}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


_collect_tab_animation_state()
