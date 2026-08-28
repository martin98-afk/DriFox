# -*- coding: utf-8 -*-
"""
单实例守护 - 确保程序只运行一个实例
重复启动时，通过 IPC 通知已有实例显示窗口
"""
from typing import Optional

from loguru import logger
from PySide6.QtCore import QObject, QSharedMemory, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstanceGuard(QObject):
    """
    单实例守护，确保程序只运行一个实例。

    工作流程：
    1. 首次启动：创建 QSharedMemory 标记 + 启动 QLocalServer 监听
    2. 二次启动：QSharedMemory attach 成功 → 通过 QLocalSocket 发送 "show" 命令
    3. 主实例收到命令 → 发出 show_requested 信号 → 窗口激活

    注意：必须在 QApplication 创建之后使用（依赖 QtNetwork）
    """

    show_requested = Signal()

    def __init__(self, app_name: str = "Drifox"):
        super().__init__(parent=None)
        self._app_name = app_name
        self._shared_memory: Optional[QSharedMemory] = None
        self._server: Optional[QLocalServer] = None
        self._is_first_instance = False

    def try_lock(self) -> bool:
        """
        尝试获取单实例锁

        Returns:
            True: 当前实例是第一个实例，可以继续启动
            False: 已有实例在运行
        """
        key = f"{self._app_name}_SingleInstance"
        self._shared_memory = QSharedMemory(key)

        if self._shared_memory.attach():
            # 共享内存已存在 → 可能已有实例在运行
            # 但需要验证该实例是否还活着（进程崩溃后共享内存不会自动清理）
            if self._is_server_alive():
                logger.info("检测到已有实例在运行")
                self._is_first_instance = False
                return False
            else:
                # 共享内存残留：前一个实例已崩溃或异常退出
                logger.warning(
                    "检测到残留的单实例锁（前一个实例可能已崩溃），"
                    "将自动清理并重新获取"
                )
                self._shared_memory.detach()

        # 创建共享内存（标记自己是第一个实例）
        if self._shared_memory.create(1):
            logger.info("单实例锁已获取，作为第一个实例启动")
            self._is_first_instance = True
            self._start_local_server()
            return True

        # 创建失败：在 POSIX 系统（macOS/Linux）上，QSharedMemory::detach()
        # 不会调用 shm_unlink()，命名共享内存段仍然存在。
        # 此时尝试 attach 并"接管"这个残留锁。
        if self._shared_memory.attach():
            logger.info(
                "单实例锁已接管（残留共享内存），作为第一个实例启动"
            )
            self._is_first_instance = True
            self._start_local_server()
            return True

        # 创建失败（极少情况：并发启动时的竞争）
        logger.warning(f"单实例锁创建失败: {self._shared_memory.error()}")
        return False

    def request_show_window(self) -> None:
        """向已有实例发送显示窗口请求，然后退出"""
        server_name = f"{self._app_name}_SingleInstanceServer"
        socket = QLocalSocket()

        socket.connectToServer(server_name)
        if socket.waitForConnected(2000):
            socket.write(b"show")
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()
            logger.info("已通知运行中的实例显示窗口")
        else:
            # 连接失败 → 服务器可能还没启动（罕见竞争），忽略
            logger.warning(
                f"无法连接到运行中的实例: {socket.errorString()}，"
                f"将忽略本次启动"
            )

    def _is_server_alive(self) -> bool:
        """
        验证之前实例的 IPC 服务器是否还活着。

        尝试连接 QLocalServer，如果连接成功说明进程确实在运行。
        如果连接失败（超时或拒绝），说明是残留锁。
        """
        server_name = f"{self._app_name}_SingleInstanceServer"
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        alive = socket.waitForConnected(1000)  # 1 秒超时
        if alive:
            socket.disconnectFromServer()
        return alive

    def _start_local_server(self) -> None:
        """启动本地服务器，监听其他实例的显示请求"""
        server_name = f"{self._app_name}_SingleInstanceServer"

        # 移除可能残留的服务器（程序崩溃等场景）
        QLocalServer.removeServer(server_name)

        self._server = QLocalServer(self)
        if self._server.listen(server_name):
            logger.info(f"单实例 IPC 服务器已启动: {server_name}")
            self._server.newConnection.connect(self._on_new_connection)
        else:
            logger.warning(
                f"单实例 IPC 服务器启动失败: {self._server.errorString()}"
            )

    def _on_new_connection(self) -> None:
        """处理新连接：读取命令并发出信号"""
        socket = self._server.nextPendingConnection()
        if not socket:
            return

        # 等待数据到达（最多 1 秒）
        if socket.waitForReadyRead(1000):
            data = socket.readAll().data().decode().strip()
            if data == "show":
                logger.info("收到其他实例的显示请求")
                self.show_requested.emit()

        socket.disconnectFromServer()
        socket.deleteLater()

    def cleanup(self) -> None:
        """主动清理单实例资源"""
        if self._server:
            server_name = f"{self._app_name}_SingleInstanceServer"
            self._server.close()
            QLocalServer.removeServer(server_name)
            self._server = None

        if self._shared_memory and self._is_first_instance:
            self._shared_memory.detach()
            self._shared_memory = None

        logger.info("单实例资源已清理")
