# -*- coding: utf-8 -*-
"""API Key 加密方式设置卡片（系统钥匙串 / 密码加密 / 不加密）

挂载位置：设置 → 服务商页，Gitee 账号绑定卡片下方。
卡片只做交互与提示，加解密、落盘、模式迁移全部走 Settings.switch_secret_mode。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    InfoBar,
    InfoBarPosition,
    PushButton,
    RadioButton,
    SettingCard,
)

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css
from app.utils.secret_store import MODE_KEYRING, MODE_NONE, MODE_PASSWORD, SecretStore
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.secret_unlock_dialog import SecretPasswordSetupDialog


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


class SecretModeSettingCard(SettingCard):
    """API Key 加密方式选择卡"""

    _MODES = (
        (MODE_KEYRING, "系统钥匙串", "密钥存操作系统凭证库，本机免配置；换机器需重新填写"),
        (MODE_PASSWORD, "密码加密", "密钥加密后随配置同步，换机器输入同一密码即可解出"),
        (MODE_NONE, "不加密（明文）", "密钥明文保存在配置里，仅建议本机离线使用"),
    )

    def __init__(self, parent=None):
        super().__init__(
            get_icon("锁定"),
            "API Key 加密方式",
            "决定服务商密钥以什么形式保存在 app.config 中",
            parent,
        )
        self.cfg = Settings.get_instance()
        self._switching = False  # 抑制 radio 信号回环
        self._radios: dict[str, RadioButton] = {}
        self._setup_right()
        self._connect_signals()
        self._refresh()

    # ── UI ──

    def _setup_right(self):
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        for mode, text, desc in self._MODES:
            rb = RadioButton(text, container)
            rb.toggled.connect(lambda checked, m=mode: self._on_mode_toggled(m, checked))
            self._radios[mode] = rb
            layout.addWidget(rb)
            layout.addWidget(_hint(f"　{desc}", container))

        # ── 密码模式子区 ──
        self._pwd_status = _hint("", container)
        layout.addWidget(self._pwd_status)

        self._pwd_btn = _small_button("设置或修改密码", container)
        self._pwd_btn.clicked.connect(self._on_set_password)
        layout.addWidget(self._pwd_btn)

        self._forget_remembered_btn = _small_button("清除本机记住的密码", container)
        self._forget_remembered_btn.clicked.connect(self._on_forget_remembered)
        layout.addWidget(self._forget_remembered_btn)

        # 本机无钥匙串时的降级提示
        self._keyring_warn = _hint("", container)
        self._keyring_warn.setVisible(False)
        layout.addWidget(self._keyring_warn)

        self.hBoxLayout.addWidget(container, 1)

    def _connect_signals(self):
        try:
            self.cfg.secret_mode.valueChanged.disconnect(self._on_mode_changed_external)
        except TypeError:
            pass
        self.cfg.secret_mode.valueChanged.connect(self._on_mode_changed_external)

    def _on_mode_changed_external(self, _value=None):
        """外部改动（如忘记密码重置）→ 同步卡片状态"""
        self._refresh()

    def _refresh(self):
        self._switching = True
        try:
            current = str(self.cfg.secret_mode.value or MODE_KEYRING)
            for mode, rb in self._radios.items():
                rb.setChecked(mode == current)
        finally:
            self._switching = False

        is_password = current == MODE_PASSWORD
        self._pwd_status.setVisible(is_password)
        self._pwd_btn.setVisible(is_password)
        self._forget_remembered_btn.setVisible(is_password)

        if is_password:
            self._pwd_status.setText("状态：" + ("密码已设置" if self._has_password() else "尚未设置密码"))
            self._pwd_btn.setText("设置密码" if not self._has_password() else "修改密码")

        store_available = SecretStore().available
        warn = (
            "" if store_available else "⚠ 本机无可用系统钥匙串：钥匙串模式将退化为明文，密码模式下每次启动都需输入密码"
        )
        self._keyring_warn.setText(warn)
        self._keyring_warn.setVisible(not store_available)
        self._forget_remembered_btn.setEnabled(store_available)

    def _has_password(self) -> bool:
        """是否已有加密密码（内存持有 / 存在未解密密文）"""
        return bool(getattr(self.cfg, "_secret_password", "")) or bool(getattr(self.cfg, "_cipher_backup", {}))

    # ── 交互 ──

    def _on_mode_toggled(self, mode: str, checked: bool):
        if self._switching or not checked:
            return
        current = str(self.cfg.secret_mode.value or MODE_KEYRING)
        if mode == current:
            return

        if mode == MODE_PASSWORD:
            self._switching = True
            try:
                dialog = SecretPasswordSetupDialog(has_old=self._has_password(), parent=self.window())
                dialog.confirmed.connect(lambda old_pwd, new_pwd: self._do_switch(mode, new_pwd, old_pwd))
                dialog.exec_()
            finally:
                self._switching = False
            self._refresh()  # 用户取消 → 回滚 radio
            return

        self._do_switch(mode)

    def _do_switch(self, mode: str, new_password: str = "", old_password: str = ""):
        ok, message = self.cfg.switch_secret_mode(mode, new_password, old_password)
        if not ok:
            self._notify(False, "切换失败", message or "无法切换加密方式")
            return
        hint = ""
        if mode == MODE_PASSWORD and SecretStore().available:
            hint = "密码尚未记住到本机，下次启动仍需输入"
        self._notify(True, "已切换加密方式", self._mode_text(mode) + ("；" + hint if hint else ""))
        self._refresh()

    def _on_set_password(self):
        dialog = SecretPasswordSetupDialog(has_old=self._has_password(), parent=self.window())
        dialog.confirmed.connect(self._apply_password)
        dialog.exec_()

    def _apply_password(self, old_password: str, new_password: str):
        ok = self.cfg.set_secret_password(new_password, old_password)
        if not ok:
            self._notify(False, "密码设置失败", "旧密码不正确，或当前存在未解密的密钥")
            return
        if SecretStore().available:
            self.cfg.remember_secret_password(new_password)
        self._notify(True, "密码已更新", "密钥已用新密码重新加密；请牢记，密码无法找回")
        self._refresh()

    def _on_forget_remembered(self):
        self.cfg.forget_secret_password()
        self._notify(True, "已清除", "本机记住的密码已删除，下次启动需要输入密码")

    def _mode_text(self, mode: str) -> str:
        return next((text for m, text, _ in self._MODES if m == mode), mode)

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
