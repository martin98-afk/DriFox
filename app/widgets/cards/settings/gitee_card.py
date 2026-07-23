# -*- coding: utf-8 -*-
"""
Gitee 账号绑定设置卡片

轻量卡片，嵌入系统设置内部：
- 未绑定：SVG 图标 + 绑定按钮同行
- 已绑定：用户头像 + 用户名 + 仓库名，点击头像解绑
"""

import threading

import requests
from loguru import logger
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout
from qfluentwidgets import CardWidget, Dialog, InfoBar, InfoBarPosition, PrimaryPushButton

from app.utils.config import Settings
from app.utils.design_tokens import ButtonStyles, Colors, scale_font_size
from app.utils.utils import get_font_family_css, get_icon, get_unified_font


class AvatarButton(QLabel):
    """可点击的圆形头像"""

    clicked = pyqtSignal()

    def __init__(self, size=44, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setAlignment(Qt.AlignCenter)
        self._set_placeholder()

    def _set_placeholder(self):
        from PyQt5.QtGui import QPainter, QBrush, QColor
        from PyQt5.QtCore import QRectF

        pix = QPixmap(self._size, self._size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QBrush(QColor("#c71d23")))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QRectF(1, 1, self._size - 2, self._size - 2))
        painter.end()
        self.setPixmap(pix)

    def set_avatar_url(self, url: str):
        """从 URL 下载头像并设为圆形"""
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                self._set_round_pixmap(resp.content)
            else:
                self._set_placeholder()
        except Exception:
            self._set_placeholder()

    def _set_round_pixmap(self, data: bytes):
        from PyQt5.QtGui import QBitmap, QPainter, QPixmap
        from PyQt5.QtCore import QRectF

        src = QPixmap()
        src.loadFromData(data)
        src = src.scaled(self._size, self._size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)

        mask = QBitmap(self._size, self._size)
        mask.fill(Qt.color0)
        mp = QPainter(mask)
        mp.setRenderHint(QPainter.Antialiasing)
        mp.setBrush(Qt.color1)
        mp.setPen(Qt.NoPen)
        mp.drawEllipse(QRectF(1, 1, self._size - 2, self._size - 2))
        mp.end()

        rounded = QPixmap(self._size, self._size)
        rounded.fill(Qt.transparent)
        rp = QPainter(rounded)
        rp.setRenderHint(QPainter.Antialiasing)
        rp.setClipRegion(mask)
        rp.drawPixmap(0, 0, src)
        rp.end()

        self.setPixmap(rounded)

    def mousePressEvent(self, event):
        self.clicked.emit()


class GiteeCard(CardWidget):
    """Gitee 账号绑定卡片"""

    oauthResult = pyqtSignal(bool, str)  # (success, message)
    avatarReady = pyqtSignal(str)  # avatar_url

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = Settings.get_instance()
        self._binding = False

        self.oauthResult.connect(self._on_oauth_result)
        self.avatarReady.connect(self._on_avatar_ready)

        self._setup_ui()
        self._refresh_ui()

    def _setup_ui(self):
        Colors.refresh()
        self.setStyleSheet(
            f"GiteeCard {{ background: {Colors.CARD_BG.format(alpha=200)};"
            f" border: 1px solid {Colors.BORDER}; border-radius: 8px; }}"
        )

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 12, 14, 12)
        main_layout.setSpacing(8)

        # ── 头部行：图标 + 标题 ──
        header_row = QHBoxLayout()
        header_row.setSpacing(10)

        # Gitee 图标（主题自动适配）
        self._icon_label = QLabel()
        icon_size = scale_font_size(26)
        self._icon_label.setFixedSize(icon_size, icon_size)
        icon = get_icon("gitee")
        self._icon_label.setPixmap(icon.pixmap(icon_size, icon_size))
        header_row.addWidget(self._icon_label)

        # 标题
        title = QLabel("Gitee 账号绑定")
        title.setFont(get_unified_font(12, True))
        title.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()};")
        header_row.addWidget(title)

        header_row.addStretch()
        main_layout.addLayout(header_row)

        # ── 内容行：头像区 + 状态文字 + 绑定按钮 ──
        content_row = QHBoxLayout()
        content_row.setSpacing(10)

        # 左侧：头像 / 圆形占位
        self._avatar = AvatarButton(44, self)
        self._avatar.clicked.connect(self._on_unbind)
        content_row.addWidget(self._avatar)

        # 中间：状态文字
        self._status_label = QLabel()
        self._status_label.setFont(get_unified_font(11))
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()};")
        content_row.addWidget(self._status_label, 1)

        # 右侧：绑定按钮
        self._bind_btn = PrimaryPushButton("绑定")
        self._bind_btn.setFixedWidth(80)
        self._bind_btn.setStyleSheet(ButtonStyles.primary_action())
        self._bind_btn.clicked.connect(self._on_bind)
        content_row.addWidget(self._bind_btn)

        main_layout.addLayout(content_row)

    # ── UI 刷新 ──────────────────────────────────────────

    def _refresh_ui(self):
        is_bound = self.cfg.gitee_bound.value
        owner = self.cfg.gitee_user_owner.value

        Colors.refresh()
        if is_bound and owner:
            repo = self.cfg.gitee_user_repo.value
            self._status_label.setText(f"<b>{owner}</b> / {repo}")
            self._status_label.setToolTip(f"点击左侧头像解绑 | 仓库：{owner}/{repo}")
            self._bind_btn.setVisible(False)
            self._avatar.setCursor(Qt.PointingHandCursor)
            self._avatar.setToolTip(f"点击解绑 {owner}")
            self._load_avatar(owner)
        else:
            self._status_label.setText("未绑定（使用共享图床）")
            self._status_label.setToolTip("")
            self._bind_btn.setVisible(True)
            self._avatar.setCursor(Qt.ArrowCursor)
            self._avatar.setToolTip("")
            self._avatar._set_placeholder()

    def _load_avatar(self, owner: str):
        """异步加载用户头像"""

        def _fetch():
            try:
                resp = requests.get(
                    f"https://gitee.com/api/v5/users/{owner}",
                    timeout=10,
                )
                if resp.status_code == 200:
                    avatar_url = resp.json().get("avatar_url", "")
                    if avatar_url:
                        self.avatarReady.emit(avatar_url)
            except Exception:
                pass

        t = threading.Thread(target=_fetch, daemon=True)
        t.start()

    def _on_avatar_ready(self, url: str):
        """主线程更新头像"""
        self._avatar.set_avatar_url(url)

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
        self._bind_btn.setText("绑定")
        self._bind_btn.setEnabled(True)

        if success:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            GiteeUploader.get_instance().reset_config()
            self._refresh_ui()
            InfoBar.success(
                title="绑定成功",
                content=msg,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        else:
            InfoBar.error(
                title="绑定失败",
                content=msg,
                position=InfoBarPosition.TOP_RIGHT,
                duration=5000,
                parent=self.window(),
            )

    # ── 解绑 ─────────────────────────────────────────────

    def _on_unbind(self):
        if not self.cfg.gitee_bound.value:
            return

        owner = self.cfg.gitee_user_owner.value
        dialog = Dialog(
            "确认解绑",
            f"解绑后上传将恢复使用共享图床仓库。\n当前绑定：{owner}",
            self.window(),
        )
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
                title="已解绑",
                content="Gitee 账号已解绑",
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        except Exception as e:
            logger.error(f"[GiteeCard] 解绑异常: {e}")
            InfoBar.error(
                title="解绑失败",
                content=str(e),
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
