# -*- coding: utf-8 -*-
"""守卫测试：弹窗显隐广播总线驱动 WebView 让位（app 级事件过滤器已摘除）。

背景（mac SIGSEGV 根因）：message_card 原先向 QApplication 挂全局事件过滤器，
PySide6/shiboken 在 `QObjectWrapper::sbk_o_eventFilter` 里会把事件接收者无条件
包装成 Python 对象；接收者正处于析构过程中（`QObject::d_ptr` 已被置空）时该包装
直接读地址 0x8 → SIGSEGV。崩溃发生在进入 Python 回调之前，Python 侧加
`shiboken6.isValid` 判活根本执行不到，无法防御。

现改为 `dialog_visibility` 一次性 patch 已知弹窗类的 showEvent/hideEvent 做广播，
message_card 只作监听者。本文件守住三件事：
1. 遮罩对话框（MaskDialogBase 系）show/hide 能经总线完成 viewer 隐藏 → 恢复；
2. 非遮罩弹层走降层级分支，不会被误判成遮罩而隐藏 viewer；
3. message_card 里不再出现任何事件过滤器安装调用，防止 app 级过滤器被重新引入。
"""

import inspect

from PySide6.QtCore import QCoreApplication, QObject, Qt
from PySide6.QtWidgets import QWidget

# 必须在导入 message_card（间接拉起 QtWebEngine）之前设置
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.widgets import message_card as mc  # noqa: E402
from app.widgets.dialog_visibility import install  # noqa: E402


class _FakeViewer(QWidget):
    """绑定 CodeWebViewer 让位方法的轻量替身（不拉起 WebEngine）。"""

    _find_chat_scroll_area = mc.CodeWebViewer._find_chat_scroll_area
    _is_mask_dialog = mc.CodeWebViewer._is_mask_dialog
    _hide_for_dialog = mc.CodeWebViewer._hide_for_dialog
    _restore_from_dialog = mc.CodeWebViewer._restore_from_dialog
    _restore_chat_scroll_pos = mc.CodeWebViewer._restore_chat_scroll_pos

    def __init__(self):
        super().__init__()
        self._hidden_dialogs = set()
        self._saved_dialog_scroll_pos = -1
        self.setFixedHeight(400)


class _CountingViewer(_FakeViewer):
    """记录 lower() 次数，用于验证非遮罩弹层走的是降层级分支。"""

    def __init__(self):
        super().__init__()
        self.lower_calls = 0

    def lower(self):
        self.lower_calls += 1


class _FakeFlyout(QWidget):
    """类名含 Flyout 不含 Dialog 的贴附弹层替身。"""


def _make_mask_dialog(qapp):
    """构造真实遮罩对话框（ConfirmDialog 是 MaskDialogBase 子类，带半透明属性）。"""
    from app.widgets.common_dialogs import ConfirmDialog

    return ConfirmDialog("守卫测试", "确认？", parent=None)


def test_no_app_level_event_filter_left():
    """结构性守卫：message_card 不得再向 QApplication 装事件过滤器。

    一旦有人把 app 级过滤器加回来，mac 上的析构期 SIGSEGV 就会复现，
    而这类崩溃在 Python 侧无法捕获，只能靠本测试拦住。
    """
    src = inspect.getsource(mc)
    assert ".installEventFilter(" not in src, "message_card 重新引入了事件过滤器安装，mac 会硬崩"
    bridge = mc._dialog_visibility_bridge
    assert not isinstance(bridge, QObject), "桥不应是 QObject（QObject 才可能被当成事件过滤器）"
    assert not hasattr(mc, "_DialogEventFilter"), "旧的 _DialogEventFilter 不得复活"


def test_bus_installs_and_patches_mask_dialog():
    """install() 能包装到遮罩对话框类，且幂等（重复调用不二次包装）。"""
    assert install() is True, "未能包装任何弹窗类，WebView 让位会静默失效"
    assert install() is True, "幂等重入应仍报告成功"
    from qfluentwidgets import MaskDialogBase

    assert getattr(MaskDialogBase, "_drifox_visibility_patched", False)


def test_mask_dialog_show_hide_drives_viewer(qapp):
    """端到端：真实遮罩对话框 show() → viewer 隐藏；close() → viewer 恢复。

    全程不手动调桥的方法，只靠 showEvent/hideEvent 广播，确保接线本身有效。
    """
    bridge = mc._dialog_visibility_bridge
    viewer = _FakeViewer()
    bridge.register(viewer)
    dialog = _make_mask_dialog(qapp)
    try:
        assert not viewer.isHidden()
        dialog.show()
        qapp.processEvents()
        assert viewer.isHidden(), "遮罩对话框显示后 viewer 应让位隐藏"
        assert dialog in viewer._hidden_dialogs

        dialog.close()
        qapp.processEvents()
        assert not viewer.isHidden(), "对话框关闭后 viewer 应恢复显示"
        assert not viewer._hidden_dialogs
    finally:
        bridge.unregister(viewer)
        dialog.deleteLater()
        viewer.deleteLater()
        qapp.processEvents()


def test_hide_without_close_still_restores(qapp):
    """对话框被直接 hide()（不走 close()，因而无 finished 信号）时靠 hideEvent 兜底恢复。"""
    bridge = mc._dialog_visibility_bridge
    viewer = _FakeViewer()
    bridge.register(viewer)
    dialog = _make_mask_dialog(qapp)
    try:
        dialog.show()
        qapp.processEvents()
        assert viewer.isHidden()

        dialog.hide()
        qapp.processEvents()
        assert not viewer.isHidden(), "hideEvent 兜底应恢复 viewer，否则 WebView 永久空白"
    finally:
        bridge.unregister(viewer)
        dialog.deleteLater()
        viewer.deleteLater()
        qapp.processEvents()


def test_non_mask_layer_lowers_instead_of_hiding(qapp):
    """贴附弹层只降层级，绝不该隐藏 viewer（误判会让聊天区突然空白）。"""
    bridge = mc._dialog_visibility_bridge
    viewer = _CountingViewer()
    bridge.register(viewer)
    layer = _FakeFlyout()
    try:
        bridge.on_dialog_shown(layer)
        assert viewer.lower_calls >= 1, "非遮罩弹层应触发降层级"
        assert not viewer.isHidden(), "非遮罩弹层不得隐藏 viewer"
        assert not viewer._hidden_dialogs
    finally:
        bridge.unregister(viewer)
        layer.deleteLater()
        viewer.deleteLater()
        qapp.processEvents()


def test_unregister_is_idempotent_and_broadcast_is_noop(qapp):
    """最后一个 viewer 注销后广播回调须安全空转（总线常驻不卸载）。"""
    bridge = mc._dialog_visibility_bridge
    viewer = _FakeViewer()
    bridge.register(viewer)
    bridge.unregister(viewer)
    bridge.unregister(viewer)  # 幂等
    assert not bridge._viewers
    dialog = _make_mask_dialog(qapp)
    try:
        dialog.show()  # 无 viewer 注册：广播照发，不得抛异常
        qapp.processEvents()
    finally:
        dialog.deleteLater()
        qapp.processEvents()
