# -*- coding: utf-8 -*-
"""API Key 加密方式设置卡片（加密开关 + 钥匙串/密码两种方式）

挂载位置：设置 → 服务商页，Gitee 账号绑定卡片下方。
展开式卡片（ExpandSettingCard）：
- 开关「加密 API Key」：关闭 = 明文落盘（none 模式）；
- 下拉二选一：系统钥匙串（keyring）/ 密码加密（password）。
卡片只做交互与提示，加解密、落盘、模式迁移全部走 Settings.switch_secret_mode。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
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
from app.widgets.common_dialogs import ConfirmDialog
from app.widgets.secret_unlock_dialog import SecretPasswordSetupDialog, SecretUnlockDialog

_COMBO_INDEX_MODES = (MODE_KEYRING, MODE_PASSWORD)  # 下拉 index → mode


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


class SecretModeSettingCard(ExpandSettingCard):
    """API Key 加密方式选择卡（展开式：开关 + 二选一）"""

    def __init__(self, parent=None):
        super().__init__(
            get_icon("锁定"),
            "API Key 加密方式",
            "决定服务商密钥以什么形式保存在 app.config 中",
            parent,
        )
        self.cfg = Settings.get_instance()
        self._switching = False  # 抑制控件信号回环
        self._build_header_value()
        self._build_content()
        self._connect_signals()
        self._refresh()

    # ── UI ──

    def _build_header_value(self):
        """header 右侧实时显示当前状态"""
        self._header_label = QLabel(self)
        self._header_label.setObjectName("titleLabel")
        self.addWidget(self._header_label)

    def _build_content(self):
        self.viewLayout.setContentsMargins(48, 4, 24, 12)
        self.viewLayout.setSpacing(10)

        # ── 行 1：加密总开关 ──
        switch_row = QWidget(self.view)
        h1 = QHBoxLayout(switch_row)
        h1.setContentsMargins(0, 0, 0, 0)
        h1.setSpacing(12)
        self._encrypt_switch = SwitchButton("加密 API Key", switch_row)
        self._encrypt_switch.checkedChanged.connect(self._on_switch_changed)
        h1.addWidget(self._encrypt_switch)
        h1.addWidget(_hint("关闭后密钥明文保存在 app.config，仅建议本机离线使用", switch_row), 1)
        self.viewLayout.addWidget(switch_row)

        # ── 行 2：加密方式下拉 ──
        combo_row = QWidget(self.view)
        h2 = QHBoxLayout(combo_row)
        h2.setContentsMargins(0, 0, 0, 0)
        h2.setSpacing(12)
        self._mode_label = BodyLabel("加密方式", combo_row)
        self._mode_combo = ComboBox(combo_row)
        self._mode_combo.addItems(["系统钥匙串", "密码加密"])
        self._mode_combo.setMinimumWidth(140)
        self._mode_combo.currentIndexChanged.connect(self._on_combo_changed)
        h2.addWidget(self._mode_label)
        h2.addWidget(self._mode_combo)
        self._mode_desc = _hint("", combo_row)
        h2.addWidget(self._mode_desc, 1)
        self.viewLayout.addWidget(combo_row)

        # ── 行 3：密码子区（密码加密时可用） ──
        pwd_row = QWidget(self.view)
        h3 = QHBoxLayout(pwd_row)
        h3.setContentsMargins(0, 0, 0, 0)
        h3.setSpacing(8)
        self._pwd_status = _hint("", pwd_row)
        self._pwd_btn = _small_button("设置密码", pwd_row)
        self._pwd_btn.clicked.connect(self._on_set_password)
        self._forget_btn = _small_button("清除记住的密码", pwd_row)
        self._forget_btn.clicked.connect(self._on_forget_remembered)
        h3.addWidget(self._pwd_status, 1)
        h3.addWidget(self._pwd_btn)
        h3.addWidget(self._forget_btn)
        self.viewLayout.addWidget(pwd_row)

        # ── 行 4：本机无钥匙串降级提示 ──
        self._keyring_warn = _hint("", self.view)
        self.viewLayout.addWidget(self._keyring_warn)

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
            current = str(self.cfg.secret_mode.value or MODE_KEYRING)
            encrypted = current != MODE_NONE
            self._encrypt_switch.setChecked(encrypted)
            self._mode_combo.setEnabled(encrypted)
            if current in _COMBO_INDEX_MODES:
                self._mode_combo.setCurrentIndex(_COMBO_INDEX_MODES.index(current))
        finally:
            self._switching = False

        # header 与下拉旁说明
        if current == MODE_NONE:
            self._header_label.setText("未加密（明文）")
            self._mode_desc.setText("开启加密后可选择保存方式")
        else:
            if current == MODE_PASSWORD:
                self._header_label.setText("已加密（密码）")
                self._mode_desc.setText("密钥加密后随配置云同步，换机输入同一密码即可解出")
            else:
                self._header_label.setText("已加密（钥匙串）")
                self._mode_desc.setText("密钥存操作系统凭证库，本机免配置；换机器需重新填写")

        # 密码子区
        is_password = current == MODE_PASSWORD
        locked = bool(getattr(self.cfg, "_cipher_backup", {}))
        self._pwd_status.setText(
            "状态：" + ("等待解锁（密钥已同步但未解锁）" if locked else "密码已设置") if is_password else "未启用密码加密"
        )
        self._pwd_btn.setVisible(is_password)
        self._pwd_btn.setText("输入密码解锁" if locked else ("修改密码" if self._has_password() else "设置密码"))
        self._forget_btn.setVisible(is_password)
        self._forget_btn.setEnabled(SecretStore().available)

        store_available = SecretStore().available
        warn = (
            "" if store_available else "⚠ 本机无可用系统钥匙串：钥匙串方式将退化为明文，密码方式下每次启动都需输入密码"
        )
        self._keyring_warn.setText(warn)
        self._keyring_warn.setVisible(bool(warn))

        if self.isExpand:
            # 展开态下子区可见性/文本变化会改变内容高度，需重算展开高度
            try:
                self._adjustViewSize()
            except Exception:
                pass

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
            target = _COMBO_INDEX_MODES[self._mode_combo.currentIndex()]
            self._switch_to(target)
        elif not checked and current != MODE_NONE:
            self._switch_to(MODE_NONE)

    def _on_combo_changed(self, _index: int):
        if self._switching:
            return
        current = self._current_mode()
        if current == MODE_NONE:
            return  # 开关关闭时下拉置灰，不该有信号
        target = _COMBO_INDEX_MODES[self._mode_combo.currentIndex()]
        if target != current:
            self._switch_to(target)

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
        if not getattr(self.cfg, "_cipher_backup", {}):
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
        if self.cfg._cipher_backup:
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
