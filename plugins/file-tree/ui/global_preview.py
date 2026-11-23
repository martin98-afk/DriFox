# -*- coding: utf-8 -*-
"""全局预览浮层 —— 遮罩式大图 / 文本查看

右键「全局预览」用它打开：脱离右侧工作台页的宽度限制，占父窗口约 86%，
图片沿用 ``_ImageView`` 的手感（滚轮缩放、自由平移、双击适应/1:1），
文本沿用预览页的代码视图与 markdown 三态。

关闭：顶栏返回按钮 / Esc / 点击遮罩空白。

约束同 cards.py 与 preview.py：不导入 app.core / app.widgets。
"""

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget
from qfluentwidgets import MaskDialogBase

from .preview import FilePreviewPage

_OVERLAY_OBJECT = "file-tree-global-preview"


class GlobalPreviewDialog(MaskDialogBase):
    """遮罩式全局面板（非模态，遮住父窗口，点遮罩/Esc 关闭）

    ⚠️ 类名必须带 "Dialog"：主程序的全局弹窗事件过滤器
    （card_viewers._DialogEventFilter）只对类名命中 _POPUP_KEYWORDS
    （Dialog/Popup/Flyout/InfoBar/Toast/ComboBox）且带 WA_TranslucentBackground
    的窗口隐藏消息卡 WebView，防原生 HWND 穿透半透明遮罩。
    叫 ××Overlay 会被直接跳过，表现为遮罩上浮着一片卡片 HTML 内容。
    """

    def __init__(
        self,
        path: str,
        colors: Optional[dict] = None,
        parent: Optional[QWidget] = None,
        scrollbar_qss: str = "",
    ):
        host = parent if parent is not None else QApplication.activeWindow()
        super().__init__(host)
        self._colors = dict(colors or {})
        self._scrollbar_qss = scrollbar_qss

        self.setObjectName("file-tree-global-preview-overlay")
        self.setWindowTitle("全局预览")
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 120))
        self.setClosableOnMaskClicked(True)
        # 刻意不开 setDraggable：拖拽手势要留给图片平移，抢走会变成拖窗口
        self.setMaskColor(QColor(0, 0, 0, 150))

        self.widget.setObjectName(_OVERLAY_OBJECT)
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._page = FilePreviewPage(self.widget)
        self._page.set_back_tip("关闭预览")
        self._page.set_zoom_label_visible(True)  # 面板够宽，显示百分比便于判断 1:1
        self._page.back_requested.connect(self._close)
        layout.addWidget(self._page)

        # 内容居中（与插件内 _styled_message_box 同法：交给布局对齐，
        # 不 removeWidget —— 脱离布局后位置会钉在左上角）
        self._hBoxLayout.addWidget(self.widget, 0, Qt.AlignCenter)

        self._apply_theme()
        self._fit_size()
        self._page.load(path)
        self._page.setFocus()

    # ── 公开接口 ──

    def page(self) -> FilePreviewPage:
        return self._page

    def current_path(self) -> str:
        return self._page.current_path()

    def show_overlay(self) -> None:
        """显示并抢焦点（非模态：用户可以随时点遮罩关闭）"""
        self._fit_size()
        self.show()
        self.raise_()
        self.activateWindow()
        self._page.setFocus()

    # ── 尺寸与主题 ──

    def _fit_size(self) -> None:
        """按父窗口 86% / 88% 定尺寸，屏幕可用区留边保护"""
        base = self.parentWidget()
        src_w = base.width() if base is not None else self.width()
        src_h = base.height() if base is not None else self.height()
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else self.geometry()
        width = max(560, int(src_w * 0.86))
        height = max(400, int(src_h * 0.88))
        width = min(width, max(360, avail.width() - 80))
        height = min(height, max(240, avail.height() - 80))
        self.widget.setFixedSize(width, height)

    def _apply_theme(self) -> None:
        colors = self._colors
        bg = colors.get("card_bg", QColor(33, 33, 38))
        border = colors.get("border", QColor(60, 60, 66))
        self.widget.setStyleSheet(
            f"#{_OVERLAY_OBJECT} {{ background: {bg.name()};"
            f"  border: 1px solid {border.name()}; border-radius: 10px; }}"
        )
        self._page.set_colors(
            colors,
            font_family=colors.get("font_family", ""),
            font_size=colors.get("font_size", 0),
            scrollbar_qss=self._scrollbar_qss,
        )

    # ── 事件 ──

    def _close(self) -> None:
        self.reject()

    def resizeEvent(self, event):  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        if self.isVisible():
            self._fit_size()
