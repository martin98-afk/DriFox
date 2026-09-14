# -*- coding: utf-8 -*-
"""密钥解锁 / 密码设置弹窗（密码加密模式）

- SecretUnlockDialog：本机没有记住的密码或解密失败时，主窗口显示后弹窗要密码。
- SecretPasswordSetupDialog：设置或修改加密密码（改密码需先输旧密码）。

两个弹窗只负责收集输入与展示错误，加解密与落盘一律在 Settings 侧完成。
"""

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    CheckBox,
    LineEdit,
    MaskDialogBase,
    PrimaryPushButton,
    PushButton,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

_ERROR_COLOR = "#f85149"


def _password_edit(parent, placeholder: str) -> LineEdit:
    """密码输入框（掩码显示 + 回车提交由调用方连接）"""
    edit = LineEdit(parent)
    edit.setPlaceholderText(placeholder)
    edit.setEchoMode(LineEdit.Password)
    edit.setFixedHeight(36)
    edit.setStyleSheet(f"""
        LineEdit {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 {Colors.INPUT_BG_START}, stop:1 {Colors.INPUT_BG_END});
            border: 1px solid {Colors.INPUT_BORDER};
            color: {Colors.INPUT_TEXT};
            padding: 4px 12px;
            border-radius: 6px;
            {get_font_family_css()} {font_size_css(13)}
        }}
        LineEdit:focus {{
            border-color: {Colors.INFO};
        }}
    """)
    return edit


def _title_label(parent, text: str) -> BodyLabel:
    label = BodyLabel(text, parent)
    label.setStyleSheet(
        f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(16)}"
    )
    return label


def _hint_label(parent, text: str) -> BodyLabel:
    label = BodyLabel(text, parent)
    label.setWordWrap(True)
    label.setStyleSheet(
        f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)}"
    )
    return label


def _error_label(parent) -> BodyLabel:
    label = BodyLabel("", parent)
    label.setWordWrap(True)
    label.setVisible(False)
    label.setStyleSheet(f"color: {_ERROR_COLOR}; background: transparent; {get_font_family_css()} {font_size_css(11)}")
    return label


def _button_row(parent, cancel_text: str, confirm_text: str) -> tuple[QHBoxLayout, PushButton, PrimaryPushButton]:
    layout = QHBoxLayout()
    layout.setSpacing(8)
    layout.addStretch()

    cancel_btn = PushButton(cancel_text, parent)
    cancel_btn.setStyleSheet(f"""
        PushButton {{
            background-color: {Colors.CARD_BG.format(alpha=180)};
            color: {Colors.TEXT_PRIMARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 6px;
            padding: 4px 20px;
            {font_size_css(12)}
        }}
        PushButton:hover {{
            background-color: {Colors.HOVER_BG};
            border-color: {Colors.BORDER_ACCENT};
        }}
    """)

    confirm_btn = PrimaryPushButton(confirm_text, parent)
    confirm_btn.setStyleSheet(f"""
        PrimaryPushButton {{
            background-color: {Colors.INFO};
            color: white;
            border: none;
            border-radius: 6px;
            padding: 4px 20px;
            {font_size_css(12)}
        }}
        PrimaryPushButton:hover {{
            background-color: {Colors.SEND_BTN_END};
        }}
    """)

    layout.addWidget(cancel_btn)
    layout.addWidget(confirm_btn)
    return layout, cancel_btn, confirm_btn


class _SecretDialogBase(MaskDialogBase):
    """MaskDialogBase 居中适配（与 SingleInputDialog 同范式）"""

    DEFAULT_WIDTH = 420
    DEFAULT_HEIGHT = 260

    def _finish_widget(self):
        # MaskDialogBase 的 QHBoxLayout 会把 widget 拉伸到全屏，必须先摘出来再居中
        self.layout().removeWidget(self.widget)
        self.widget.setParent(self)
        self.widget.setMinimumSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self.widget.setMaximumSize(self.DEFAULT_WIDTH + 200, self.DEFAULT_HEIGHT + 320)
        self.widget.adjustSize()
        self._center_widget()

    def _center_widget(self):
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center_widget()


