# -*- coding: utf-8 -*-
"""独立弹窗示例 — 内容页（register_window 的 widget_class）

窗口壳（无边框/标题栏/拖动缩放/最小化/关闭/主题/生命周期）全由主程序提供，
本页只写客户区内容。三个约定接口（强烈建议全实现）：

- ``set_context_provider(provider)`` — 主程序注入上下文拉模型，每次 provider() 返回最新值
- ``show_card()`` — 弹窗打开/激活时主程序调用；**数据加载与主题应用的唯一入口**
  （别在 __init__ 里做重加载，否则不同宿主行为不一致——pitfalls §19）
- ``refresh_theme()`` — 主题切换时主程序调用
"""

from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from qfluentwidgets import isDarkTheme


def _ctx_colors(ctx) -> dict:
    """上下文取色：优先 ctx["colors"]，缺失按明暗主题回退安全色"""
    colors = (ctx or {}).get("colors", {}) or {}
    dark = isDarkTheme()
    return {
        "text_primary": colors.get("text_primary")
        or ("rgba(255,255,255,0.9)" if dark else "rgba(0,0,0,0.85)"),
        "text_secondary": colors.get("text_secondary")
        or ("rgba(255,255,255,0.55)" if dark else "rgba(0,0,0,0.45)"),
    }


class ExampleWindowPage(QWidget):
    """示例内容页：说明 + 计数器（把你的 UI 填在这里）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._provider = None
        self._count = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)

        self._title = QLabel("示例工具窗", self)
        lay.addWidget(self._title)

        self._desc = QLabel(
            "本内容页由 register_window 挂载：窗口壳由主程序提供，\n"
            "左侧插件栏点击开/关，右键菜单「弹出」重新打开。", self)
        self._desc.setWordWrap(True)
        lay.addWidget(self._desc)

        row = QHBoxLayout()
        self._btn = QPushButton("点我 +1", self)
        self._btn.clicked.connect(self._on_count)
        self._lbl = QLabel("点击次数：0", self)
        row.addWidget(self._btn)
        row.addWidget(self._lbl)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addStretch(1)

    # ── 主程序约定接口 ──

    def set_context_provider(self, provider):
        self._provider = provider

    def show_card(self):
        """打开/激活入口：刷新主题 + 加载数据（本例数据即计数器）"""
        self._apply_theme()

    def refresh_theme(self):
        self._apply_theme()

    # ── 内部 ──

    def _on_count(self):
        self._count += 1
        self._lbl.setText(f"点击次数：{self._count}")

    def _apply_theme(self):
        ctx = {}
        if self._provider is not None:
            try:
                ctx = self._provider()
            except Exception:
                ctx = {}
        c = _ctx_colors(ctx)
        self._title.setStyleSheet(
            f"color: {c['text_primary']}; font-size: 16px; font-weight: 600; background: transparent;"
        )
        self._desc.setStyleSheet(
            f"color: {c['text_secondary']}; font-size: 12px; background: transparent;"
        )
        self._lbl.setStyleSheet(f"color: {c['text_primary']}; background: transparent;")
