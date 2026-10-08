# -*- coding: utf-8 -*-
"""
Gitee 账号绑定设置卡片

继承 SettingCard：图标 + 标题 + 说明文字自动布局，
右侧追加头像 + 绑定/解绑按钮。
"""

import hashlib
import sip
import threading
import time
import webbrowser
from loguru import logger
from PyQt5.QtCore import Qt, QSize, pyqtSignal, QPoint, QRectF, QTimer
from PyQt5.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, InfoBar, InfoBarPosition, MaskDialogBase, SettingCard, SwitchButton

from app.core.infra import window_registry
from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css, get_icon, get_unified_font
from app.widgets.common_dialogs import ConfirmDialog
from app.widgets.elided_label import _ElidedLabel

_AVATAR_COLORS = [
    "#c71d23",
    "#e74c3c",
    "#e67e22",
    "#f39c12",
    "#27ae60",
    "#2ecc71",
    "#1abc9c",
    "#3498db",
    "#2980b9",
    "#9b59b6",
    "#8e44ad",
    "#34495e",
]


def _color_for_name(name: str) -> str:
    idx = int(hashlib.md5(name.encode()).hexdigest(), 16) % len(_AVATAR_COLORS)
    return _AVATAR_COLORS[idx]


class _AvatarCircleWidget(QWidget):
    """使用 QPainter 绘制的圆形头像 — DPI 感知

    替代 QLabel + QPixmap + setDevicePixelRatio 方案。
    直接在 paintEvent 中用 QPainter 绘制圆 + 文字，Qt 自动处理 DPI 缩放，
    避免物理像素四舍五入导致的逻辑尺寸不匹配和裁剪问题。
    """

    clicked = pyqtSignal()

    def __init__(self, text: str = "?", parent=None):
        super().__init__(parent)
        self._text = text[0].upper() if text else "?"
        self._bg_color = QColor(_color_for_name(text))
        self._size = 28
        self._invalid = False  # 绑定失效警示（红边 + 红点角标）
        self.setFixedSize(self._size, self._size)
        self.setCursor(Qt.PointingHandCursor)

    def set_avatar(self, text: str):
        """更新头像文字和背景色"""
        self._text = text[0].upper() if text else "?"
        self._bg_color = QColor(_color_for_name(text))
        self.setToolTip(text)
        self.update()

    def set_invalid(self, invalid: bool):
        """设置绑定失效警示样式（红色圆环边框 + 右上角红点角标）"""
        if self._invalid == invalid:
            return
        self._invalid = invalid
        self.update()

    def set_size(self, size: int):
        """更新头像逻辑尺寸"""
        self._size = size
        self.setFixedSize(size, size)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = self.rect()
        size = min(rect.width(), rect.height())

        # 圆形裁剪区域（留 1px 内边距避免抗锯齿溢出）
        ellipse_rect = QRectF(1, 1, size - 2, size - 2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._bg_color)
        painter.drawEllipse(ellipse_rect)

        # 居中白色文字
        painter.setPen(QColor("#ffffff"))
        font = get_unified_font(int(size * 0.42), bold=True)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, self._text)

        # 绑定失效警示：红色圆环边框 + 右上角红点角标
        if self._invalid:
            ring_width = max(1.5, size * 0.08)
            painter.setPen(QPen(QColor("#f85149"), ring_width))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(ellipse_rect)
            dot_r = max(2.5, size * 0.22)
            painter.setPen(QPen(QColor("#ffffff"), 1.0))
            painter.setBrush(QColor("#f85149"))
            painter.drawEllipse(QRectF(size - 2 * dot_r - 1, 0, 2 * dot_r, 2 * dot_r))

    def mousePressEvent(self, event: QMouseEvent):
        self.clicked.emit()


