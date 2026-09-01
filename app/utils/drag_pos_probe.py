# -*- coding: utf-8 -*-
"""窗口拖拽后位置跳回问题的诊断探针（临时，tag: [DRAG-POS]）

背景：偶发"拖完窗口又跳回原位置"，静态排查未发现应用层写回位置的代码。
本探针用于抓现场，回答两个问题：

1. 跳回是不是 Python 层代码写回的？
   → patch 主窗口的 move()/setGeometry()，任何调用都输出调用栈。
2. 跳回发生在什么时间点？（拖动循环内 / 松手之后）
   → 结合 nativeEvent 的 WM_ENTERSIZEMOVE / WM_EXITSIZEMOVE 通知，
     输出完整时间线：发起 → 跟随移动 → 松手 → （可能的）瞬移。

瞬移判定：相邻两次 Move 事件位置差 > _JUMP_THRESHOLD 像素
（真实拖动中鼠标事件间隔内位移远小于该值）。

使用：TabManagerWindow.__init__ 末尾 install_drag_pos_probe(self)；
nativeEvent 拖拽起止分支调用 drag_pos_probe.mark_drag_start()/mark_drag_end()。
定位完成后 ENABLED 置 False 一键关闭，再整体删除本文件与接线点。
"""

import sys
import time
import traceback

from loguru import logger
from PyQt5.QtCore import QEvent, QObject

# 一键开关：False 时 install/mark 全部变为空操作
ENABLED = True

# 瞬移（跳回）判定阈值（像素）
_JUMP_THRESHOLD = 80
# 常规移动日志限频（秒）
_MOVE_LOG_INTERVAL = 0.5


class DragPosProbe(QObject):
    """监听主窗口 Move 事件并记录时间线（单实例，挂在窗口上）"""

    def __init__(self, win) -> None:
        super().__init__(win)
        self._win = win
        self._last_move_pos: tuple[int, int] | None = None
        self._last_move_log_at = 0.0
        self._drag_active = False

    # ── 生命周期 ──────────────────────────────────────────────

    def attach(self) -> None:
        """安装事件过滤器 + patch move/setGeometry"""
        self._win.installEventFilter(self)
        self._patch("move")
        self._patch("setGeometry")
        logger.info("[DRAG-POS] 探针已安装 tag=DRAG-POS")

    def detach(self) -> None:
        """卸载（保留给收尾清理用）"""
        self._win.removeEventFilter(self)
        for name in ("move", "setGeometry"):
            if getattr(self._win, f"_probe_orig_{name}", None) is not None:
                try:
                    delattr(self._win, name)
                except AttributeError:
                    pass

    # ── 拖拽起止通知（nativeEvent 调用） ──────────────────────

    def mark_drag_start(self) -> None:
        self._drag_active = True
        g = self._win.geometry()
        self._last_move_pos = (g.x(), g.y())
        self._last_move_log_at = time.perf_counter()
        logger.info(f"[DRAG-POS] ENTER 几何=({g.x()},{g.y()} {g.width()}x{g.height()})")

    def mark_drag_end(self) -> None:
        g = self._win.geometry()
        self._drag_active = False
        self._last_move_pos = (g.x(), g.y())
        self._last_move_log_at = time.perf_counter()
        logger.info(f"[DRAG-POS] EXIT  几何=({g.x()},{g.y()} {g.width()}x{g.height()})")

    # ── 事件处理 ──────────────────────────────────────────────

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 约定
        if obj is self._win and event.type() == QEvent.Move:
            self._on_move(int(event.pos().x()), int(event.pos().y()))
        return super().eventFilter(obj, event)

    def _on_move(self, x: int, y: int) -> None:
        prev = self._last_move_pos
        if prev is None:
            self._last_move_pos = (x, y)
            return

        dx, dy = x - prev[0], y - prev[1]
        dist2 = dx * dx + dy * dy

        # 瞬移检测：一次 Move 内位移超阈值 → 大概率就是"跳回"现场
        if dist2 > _JUMP_THRESHOLD * _JUMP_THRESHOLD:
            logger.warning(f"[DRAG-POS] ★JUMP 位移=({dx},{dy}) {prev}→({x},{y}) 拖拽中={self._drag_active}")
            # 若是 Python 层写回，patch 的 wrapped 已打印调用栈；此处补当前栈
            logger.warning("[DRAG-POS] JUMP 现场 Qt 事件栈:\n" + "".join(traceback.format_stack(limit=8)))
            self._last_move_pos = (x, y)
            self._last_move_log_at = time.perf_counter()
            return

        # 常规移动限频记录（跟随性证据）
        now = time.perf_counter()
        if now - self._last_move_log_at >= _MOVE_LOG_INTERVAL:
            self._last_move_pos = (x, y)
            self._last_move_log_at = now
            logger.debug(f"[DRAG-POS] move → ({x},{y}) 拖拽中={self._drag_active}")

    # ── patch 基础设施 ────────────────────────────────────────

    def _patch(self, name: str) -> None:
        """包装实例的 move/setGeometry：位置写入时输出调用栈"""
        orig = getattr(type(self._win), name)

        def wrapped(*args, **kwargs):  # noqa: ANN002, ANN003
            try:
                before = self._win.geometry()
                result = orig(self._win, *args, **kwargs)
                after = self._win.geometry()
                if (before.x(), before.y()) != (after.x(), after.y()):
                    stack = "".join(traceback.format_stack(limit=10))
                    logger.warning(
                        f"[DRAG-POS] ★PY-WRITE {name}: ({before.x()},{before.y()})→({after.x()},{after.y()})\n{stack}"
                    )
                return result
            except Exception:
                # 探针自身异常绝不影响正常调用
                return orig(self._win, *args, **kwargs)

        # 保存原函数供 detach 恢复；实例属性覆盖类方法
        setattr(self._win, f"_probe_orig_{name}", orig)
        setattr(self._win, name, wrapped)  # type: ignore[method-assign]


_probe: DragPosProbe | None = None


def install_drag_pos_probe(win) -> None:
    """模块入口：给主窗口装探针（幂等）"""
    global _probe
    if not ENABLED or _probe is not None:
        return
    if sys.platform != "win32":
        return
    _probe = DragPosProbe(win)
    _probe.attach()


def mark_drag_start() -> None:
    if _probe is not None:
        _probe.mark_drag_start()


def mark_drag_end() -> None:
    if _probe is not None:
        _probe.mark_drag_end()
