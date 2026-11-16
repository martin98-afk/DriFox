# -*- coding: utf-8 -*-
"""性能观测 JSONL 追加工具（观测线公共件）。

产物：``.perf_out/trace_events.jsonl``（相对进程 CWD，已 gitignore，仅留本地）。

设计约束
--------
* **零侵入**：只 import 不调用时不产生任何文件/目录，不改任何产品行为。
* **失败静默**：观测不得反噬主功能，任何异常（权限/磁盘/不可序列化）一律吞掉。
* **并发安全**：模块级单例 Lock 串行化写入，多线程追加不撕裂行。
* **不依赖 Qt**：后台线程亦可调用。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

_OUT_DIR = ".perf_out"
_FILE_NAME = "trace_events.jsonl"

# 模块级单例锁：全进程共享，追加串行化。
_LOCK = threading.Lock()


def env_flag(name: str) -> bool:
    """布尔型环境变量门控（1/true/on/yes 为开，其余含空值一律关）。

    埋点统一走这里，保证口径一致（对齐 main.py / webview_pool.py 现有写法）。
    """
    return os.environ.get(name, "").strip().lower() in ("1", "true", "on", "yes")


def trace_path() -> Path:
    """JSONL 落点。只算路径，不建目录。"""
    return Path(_OUT_DIR) / _FILE_NAME


def append_event(metric: str, value: Any, tags: Optional[Dict[str, Any]] = None) -> None:
    """追加一行 ``{"ts", "proc", "metric", "value", "tags"}``；失败静默丢弃本条。"""
    try:
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "proc": "main",
            "metric": metric,
            "value": value,
            "tags": tags or {},
        }
        # default=str：datetime/UUID/Path 等降级为字符串，避免整行丢失。
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with _LOCK:
            path = trace_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
    except Exception:
        return