class _ClickableElidedLabel(_ElidedLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ── 仓库可见性选择弹窗（参考 _ProjectExportChoiceDialog） ──


class _RepoVisibilityDialog(MaskDialogBase):
    """选择公开/私有仓库 — 与项目导出弹窗统一风格"""

    PUBLIC = False
    PRIVATE = True

    chosen = pyqtSignal(bool)  # True=私有, False=公开

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("repoVisibilityDialog")
        self.widget.setStyleSheet(f"""
            #{self.widget.objectName()} {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)

        title_label = QLabel("🔒 仓库可见性", self.widget)
        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(16)}; font-weight: bold;"
        )
        layout.addWidget(title_label)

        hint_label = QLabel("选择要创建的仓库类型：", self.widget)
        hint_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)}; padding-left: 2px;"
        )
        layout.addWidget(hint_label)

        layout.addSpacing(4)

        btn_style = f"""
            QPushButton {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 8px 16px;
                text-align: left;
                {get_font_family_css()} {font_size_css(14)}
            }}
            QPushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.INFO};
            }}
        """
        hint_style = (
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)}; padding-left: 4px;"
        )

        # ── 公开按钮 ──
        public_btn = QPushButton("🌐  公开仓库", self.widget)
        public_btn.setCursor(Qt.PointingHandCursor)
        public_btn.setFixedHeight(56)
        public_btn.setStyleSheet(btn_style)
        public_btn.clicked.connect(lambda: self._choose(False))
        layout.addWidget(public_btn)

        public_hint = QLabel("任何人可见，适合分享用途", self.widget)
        public_hint.setStyleSheet(hint_style)
        layout.addWidget(public_hint)

        # ── 私有按钮 ──
        private_btn = QPushButton("🔒  私有仓库", self.widget)
        private_btn.setCursor(Qt.PointingHandCursor)
        private_btn.setFixedHeight(56)
        private_btn.setStyleSheet(btn_style)
        private_btn.clicked.connect(lambda: self._choose(True))
        layout.addWidget(private_btn)

        private_hint = QLabel("仅自己可访问，链接需登录后查看", self.widget)
        private_hint.setStyleSheet(hint_style)
        layout.addWidget(private_hint)

        self.widget.setFixedSize(400, 280)
        self._center()

    def _choose(self, is_private: bool):
        self.close()
        self.chosen.emit(is_private)

    def _center(self):
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center()


# ── Tab 模式紧凑账户行 ────────────────────────────────────


class GiteeAccountRow(QFrame):
    """Tab 模式底部的紧凑 Gitee 账户快捷栏。"""

    oauthResult = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("giteeAccountRow")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.cfg = Settings.get_instance()
        self._binding = False
        self._bound_owner = ""
        self._bound_repo = ""
        self._compact = False  # 是否紧凑模式（收起时垂直堆叠）
        self._invalid = False  # 绑定是否已失效（token 失效，需重新绑定）

        from app.core.sync.config_sync import ConfigSyncService

        self._sync_svc = ConfigSyncService.get_instance()
        self._setup_ui()
        self._connect_config_signals()
        self._connect_sync_signals()
        self._connect_uploader_signals()
        self._refresh_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        avatar_size = scale_font_size(28)
        self._avatar = _AvatarCircleWidget("?", self)
        self._avatar.set_size(avatar_size)
        layout.addWidget(self._avatar)

        text_container = QVBoxLayout()
        text_container.setContentsMargins(0, 0, 0, 0)
        text_container.setSpacing(0)

        self._name_label = _ClickableElidedLabel("", self)
        self._repo_label = _ClickableElidedLabel("", self)
        text_container.addWidget(self._name_label)
        text_container.addWidget(self._repo_label)
        layout.addLayout(text_container, 1)

        # ── 设置按钮（齿轮图标，点击打开完整设置卡片） ──
        from qfluentwidgets import FluentIcon as _FIF
        from qfluentwidgets import TransparentToolButton as _TransparentToolButton

        self._settings_btn = _TransparentToolButton(self)
        # popup 关闭时刻（monotonic 秒）：防抖用，见 _toggle_popup
        self._popup_closed_at = 0.0
        self._settings_btn.setIcon(_FIF.SETTING)
        btn_size = scale_font_size(24)
        self._settings_btn.setFixedSize(btn_size, btn_size)
        self._settings_btn.setIconSize(QSize(btn_size - 2, btn_size - 2))
        self._settings_btn.setToolTip("设置")
        self._settings_btn.setCursor(Qt.PointingHandCursor)
        self._settings_btn.setFocusPolicy(Qt.NoFocus)
        layout.addWidget(self._settings_btn)

        # 整块区域点击触发 Gitee 管理弹窗（头像/名称/仓库）
        # 设置按钮独立触发完整设置卡片
        self.setCursor(Qt.PointingHandCursor)
        self._avatar.setCursor(Qt.PointingHandCursor)
        self._name_label.setCursor(Qt.PointingHandCursor)
        self._repo_label.setCursor(Qt.PointingHandCursor)
        self._avatar.clicked.connect(self._toggle_popup)
        self._name_label.clicked.connect(self._toggle_popup)
        self._repo_label.clicked.connect(self._toggle_popup)
        self._settings_btn.clicked.connect(self._toggle_settings_card)
        self.oauthResult.connect(self._on_oauth_result)
        self._popup: "_GiteeMorePopup" | None = None

    def _connect_config_signals(self):
        for item in (
            self.cfg.gitee_bound,
            self.cfg.gitee_user_owner,
            self.cfg.gitee_user_repo,
        ):
            item.valueChanged.connect(self._on_config_changed)

    def _on_config_changed(self, _value=None):
        """配置变化：重新绑定成功（bound+owner）→ 恢复绑定态；否则按现状刷新"""
        is_bound = bool(self.cfg.gitee_bound.value)
        owner = str(self.cfg.gitee_user_owner.value or "")
        if is_bound and owner:
            # 重新绑定成功 → 清除失效标志，恢复绑定态
            self._invalid = False
        self._refresh_ui()

    def _connect_sync_signals(self):
        """监听 ConfigSyncService.syncDone：token 失效 → 失效标识；成功 → 恢复"""
        try:
            self._sync_svc.syncDone.disconnect(self._on_sync_done)
        except TypeError:
            pass
        self._sync_svc.syncDone.connect(self._on_sync_done)

    def _connect_uploader_signals(self):
        """监听 GiteeUploader.tokenInvalid：上传 401 重试失败 → 失效标识"""
        try:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            uploader = GiteeUploader.get_instance()
            try:
                uploader.tokenInvalid.disconnect(self._on_uploader_token_invalid)
            except TypeError:
                pass
            uploader.tokenInvalid.connect(self._on_uploader_token_invalid)
        except Exception as e:
            logger.warning(f"[GiteeAccountRow] 连接上传器失效信号失败: {e}")

    def _on_uploader_token_invalid(self):
        """上传 401 重试仍失败 → 绑定失效，置红标并触发全局提醒"""
        self.set_invalid(True)
        self._notify_token_invalid_reminder()

    def _notify_token_invalid_reminder(self):
        """触发全局「绑定已失效」提醒（去重/开关守卫在 controller 内）"""
        try:
            from app.widgets.cards.global_card_controller import get_global_card_controller

            ctrl = get_global_card_controller()
            if ctrl is not None:
                ctrl.check_gitee_token_invalid_reminder()
        except Exception as e:
            logger.warning(f"[GiteeAccountRow] 触发失效提醒失败: {e}")

    def _on_sync_done(self, success: bool, msg: str):
        """同步完成回调：维护绑定失效状态。

        - msg 含「已失效」→ token 被吊销，绑定已失效；
        - 同步成功（True）→ 绑定恢复有效；
        - 网络异常（刷新失败）不算失效，保持现状。
        """
        if "已失效" in msg:
            self.set_invalid(True)
        elif success:
            self.set_invalid(False)

    def set_invalid(self, invalid: bool):
        """设置绑定失效状态：True 显示红色失效标识，False 恢复绑定态"""
        if self._invalid == invalid:
            return
        self._invalid = invalid
        self._refresh_ui()

    def _refresh_ui(self, _value=None):
        is_bound = bool(self.cfg.gitee_bound.value)
        owner = str(self.cfg.gitee_user_owner.value or "")
        repo = str(self.cfg.gitee_user_repo.value or "")
        avatar_size = scale_font_size(28)

        if self._invalid:
            # 绑定已失效：红色警示，明显区别于「从未绑定」（失效信号优先于 cfg 绑定态）
            self._bound_owner = ""
            self._bound_repo = ""
            self._avatar.set_avatar(owner or "?")
            self._avatar.set_size(avatar_size)
            self._avatar.set_invalid(True)
            self._avatar.setToolTip("绑定已失效，点击重新绑定")
            self._name_label.setText("绑定已失效")
            self._name_label.setToolTip("绑定已失效，点击重新绑定")
            self._repo_label.setText("点击重新绑定")
            self._repo_label.setToolTip("点击重新绑定")
        elif is_bound and owner:
            self._bound_owner = owner
            self._bound_repo = repo
            self._avatar.set_avatar(owner)
            self._avatar.set_size(avatar_size)
            self._avatar.set_invalid(False)
            self._avatar.setToolTip(f"点击打开仓库 {owner}/{repo}")
            self._name_label.setText(owner)
            self._name_label.setToolTip(owner)
            self._repo_label.setText(f"{repo} ↗")
            self._repo_label.setToolTip(repo)
        else:
            self._bound_owner = ""
            self._bound_repo = ""
            self._avatar.set_avatar("?")
            self._avatar.set_size(avatar_size)
            self._avatar.set_invalid(False)
            self._avatar.setToolTip("未绑定")
            self._name_label.setText("Gitee 未绑定")
            self._name_label.setToolTip("Gitee 未绑定")
            self._repo_label.setText("绑定后可备份与分享")
            self._repo_label.setToolTip("绑定后可备份与分享")

        # 更新整行可点状态
        self._settings_btn.setEnabled(not self._binding)
        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet("""
            QFrame#giteeAccountRow {
                background: transparent;
                border: none;
            }
        """)
        # 失效时名称显示红色警示
        name_color = "#f85149" if self._invalid else Colors.TEXT_PRIMARY
        self._name_label.setStyleSheet(
            f"color: {name_color}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(12)}; font-weight: 600;"
        )
        self._repo_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(10)};"
        )
        btn_size = scale_font_size(24)
        self._settings_btn.setFixedSize(btn_size, btn_size)
        self._settings_btn.setIconSize(QSize(btn_size - 2, btn_size - 2))
        self._settings_btn.setStyleSheet("""
            TransparentToolButton {
                background: transparent;
                border: none;
            }
        """)

    def _open_repository(self):
        if self._bound_owner and self._bound_repo:
            webbrowser.open(f"https://gitee.com/{self._bound_owner}/{self._bound_repo}")

    def _on_bind(self):
        if self._binding:
            return

        dialog = _RepoVisibilityDialog(self.window())
        dialog.chosen.connect(self._start_oauth_with_backup)
        dialog.exec_()

    def _start_oauth_with_backup(self, repo_private: bool):
        self._sync_svc.backup_local()
        self._start_oauth(repo_private)

    def _start_oauth(self, repo_private: bool):
        self._binding = True
        self._refresh_ui()
        worker = threading.Thread(
            target=self._do_oauth,
            args=(repo_private,),
            daemon=True,
        )
        worker.start()

    def _do_oauth(self, repo_private: bool):
        try:
            from app.gateway.auth import get_oauth_backend

            success, message = get_oauth_backend("gitee").bind(
                repo_private=repo_private,
            )
            self.oauthResult.emit(success, message)
        except Exception as error:
            logger.error(f"[GiteeAccountRow] OAuth 异常: {error}")
            self.oauthResult.emit(False, f"绑定异常：{error}")

    def _on_oauth_result(self, success: bool, message: str):
        self._binding = False
        if success:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            GiteeUploader.get_instance().reset_config()
            self._refresh_ui()
            self._auto_enable_sync()
            InfoBar.success(
                title="绑定成功",
                content=message,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
            return

        self._refresh_ui()
        InfoBar.error(
            title="绑定失败",
            content=message,
            position=InfoBarPosition.TOP_RIGHT,
            duration=5000,
            parent=self.window(),
        )

    def _auto_enable_sync(self):
        if self._sync_svc._state != "disabled":
            return
        from app.gateway.auth import get_oauth_backend

        bound_info = get_oauth_backend("gitee").get_bound_info()
        if bound_info and bound_info.get("token") and bound_info.get("owner"):
            logger.info("[GiteeAccountRow] 检测到已绑定，自动启动配置同步")
            self._sync_svc.enable(bound_info["token"], bound_info["owner"])

    def _on_unbind(self):
        owner = str(self.cfg.gitee_user_owner.value or "")
        dialog = ConfirmDialog(
            title="确认解绑",
            content=f"解绑后上传将恢复使用共享图床仓库。\n当前绑定：{owner}",
            confirm_text="确定解绑",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(self._do_unbind)
        dialog.exec_()

    def _do_unbind(self):
        try:
            from app.gateway.auth import get_oauth_backend
            from app.gateway.utils.gitee_uploader import GiteeUploader

            self._sync_svc.disable()
            success, message = get_oauth_backend("gitee").unbind()
            if not success:
                raise RuntimeError(message)
            GiteeUploader.get_instance().reset_config()

            if self._sync_svc.restore_local():
                self.cfg.load()

            self._invalid = False  # 主动解绑 → 恢复未绑定态，不再显示失效警示
            self._refresh_ui()
            InfoBar.success(
                title="已解绑",
                content="Gitee 账号已解绑，配置已恢复",
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        except Exception as error:
            logger.error(f"[GiteeAccountRow] 解绑异常: {error}")
            self._refresh_ui()
            InfoBar.error(
                title="解绑失败",
                content=str(error),
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )

    def set_show_only_avatar(self, avatar_only: bool):
        """收起侧边栏时仅显示头像，隐藏文字和设置按钮"""
        self._settings_btn.setVisible(not avatar_only)
        self._name_label.setVisible(not avatar_only)
        self._repo_label.setVisible(not avatar_only)

    def refresh_style(self):
        """主题或字号变化后重建头像、尺寸和样式。"""
        avatar_size = scale_font_size(28)
        self._avatar.set_size(avatar_size)
        btn_size = scale_font_size(24)
        self._settings_btn.setFixedSize(btn_size, btn_size)
        self._settings_btn.setIconSize(QSize(btn_size - 2, btn_size - 2))
        self._refresh_ui()

    def _toggle_popup(self):
        """点击整块区域切换浮动卡片显示状态"""
        if self._binding:
            return

        # 防抖：Qt.Popup 被点击外部关闭的瞬间，⚙ 按钮的 click 才到达；
        # 此时 _popup 已被置 None，不拦住就会「关闭后立刻重开」（表现为点切换失效）
        if time.monotonic() - self._popup_closed_at < 0.3:
            return

        # 防御性检查：如果 C++ 对象已被删除，清理引用
        if self._popup is not None and sip.isdeleted(self._popup):
            self._popup = None

        if self._popup and self._popup.isVisible():
            self._popup.close()
            self._popup = None
            return

        # 确保旧 popup 已清理
        if self._popup:
            self._popup.deleteLater()
            self._popup = None

        popup = _GiteeMorePopup(self)
        popup.destroyed.connect(self._on_popup_closed)
        popup.adjustSize()
        popup_width = max(popup.sizeHint().width(), 220)
        popup_height = popup.sizeHint().height()

        # 定位：在当前行上方弹出，与窗口左边缘对齐，确保不超出窗口
        row_global = self.mapToGlobal(QPoint(0, 0))
        window = self.window()
        if window:
            win_rect = window.frameGeometry()
        else:
            win_rect = QApplication.primaryScreen().availableGeometry()

        # X：贴近窗口左边框（留 3px 呼吸），不超出窗口左右边界
        x = win_rect.left() + 3
        if x + popup_width > win_rect.right() - 4:
            x = win_rect.right() - 4 - popup_width
        if x < win_rect.left() + 2:
            x = win_rect.left() + 2

        # Y：贴着本行（设置栏）上方弹出，留 3px 呼吸
        y = row_global.y() - popup_height - 3
        if y < win_rect.top() + 4:
            # 空间不够则向下弹出
            y = row_global.y() + self.height() + 3
            # 向下弹出也超出底部时，对齐窗口底部
            if y + popup_height > win_rect.bottom() - 4:
                y = win_rect.bottom() - 4 - popup_height
        if y < win_rect.top() + 4:
            y = win_rect.top() + 4

        popup.setFixedSize(popup_width, popup_height)
        popup.move(x, y)
        popup.show()
        self._popup = popup

    def _toggle_settings_card(self):
        """打开完整设置卡片"""
        # 沿父链向上找 OpenAIChatToolWindow._toggle_settings_card
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, "_toggle_settings_card"):
                parent._toggle_settings_card()
                return
            parent = parent.parent()
        # 兜底：通过 TabManagerWindow 获取当前窗口
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm:
            current = tm.get_current_window()
            if current and hasattr(current, "_toggle_settings_card"):
                current._toggle_settings_card()

    def _on_popup_closed(self):
        """浮动卡片关闭后的清理"""
        self._popup = None
        self._popup_closed_at = time.monotonic()


# ── 浮动卡片 ──────────────────────────────────────────────


def _menu_icon_pixmap(icon_src, size: int) -> QPixmap:
    """菜单行图标 → QPixmap：字符串走主题感知资源图标，FluentIcon 枚举走动态主题图标"""
    if isinstance(icon_src, str):
        return get_icon(icon_src).pixmap(size, size)
    if hasattr(icon_src, "qicon"):
        return icon_src.qicon().pixmap(size, size)
    if hasattr(icon_src, "icon"):
        return icon_src.icon().pixmap(size, size)
    return QPixmap()


class _MenuRow(QPushButton):
    """WorkBuddy 风格菜单行：图标 + 文字 + 右侧插槽（开关/箭头），hover 圆角高亮

    借 QPushButton 的 :hover/:pressed 伪类实现高亮；文字/图标子控件
    设鼠标穿透保证整行 hover 生效。右侧交互控件（SwitchButton）自身
    消费鼠标事件，点击开关时行高亮暂时消失属预期行为。
    """

    def __init__(self, icon_src, text: str, right: QWidget | None = None, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(scale_font_size(34))
        row_layout = QHBoxLayout(self)
        row_layout.setContentsMargins(scale_font_size(12), 0, scale_font_size(12), 0)
        row_layout.setSpacing(scale_font_size(10))

        icon_lbl = QLabel(self)
        icon_lbl.setPixmap(_menu_icon_pixmap(icon_src, scale_font_size(16)))
        icon_lbl.setFixedSize(scale_font_size(16), scale_font_size(16))
        icon_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row_layout.addWidget(icon_lbl)

        text_lbl = QLabel(text, self)
        text_lbl.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(12)};"
        )
        text_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row_layout.addWidget(text_lbl)
        row_layout.addStretch(1)

        if right is not None:
            row_layout.addWidget(right)

        Colors.refresh()
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                text-align: left;
                padding: 0;
            }}
            QPushButton:hover {{ background: {Colors.HOVER_BG}; }}
            QPushButton:pressed {{ background: {Colors.SELECTED_BG}; }}
        """)


