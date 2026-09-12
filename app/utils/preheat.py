# -*- coding: utf-8 -*-
"""preheat — 进程级预热（批5 壳先行配套）。

壳先行（TabManagerWindow.show() 先于首窗构造）把「可见但未就绪」窗口期拉长
到数百 ms，此间首窗构造的重头 I/O 会集中爆发。本模块把其中**进程级、无 UI
依赖**的三项提前到壳显示之后、首窗构造之前执行，缩短首窗构造阻塞：

- SessionStore 单例（建库 + 完整性检查/修复 `_check_and_repair_database`）
- `get_session_storage()`（StorageRegistry 活跃引擎激活）
- `tool_classifier.get_all_tools()`（app.tools 内置工具级联 import）

⚠️ 刻意**不含** HistoryManager：其 get_instance 无锁（非线程安全）且 __init__
构造 QObject（必须主线程）——留原路径，见总纲 v2 批5 / T8 结论。

幂等：模块级 `_done` 标志，进程内只执行一次。
"""

import time

from loguru import logger

_done = False


def _preheat_session_store():
    from app.core.store.session_store import SessionStore

    SessionStore.get_instance()


def _preheat_session_storage():
    from app.core.backend import get_session_storage

    get_session_storage()


def _preheat_tools():
    from app.tools.tool_classifier import get_all_tools

    return get_all_tools()


def preheat_process_level() -> None:
    """进程级预热入口（主线程调用；进程内幂等）。"""
    global _done
    if _done:
        return
    _done = True
    t0 = time.perf_counter()
    try:
        _preheat_session_store()
        _preheat_session_storage()
        tools = _preheat_tools()
        logger.info(
            f"[Preheat] 进程级预热完成 "
            f"{(time.perf_counter() - t0) * 1000:.0f}ms（SessionStore/StorageRegistry/tools×{len(tools)}）"
        )
    except Exception:  # noqa: BLE001
        # 预热失败不阻塞启动（首窗构造路径会自然重建这些单例）
        logger.exception("[Preheat] 进程级预热失败（降级为首窗构造路径）")
