"""命令泵：Qt 主线程 QTimer 周期消费命令队列。

命令路径（架构文档 §一）：WS/REST → command_queue → QTimer(10ms) 主线程
drain → handler 调 ChatBackend 方法。需返回值的命令 M2 经
run_coroutine_threadsafe 回传；M1 骨架以 ping→pong 事件广播验证通路。
"""
import logging
import queue as _q

from PyQt5.QtCore import QObject, QTimer

logger = logging.getLogger("drifox.server.pump")


class CommandPump(QObject):
    def __init__(
        self,
        command_queue: "_q.Queue",
        handler,
        interval_ms: int = 10,
        parent=None,
    ):
        super().__init__(parent)
        self._q = command_queue
        self._handler = handler
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._drain)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _drain(self) -> None:
        while True:
            try:
                cmd = self._q.get_nowait()
            except _q.Empty:
                return
            try:
                self._handler(cmd)
            except Exception:
                # 单条命令失败不影响泵；M2 起 payload 带 error 事件回执
                logger.exception("命令处理失败: %r", cmd)