class _GiteeMorePopup(QWidget):
    """Gitee 更多操作浮动卡片 — WorkBuddy 风格悬浮菜单

    点击 GiteeAccountRow 的 ⚙ 按钮后弹出，分区结构：
    ① 账号区（头像 + 名称/仓库，点击跳仓库）+ 绑定/解绑
    ② 快捷开关：深色模式 / 窗口置顶
    ③ 快捷导航：外观 / 插件设置（直跳设置卡对应页）
    ④ 设置 / 帮助与反馈（新建会话发起 issue 提交引导）
    """

    def __init__(self, account_row: "GiteeAccountRow", parent=None):
        # NoDropShadowWindowHint：禁掉 Windows 对 popup 的系统直角投影（会在透明边距外露出套层直角框）
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._account_row = account_row
        self._cfg = account_row.cfg
        self._build_ui()

    def _build_ui(self):
        Colors.refresh()

        # ── 主容器：圆角卡片背景（阴影由 paintEvent 自绘；DropShadowEffect
        # 在 Qt.Popup + WA_TranslucentBackground 上会带出直角不透明底框，不可用）──
        self._container = QWidget(self)
        self._container.setObjectName("giteePopupContainer")
        self._container.setStyleSheet(f"""
            #giteePopupContainer {{
                background: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 12px;
            }}
        """)

        layout = QVBoxLayout(self._container)
        layout.setContentsMargins(scale_font_size(6), scale_font_size(6), scale_font_size(6), scale_font_size(6))
        layout.setSpacing(0)
        self._container.setFixedWidth(scale_font_size(252))

        # ── 账号信息区域（可点击跳转仓库） ──
        self._info_widget = QWidget(self._container)
        self._info_widget.setCursor(Qt.PointingHandCursor)
        info_layout = QHBoxLayout(self._info_widget)
        info_layout.setContentsMargins(scale_font_size(10), scale_font_size(8), scale_font_size(10), scale_font_size(8))
        info_layout.setSpacing(10)

        self._popup_avatar = _AvatarCircleWidget("?", self._info_widget)
        self._popup_avatar.set_size(scale_font_size(34))
        # 鼠标事件穿透，由 _info_widget 统一处理点击
        self._popup_avatar.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        info_layout.addWidget(self._popup_avatar)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        self._popup_name = QLabel("", self._info_widget)
        self._popup_name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(13)}; font-weight: 600;"
        )
        self._popup_name.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._popup_repo = QLabel("", self._info_widget)
        self._popup_repo.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(10)};"
        )
        self._popup_repo.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        text_col.addWidget(self._popup_name)
        text_col.addWidget(self._popup_repo)
        info_layout.addLayout(text_col, 1)
        # 整行点击打开仓库
        self._info_widget.mousePressEvent = lambda ev: self._on_info_clicked(ev)
        layout.addWidget(self._info_widget)

        # ── 绑定/解绑按钮 ──
        self._popup_action_btn = QPushButton("", self._container)
        self._popup_action_btn.setCursor(Qt.PointingHandCursor)
        self._popup_action_btn.setFixedHeight(28)
        self._popup_action_btn.clicked.connect(self._on_action_clicked)
        layout.addWidget(self._popup_action_btn, 0, Qt.AlignCenter)

        layout.addSpacing(scale_font_size(6))

        # ── 分隔线 ──
        layout.addWidget(self._make_separator())

        # ── 快捷开关：深色模式 / 窗口置顶 ──
        dark_switch = SwitchButton(self._container)
        dark_switch.setOnText("")
        dark_switch.setOffText("")
        dark_switch.label.setVisible(False)  # 只留开关本体；_updateText 会重写文字，仅 setText 压不住
        dark_switch.setChecked(not self._cfg.ui_light_mode.value)
        dark_switch.checkedChanged.connect(self._on_dark_mode_toggled)
        self._dark_mode_row = _MenuRow(FluentIcon.BRIGHTNESS, "深色模式", right=dark_switch, parent=self._container)
        layout.addWidget(self._dark_mode_row)

        topmost_switch = SwitchButton(self._container)
        topmost_switch.setOnText("")
        topmost_switch.setOffText("")
        topmost_switch.label.setVisible(False)
        topmost_switch.setChecked(bool(self._cfg.window_always_on_top.value))
        topmost_switch.checkedChanged.connect(self._on_topmost_toggled)
        self._topmost_row = _MenuRow(FluentIcon.PIN, "窗口置顶", right=topmost_switch, parent=self._container)
        layout.addWidget(self._topmost_row)
        # 保持 Python 引用，防止 SwitchButton 被提前 GC 回收
        self._switch_refs = [dark_switch, topmost_switch]

        layout.addWidget(self._make_separator())

        # ── 快捷导航：直跳设置卡对应页 ──
        self._appearance_row = _MenuRow(
            "主题风格", "外观", right=self._make_chevron(), parent=self._container
        )
        self._appearance_row.clicked.connect(lambda: self._open_settings_tab("appearance"))
        layout.addWidget(self._appearance_row)

        self._plugins_row = _MenuRow(
            FluentIcon.APPLICATION, "插件设置", right=self._make_chevron(), parent=self._container
        )
        self._plugins_row.clicked.connect(lambda: self._open_settings_tab("plugins"))
        layout.addWidget(self._plugins_row)

        layout.addWidget(self._make_separator())

        # ── 设置 / 帮助与反馈 ──
        self._open_settings_btn = _MenuRow(
            "配置管理", "设置", right=self._make_chevron(), parent=self._container
        )
        self._open_settings_btn.clicked.connect(self._on_open_settings)
        layout.addWidget(self._open_settings_btn)

        self._feedback_row = _MenuRow(
            FluentIcon.FEEDBACK, "帮助与反馈", right=self._make_chevron(), parent=self._container
        )
        self._feedback_row.clicked.connect(self._on_help_feedback)
        layout.addWidget(self._feedback_row)

        # ── 检查更新（右侧显示当前版本号，与托盘菜单同款）──
        version_lbl = QLabel(str(self._cfg.current_version or ""), self._container)
        version_lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)};"
        )
        version_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._update_row = _MenuRow(FluentIcon.UPDATE, "检查更新", right=version_lbl, parent=self._container)
        self._update_row.clicked.connect(self._on_check_update)
        layout.addWidget(self._update_row)

        # 阴影透明边距：投影画在容器外（popup 自身为透明层）；收紧边距让卡片贴近窗口边角
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(
            scale_font_size(5), scale_font_size(5), scale_font_size(5), scale_font_size(7)
        )
        main_layout.addWidget(self._container)

        # 刷新账号状态
        self._refresh_account_state()

    def _make_separator(self) -> QWidget:
        """带左右留白的分隔线（全宽直顶圆角边不够精致）"""
        wrap = QWidget(self._container)
        wrap.setStyleSheet("background: transparent;")
        wrap_layout = QVBoxLayout(wrap)
        wrap_layout.setContentsMargins(scale_font_size(10), 0, scale_font_size(10), 0)
        sep = QFrame(wrap)
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {Colors.DIVIDER_COLOR}; border: none;")
        wrap_layout.addWidget(sep)
        return wrap

    def _make_chevron(self) -> QLabel:
        """导航行右侧箭头"""
        lbl = QLabel("›", self._container)
        lbl.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(14)};"
        )
        lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        return lbl

    def _refresh_account_state(self):
        """根据当前绑定状态刷新账号信息区域"""
        is_bound = bool(self._cfg.gitee_bound.value)
        owner = str(self._cfg.gitee_user_owner.value or "")
        repo = str(self._cfg.gitee_user_repo.value or "")
        avatar_size = scale_font_size(34)

        if is_bound and owner:
            self._popup_avatar.set_avatar(owner)
            self._popup_avatar.set_size(avatar_size)
            hint = f"点击打开 {owner}/{repo}"
            self._info_widget.setToolTip(hint)
            self._popup_name.setText(owner)
            self._popup_repo.setText(f"{repo} ↗")
            self._popup_action_btn.setText("解绑账号")
            self._popup_action_btn.setStyleSheet(f"""
                QPushButton {{
                    color: {Colors.ERROR};
                    background: transparent;
                    border: 1px solid {Colors.ERROR};
                    border-radius: 6px;
                    padding: 4px 20px;
                    {get_font_family_css()} {font_size_css(12)};
                }}
                QPushButton:hover {{
                    background: rgba(250, 81, 81, 0.10);
                }}
            """)
        else:
            self._popup_avatar.set_avatar("?")
            self._popup_avatar.set_size(avatar_size)
            self._popup_avatar.setToolTip("未绑定")
            self._popup_name.setText("未绑定 Gitee")
            self._popup_repo.setText("绑定后可备份与分享")
            self._popup_action_btn.setText("绑定账号")
            self._popup_action_btn.setStyleSheet(f"""
                QPushButton {{
                    color: #ffffff;
                    background: {Colors.INFO};
                    border: none;
                    border-radius: 6px;
                    padding: 4px 20px;
                    {get_font_family_css()} {font_size_css(12)};
                }}
                QPushButton:hover {{
                    background: {Colors.INFO};
                }}
            """)

    def _on_info_clicked(self, event):
        """点击账号信息区域 → 打开仓库"""
        if event.button() == Qt.LeftButton and self._account_row:
            self._account_row._open_repository()

    def _on_action_clicked(self):
        """点击浮动卡片中的绑定/解绑按钮"""
        if self._cfg.gitee_bound.value:
            self._account_row._on_unbind()
        else:
            self._account_row._on_bind()
        # 延迟关闭：_on_unbind/_on_bind 中的模态对话框可能已导致
        # Qt 自动关闭 Qt.Popup 并触发 WA_DeleteOnClose 销毁 C++ 对象，
        # 同步调用 self.close() 会触发 RuntimeError，故延迟到下一帧关闭。
        QTimer.singleShot(0, self.close)

    def _on_open_settings(self):
        """打开设置卡片（默认页）"""
        self.close()
        if self._account_row:
            self._account_row._toggle_settings_card()

    def _open_settings_tab(self, tab_id: str):
        """直跳设置卡并切换到指定导航页（appearance/plugins 等）"""
        self.close()
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc.open_settings(tab_id)
            return
        # 兜底：控制器不可用时走原 toggle 链（无指定页）
        if self._account_row:
            self._account_row._toggle_settings_card()

    def _on_help_feedback(self):
        """帮助与反馈：新建会话并自动发起 issue 提交引导（issue-reporter 技能）"""
        self.close()
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm is None:
            return
        source = tm.get_current_window()
        if source is None and self._account_row is not None:
            source = self._account_row.window()
        if source is None:
            return
        new_win = tm.spawn_tab(source, new_session=True)
        if new_win is None:
            return
        # 等新 tab 就绪再预填输入框（不自动发送，用户改完自己发）；经类名调用避免捕获已销毁的 self
        QTimer.singleShot(300, lambda w=new_win: _GiteeMorePopup._send_feedback_prompt(w))

    def _on_check_update(self):
        """检查更新：手动触发，结果 InfoBar 落地在 TabManagerWindow（与托盘同款）"""
        self.close()
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm is None:
            return
        try:
            from app.update_checker import UpdateChecker

            UpdateChecker.get_instance(tm).check_update()
        except Exception as e:
            logger.warning(f"[gitee-popup] 检查更新失败: {e}")

    @staticmethod
    def _send_feedback_prompt(win):
        """把 issue 提交引导 prompt 预填到新窗口输入框，由用户修改后自行发送"""
        try:
            if getattr(win, "_is_destroyed", False):
                return
            prompt = (
                "我想给 DriFox 提交一个 issue（反馈 bug / 建议）。"
                "请使用 issue-reporter 技能：先自动收集环境信息（版本、系统、最近错误日志），"
                "再引导我补充问题描述，与我确认后提交到 GitHub 仓库，并把 issue 链接贴给我。"
            )
            win.input_area.setPlainText(prompt)
            win.input_area.setFocus()
        except Exception:
            pass

    # ── 快捷开关回调 ──

    def _on_dark_mode_toggled(self, checked: bool):
        """深色模式切换

        完整刷新链（ui_light_mode.valueChanged → LLMSettingsCard._on_light_mode_changed
        → ui_theme_style → configChanged → 全量刷新）依赖懒构建设置卡；本次启动
        未打开过设置页时无人监听 valueChanged，界面不会切换。此处兜底：设置卡
        未构建时直写目标主题并 dispatch_refresh 显式全量刷新（与 config_sync 同款）。
        """
        is_light = not checked
        self._cfg.ui_light_mode.value = is_light
        self._cfg.save()

        from app.widgets.cards.global_card_controller import get_global_card_controller

        controller = get_global_card_controller()
        if controller is not None and controller._settings_popup is not None:
            # 设置卡已构建 → valueChanged 链自动完成主题切换，不重复触发
            return

        from app.utils.config import update_theme_options
        from app.utils.theme_manager import theme_manager

        # 写 ui_theme_style 前先刷新 validator 选项集，防止目标主题未注册时
        # 被 OptionsValidator.correct() 静默回退到 options[0]
        update_theme_options()
        target_theme = "lumia" if is_light else "fallout"
        if self._cfg.ui_theme_style.value != target_theme:
            self._cfg.set(self._cfg.ui_theme_style, target_theme, save=True)
        theme_manager.dispatch_refresh()

    def _on_topmost_toggled(self, checked: bool):
        """窗口置顶开关切换

        统一走 _apply_window_topmost：macOS 走软置顶（不加
        WindowStaysOnTopHint，避免最小化按钮被系统丢弃），其他平台
        加/摘 WS_EX_TOPMOST hint。

        注意：self.window() 返回的是 popup 自身（Qt.Popup 自带 Window 标志），
        必须通过 _account_row 的父链才能获取真正的应用顶层窗口（TabManagerWindow / OpenAIChatToolWindow）
        """
        self._cfg.window_always_on_top.value = checked
        self._cfg.save()

        window = self._account_row.window() if self._account_row else None
        if not window:
            return

        from app.widgets.tab_manager_window import _apply_window_topmost

        _apply_window_topmost(window)

    # ── 自绘柔和阴影（画在卡片下层的透明边距内）──

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        Colors.refresh()
        margin = scale_font_size(5)
        radius = scale_font_size(12)
        # 与 main_layout 边距一致（底部多 2px 给下沉阴影），container 覆盖中部露出外环
        card = self.rect().adjusted(margin, margin, -margin, -(margin + scale_font_size(2)))
        painter.setPen(Qt.NoPen)
        for grow, alpha in ((4, 8), (3, 12), (2, 16), (1, 22)):
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(card.adjusted(-grow, -grow, grow, grow), radius + grow, radius + grow)

    def closeEvent(self, event):
        if self._account_row:
            self._account_row._on_popup_closed()
        super().closeEvent(event)


