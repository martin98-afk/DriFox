# -*- coding: utf-8 -*-
"""qfluentwidgets RoundMenu 信号泄漏缺陷防御补丁。

上游缺陷（v1.x 未修）：``RoundMenu.removeAction`` 只把
``action.setProperty('item', None)``，**不断开** ``action.changed →
_onActionChanged`` 的连接。此后该 action 任何属性变化（setText / setIcon /
setEnabled / setVisible 等）都会进入槽，拿到 None 的 item 直接抛
AttributeError：

    File .../qfluentwidgets/components/widgets/menu.py, line 764,
    in _onActionChanged
        item.setIcon(self._createItemIcon(action))
    AttributeError: 'NoneType' object has no attribute 'setIcon'

实测触发于应用退出清理期（2026-11-14 01:27 crash log），偶发、无法溯源到
具体业务调用点（Qt C++ 层的 removeAction / 析构清理同样触发 changed，不经过
Python 覆写，业务侧拦不住）。

修复：接管 ``RoundMenu._onActionChanged``，item 为 None 时断开本次连接后
直接返回。SystemTrayMenu / CheckableMenu / EditMenu 等子类均未覆写该方法
（已核对源码），patch 基类一处即覆盖全部菜单类型。action 重新 add 回菜单时
``addAction`` 会重新连接信号，断开不影响后续正常使用（offscreen 实测）。

调用时机：任何菜单实例化之前即可（仅类属性赋值，不依赖 QApplication 实例），
由 main.py 在 import 段调用。幂等，重复调用无害。
"""

from PyQt5.QtCore import Qt


def patch_qfluentwidgets_menu() -> None:
    """给 RoundMenu._onActionChanged 补 None 防御（详见模块注释）"""
    from qfluentwidgets.components.widgets.menu import RoundMenu

    def _onActionChanged(self):
        """action changed slot（None 防御版，逻辑逐行对齐上游实现）"""
        action = self.sender()
        item = action.property('item')
        if item is None:
            # action 已移出菜单（上游 removeAction 只清 item 属性、不断信号）：
            # 断开本次连接防止后续每次属性变化都重复触发；action 再被 add 回
            # 菜单时 addAction 会重新连接。
            try:
                action.changed.disconnect(self._onActionChanged)
            except (TypeError, RuntimeError):
                pass
            return

        item.setIcon(self._createItemIcon(action))

        if action.text() != action.toolTip():
            item.setToolTip(action.toolTip())

        self._adjustItemText(item, action)

        if action.isEnabled():
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        else:
            item.setFlags(Qt.NoItemFlags)

        self.view.adjustSize()
        self.adjustSize()

    RoundMenu._onActionChanged = _onActionChanged
