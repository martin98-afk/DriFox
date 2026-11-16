# -*- coding: utf-8 -*-
"""单次按顶层宿主聚合真实控件；仅输出类名、数量和标识，不输出正文。"""


def _collect_widget_owners():
    import collections
    import json
    import os
    import time
    from pathlib import Path

    import sip
    from PyQt5.QtWidgets import QApplication
    from qfluentwidgets.common.style_sheet import styleSheetManager

    start = time.perf_counter()
    owners = {}
    card_owners = {}
    widgets = QApplication.allWidgets()
    for widget in widgets:
        top = widget.window()
        key = id(top)
        if key not in owners:
            owners[key] = {
                "class": type(top).__name__,
                "object_name": top.objectName(),
                "visible": top.isVisible(),
                "widget_count": 0,
                "qss_chars": 0,
                "classes": collections.Counter(),
            }
        owner = owners[key]
        owner["widget_count"] += 1
        owner["qss_chars"] += len(widget.styleSheet())
        owner["classes"][type(widget).__name__] += 1
        parent = widget
        while parent is not None:
            cls = type(parent).__name__
            if cls.endswith("Card") or cls.endswith("CardContent"):
                card_key = id(parent)
                if card_key not in card_owners:
                    card_owners[card_key] = {"class": cls, "visible": parent.isVisible(), "widget_count": 0, "qss_chars": 0}
                card_owners[card_key]["widget_count"] += 1
                card_owners[card_key]["qss_chars"] += len(widget.styleSheet())
                break
            parent = parent.parentWidget()
    for row in owners.values():
        row["classes"] = row["classes"].most_common(12)
    style_widgets = list(styleSheetManager.widgets)
    result = {
        "pid": os.getpid(),
        "duration_ms": round((time.perf_counter() - start) * 1000, 1),
        "owners": sorted(owners.values(), key=lambda row: row["widget_count"], reverse=True)[:30],
        "card_owners": sorted(card_owners.values(), key=lambda row: row["widget_count"], reverse=True)[:35],
        "registered_styles": len(style_widgets),
        "deleted_style_widgets": sum(sip.isdeleted(widget) for widget in style_widgets),
        "orphan_styles": sum(not sip.isdeleted(widget) and widget.parent() is None for widget in style_widgets),
    }
    path = Path("D:/work/DriFox/.perf_out/widget-owners.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


try:
    _collect_widget_owners()
except Exception:
    import traceback
    from pathlib import Path

    Path("D:/work/DriFox/.perf_out/widget-owners-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
