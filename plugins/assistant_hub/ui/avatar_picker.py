# -*- coding: utf-8 -*-
"""avatar_picker.py — 用户头像选择弹窗（预置库网格 + 默认项 + 本地上传）

- 预置库：plugins/assistant_hub/icons/avatars/avatar-*.png（点选后复制落盘，
  不引用插件源路径，避免插件目录变动导致头像失效）
- 首格「默认」= 清除自定义头像，回落渲染层色块 + 首字母
- 本地上传：png/jpg/jpeg/webp/svg，读 bytes 走同一保存链
- 风格对齐 assistant_card._confirm_dialog 的 MaskDialogBase 用法
  （parent 传 _host_window()，勿对手动 setGeometry）
"""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import MaskDialogBase

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

from .assistant_avatar import RoundAvatar

_PRESET_DIR = Path(__file__).resolve().parent.parent / "icons" / "avatars"
_IMAGE_FILTER = "图片 (*.png *.jpg *.jpeg *.webp *.svg)"
_TILE = 56  # 头像格内容尺寸


class _DefaultTile(QWidget):
    """「默认」格：虚线圆 + 居中文字（与弧形堆叠「新建」卡虚线圆风格呼应）。"""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(_TILE + 8, _TILE + 8)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("恢复默认头像（色块 + 首字母）")
        self._hover = False

    def enterEvent(self, e):  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, e):  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, e):  # noqa: N802
        self.clicked.emit()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(4, 4, _TILE, _TILE)
        pen = QPen(QColor(Colors.TEXT_ACCENT if self._hover else Colors.BORDER), 1.5, Qt.DashLine)
        pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rect)
        p.setPen(QColor(Colors.TEXT_MUTED))
        font = self.font()
        font.setPixelSize(11)
        p.setFont(font)
        p.drawText(rect, Qt.AlignCenter, "默认")
        p.end()


class _AvatarTile(QWidget):
    """预置头像格：RoundAvatar + hover accent 环。"""

    clicked = pyqtSignal()

    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.path = image_path
        self.setFixedSize(_TILE + 8, _TILE + 8)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = False
        self._avatar = RoundAvatar(size=_TILE, text="", color="#7C3AED", image_path=image_path, parent=self)
        self._avatar.move(4, 4)
        self._avatar.show()

    def enterEvent(self, e):  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, e):  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, e):  # noqa: N802
        self.clicked.emit()

    def paintEvent(self, _e) -> None:  # noqa: N802
        if not self._hover:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(Colors.TEXT_ACCENT), 2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(3, 3, _TILE + 2, _TILE + 2))
        p.end()


class UserAvatarDialog(MaskDialogBase):
    """用户头像选择：预置网格点选即生效；首格「默认」恢复默认；底部本地上传。

    信号：
        picked(bytes, str) — 选中图片（数据, 扩展名），由调用方落盘
        cleared()          — 选择「默认」，由调用方清除已有头像
    """

    picked = pyqtSignal(bytes, str)
    cleared = pyqtSignal()

    _COLS = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))
        self.widget.setFixedSize(420, 400)
        self.widget.setObjectName("hubAvatarPicker")
        self.widget.setStyleSheet(
            f"#hubAvatarPicker {{ background: {Colors.CARD_BG_SOLID}; border: 1px solid {Colors.BORDER};"
            f"border-radius: 12px; }}"
        )

        v = QVBoxLayout(self.widget)
        v.setContentsMargins(24, 20, 24, 18)
        v.setSpacing(10)

        title = QLabel("选择用户头像")
        title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;"
            f"{get_font_family_css()} {font_size_css(14)}; font-weight: 600;"
        )
        v.addWidget(title)

        # 预置网格（滚动区：预置增多时不撑爆弹窗；首格固定「默认」）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        tiles: list[QWidget] = [_DefaultTile()]
        tiles.extend(_AvatarTile(str(p)) for p in sorted(_PRESET_DIR.glob("avatar-*.png")))
        for i, tile in enumerate(tiles):
            row, col = divmod(i, self._COLS)
            grid.addWidget(tile, row, col, Qt.AlignLeft | Qt.AlignTop)
            if isinstance(tile, _DefaultTile):
                tile.clicked.connect(self._emit_cleared)
            else:
                tile.clicked.connect(lambda path=tile.path: self._emit_picked(Path(path)))
        grid.setColumnStretch(self._COLS, 1)  # 尾列弹性 → 每行靠左，不横向拉伸
        scroll.setWidget(holder)
        v.addWidget(scroll, 1)

        # 底部按钮行：上传 + 取消
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        upload_btn = QPushButton("上传本地图片…")
        upload_btn.setFixedHeight(32)
        upload_btn.setCursor(Qt.PointingHandCursor)
        upload_btn.setStyleSheet(self._btn_css())
        upload_btn.clicked.connect(self._on_upload)
        btn_row.addWidget(upload_btn)
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedHeight(32)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(self._btn_css())
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        v.addLayout(btn_row)

    @staticmethod
    def _btn_css() -> str:
        return f"""
            QPushButton {{
                color: {Colors.TEXT_PRIMARY}; background: {Colors.CARD_BG.format(alpha=90)};
                border: 1px solid {Colors.BORDER}; border-radius: 6px; padding: 0 16px;
                {get_font_family_css()} {font_size_css(12)};
            }}
            QPushButton:hover {{ border-color: {Colors.TEXT_ACCENT}; background: {Colors.HOVER_BG}; }}
        """

    def _emit_picked(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            return
        self.picked.emit(data, path.suffix.lstrip("."))
        self.accept()

    def _emit_cleared(self) -> None:
        self.cleared.emit()
        self.accept()

    def _on_upload(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self.widget, "选择图片", "", _IMAGE_FILTER)
        if not path:
            return
        self._emit_picked(Path(path))
