"""首次使用时的模型、账户与安全设置入口。"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import InfoBar, PrimaryPushButton, PushButton, ScrollArea, TransparentPushButton

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css
from app.utils.safe_mode import SAFE_MODE_FLAG, is_safe_mode
from app.utils.secret_store import MODE_KEYRING, MODE_NONE, MODE_PASSWORD, SecretStore
from app.utils.utils import get_font_family_css, get_icon


def _label(text, role):
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setProperty("role", role)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return label


class FirstRunGuide(ScrollArea):
    addProvider = pyqtSignal()
    chooseModel = pyqtSignal()
    openAccount = pyqtSignal()
    openSecrets = pyqtSignal()
    openSecurity = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = Settings.get_instance()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        surface = QWidget()
        self.setWidget(surface)
        self.viewport().setAutoFillBackground(False)
        surface.setAutoFillBackground(False)
        outer = QHBoxLayout(surface)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.addStretch(1)
        column = QWidget(surface)
        column.setMaximumWidth(640)
        outer.addWidget(column, 100)
        outer.addStretch(1)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addStretch()

        hero = QFrame()
        hero.setObjectName("guideHero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(22, 18, 22, 18)
        intro = QVBoxLayout()
        intro.setSpacing(5)
        intro.addWidget(_label("欢迎使用 DriFox", "title"))
        self.model_status = _label("", "hint")
        intro.addWidget(self.model_status)
        intro.addSpacing(7)
        self.primary = PrimaryPushButton(hero)
        self.primary.clicked.connect(self._advance)
        intro.addWidget(self.primary, 0, Qt.AlignLeft)
        hero_layout.addLayout(intro, 1)
        logo = QLabel(hero)
        logo.setPixmap(QIcon(":/icons/drifox.ico").pixmap(60, 60))
        hero_layout.addWidget(logo, 0, Qt.AlignVCenter)
        layout.addWidget(hero)

        header = QHBoxLayout()
        header.addWidget(_label("账户与安全", "heading"), 1)
        self.skip = TransparentPushButton("跳过引导", column)
        self.skip.clicked.connect(self.finished)
        header.addWidget(self.skip)
        layout.addLayout(header)
        panel = QFrame()
        panel.setObjectName("guidePanel")
        rows = QVBoxLayout(panel)
        rows.setContentsMargins(16, 3, 16, 3)
        rows.setSpacing(0)
        self._icons = []
        self.key_status, key_button = self._row(rows, "锁定", "API Key 保存方式", "选择方式")
        key_button.clicked.connect(self.openSecrets)
        self.account_status, self.account_button = self._row(rows, "gitee", "Gitee 账户", "绑定账户")
        self.account_button.clicked.connect(self.openAccount)
        security_hint, security_button = self._row(rows, "shield-check", "工具安全", "安全中心")
        security_hint.setText("设置文件与命令的沙箱检查，选择人工或 AI 审批。")
        security_button.clicked.connect(self.openSecurity)
        mode_hint, mode_button = self._row(rows, "plug", "插件启动模式", "恢复正常" if is_safe_mode() else "安全模式")
        mode_hint.setText(
            "安全模式 · 本次仅加载系统插件。" if is_safe_mode() else "正常模式 · 加载系统插件和已启用的用户插件。"
        )
        mode_button.clicked.connect(self._choose_startup_mode)
        layout.addWidget(panel)
        layout.addStretch()

        for item in (self.cfg.secret_mode, self.cfg.gitee_bound, self.cfg.gitee_user_owner):
            item.valueChanged.connect(self._refresh_preferences)
        self._refresh_preferences()
        self.refresh_style()
        self.update_progress(False, "")

    def _row(self, rows, icon_name, title, action):
        if rows.count():
            line = QFrame()
            line.setObjectName("guideDivider")
            line.setFixedHeight(1)
            rows.addWidget(line)
        row = QHBoxLayout()
        row.setContentsMargins(0, 10, 0, 10)
        row.setSpacing(12)
        icon = QLabel()
        icon.setObjectName("guideIcon")
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(32, 32)
        self._icons.append((icon, icon_name))
        row.addWidget(icon)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(_label(title, "heading"))
        hint = _label("", "hint")
        copy.addWidget(hint)
        row.addLayout(copy, 1)
        button = PushButton(action, self)
        button.setAccessibleName(f"{title}，{action}")
        row.addWidget(button)
        rows.addLayout(row)
        return hint, button

    def refresh_style(self):
        Colors.refresh()
        self.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QFrame#guideHero {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.CONTENT_BG}, stop:1 {Colors.TAB_ACTIVE_BG});
                border: 1px solid {Colors.BORDER}; border-radius: 14px;
            }}
            QFrame#guidePanel {{
                background: {Colors.CARD_BG_SOLID}; border: 1px solid {Colors.BORDER}; border-radius: 12px;
            }}
            QFrame#guideDivider {{ background: {Colors.BORDER}; }}
            QLabel#guideIcon {{ background: {Colors.HOVER_BG}; border-radius: 8px; }}
            QLabel[role] {{ color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} }}
            QLabel[role="title"] {{ {font_size_css(25)} font-weight: 650; }}
            QLabel[role="heading"] {{ {font_size_css(15)} font-weight: bold; }}
            QLabel[role="hint"] {{ {font_size_css(13)} color: {Colors.TEXT_SECONDARY}; }}
        """)
        for label, name in self._icons:
            label.setPixmap(get_icon(name).pixmap(18, 18))

    def _refresh_preferences(self):
        modes = {
            MODE_KEYRING: "系统钥匙串 · 换设备后需要重新填写密钥。",
            MODE_PASSWORD: "密码加密 · 换设备后可用同一密码解锁密钥。",
            MODE_NONE: "不加密 · API Key 以明文保存在配置文件中。",
        }
        mode = self.cfg.secret_mode.value
        hint = modes.get(mode, "选择服务商密钥的保存方式。")
        if mode == MODE_KEYRING and not SecretStore().available:
            hint = "本机钥匙串不可用，密钥将以明文保存。可改用密码加密。"
        self.key_status.setText(hint)
        bound = self.cfg.gitee_bound.value
        owner = self.cfg.gitee_user_owner.value
        prefix = f"已绑定 {owner}。" if bound and owner else ""
        self.account_status.setText(f"{prefix}绑定后可跨设备同步配置，用自己的仓库存放分享的图片。")
        action = "管理账户" if bound else "绑定账户"
        self.account_button.setText(action)
        self.account_button.setAccessibleName(f"Gitee {action}")

    def update_progress(self, has_provider: bool, model: str):
        self.has_provider = has_provider
        self.model = model
        if model:
            hint, action = f"当前聊天模型 · {model}", "开始对话"
        elif has_provider:
            hint, action = "服务商已连接，选择一个模型即可开始对话。", "选择模型"
        else:
            hint, action = "连接聊天模型，开始你的第一段对话。", "添加服务商"
        self.model_status.setText(hint)
        self.primary.setText(action)
        self.skip.setVisible(not model)

    def _advance(self):
        if self.model:
            self.finished.emit()
        elif self.has_provider:
            self.chooseModel.emit()
        else:
            self.addProvider.emit()

    def _choose_startup_mode(self):
        from app.utils.app_restart import restart_application
        from app.widgets.common_dialogs import ConfirmDialog

        safe = is_safe_mode()
        target = "正常模式" if safe else "安全模式"
        description = (
            "重启后将加载系统插件和已启用的用户插件。"
            if safe
            else "本次重启将只加载系统插件。下次从桌面打开 DriFox 时会恢复正常模式。"
        )
        dialog = ConfirmDialog(f"切换到{target}？", description, confirm_text="立即重启", parent=self.window())

        def restart():
            if safe:
                ok = restart_application(drop_args=[SAFE_MODE_FLAG])
            else:
                ok = restart_application(extra_args=[SAFE_MODE_FLAG])
            if not ok:
                InfoBar.error("重启失败", "无法启动新的 DriFox 进程。", parent=self.window())

        dialog.confirmed.connect(restart)
        dialog.exec_()