# ── 卡片 ──────────────────────────────────────────────────


class GiteeCard(SettingCard):
    """Gitee 账号绑定 — SettingCard 子类，布局与其他设置卡片一致"""

    oauthResult = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(
            get_icon("gitee"),
            "Gitee 账号绑定",
            "云端备份配置、分享会话",
            parent,
        )
        self.cfg = Settings.get_instance()
        self._binding = False
        self._bound_owner = ""
        self._bound_repo = ""
        self.oauthResult.connect(self._on_oauth_result)

        self._setup_right()
        self._setup_sync_indicator()
        self._connect_config_signals()
        self._refresh_ui()

    def _connect_config_signals(self):
        """监听绑定配置变化（外部清绑/自动解绑/其他入口绑定）→ 刷新卡片按钮

        与 GiteeAccountRow 保持一致：绑定状态可能由 ConfigSync 自动清绑、
        GiteeAccountRow 解绑、GiteeOAuthBackend.unbind 等入口修改，
        卡片需同步响应，否则按钮停留在「解绑」。
        """
        for item in (
            self.cfg.gitee_bound,
            self.cfg.gitee_user_owner,
            self.cfg.gitee_user_repo,
        ):
            try:
                item.valueChanged.disconnect(self._on_config_changed)
            except TypeError:
                pass
            item.valueChanged.connect(self._on_config_changed)

    def _on_config_changed(self, _value=None):
        """配置变化（gitee 绑定态/owner/repo）→ 刷新卡片 UI"""
        self._refresh_ui()

    def _setup_right(self):
        avatar_size = scale_font_size(28)
        self._avatar = _AvatarCircleWidget("?", self)
        self._avatar.set_size(avatar_size)
        self._avatar.clicked.connect(self._on_avatar_clicked)
        self.hBoxLayout.addWidget(self._avatar)

        self.hBoxLayout.addSpacing(6)

        self._bind_btn = QPushButton("绑定")
        self._bind_btn.setFixedWidth(76)
        self._bind_btn.setMinimumHeight(30)
        self._bind_btn.setCursor(Qt.PointingHandCursor)
        self._bind_btn.setStyleSheet(
            f"QPushButton {{"
            f"background-color: #0078d4; color: #ffffff; border: none;"
            f"border-radius: 5px; padding: 5px 16px; {font_size_css(13)}"
            f"font-weight: bold;"
            f"}}"
            f"QPushButton:hover {{ background-color: {Colors.BORDER_ACCENT}; }}"
            f"QPushButton:pressed {{ background-color: {Colors.SELECTED_BG}; }}"
            f"QPushButton:disabled {{ background-color: #444; color: #888; }}"
        )
        self._bind_btn.clicked.connect(self._on_bind_clicked)
        self.hBoxLayout.addWidget(self._bind_btn)

        self.hBoxLayout.addSpacing(4)

    def _setup_sync_indicator(self):
        """同步状态指示圆点（显示在按钮右侧）"""
        dot_size = scale_font_size(7)
        self._sync_dot = QLabel()
        self._sync_dot.setFixedSize(dot_size, dot_size)
        self._sync_dot.hide()
        self.hBoxLayout.addWidget(self._sync_dot)
        self.hBoxLayout.addSpacing(2)

        from app.core.sync.config_sync import ConfigSyncService

        self._sync_svc = ConfigSyncService.get_instance()
        # 先断开旧连接，防止 GiteeCard 重建（如设置面板关闭再打开）导致重复连接
        try:
            self._sync_svc.stateChanged.disconnect(self._on_sync_state_changed)
        except TypeError:
            pass
        try:
            self._sync_svc.syncDone.disconnect(self._on_initial_sync_done)
        except TypeError:
            pass
        self._sync_svc.stateChanged.connect(self._on_sync_state_changed)
        self._sync_svc.syncDone.connect(self._on_initial_sync_done)

    # ── UI 刷新 ──────────────────────────────────────────

    def _refresh_ui(self):
        is_bound = self.cfg.gitee_bound.value
        owner = self.cfg.gitee_user_owner.value
        repo = self.cfg.gitee_user_repo.value
        avatar_size = scale_font_size(28)

        if is_bound and owner:
            self._bound_owner = owner
            self._bound_repo = repo
            self._avatar.set_avatar(owner)
            self._avatar.set_size(avatar_size)
            self._avatar.setToolTip(f"点击打开仓库 {owner}/{repo}")
            self._bind_btn.setText("解绑")
            self._bind_btn.setCursor(Qt.PointingHandCursor)
            self._bind_btn.setStyleSheet(
                f"QPushButton {{"
                f"color: #fa5151; border: 1px solid #fa5151; border-radius: 6px;"
                f"padding: 5px 12px; font-size: {scale_font_size(12)}px;"
                f"background: transparent;"
                f"}}"
                f"QPushButton:hover {{ background-color: rgba(250, 81, 81, 0.1); }}"
            )
            # 已绑定 → 自动启动同步（如尚未启动）
            self._auto_enable_sync()
        else:
            self._bound_owner = ""
            self._bound_repo = ""
            self._avatar.set_avatar("?")
            self._avatar.set_size(avatar_size)
            self._avatar.setToolTip("未绑定")
            self._bind_btn.setText("绑定")
            self._bind_btn.setCursor(Qt.PointingHandCursor)
            self._bind_btn.setStyleSheet(
                f"QPushButton {{"
                f"background-color: #0078d4; color: #ffffff; border: none;"
                f"border-radius: 5px; padding: 5px 16px; {font_size_css(13)}"
                f"font-weight: bold;"
                f"}}"
                f"QPushButton:hover {{ background-color: {Colors.BORDER_ACCENT}; }}"
                f"QPushButton:pressed {{ background-color: {Colors.SELECTED_BG}; }}"
                f"QPushButton:disabled {{ background-color: #444; color: #888; }}"
            )
            self._sync_dot.hide()

    def _auto_enable_sync(self):
        """如果已绑定且同步未启动，自动 enable（重启恢复场景）"""
        if self._sync_svc._state != "disabled":
            return
        # 通过 OAuth 后端获取有效 token（支持自动刷新）
        from app.gateway.auth import get_oauth_backend

        bound_info = get_oauth_backend("gitee").get_bound_info()
        if bound_info and bound_info.get("token") and bound_info.get("owner"):
            logger.info("[GiteeCard] 检测到已绑定，自动启动配置同步")
            self._sync_svc.enable(bound_info["token"], bound_info["owner"])
        elif bool(self.cfg.gitee_bound.value):
            # ★ T8B 断点① 修复：get_bound_info 返回 None 但 cfg 仍标记已绑定
            # （refresh_token 被吊销，_ensure_valid_token 返回 None）→ 之前静默
            # 跳过导致 ConfigSync 永不启动、syncDone 永不发。此处主动执行失效处理。
            logger.warning("[GiteeCard] 绑定已失效（token 无法刷新），清除绑定并提示重新绑定")
            self._handle_binding_invalid()

    def _handle_binding_invalid(self):
        """绑定失效处理：清绑定 + 账户行失效红标 + 全局失效提醒"""
        # 1) 清除本地绑定（等效 ConfigSync 清绑逻辑）
        try:
            self.cfg.gitee_bound.value = False
            self.cfg.gitee_user_token.value = ""
            self.cfg.gitee_user_refresh_token.value = ""
            self.cfg.gitee_token_expires_at.value = 0.0
            self.cfg.save()
        except Exception as e:
            logger.warning(f"[GiteeCard] 清除绑定失败: {e}")

        # 2) 通知各窗口的 GiteeAccountRow 置失效红标
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is not None:
                for win in list(getattr(tm, "_windows", []) or []):
                    panel = getattr(win, "_tab_panel", None)
                    if panel is None:
                        continue
                    row = getattr(panel, "_gitee_account_row", None)
                    if row is not None:
                        row.set_invalid(True)
        except Exception as e:
            logger.warning(f"[GiteeCard] 通知账户行失效失败: {e}")

        # 3) 全局失效提醒（去重/开关守卫在 controller 内）
        try:
            from app.widgets.cards.global_card_controller import get_global_card_controller

            ctrl = get_global_card_controller()
            if ctrl is not None:
                ctrl.check_gitee_token_invalid_reminder()
        except Exception as e:
            logger.warning(f"[GiteeCard] 触发失效提醒失败: {e}")

    # ── 同步状态指示 ─────────────────────────────────────

    def _on_sync_state_changed(self, state: str):
        """ConfigSyncService 状态变更 → 更新圆点颜色"""
        dot_size = scale_font_size(7)
        if state == "disabled":
            self._sync_dot.hide()
        elif state == "idle":
            self._sync_dot.setStyleSheet(f"background: #3fb950; border-radius: {dot_size // 2}px;")
            self._sync_dot.setToolTip("同步正常")
            self._sync_dot.show()
        elif state == "syncing":
            self._sync_dot.setStyleSheet(f"background: #58a6ff; border-radius: {dot_size // 2}px;")
            self._sync_dot.setToolTip("正在同步…")
            self._sync_dot.show()
        elif state == "error":
            self._sync_dot.setStyleSheet(f"background: #f85149; border-radius: {dot_size // 2}px;")
            self._sync_dot.setToolTip("同步失败，点击重试")
            self._sync_dot.setCursor(Qt.PointingHandCursor)
            self._sync_dot.mousePressEvent = self._on_sync_retry
            self._sync_dot.show()

    def _on_sync_retry(self, event):
        """点击红点重试"""
        import threading

        t = threading.Thread(target=self._sync_svc.upload, daemon=True)
        t.start()

    def _on_initial_sync_done(self, success: bool, message: str):
        """首次同步完成回调（仅绑定后首次检查远端时触发）"""
        if not success:
            # 失败时由状态机显示红点，不弹 InfoBar
            return

        # 远端配置已下载并覆盖本地。
        # 不再调用 _refresh_app_ui() — Settings.load() 已在主线程由
        # _reload_settings_on_main_thread 执行，所有 ConfigItem 的
        # valueChanged 信号已同步分发给 UI 槽，UI 已自然更新。
        # 移除额外的全量刷新避免了 findChildren 遍历全窗口树的 6s+ 卡顿。
        # 静默同步成功，不弹 InfoBar（避免启动时打扰）

    def _refresh_app_ui(self):
        """配置恢复后刷新整个 UI：通过标准配置变更链路逐窗口刷新"""
        try:
            # QTimer 回调可能在窗口已销毁后触发（极端情况：100ms 内关闭窗口）
            main_win = self.window()
            if main_win and getattr(main_win, "_is_destroyed", False):
                return


            # 1. 通过标准配置变更路径逐窗口刷新（与用户手动更改设置走同一路径）
            #    _apply_runtime_ui_settings 内部：
            #      - Colors.refresh() → 重算颜色 token
            #      - 按 scope 精准刷新 widget 树（message card / settings card / 圆环等）
            #      - 不触发 dispatch_refresh() 的全量 refresh_theme() 调用
            for win in window_registry.alive_window_instances():
                if getattr(win, "_is_destroyed", False):
                    continue
                try:
                    win._apply_runtime_ui_settings(scope=None)
                except Exception as e:
                    logger.warning(f"[GiteeCard] 窗口 {win._window_id} 刷新失败: {e}")

            # 2. 销毁全局设置弹窗：下次打开时重建，
            #    所有子卡片（provider/MCP/gateway/font 等）从 Settings 读取最新值
            from app.widgets.cards.global_card_controller import get_global_card_controller

            cc = get_global_card_controller()
            if cc is not None:
                cc.invalidate_settings_popup()

            logger.info("[GiteeCard] UI 已根据恢复的配置全面刷新")
        except Exception as e:
            logger.warning(f"[GiteeCard] UI 刷新失败: {e}")

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

        dialog = _RepoVisibilityDialog(self.window())
        dialog.chosen.connect(self._start_oauth_with_backup)
        dialog.exec_()

    def _start_oauth_with_backup(self, repo_private: bool):
        """绑定前先备份本地配置"""
        from app.core.sync.config_sync import ConfigSyncService

        ConfigSyncService.get_instance().backup_local()
        self._start_oauth(repo_private)

    def _start_oauth(self, repo_private: bool):
        self._binding = True
        self._bind_btn.setText("授权中…")
        self._bind_btn.setEnabled(False)

        t = threading.Thread(target=self._do_oauth, args=(repo_private,), daemon=True)
        t.start()

    def _do_oauth(self, repo_private: bool):
        try:
            from app.gateway.auth import get_oauth_backend

            success, msg = get_oauth_backend("gitee").bind(repo_private=repo_private)
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
            # _refresh_ui() → _auto_enable_sync() 已调 enable()，无需重复调用

            InfoBar.success(
                title="绑定成功",
                content=msg,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        else:
            self._bind_btn.setText("绑定")
            InfoBar.error(
                title="绑定失败",
                content=msg,
                position=InfoBarPosition.TOP_RIGHT,
                duration=5000,
                parent=self.window(),
            )

    # ── 解绑 ─────────────────────────────────────────────

    def _on_unbind(self):
        owner = self.cfg.gitee_user_owner.value
        dialog = ConfirmDialog(
            title="确认解绑",
            content=f"解绑后上传将恢复使用共享图床仓库。\n当前绑定：{owner}",
            confirm_text="确定解绑",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(self._do_unbind)
        dialog.exec_()

    def _do_unbind(self):
        try:
            from app.gateway.auth import get_oauth_backend
            from app.gateway.utils.gitee_uploader import GiteeUploader

            # 先停止同步，再解绑
            self._sync_svc.disable()
            get_oauth_backend("gitee").unbind()
            GiteeUploader.get_instance().reset_config()

            # 恢复绑定前的本地配置
            if self._sync_svc.restore_local():
                try:
                    self.cfg.load()
                except Exception:
                    pass

            self._refresh_ui()
            InfoBar.success(
                title="已解绑",
                content="Gitee 账号已解绑，配置已恢复",
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
