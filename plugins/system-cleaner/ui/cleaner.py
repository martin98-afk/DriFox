# -*- coding: utf-8 -*-
"""UI 组件 — 缓存行 + 风格化弹窗

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
"""

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, MaskDialogBase, TransparentPushButton, isDarkTheme

from .scanner import _format_size


# ── 单个缓存行 ──────────────────────────────────────────


class _CacheItemRow(QWidget):
    """单个缓存项行：勾选框 + 图标 + 名称 + 大小"""

    toggled = pyqtSignal()

    def __init__(self, cache_id: str, icon: str, label: str, parent=None):
        super().__init__(parent)
        self._cache_id = cache_id
        self._icon = icon
        self._label = label
        self._size_bytes = 0
        self._checked = True
        self._font_family = ""
        self._font_size = 0
        self._accent_color = "#62a0ea"
        self._is_dark = isDarkTheme()
        self._setup_ui()
        self.setFixedHeight(42)

    def set_font_ctx(self, font_family: str, font_size: int):
        self._font_family = font_family
        self._font_size = font_size
        self._update_styles()

    def set_accent_color(self, color: str):
        self._accent_color = color
        self._update_checkbox_style()

    def set_dark_mode(self, is_dark: bool):
        self._is_dark = is_dark
        self._update_styles()

    def _setup_ui(self):
        self.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(10)

        self._checkbox = QCheckBox(self)
        self._checkbox.setChecked(True)
        self._checkbox.setFixedSize(18, 18)
        self._checkbox.stateChanged.connect(self._on_toggled)
        self._update_checkbox_style()
        layout.addWidget(self._checkbox)

        self._icon_label = QLabel(self._icon, self)
        self._icon_label.setFixedWidth(20)
        self._icon_label.setStyleSheet("background: transparent; font-size: 15px;")
        layout.addWidget(self._icon_label)

        _tc_init = "rgba(255,255,255,0.88)" if self._is_dark else "rgba(0,0,0,0.88)"
        self._name_label = QLabel(self._label, self)
        self._name_label.setStyleSheet(f"color: {_tc_init}; background: transparent; font-size: 13px;")
        layout.addWidget(self._name_label)

        layout.addStretch(1)

        _tcs_init = "rgba(255,255,255,0.45)" if self._is_dark else "rgba(0,0,0,0.45)"
        self._size_label = QLabel("扫描中…", self)
        self._size_label.setStyleSheet(
            f"color: {_tcs_init}; background: transparent; font-size: 13px; font-weight: 500;"
        )
        layout.addWidget(self._size_label)

        self.setCursor(Qt.PointingHandCursor)

    def _update_styles(self):
        ff = self._font_family
        fs = self._font_size
        checked = self._checkbox.isChecked()
        opacity = 1.0 if checked else 0.35

        if not hasattr(self, "_is_dark"):
            self._is_dark = isDarkTheme()

        if self._is_dark:
            tc = f"rgba(255,255,255,{0.88 * opacity})"
            tcs = f"rgba(255,255,255,{0.45 * opacity})"
        else:
            tc = f"rgba(0,0,0,{0.88 * opacity})"
            tcs = f"rgba(0,0,0,{0.45 * opacity})"

        font_qss = f"font-family: '{ff}';" if ff else ""
        self._icon_label.setStyleSheet(f"background: transparent; font-size: {max(12, fs + 1)}px;")
        self._name_label.setStyleSheet(
            f"color: {tc}; background: transparent; font-size: {max(13, fs - 1)}px; {font_qss}"
        )
        self._size_label.setStyleSheet(
            f"color: {tcs}; background: transparent; font-size: {max(13, fs - 1)}px; font-weight: 500; {font_qss}"
        )

    def _update_checkbox_style(self):
        c = self._accent_color
        self._checkbox.setStyleSheet(f"""
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 3px;
                border: 1px solid rgba(128,128,128,0.25);
                background: transparent;
            }}
            QCheckBox::indicator:checked {{
                background-color: {c};
                border: 1px solid {c};
            }}
            QCheckBox::indicator:hover {{
                border: 1px solid rgba(128,128,128,0.45);
            }}
        """)

    def mousePressEvent(self, event):
        self._checkbox.setChecked(not self._checkbox.isChecked())
        super().mousePressEvent(event)

    def _on_toggled(self):
        self._update_styles()
        self.toggled.emit()

    def cache_id(self) -> str:
        return self._cache_id

    def is_checked(self) -> bool:
        return self._checkbox.isChecked()

    def set_checked(self, checked: bool):
        self._checkbox.setChecked(checked)

    def set_size(self, size_bytes: int):
        self._size_bytes = size_bytes
        if size_bytes < 0:
            self._size_label.setText("N/A")
            self._checkbox.setEnabled(False)
        elif size_bytes == 0:
            self._size_label.setText("0 B")
            self._checkbox.setChecked(False)
            self._checkbox.setEnabled(False)
        else:
            self._size_label.setText(_format_size(size_bytes))
            self._checkbox.setEnabled(True)
        self._update_styles()

    def get_size(self) -> int:
        return self._size_bytes if self.is_checked() else 0

    def get_contribution(self) -> int:
        return self._size_bytes if (self.is_checked() and self._size_bytes > 0) else 0


