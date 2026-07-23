# -*- coding: utf-8 -*-
"""
Gitee 账号绑定设置卡片

继承 SettingCard：图标 + 标题 + 说明文字自动布局，
右侧追加头像 + 绑定/解绑按钮。
"""
import hashlib
import threading
import webbrowser
from loguru import logger
from PyQt5.QtCore import Qt, pyqtSignal, QRectF
from PyQt5.QtGui import QColor, QMouseEvent, QPainter, QPixmap
from PyQt5.QtWidgets import QLabel
from qfluentwidgets import Dialog, InfoBar, InfoBarPosition, PrimaryPushButton, SettingCard

from app.utils.config import Settings
from app.utils.design_tokens import ButtonStyles, scale_font_size
from app.utils.utils import get_icon, get_unified_font

_AVATAR_COLORS = [
    "#c71d23", "#e74c3c", "#e67e22", "#f39c12",
    "#27ae60", "#2ecc71", "#1abc9c", "#3498db",
    "#2980b9", "#9b59b6", "#8e44ad", "#34495e",
]


def _color_for_name(name: str) -> str:
    idx = int(hashlib.md5(name.encode()).hexdigest(), 16) % len(_AVATAR_COLORS)
    return _AVATAR_COLORS[idx]


def _make_avatar_pixmap(text: str, size: int = 32) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(_color_for_name(text)))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    painter.setPen(QColor("#ffffff"))
    font = get_unified_font(int(size * 0.42), True)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, text[0].upper())
    painter.end()
    return pix


class _ClickableAvatar(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event: QMouseEvent):
        self.clicked.emit()


class GiteeCard(SettingCard):
    """Gitee 账号绑定 — SettingCard 子类，布局与其他设置卡片一致"""

    oauthResult = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(
            get_icon("gitee"),
            "Gitee 账号绑定",
            "绑定后可云端备份配置、分享会话",
            parent,
        )
        self.cfg = Settings.get_instance()
        self._binding = False
        self._bound_owner = ""
        self._bound_repo = ""
        self.oauthResult.connect(self._on_oauth_result)

        self._setup_right()
        self._refresh_ui()

    def _setup_right(self):
        avatar_size = scale_font_size(32)
        self._avatar = _ClickableAvatar()
        self._avatar.setFixedSize(avatar_size, avatar_size)
        self._avatar.setCursor(Qt.PointingHandCursor)
        self._avatar.setAlignment(Qt.AlignCenter)
        self._avatar.clicked.connect(self._on_avatar_clicked)
        self.hBoxLayout.addWidget(self._avatar)

        self._bind_btn = PrimaryPushButton("绑定")
        self._bind_btn.setFixedWidth(72)
        self._bind_btn.setMinimumHeight(30)
        self._bind_btn.setStyleSheet(ButtonStyles.primary_action())
        self._bind_btn.clicked.connect(self._on_bind_clicked)
        self.hBoxLayout.addWidget(self._bind_btn, 0, Qt.AlignRight)

    # ── UI 刷新 ──────────────────────────────────────────

    def _refresh_ui(self):
        is_bound = self.cfg.gitee_bound.value
        owner = self.cfg.gitee_user_owner.value
        repo = self.cfg.gitee_user_repo.value
        avatar_size = scale_font_size(32)

        if is_bound and owner:
            self._bound_owner = owner
            self._bound_repo = repo
            self._avatar.setPixmap(_make_avatar_pixmap(owner, avatar_size))
            self._avatar.setToolTip(f"点击打开仓库 {owner}/{repo}")
            self._bind_btn.setText("解绑")
            self._bind_btn.setStyleSheet(
                f"color: #fa5151; border: 1px solid #fa5151; border-radius: 6px;"
                f"padding: 5px 12px; font-size: {scale_font_size(12)}px;"
                f"background: transparent;"
            )
        else:
            self._bound_owner = ""
            self._bound_repo = ""
            self._avatar.setPixmap(_make_avatar_pixmap("?", avatar_size))
            self._avatar.setToolTip("未绑定")
            self._bind_btn.setText("绑定")
            self._bind_btn.setStyleSheet(ButtonStyles.primary_action())

    def _on_avatar_clicked(self):
        if self._bound_owner:
            webbrowser.open(f"https://gitee.com/{self._bound_owner}/{self._bound_repo}")

    def _on_bind_clicked(self):
        if self.cfg.gitee_bound.value:
            self._on_unbind()
        else:
            self._on_bind()

    # ── 绑定 ─────────────────────────────────────────────

    def _on_bind(self):
        if self._binding:
            return

        dialog = Dialog("仓库可见性", "选择要创建的仓库类型：", self.window())
        dialog.yesButton.setText("公开")
        dialog.cancelButton.setText("私有")
        dialog.yesButton.setStyleSheet(ButtonStyles.primary_action())
        dialog.cancelButton.setStyleSheet(ButtonStyles.primary_action())

        if dialog.exec():
            repo_private = False
        else:
            repo_private = True

        self._binding = True
        self._bind_btn.setText("授权中…")
        self._bind_btn.setEnabled(False)

        t = threading.Thread(target=self._do_oauth, args=(repo_private,), daemon=True)
        t.start()

    def _do_oauth(self, repo_private: bool):
        try:
            from app.gateway.utils.gitee_oauth import start_oauth_flow

            success, msg = start_oauth_flow(repo_private)
            self.oauthResult.emit(success, msg)
        except Exception as e:
            logger.error(f"[GiteeCard] OAuth 异常: {e}")
            self.oauthResult.emit(False, f"绑定异常：{e}")

    def _on_oauth_result(self, success: bool, msg: str):
        self._binding = False
        self._bind_btn.setEnabled(True)

        if success:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            GiteeUploader.get_instance().reset_config()
            self._refresh_ui()
            InfoBar.success(
                title="绑定成功", content=msg,
                position=InfoBarPosition.TOP_RIGHT, duration=3000,
                parent=self.window(),
            )
        else:
            self._bind_btn.setText("绑定")
            InfoBar.error(
                title="绑定失败", content=msg,
                position=InfoBarPosition.TOP_RIGHT, duration=5000,
                parent=self.window(),
            )

    # ── 解绑 ─────────────────────────────────────────────

    def _on_unbind(self):
        owner = self.cfg.gitee_user_owner.value
        dialog = Dialog("确认解绑", f"解绑后上传将恢复使用共享图床仓库。\n当前绑定：{owner}", self.window())
        dialog.yesButton.setText("确定解绑")
        dialog.cancelButton.setText("取消")
        dialog.yesButton.setStyleSheet(
            f"color: #fa5151; border: 1px solid #fa5151; border-radius: 6px;"
            f"padding: 6px 16px; font-size: {scale_font_size(13)}px;"
        )

        if not dialog.exec():
            return

        try:
            from app.gateway.utils.gitee_oauth import unbind_account
            from app.gateway.utils.gitee_uploader import GiteeUploader

            unbind_account()
            GiteeUploader.get_instance().reset_config()
            self._refresh_ui()
            InfoBar.success(
                title="已解绑", content="Gitee 账号已解绑",
                position=InfoBarPosition.TOP_RIGHT, duration=3000,
                parent=self.window(),
            )
        except Exception as e:
            logger.error(f"[GiteeCard] 解绑异常: {e}")
            InfoBar.error(
                title="解绑失败", content=str(e),
                position=InfoBarPosition.TOP_RIGHT, duration=3000,
                parent=self.window(),
            )
