# -*- coding: utf-8 -*-
"""UI 驱动观测：截图 / 状态快照 / 内存采样（缺失字段一律降级 None，不抛错）。

state 字段对应 main_widget 虚拟滚动与渲染配额的内部容器（S1 观测面）：
会话 / 批次 / 卡 / 池 / 配额 / 懒队列 / _unloaded_pids。
"""

from __future__ import annotations

from typing import Any, Optional, cast

from PyQt5.QtCore import QBuffer, QIODevice
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication, QWidget

from .bus import guard, invoke

_MAX_GRAB_WIDTH = 1280

# PyQt5 存根未暴露该枚举成员（pyright 误报），运行时常量存在
_WRITE_ONLY = cast("QIODevice.OpenModeFlag", getattr(QIODevice, "WriteOnly"))


def screenshot(w: Optional[QWidget] = None, max_width: int = _MAX_GRAB_WIDTH) -> Optional[bytes]:
    """控件 grab() 截图 → PNG bytes（超宽按比例下采样到 max_width）。

    w=None 时截 QApplication 首个可见顶层窗口。grab 失败返回 None。
    """
    guard("observe.screenshot")

    def _run() -> Optional[bytes]:
        target = w
        if target is None:
            target = next((x for x in QApplication.allWidgets() if x.isWindow() and x.isVisible()), None)
        if target is None:
            return None
        try:
            pixmap = target.grab()
        except Exception:  # noqa: BLE001 — grab 在窗口析构竞态下可能失败
            return None
        if pixmap.isNull():
            return None
        image = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)
        if image.width() > max_width:
            image = image.scaledToWidth(max_width)
        buf = QBuffer()
        buf.open(_WRITE_ONLY)
        image.save(buf, "PNG")
        return bytes(buf.data())

    return invoke(_run)


def _window_state() -> dict[str, Any]:
    """从存活主窗口提取虚拟滚动/渲染容器快照；任意字段缺失降级 None。"""
    out: dict[str, Any] = {}
    try:
        from app.core.infra.window_registry import alive_window_instances

        wins = alive_window_instances()
        if not wins:
            return {"window_alive": False}
        mw = wins[0]
        cards = getattr(mw, "_batch_cards", None) or []
        out["window_id"] = str(getattr(mw, "_window_id", "") or "") or None
        out["session_id"] = _short(getattr(mw, "_current_session_id", None))
        manager = getattr(mw, "session_manager", None)
        session = manager.get_current_session() if manager is not None else None
        msgs = getattr(session, "messages", None) if session else None
        out["session_msgs"] = len(msgs) if isinstance(msgs, list) else None
        out["batches"] = _len_or_none(getattr(mw, "_message_batch", None))
        out["cards_alive"] = sum(1 for c in cards if c is not None) if isinstance(cards, list) else None
        out["visible_batch_start"] = getattr(mw, "_visible_batch_start", None)
        out["visible_batch_end"] = getattr(mw, "_visible_batch_end", None)
    except Exception as exc:  # noqa: BLE001 — 观测不抛错
        out["window_state_error"] = repr(exc)
    return out


def _pool_state() -> dict[str, Any]:
    """WebView 池 + 配额 + 懒队列 + 已卸载 pids。"""
    out: dict[str, Any] = {}
    try:
        from app.widgets.webview_pool import WebViewPool

        pool = WebViewPool.get_instance()
        pids = pool.pids() if hasattr(pool, "pids") else None
        out["pool_pids"] = list(pids) if pids is not None else None
        out["pool_pids_count"] = len(pids) if pids is not None else None
    except Exception as exc:  # noqa: BLE001
        out["pool_error"] = repr(exc)
    try:
        from app.core.infra.window_registry import alive_window_instances

        wins = alive_window_instances()
        if wins:
            mw = wins[0]
            unloaded = getattr(mw, "_unloaded_pids", None)
            out["unloaded_pids"] = sorted(unloaded) if isinstance(unloaded, (set, list)) else None
            out["render_quota_global"] = getattr(mw, "_global_rendered_cards", None)
            lazy = getattr(mw, "_lazy_render_queue", None)
            out["lazy_queue_len"] = len(lazy) if isinstance(lazy, (list, set)) else None
    except Exception as exc:  # noqa: BLE001
        out["quota_error"] = repr(exc)
    return out


def state() -> dict[str, Any]:
    """会话/批次/卡/池/配额/懒队列 快照（主线程投递执行）。"""
    guard("observe.state")

    def _run() -> dict[str, Any]:
        snap: dict[str, Any] = {}
        snap.update(_window_state())
        snap.update(_pool_state())
        return snap

    return invoke(_run)


def memory() -> dict[str, Any]:
    """主进程 Private/WS + WebEngine 子进程 RSS 汇总 + 容器计数（psutil 缺失降级 None）。"""
    guard("observe.memory")

    def _run() -> dict[str, Any]:
        out: dict[str, Any] = {
            "main_private_mb": None,
            "main_ws_mb": None,
            "webengine_count": None,
            "webengine_rss_mb": None,
        }
        try:
            import psutil

            proc = psutil.Process()
            mem = proc.memory_info()
            out["main_ws_mb"] = round(mem.rss / 1048576, 1)
            # Windows 口径对齐 T1b/T2：memory_info().private = PrivateUsage
            # （= PrivateMemorySize64，进程私有提交字节）；uss 仅物理去共享，不等价
            private = getattr(mem, "private", None)
            out["main_private_mb"] = (
                round(private / 1048576, 1) if isinstance(private, int) else None
            )
            children = [c for c in proc.children(recursive=True) if "webengine" in c.name().lower()]
            out["webengine_count"] = len(children)
            out["webengine_rss_mb"] = round(sum(c.memory_info().rss for c in children) / 1048576, 1) if children else 0
        except Exception as exc:  # noqa: BLE001 — psutil 缺失/权限不足降级
            out["memory_error"] = repr(exc)
        out["containers"] = _window_state()
        return out

    return invoke(_run)


def _len_or_none(value: Any) -> Optional[int]:
    return len(value) if isinstance(value, (list, dict, set)) else None


def _short(value: Any) -> Optional[str]:
    s = str(value or "")
    return s[:8] if s else None


__all__ = ["memory", "screenshot", "state"]