class SecretUnlockDialog(_SecretDialogBase):
    """解锁弹窗：密码加密模式下读不到密码时要求输入。

    信号 unlocked(password, remember)：remember 仅在 can_remember=True 时可能为 True。
    信号 forgotPassword()：用户点「忘记密码」，由外部走重置流程。
    """

    unlocked = pyqtSignal(str, bool)
    forgotPassword = pyqtSignal()

    DEFAULT_HEIGHT = 280

    def __init__(self, can_remember: bool = True, parent=None):
        super().__init__(parent)
        self._can_remember = can_remember
        self._init_ui()

    def _init_ui(self):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), self._mask_color())
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(self._mask_color(76))
        self.widget.setObjectName("secretUnlockDialogWidget")
        self.widget.setStyleSheet(f"""
            #secretUnlockDialogWidget {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(_title_label(self.widget, "🔑 解锁 API Key"))
        layout.addWidget(_hint_label(self.widget, "当前配置用密码加密保存，请输入密码以解密服务商密钥。"))

        self._pwd = _password_edit(self.widget, "请输入加密密码")
        self._pwd.returnPressed.connect(self._on_accept)
        layout.addWidget(self._pwd)

        self._error = _error_label(self.widget)
        layout.addWidget(self._error)

        # 记住本机密码：无可用钥匙串时置灰（记不住就只能每次启动输）
        self._remember = CheckBox("记住本机密码（存系统钥匙串）", self.widget)
        self._remember.setChecked(self._can_remember)
        self._remember.setEnabled(self._can_remember)
        if not self._can_remember:
            self._remember.setToolTip("本机无可用系统钥匙串，无法记住密码")
        layout.addWidget(self._remember)

        forget_btn = PushButton("忘记密码？", self.widget)
        forget_btn.setStyleSheet(f"""
            PushButton {{
                background: transparent;
                color: {Colors.TEXT_MUTED};
                border: none;
                padding: 0;
                {font_size_css(11)}
            }}
            PushButton:hover {{
                color: {_ERROR_COLOR};
            }}
        """)
        forget_btn.clicked.connect(self.forgotPassword.emit)
        layout.addWidget(forget_btn)

        btn_layout, cancel_btn, confirm_btn = _button_row(self.widget, "取消", "解锁")
        cancel_btn.clicked.connect(self.close)
        confirm_btn.clicked.connect(self._on_accept)
        layout.addLayout(btn_layout)

        self._finish_widget()

    def _mask_color(self, alpha: int = 100):
        from PyQt5.QtGui import QColor

        return QColor(0, 0, 0, alpha)

    def set_error(self, message: str):
        self._error.setText(message)
        self._error.setVisible(bool(message))

    def _on_accept(self):
        pwd = self._pwd.text()
        if not pwd:
            self.set_error("请输入密码")
            return
        self.unlocked.emit(pwd, self._remember.isChecked())
        self.accept()

    def showEvent(self, e):
        super().showEvent(e)
        self._pwd.setFocus()


class SecretPasswordSetupDialog(_SecretDialogBase):
    """设置 / 修改加密密码。

    has_old=True 时要求先输旧密码（用于改密码、或存在未解密条目时的模式切换）。
    信号 confirmed(old_password, new_password)。
    """

    confirmed = pyqtSignal(str, str)

    DEFAULT_HEIGHT = 320

    def __init__(self, has_old: bool = False, parent=None):
        super().__init__(parent)
        self._has_old = has_old
        self._init_ui()

    def _init_ui(self):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), self._mask_color())
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(self._mask_color(76))
        self.widget.setObjectName("secretPasswordSetupDialogWidget")
        self.widget.setStyleSheet(f"""
            #secretPasswordSetupDialogWidget {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(_title_label(self.widget, "🔒 设置加密密码"))
        layout.addWidget(_hint_label(self.widget, "密钥将用该密码加密后随配置同步；密码无法找回，请务必记住。"))

        if self._has_old:
            self._old = _password_edit(self.widget, "旧密码")
            layout.addWidget(self._old)
        else:
            self._old = None

        self._new = _password_edit(self.widget, "新密码")
        layout.addWidget(self._new)
        self._confirm = _password_edit(self.widget, "确认新密码")
        self._confirm.returnPressed.connect(self._on_accept)
        layout.addWidget(self._confirm)

        self._error = _error_label(self.widget)
        layout.addWidget(self._error)

        btn_layout, cancel_btn, confirm_btn = _button_row(self.widget, "取消", "确定")
        cancel_btn.clicked.connect(self.close)
        confirm_btn.clicked.connect(self._on_accept)
        layout.addLayout(btn_layout)

        self._finish_widget()

    def _mask_color(self, alpha: int = 100):
        from PyQt5.QtGui import QColor

        return QColor(0, 0, 0, alpha)

    def set_error(self, message: str):
        self._error.setText(message)
        self._error.setVisible(bool(message))

    def _on_accept(self):
        old_pwd = self._old.text() if self._old is not None else ""
        new_pwd = self._new.text()
        if self._has_old and not old_pwd:
            self.set_error("请输入旧密码")
            return
        if len(new_pwd) < 6:
            self.set_error("新密码至少 6 位")
            return
        if new_pwd != self._confirm.text():
            self.set_error("两次输入的新密码不一致")
            return
        self.confirmed.emit(old_pwd, new_pwd)
        self.accept()

    def showEvent(self, e):
        super().showEvent(e)
        (self._old if self._old is not None else self._new).setFocus()
