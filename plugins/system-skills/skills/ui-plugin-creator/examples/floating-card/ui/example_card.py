# -*- coding: utf-8 -*-
"""浮动卡片类骨架 — ui/example_card.py

参照自系统插件 file-tree 的 FileTreeCard（D:/work/DriFox/plugins/file-tree/ui/cards.py）。

卡片类约定（主程序按此实例化）：
- 必须是 QWidget 子类
- __init__ 必须接受 parent=None（主程序会传停靠区容器作 parent）
- 后台线程必须在 destroyed 信号里清理，否则关窗后线程残留崩溃
- Qt 绑定：DriFox 当前使用 PyQt5（2026-09 实测仓库现状；
  动手时以 grep "from PyQt5" 仓库结果为准）

样式注入惯用法：跟随明暗主题取色（isDarkTheme），objectName 供 QSS 定位。
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget
from qfluentwidgets import isDarkTheme


def _text_color() -> str:
    """主题色辅助：跟随明暗主题（file-tree 同款写法）"""
    if isDarkTheme():
        return "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.85)"


class ExampleCard(QWidget):
    """示例浮动卡片"""

    closed = pyqtSignal()  # [惯例] 卡片关闭信号，主程序据此同步可见状态

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("floating-card-example")  # [改名] QSS 定位用

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)

        self._label = QLabel("Hello, floating card!", self)
        self._label.setStyleSheet(f"color: {_text_color()};")
        layout.addWidget(self._label)
        layout.addStretch(1)

        # [惯例] 销毁时清理后台资源（线程/监听器），防关窗残留
        self.destroyed.connect(self._cleanup)

    def _cleanup(self):
        # 真实插件在这里 stop QThread / 断开 watcher / 释放句柄
        pass
