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
from PyQt5.QtCore import Qt, pyqtSignal, QRectF, QTimer
from PyQt5.QtGui import QColor, QMouseEvent, QPainter, QPixmap
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from qfluentwidgets import InfoBar, InfoBarPosition, MaskDialogBase, SettingCard

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css, get_icon, get_unified_font
from app.widgets.common_dialogs import ConfirmDialog

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

        self.widget.setFixedSize(400, 320)
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
        self._refresh_ui()

    def _setup_right(self):
        avatar_size = scale_font_size(32)
        self._avatar = _ClickableAvatar()
        self._avatar.setFixedSize(avatar_size, avatar_size)
        self._avatar.setCursor(Qt.PointingHandCursor)
        self._avatar.setAlignment(Qt.AlignCenter)
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

        from app.core.config_sync import ConfigSyncService

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
        avatar_size = scale_font_size(32)

        if is_bound and owner:
            self._bound_owner = owner
            self._bound_repo = repo
            self._avatar.setPixmap(_make_avatar_pixmap(owner, avatar_size))
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
            self._avatar.setPixmap(_make_avatar_pixmap("?", avatar_size))
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
        token = self.cfg.gitee_user_token.value
        owner = self.cfg.gitee_user_owner.value
        if token and owner:
            logger.info("[GiteeCard] 检测到已绑定，自动启动配置同步")
            self._sync_svc.enable(token, owner)

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

        # 远端配置已下载并覆盖本地，通过标准配置变更链路刷新全窗口 UI。
        # 使用 _apply_runtime_ui_settings 替代 dispatch_refresh()：
        #   - 走与用户手动改设置相同的刷新路径，确保配置值被正确传播
        #   - 30ms debounce 批处理避免重复刷新
        #   - 不再需要 2s 延迟（旧方案用 dispatch_refresh 会导致窗口创建期闪烁）
        QTimer.singleShot(100, self._refresh_app_ui)
        # 静默同步成功，不弹 InfoBar（避免启动时打扰）

    def _refresh_app_ui(self):
        """配置恢复后刷新整个 UI：通过标准配置变更链路逐窗口刷新"""
        try:
            # QTimer 回调可能在窗口已销毁后触发（极端情况：100ms 内关闭窗口）
            main_win = self.window()
            if main_win and getattr(main_win, "_is_destroyed", False):
                return

            from app.main_widget import OpenAIChatToolWindow

            # 1. 通过标准配置变更路径逐窗口刷新（与用户手动更改设置走同一路径）
            #    _apply_runtime_ui_settings 内部：
            #      - Colors.refresh() → 重算颜色 token
            #      - 按 scope 精准刷新 widget 树（message card / settings card / 圆环等）
            #      - 不触发 dispatch_refresh() 的全量 refresh_theme() 调用
            for win in getattr(OpenAIChatToolWindow, "_instances", []):
                if getattr(win, "_is_destroyed", False):
                    continue
                try:
                    win._apply_runtime_ui_settings(scope=None)
                except Exception as e:
                    logger.warning(f"[GiteeCard] 窗口 {win._window_id} 刷新失败: {e}")

            # 2. 关闭设置弹窗：下次 _open_settings_popup 会因 _settings_popup=None 而重建，
            #    所有子卡片（provider/MCP/gateway/font 等）从 Settings 读取最新值
            if main_win and hasattr(main_win, "_settings_popup"):
                popup = main_win._settings_popup
                if popup is not None and popup.isVisible():
                    if hasattr(main_win, "_card_manager") and hasattr(main_win, "_window_id"):
                        main_win._card_manager.hide_card("settings", main_win._window_id)
                main_win._settings_popup = None

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
        from app.core.config_sync import ConfigSyncService

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
            from app.gateway.utils.gitee_oauth import unbind_account
            from app.gateway.utils.gitee_uploader import GiteeUploader

            # 先停止同步，再解绑
            self._sync_svc.disable()
            unbind_account()
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