# ── 统一 MaskDialogBase 风格弹窗 ──────────────────────


class _StyledCleanerDialog(MaskDialogBase):
    """统一 MaskDialogBase 风格的确认弹窗"""

    def __init__(
        self,
        parent,
        title: str,
        text: str,
        *,
        tc: str,
        ff: str,
        fs: int,
        accent_bg: str,
        card_bg: str,
        border_c: str,
        hover_bg: str,
        yes_text: str = "是",
        no_text: str = "否",
        default_yes: bool = False,
    ):
        super().__init__(parent)
        self._result = False
        self._init_ui(title, text, tc, ff, fs, accent_bg, card_bg, border_c, hover_bg, yes_text, no_text, default_yes)

    def _init_ui(self, title, text, tc, ff, fs, accent_bg, card_bg, border_c, hover_bg, yes_text, no_text, default_yes):
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("cleanerStyledDialog")
        self.widget.setStyleSheet(f"""
            #cleanerStyledDialog {{
                background-color: {card_bg};
                border: 1px solid {border_c};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(28, 28, 28, 20)
        layout.setSpacing(0)

        title_lb = BodyLabel(title, self.widget)
        title_lb.setWordWrap(True)
        title_lb.setStyleSheet(
            f"color: {tc}; background: transparent; "
            f"{f'font-family: "{ff}";' if ff else ''}"
            f"font-size: {max(8, fs + 2)}px; font-weight: bold;"
        )
        layout.addWidget(title_lb)

        layout.addSpacing(12)

        content_lb = BodyLabel(text, self.widget)
        content_lb.setWordWrap(True)
        content_lb.setStyleSheet(
            f"color: {tc}; background: transparent; "
            f"{f'font-family: "{ff}";' if ff else ''}"
            f"font-size: {max(8, fs - 1)}px; line-height: 1.6;"
        )
        layout.addWidget(content_lb)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        cancel_btn = TransparentPushButton(no_text, self.widget)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setFixedHeight(36)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {card_bg};
                color: {tc};
                border: 1px solid {border_c};
                border-radius: 8px;
                padding: 4px 28px;
                {f'font-family: "{ff}";' if ff else ""}
                font-size: {max(8, fs - 1)}px;
            }}
            QPushButton:hover {{
                background-color: {hover_bg};
                border-color: {accent_bg};
            }}
        """)
        cancel_btn.clicked.connect(self._on_cancel)

        confirm_btn = TransparentPushButton(yes_text, self.widget)
        confirm_btn.setCursor(Qt.PointingHandCursor)
        confirm_btn.setFixedHeight(36)
        confirm_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {accent_bg};
                color: #ffffff;
                border: none;
                border-radius: 8px;
                padding: 4px 28px;
                {f'font-family: "{ff}";' if ff else ""}
                font-size: {max(8, fs - 1)}px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {accent_bg};
            }}
        """)
        confirm_btn.clicked.connect(self._on_confirm)

        if default_yes:
            confirm_btn.setDefault(True)
            confirm_btn.setFocus()
        else:
            cancel_btn.setDefault(True)
            cancel_btn.setFocus()

        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        layout.addLayout(btn_layout)

        # 内容自适应：设最小尺寸保底（420×220）、最大尺寸防撑爆（620×720）、
        # 水平 Fixed 阻止 MaskDialogBase 的 QHBoxLayout 把 widget 拉伸到全屏，
        # 垂直 Preferred 让 layout 按文本行数自适应（原 setFixedSize 长文本被遮挡）
        self.widget.setMinimumSize(420, 220)
        self.widget.setMaximumSize(620, 720)
        sp = self.widget.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Fixed)
        sp.setVerticalPolicy(QSizePolicy.Preferred)
        self.widget.setSizePolicy(sp)
        self.widget.adjustSize()

    def _on_confirm(self):
        self._result = True
        self.close()

    def _on_cancel(self):
        self._result = False
        self.close()


class _StyledCleanerInfoDialog(MaskDialogBase):
    """统一 MaskDialogBase 风格的信息提示弹窗 — 单按钮「知道了」"""

    def __init__(self, parent, title: str, text: str, *, tc, ff, fs, accent_bg, card_bg, border_c):
        super().__init__(parent)
        self._init_ui(title, text, tc, ff, fs, accent_bg, card_bg, border_c)

    def _init_ui(self, title, text, tc, ff, fs, accent_bg, card_bg, border_c):
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("cleanerStyledInfo")
        self.widget.setStyleSheet(f"""
            #cleanerStyledInfo {{
                background-color: {card_bg};
                border: 1px solid {border_c};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(28, 28, 28, 20)
        layout.setSpacing(0)

        title_lb = BodyLabel(title, self.widget)
        title_lb.setWordWrap(True)
        title_lb.setStyleSheet(
            f"color: {tc}; background: transparent; "
            f"{f'font-family: "{ff}";' if ff else ''}"
            f"font-size: {max(8, fs + 2)}px; font-weight: bold;"
        )
        layout.addWidget(title_lb)

        layout.addSpacing(12)

        content_lb = BodyLabel(text, self.widget)
        content_lb.setWordWrap(True)
        content_lb.setStyleSheet(
            f"color: {tc}; background: transparent; "
            f"{f'font-family: "{ff}";' if ff else ''}"
            f"font-size: {max(8, fs - 1)}px; line-height: 1.6;"
        )
        layout.addWidget(content_lb)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        ok_btn = TransparentPushButton("知道了", self.widget)
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setFixedHeight(36)
        ok_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {accent_bg};
                color: #ffffff;
                border: none;
                border-radius: 8px;
                padding: 4px 28px;
                {f'font-family: "{ff}";' if ff else ""}
                font-size: {max(8, fs - 1)}px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {accent_bg};
            }}
        """)
        ok_btn.setDefault(True)
        ok_btn.setFocus()
        ok_btn.clicked.connect(self.close)

        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        # 内容自适应：最小 400×180，最大 600×720；水平 Fixed 防拉伸，垂直 Preferred 跟内容
        self.widget.setMinimumSize(400, 180)
        self.widget.setMaximumSize(600, 720)
        sp = self.widget.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Fixed)
        sp.setVerticalPolicy(QSizePolicy.Preferred)
        self.widget.setSizePolicy(sp)
        self.widget.adjustSize()


# ── 弹窗工厂函数 ──────────────────────────────────────


def _styled_cleaner_confirm(
    parent: QWidget,
    title: str,
    text: str,
    *,
    color_source: Optional[QWidget] = None,
    yes_text: str = "是",
    no_text: str = "否",
    default_yes: bool = False,
) -> bool:
    """系统清理插件确认弹窗 — 从父链查找卡片缓存的颜色"""
    tc = "rgba(255,255,255,0.9)"
    ff = ""
    fs = 14
    theme_colors: dict = {}

    p = color_source or parent
    while p is not None:
        cached = getattr(p, "_cached_tc", None)
        if cached is not None:
            tc = cached
            ff = getattr(p, "_cached_font_family", "")
            fs = getattr(p, "_cached_font_size", 14)
            theme_colors = getattr(p, "_cached_theme_colors", {})
            break
        p = p.parent()

    accent_bg = theme_colors.get("accent", "") or ("#62a0ea" if isDarkTheme() else "#2878dc")
    card_bg = theme_colors.get("content_bg", "#2a2a2e" if isDarkTheme() else "#ffffff")
    border_c = theme_colors.get("border", "rgba(128,128,128,0.15)")
    hover_bg = theme_colors.get("hover_bg", "rgba(255,255,255,0.08)" if isDarkTheme() else "rgba(0,0,0,0.06)")

    dialog = _StyledCleanerDialog(
        parent,
        title,
        text,
        tc=tc,
        ff=ff,
        fs=fs,
        accent_bg=accent_bg,
        card_bg=card_bg,
        border_c=border_c,
        hover_bg=hover_bg,
        yes_text=yes_text,
        no_text=no_text,
        default_yes=default_yes,
    )
    dialog.exec_()
    return dialog._result


def _styled_cleaner_info(parent: QWidget, title: str, text: str, *, color_source: Optional[QWidget] = None):
    """系统清理插件信息弹窗 — 从父链查找卡片缓存的颜色"""
    tc = "rgba(255,255,255,0.9)"
    ff = ""
    fs = 14
    theme_colors: dict = {}

    p = color_source or parent
    while p is not None:
        cached = getattr(p, "_cached_tc", None)
        if cached is not None:
            tc = cached
            ff = getattr(p, "_cached_font_family", "")
            fs = getattr(p, "_cached_font_size", 14)
            theme_colors = getattr(p, "_cached_theme_colors", {})
            break
        p = p.parent()

    accent_bg = theme_colors.get("accent", "") or ("#62a0ea" if isDarkTheme() else "#2878dc")
    card_bg = theme_colors.get("content_bg", "#2a2a2e" if isDarkTheme() else "#ffffff")
    border_c = theme_colors.get("border", "rgba(128,128,128,0.15)")

    dialog = _StyledCleanerInfoDialog(
        parent,
        title,
        text,
        tc=tc,
        ff=ff,
        fs=fs,
        accent_bg=accent_bg,
        card_bg=card_bg,
        border_c=border_c,
    )
    dialog.exec_()
