# -*- coding: utf-8 -*-
"""macOS 只读归因探针：只统计内置容器与已知缓存，不扫 GC、不重载消息、不输出正文。"""


def _collect_mac_known_roots():
    import collections
    import json
    import os
    import sys
    import tempfile
    import threading
    import time
    from pathlib import Path

    from PyQt5.QtWidgets import QApplication

    started = time.perf_counter()
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("main thread only")
    seen = set()

    def measure(value, depth=0):
        stack = [(value, 0)]
        total = 0
        nodes = 0
        while stack:
            item, depth = stack.pop()
            ident = id(item)
            if ident in seen:
                continue
            seen.add(ident)
            nodes += 1
            if nodes > 150000:
                break
            kind = type(item)
            if kind not in (dict, collections.OrderedDict, list, tuple, set, frozenset,
                            str, bytes, bytearray, int, float, bool, type(None)):
                continue
            total += sys.getsizeof(item)
            if depth >= 16:
                continue
            if kind in (dict, collections.OrderedDict):
                stack.extend((child, depth + 1) for child in list(item.keys()) + list(item.values()))
            elif kind in (list, tuple, set, frozenset):
                stack.extend((child, depth + 1) for child in item)
        return total

    widgets = QApplication.allWidgets()
    windows = [w for w in widgets if type(w).__name__ == "OpenAIChatToolWindow"]
    rows = []
    history_seen = set()
    session_total = 0
    for index, window in enumerate(windows):
        state = vars(window)
        backend = state.get("backend")
        bs = vars(backend) if backend is not None else {}
        manager = bs.get("_session_manager")
        if manager is not None:
            for session in vars(manager).get("sessions", []):
                data = vars(session)
                session_total += measure(data)
        history = bs.get("_history_manager")
        if history is not None and id(history) not in history_seen:
            history_seen.add(id(history))
            rows.append({"history_manager_mb": round(measure(vars(history)) / 1048576, 2)})
    module_roots = {}
    for name in ("app.utils.theme_refresh", "app.utils.image_utils", "app.utils.provider_icons", "app.utils.utils"):
        module = sys.modules.get(name)
        if module is None:
            continue
        for key, value in list(vars(module).items()):
            if "cache" in key.lower() and type(value) in (dict, list, collections.OrderedDict):
                size = measure(value)
                if size > 1024:
                    module_roots[name + "." + key] = round(size / 1048576, 3)
    # Python 对象总账：sys 与 gc 模块级计数，不做全堆遍历
    import gc
    gc.freeze() if hasattr(gc, "freeze") else None
    objects = gc.get_objects()
    py_total = 0
    kind_sizes = collections.Counter()
    for obj in objects:
        kind = type(obj)
        size = sys.getsizeof(obj)
        py_total += size
        kind_sizes[kind.__name__] += size
    result = {
        "pid": os.getpid(),
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "allocated_blocks": sys.getallocatedblocks(),
        "widget_count": len(widgets),
        "session_known_mb": round(session_total / 1048576, 2),
        "history_manager_mb": rows[0]["history_manager_mb"] if rows else 0,
        "module_cache_mb": module_roots,
        "gc_tracked_objects": len(objects),
        "python_object_total_mb": round(py_total / 1048576, 2),
        "top_python_types_mb": [(name, round(size / 1048576, 2)) for name, size in kind_sizes.most_common(12)],
        "malloc_zone_hint": "对照 vmmap DefaultMallocZone 821MB 内活对象",
    }
    output = Path(tempfile.gettempdir()) / f"drifox-mac-roots-{os.getpid()}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


_collect_mac_known_roots()
