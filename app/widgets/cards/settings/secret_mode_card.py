# -*- coding: utf-8 -*-
"""API Key 加密方式设置卡片（加密开关 + 列表行二选一）

挂载位置：设置 → 服务商页，Gitee 账号绑定卡片下方。
- header 右侧开关：关闭 = 明文落盘（none 模式）；
- 展开区：列表行二选一（系统钥匙串 / 密码加密），点整行选中，选中高亮。
卡片只做交互与提示，加解密、落盘、模式迁移全部走 Settings.switch_secret_mode。
"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget
from qfluentwidgets import (
    BodyLabel,
    ExpandSettingCard,
    InfoBar,
    InfoBarPosition,
    PushButton,
    SwitchButton,
)

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css
from app.utils.secret_store import MODE_KEYRING, MODE_NONE, MODE_PASSWORD, SecretStore
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.cards.settings.expand_height_mixin import DynamicHeightExpandCardMixin
from app.widgets.common_dialogs import ConfirmDialog
from app.widgets.secret_unlock_dialog import SecretPasswordSetupDialog, SecretUnlockDialog


def _hint(text: str, parent) -> BodyLabel:
    label = BodyLabel(text, parent)
    label.setWordWrap(True)
    label.setStyleSheet(
        f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)}"
    )
    return label


def _small_button(text: str, parent) -> PushButton:
    btn = PushButton(text, parent)
    btn.setFixedHeight(28)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(f"""
        PushButton {{
            background-color: {Colors.CARD_BG.format(alpha=180)};
            color: {Colors.TEXT_PRIMARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 6px;
            padding: 2px 12px;
            {font_size_css(11)}
        }}
        PushButton:hover {{
            background-color: {Colors.HOVER_BG};
            border-color: {Colors.BORDER_ACCENT};
        }}
    """)
    return btn


class _ModeRow(QFrame):
    """加密方式可选行：整行点击选中，选中态高亮边框"""

    selected = pyqtSignal()

    def __init__(self, title: str, desc: str, parent=None):
        super().__init__(parent)
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)

        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(8)
        self._title = QLabel(title, self)
        self._title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent;"
            f"{get_font_family_css()} {font_size_css(13)}; font-weight: 600;"
        )
        h.addWidget(self._title)
        h.addWidget(_hint(desc, self), 1)
        self._apply_style()

    def set_selected(self, selected: bool):
        if self._selected == selected:
            return
        self._selected = selected
        self._apply_style()

    def _apply_style(self):
        border = Colors.BORDER_ACCENT if self._selected else Colors.BORDER
        bg = Colors.HOVER_BG if self._selected else "transparent"
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 6px;
            }}
            QFrame:hover {{
                border-color: {Colors.BORDER_ACCENT};
            }}
            QLabel {{
                border: none;
                background: transparent;
            }}
        """)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.selected.emit()
        super().mouseReleaseEvent(e)


class SecretModeSettingCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
    """API Key 加密方式选择卡（header 开关 + 展开区列表行二选一）"""

    def __init__(self, parent=None):
        super().__init__(
            get_icon("锁定"),
            "API Key 加密方式",
            "决定服务商密钥以什么形式保存在 app.config 中",
            parent,
        )
        self.cfg = Settings.get_instance()
        self._switching = False  # 抑制控件信号回环
        self._build_header_switch()
        self._build_content()
        self._connect_signals()
        self._refresh()

    # ── UI ──

    def _build_header_switch(self):
        """header 右侧：加密总开关（开关自己消费点击，不会触发展开/折叠）"""
        self._encrypt_switch = SwitchButton("", self)
        self._encrypt_switch.checkedChanged.connect(self._on_switch_changed)
        self.addWidget(self._encrypt_switch)

    def _build_content(self):
        self.viewLayout.setContentsMargins(48, 4, 24, 12)
        self.viewLayout.setSpacing(8)

        self._rows: dict[str, _ModeRow] = {}
        for mode, title, desc in (
            (MODE_KEYRING, "系统钥匙串", "存系统凭证库，换机需重填"),
            (MODE_PASSWORD, "密码加密", "随配置同步，换机输密码解出"),
        ):
            row = _ModeRow(title, desc, self.view)
            row.selected.connect(lambda m=mode: self._on_row_selected(m))
            self._rows[mode] = row
            self.viewLayout.addWidget(row)

        # 密码子区（仅密码方式显示）
        self._pwd_row = QWidget(self.view)
        h3 = QHBoxLayout(self._pwd_row)
        h3.setContentsMargins(0, 0, 0, 0)
        h3.setSpacing(8)
        self._pwd_status = _hint("", self._pwd_row)
        self._pwd_btn = _small_button("设置密码", self._pwd_row)
        self._pwd_btn.clicked.connect(self._on_set_password)
        self._forget_btn = _small_button("清除记住的密码", self._pwd_row)
        self._forget_btn.clicked.connect(self._on_forget_remembered)
        h3.addWidget(self._pwd_status, 1)
        h3.addWidget(self._pwd_btn)
        h3.addWidget(self._forget_btn)
        self.viewLayout.addWidget(self._pwd_row)

    def _connect_signals(self):
        try:
            self.cfg.secret_mode.valueChanged.disconnect(self._on_mode_changed_external)
        except TypeError:
            pass
        self.cfg.secret_mode.valueChanged.connect(self._on_mode_changed_external)

    def _on_mode_changed_external(self, _value=None):
        """外部改动（如解锁弹窗内的忘记密码重置）→ 同步卡片状态"""
        self._refresh()

    def _refresh(self):
        self._switching = True
        try:
            current = self._current_mode()
            encrypted = current != MODE_NONE
            self._encrypt_switch.setChecked(encrypted)
            for mode, row in self._rows.items():
                row.set_selected(encrypted and mode == current)
        finally:
            self._switching = False

        # 密码子区（仅密码方式显示）
        is_password = current == MODE_PASSWORD
        # 锁定判据必须走 secrets_locked（由 _apply_secret_mode 按解密结果设定）：
        # _cipher_backup 语义是「未解密密文的回写备份」，已解锁时为便于落盘仍可能
        # 留存，据此判断会把正常解锁态误报成「等待解锁」。
        locked = bool(getattr(self.cfg, "secrets_locked", False))
        self._pwd_row.setVisible(is_password)
        self._pwd_status.setText(
            "状态：" + ("等待解锁（密钥已同步但未解锁）" if locked else "密码已设置") if is_password else ""
        )
        self._pwd_btn.setText("输入密码解锁" if locked else ("修改密码" if self._has_password() else "设置密码"))
        self._forget_btn.setEnabled(SecretStore().available)

        if self.isExpand:
            # 展开态下内容变化会改变高度，mixin 负责重算
            self._adjust_view_size()

    def _has_password(self) -> bool:
        """是否已有加密密码（内存持有 / 存在未解密密文）"""
        return bool(getattr(self.cfg, "_secret_password", "")) or bool(getattr(self.cfg, "_cipher_backup", {}))

    # ── 交互 ──

    def _current_mode(self) -> str:
        return str(self.cfg.secret_mode.value or MODE_KEYRING)

    def _on_switch_changed(self, checked: bool):
        if self._switching:
            return
        current = self._current_mode()
        if checked and current == MODE_NONE:
            self._switch_to(MODE_KEYRING)
        elif not checked and current != MODE_NONE:
            self._switch_to(MODE_NONE)

    def _on_row_selected(self, mode: str):
        if self._switching:
            return
        if mode != self._current_mode():
            self._switch_to(mode)

    def _switch_to(self, target: str):
        """切换到目标模式；需要密码时按需弹窗，取消/失败回滚并刷新"""
        current = self._current_mode()
        if target == current:
            return
        # 存在未解密密文（换机未解锁）时切出必须先解锁
        if not self._ensure_unlocked():
            self._refresh()
            return
        new_password = ""
        if target == MODE_PASSWORD:
            _old, new_password = self._prompt_setup_password(require_old=False)
            if not new_password:
                self._refresh()
                return
        ok, message = self.cfg.switch_secret_mode(target, new_password)
        if not ok:
            self._notify(False, "切换失败", message or "无法切换加密方式")
        self._refresh()

    def _ensure_unlocked(self) -> bool:
        """存在未解密密文时弹解锁窗；返回密钥是否已就绪"""
        if not bool(getattr(self.cfg, "secrets_locked", False)):
            return True
        result = {"ok": False}

        def _on_unlocked(password: str, remember: bool):
            result["ok"] = self.cfg.unlock_secrets(password)
            if result["ok"] and remember:
                self.cfg.remember_secret_password(password)
            else:
                dialog.set_error("密码不正确，请重试")

        dialog = SecretUnlockDialog(can_remember=SecretStore().available, parent=self.window())
        dialog.unlocked.connect(_on_unlocked)
        dialog.forgotPassword.connect(self._on_forgot_password)
        dialog.exec_()
        return result["ok"]

    def _prompt_setup_password(self, require_old: bool) -> tuple[str, str]:
        """弹「设置密码」弹窗，返回 (旧密码, 新密码)；取消时新密码为空串"""
        holder = {"old": "", "new": ""}

        def _on_confirmed(old_password: str, new_password: str):
            holder["old"] = old_password
            holder["new"] = new_password

        dialog = SecretPasswordSetupDialog(has_old=require_old, parent=self.window())
        dialog.confirmed.connect(_on_confirmed)
        dialog.exec_()
        return holder["old"], holder["new"]

    def _on_set_password(self):
        # 换机未解锁：按钮即「解锁」入口，成功后回到已解锁态
        if bool(getattr(self.cfg, "secrets_locked", False)):
            if not self._ensure_unlocked():
                return
            self._refresh()
            return
        old_password, new_password = self._prompt_setup_password(require_old=self._has_password())
        if not new_password:
            return
        if self._has_password() and old_password != getattr(self.cfg, "_secret_password", ""):
            # 已解锁状态改密码：直接比对内存中的当前密码
            self._notify(False, "密码设置失败", "旧密码不正确")
            return
        if not self.cfg.set_secret_password(new_password):
            self._notify(False, "密码设置失败", "无法完成重新加密")
            return
        if SecretStore().available:
            self.cfg.remember_secret_password(new_password)
        self._notify(True, "密码已更新", "密钥已用新密码重新加密；请牢记，密码无法找回")
        self._refresh()

    def _on_forget_remembered(self):
        self.cfg.forget_secret_password()
        self._notify(True, "已清除", "本机记住的密码已删除，下次启动需要输入密码")

    def _on_forgot_password(self):
        """忘记密码：二次确认后清空所有已保存密钥并转明文模式（不可恢复）"""
        dialog = ConfirmDialog(
            title="忘记密码",
            content="密码无法找回。继续将清空所有已保存的 API Key，并把加密方式切换为不加密。\n确定继续吗？",
            confirm_text="清空并继续",
            parent=self.window(),
        )
        dialog.confirmed.connect(self._do_reset_locked_secrets)
        dialog.exec_()

    def _do_reset_locked_secrets(self):
        self.cfg.reset_locked_secrets()
        self._notify(True, "已重置", "已清空所有已保存 API Key 并切换为明文模式")
        self._refresh()

    def _notify(self, success: bool, title: str, content: str):
        try:
            parent = self.window()
            if success:
                InfoBar.success(
                    title=title,
                    content=content,
                    orient=Qt.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=4000,
                    parent=parent,
                )
            else:
                InfoBar.error(
                    title=title,
                    content=content,
                    orient=Qt.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=6000,
                    parent=parent,
                )
        except Exception:
            # 提示失败不影响主流程
            pass
