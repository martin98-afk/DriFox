# -*- coding: utf-8 -*-
"""WorktreeChangeBridge — 工具线程 → 主线程刷新信号桥

插件工具（worktree_create/remove）在后台线程执行 git 写操作，
完成后的 UI 刷新必须回主线程。本桥提供跨模块唯一的 QObject 单例：

- 工具线程 emit changed(str) → Qt 自动以 QueuedConnection 派发到主线程
- SystemWorktreePage 构造时连接该信号，收到后 refresh_data()
- 页面销毁后 Qt 自动断连，无悬挂引用

单例实现：挂在 QCoreApplication 上（objectName 查找），而非模块级变量。
原因：tool loader 以裸模块（无 __package__）加载 tools/*.py，UI loader
以包方式加载 ui/——两侧拿到的是同一份代码的不同模块副本，模块级单例
无法保证同一实例；QObject 树单例跨副本唯一。
"""

from PyQt5.QtCore import QCoreApplication, QObject, pyqtSignal

_BRIDGE_NAME = "_worktree_change_bridge"
_fallback_bridge: "WorktreeChangeBridge | None" = None


class WorktreeChangeBridge(QObject):
    """工作树变更通知（跨线程安全：任意线程 emit，主线程收槽）"""

    changed = pyqtSignal(str)  # 操作摘要（如 "created <path>"）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName(_BRIDGE_NAME)


def get_bridge() -> QObject:
    """跨模块单例入口（挂在 QCoreApplication 上，objectName 查找）

    返回 QObject（调用方仅用 .changed 信号，无需具体类型）。
    """
    global _fallback_bridge
    app = QCoreApplication.instance()
    if app is not None:
        bridge = app.findChild(QObject, _BRIDGE_NAME)
        if bridge is not None:
            return bridge
        return WorktreeChangeBridge(app)  # parent=app：app 活着它就活着
    # 无 QCoreApplication（纯测试等极端场景）：模块级缓存裸实例
    if _fallback_bridge is None:
        _fallback_bridge = WorktreeChangeBridge()
    return _fallback_bridge
