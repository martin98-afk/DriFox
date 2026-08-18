# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import ctypes
import gc
import heapq
import os
import psutil
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import orjson as json
import sip
from loguru import logger
from PyQt5.QtCore import (
    QEvent,
    QEventLoop,
    QFileSystemWatcher,
    QObject,
    QRunnable,
    Qt,
    QThread,
    QThreadPool,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtGui import QColor, QDesktopServices, QPainter
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    IconWidget,
    InfoBadge,
    InfoBadgePosition,
    InfoBar,
    InfoBarPosition,
    MaskDialogBase,
    PushButton,
    SingleDirectionScrollArea,
    TransparentToolButton,
    setFont,
)

from app.constants import (
    FREE_PROVIDERS,
    IMAGE_EXTENSIONS,
    MODEL_LEVEL_KEYS,
    PROVIDER_ICONS,
    QUOTA_EXCLUDE_KEYS,
    get_merged_provider_models,
)
from app.core import (
    ChatBackend,
    ChatSession,
    TopicSummaryTask,
    consolidate_messages,
    content_to_text,
    get_user_round_ranges,
    group_messages_for_display,
)
from app.core.builtin_commands import FunctionCommandHandlers
from app.core.command_manager import CommandManager, CommandType
from app.core.model_capabilities import apply_model_defaults, get_model_capabilities
from app.core.tool_permission_controller import ToolPermissionController


# [PERF] get_tool_counts 已移入 _refresh_tool_toggle_btn 方法内，避免模块加载时触发 app.tools 导入
from app.utils.config import Settings, update_theme_options
from app.utils.design_tokens import (
    Colors,
    apply_font_size_to_widget,
    font_size_css,
    scale_font_size,
    scale_icon_size,
)
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_font_family_css, get_icon

# ── App Widget 导入 ──
# Note: 保留模块级导入而非方法内导入，因为 widget 类型在 100+ 方法中通过 isinstance 引用，
# 方法级导入无法跨方法共享。仅将重型导入 app.tools.tool_classifier 移入方法。
from app.widgets.balance_display import BalanceDisplay
from app.widgets.bottom_input_area import (
    AttachmentChip,
    InputGlowUnderlay,
    SendableTextEdit,
)
from app.widgets.pixel_pet import PixelPetWidget

# TabManagerWindow 延迟导入：133 处 InfoBar parent 统一表达式（get_instance() or self.window()）
# 需要函数内也可用，模块级导入一次即可。tab_manager_window 顶层仅依赖 app.utils.*，
# 不反向依赖 main_widget，无循环导入风险；try/except 仅作防御（理论上不可达）。
try:
    from app.widgets.tab_manager_window import TabManagerWindow
except Exception:  # noqa: BLE001
    logger.warning("[MainWidget] TabManagerWindow 导入失败，InfoBar parent 回退 self.window()")

from app.widgets.cards import (
    BottomCardContainer,
    CardManager,
    ContainerType,
    TopCardContainer,
)
from app.widgets.cards.floating.command_card import CommandCard
from app.widgets.cards.floating.file_mention_card import FileMentionCard
from app.widgets.cards.floating.question_floating_widget import (
    QuestionFloatingWidget,
)
from app.widgets.cards.floating.sub_agent_compact_widget import (
    SubAgentCompactFloatingWidget,
)
from app.widgets.cards.floating.history_questions_card import HistoryQuestionsCardContent
from app.widgets.cards.floating.share_card import ShareCardContent
from app.widgets.cards.floating.todo_floating_widget import (
    TodoFloatingWidget,
)
from app.widgets.cards.floating.undo_delete_card import UndoDeleteCard
from app.widgets.cards.settings.base_settings_card import (
    BaseSettingsCard,
)
from app.widgets.cards.settings.history_card import (
    HistoryCard,
    get_message_preview,
)
from app.widgets.cards.settings.memory_card import (
    TAB_KEY_DOCUMENTS,
    MemoryCardContent,
)
from app.widgets.cards.settings.model_config_card import (
    ModelConfigCard,
)
from app.widgets.cards.settings.model_selector_card import (
    ModelSelectorCardContent,
    _format_cost_number,
)
from app.widgets.cards.settings.project_selector_card import (
    ProjectSelectorCardContent,
    _SquareAvatar,
    extract_project_initials,
    get_project_color,
)
from app.widgets.cards.settings.hook_setting_card import HookEditCard
from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard
from app.widgets.cards.settings.mcp_setting_card import MCPEditCard
from app.widgets.cards.settings.provider_edit_card import ProviderEditCard
from app.widgets.cards.settings.system_card_frame import SystemCardFrame
from app.widgets.cards.settings.tool_control_card import ToolControlCardFrame
from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget
from app.widgets.coding_plan_ring import (
    CodingPlanRing,
)
from app.widgets.context_usage_ring import (
    ContextUsageRing,
)
from app.widgets.conversation_node_preview import (
    ConversationNodePreview,
)
from app.widgets.cards.settings.file_undo_card import FileUndoCard
from app.widgets.file_undo_dialog import (
    FileUndoPreviewDialog,
)
from app.widgets.message_card import (
    MessageCard,
    clear_global_render_cache,
    create_welcome_card,
    resolve_initial_welcome_mode,
)
from app.widgets.simple_hover_tooltip import install_hover_tooltip, batch_install_hover_tooltips
from app.widgets.ui_helpers import *
from app.widgets.ui_helpers import (
    add_message_to_layout,
    build_node_preview_from_session,
    clear_and_show_welcome,
    create_assistant_card_widget,
    delete_widgets_from_layout,
    find_last_tool_call_id_after_round,
    find_user_round_index,
    get_first_file_operation,
    init_after_loading_session,
    init_new_session_after_archive,
    invalidate_session_card_cache,
    log_deletion_stats,
    post_append_user_message,
    refresh_history_card_if_visible,
    refresh_session_view,
    render_batch_to_assistant_card,
    restore_input_from_card,
    save_or_archive_session,
    scroll_to_bottom_if_streaming,
    setup_user_card_signals,
    show_diff_viewer,
    truncate_and_remove_round,
)


# ───────────────────────────────────────────────────────────────────────────
# 项目 icon tooltip 异步分支检测
# ───────────────────────────────────────────────────────────────────────────
class _BranchDetectSignals(QObject):
    """后台 git 分支检测的信号桥接（后台线程 → 主线程）。"""

    finished = pyqtSignal(int, str)  # request_id, branch_name（空字符串=无分支/出错）


class _BranchDetectTask(QRunnable):
    """异步 git 分支检测 worker。

    设计动机：
    - 旧实现 `subprocess.run(['git','branch','show-current'], timeout=3)`
      阻塞主线程 0~3s，期间 tooltip 不更新（更新在函数末尾）
    - 改为 QRunnable 后，tooltip 立即显示「项目名+路径」，
      分支在后台完成后追加
    - request_id 用于丢弃过期结果（用户连续切换项目时旧检测自动失效）
    """

    def __init__(self, workdir: str, request_id: int, signals: "_BranchDetectSignals"):
        super().__init__()
        self._workdir = workdir
        self._request_id = request_id
        self._signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        branch = ""
        try:
            if not self._workdir or not os.path.isdir(self._workdir):
                pass
            else:
                from app.utils.git_worktree import GitWorktreeDetector

                git_root = GitWorktreeDetector.detect_git(self._workdir)
                if git_root:
                    r = subprocess.run(
                        ["git", "branch", "--show-current"],
                        capture_output=True,
                        text=True,
                        cwd=self._workdir,
                        timeout=3,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    if r.returncode == 0:
                        branch = r.stdout.strip()
        except Exception:
            pass
        try:
            self._signals.finished.emit(self._request_id, branch)
        except Exception:
            # signals 可能在窗口销毁时被 GC，直接丢弃
            pass


class _ProjectUrlImportThread(QThread):
    """后台线程：从 URL 下载 .drifox_project 项目压缩包，避免 UI 冻结。

    finished 信号携带 (file_path, error)，二者有且仅有一个非空。
    """

    finished = pyqtSignal(str, str)

    def __init__(self, url: str):
        super().__init__()
        self._url = url

    def run(self):
        import tempfile

        try:
            import requests

            resp = requests.get(self._url, timeout=60)
            if resp.status_code != 200:
                self.finished.emit("", f"下载失败 (HTTP {resp.status_code})")
                return

            tmp = tempfile.NamedTemporaryFile(
                suffix=".drifox_project",
                prefix="drifox_import_",
                delete=False,
            )
            tmp.write(resp.content)
            tmp_path = tmp.name
            tmp.close()

            self.finished.emit(tmp_path, "")
        except requests.exceptions.Timeout:
            self.finished.emit("", "下载超时 (60s)")
        except requests.exceptions.ConnectionError:
            self.finished.emit("", "网络连接失败")
        except Exception as e:
            self.finished.emit("", f"下载失败: {e}")


class _ProjectExportThread(QThread):
    """后台线程：导出项目为 .drifox_project 压缩包，避免大项目 UI 冻结。

    exportDone 信号携带 (zip_path, error)，二者有且仅有一个非空。
    """

    exportDone = pyqtSignal(str, str)

    def __init__(self, history_manager, project_name: str, root_dir: str):
        super().__init__()
        self._hm = history_manager
        self._project_name = project_name
        self._root_dir = root_dir

    def run(self):
        try:
            zip_path = self._hm.export_project_archive(self._project_name, self._root_dir)
            if zip_path:
                self.exportDone.emit(zip_path, "")
            else:
                self.exportDone.emit("", f"项目「{self._project_name}」无会话或导出异常")
        except Exception as e:
            self.exportDone.emit("", str(e))


class _ProjectUploadThread(QThread):
    """后台线程：上传 .drifox_project 文件到 Gitee，避免 UI 冻结。

    finished 信号携带 (url, error)，二者有且仅有一个非空。
    """

    finished = pyqtSignal(str, str)

    def __init__(self, zip_path: str):
        super().__init__()
        self._zip_path = zip_path

    def run(self):
        try:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            uploader = GiteeUploader.get_instance()
            if not uploader.is_configured():
                self.finished.emit("", "Gitee 未配置（缺少 token/owner/repo）")
                return

            url, err = uploader.upload_file(self._zip_path)
            if err:
                self.finished.emit("", err)
            else:
                self.finished.emit(url, "")
        except Exception as e:
            self.finished.emit("", str(e))


class _ProjectExportChoiceDialog(MaskDialogBase):
    """项目导出方式选择弹框 — 导出前选择方式，而非导出后展示选项

    提供：💾 导出到本地 / 🔗 导出为URL链接
    """

    EXPORT_LOCAL = 1
    EXPORT_UPLOAD = 2

    exportChosen = pyqtSignal(int)  # 携带 EXPORT_LOCAL 或 EXPORT_UPLOAD

    def __init__(self, project_name: str, parent=None):
        super().__init__(parent)
        self._project_name = project_name
        self._init_ui()

    def _init_ui(self):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("projectExportChoiceDialog")
        self.widget.setStyleSheet(f"""
            #projectExportChoiceDialog {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)

        # 标题
        title_label = BodyLabel(f"📦 导出项目「{self._project_name}」", self.widget)
        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(16)}"
        )
        layout.addWidget(title_label)

        hint_label = CaptionLabel("选择导出方式：", self.widget)
        hint_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)}; padding-left: 2px;"
        )
        layout.addWidget(hint_label)

        layout.addSpacing(4)

        # 统一按钮样式
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

        # 导出到本地
        local_btn = QPushButton("💾  导出到本地", self.widget)
        local_btn.setCursor(Qt.PointingHandCursor)
        local_btn.setFixedHeight(56)
        local_btn.setStyleSheet(btn_style)
        local_btn.clicked.connect(lambda: (self.close(), self.exportChosen.emit(self.EXPORT_LOCAL)))
        layout.addWidget(local_btn)

        local_hint = CaptionLabel("直接导出到默认路径，自动打开文件夹", self.widget)
        local_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)}; padding-left: 4px;"
        )
        layout.addWidget(local_hint)

        # 导出为URL链接
        url_btn = QPushButton("🔗  导出为URL链接", self.widget)
        url_btn.setCursor(Qt.PointingHandCursor)
        url_btn.setFixedHeight(56)
        url_btn.setStyleSheet(btn_style)
        url_btn.clicked.connect(lambda: (self.close(), self.exportChosen.emit(self.EXPORT_UPLOAD)))
        layout.addWidget(url_btn)

        url_hint = CaptionLabel("导出后自动上传到 Gitee 并复制分享链接", self.widget)
        url_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)}; padding-left: 4px;"
        )
        layout.addWidget(url_hint)

        layout.addStretch()

        # 取消按钮
        cancel_btn = PushButton("取消", self.widget)
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
        cancel_btn.clicked.connect(self.close)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        self.widget.setFixedSize(400, 320)
        self._center_widget()

    def _center_widget(self):
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center_widget()


class _ProjectImportOptionDialog(MaskDialogBase):
    """项目导入选项弹框 — 与 ImportOptionDialog 一致的 MaskDialogBase 风格

    提供：📁 从文件导入 / 🔗 从URL导入
    """

    fileImportRequested = pyqtSignal()
    urlImportRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("projectImportDialogWidget")
        self.widget.setStyleSheet(f"""
            #projectImportDialogWidget {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)

        # 标题
        title_label = BodyLabel("📦 导入项目", self.widget)
        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(16)}"
        )
        layout.addWidget(title_label)

        # 从文件导入
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

        file_btn = QPushButton("📁  从文件导入", self.widget)
        file_btn.setCursor(Qt.PointingHandCursor)
        file_btn.setFixedHeight(56)
        file_btn.setStyleSheet(btn_style)
        file_btn.clicked.connect(lambda: self._on_choose("file"))
        layout.addWidget(file_btn)

        file_hint = CaptionLabel("选择本地的 .drifox_project 项目压缩包", self.widget)
        file_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)}; padding-left: 4px;"
        )
        layout.addWidget(file_hint)

        # 从URL导入
        url_btn = QPushButton("🔗  从URL导入", self.widget)
        url_btn.setCursor(Qt.PointingHandCursor)
        url_btn.setFixedHeight(56)
        url_btn.setStyleSheet(btn_style)
        url_btn.clicked.connect(lambda: self._on_choose("url"))
        layout.addWidget(url_btn)

        url_hint = CaptionLabel("输入项目压缩包的分享链接", self.widget)
        url_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)}; padding-left: 4px;"
        )
        layout.addWidget(url_hint)

        layout.addStretch()

        # 取消按钮
        cancel_btn = PushButton("取消", self.widget)
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
        cancel_btn.clicked.connect(self.close)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        self.widget.setFixedSize(400, 340)
        self._center_widget()

    def _center_widget(self):
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center_widget()

    def _on_choose(self, choice: str):
        self.close()
        if choice == "file":
            self.fileImportRequested.emit()
        else:
            self.urlImportRequested.emit()


class _ThemedIconLabel(QWidget):
    """主题感知图标标签 — 使用 QIcon 引擎自动适配浅色/深色

    替代静态 emoji/文字图标，支持主题切换时自动更新图标颜色。
    通过 QIconEngine（_ThemeIconEngine）实现每次 paint 时按当前主题加载正确颜色。
    """

    def __init__(self, icon_name: str, size: int = 18, parent=None):
        super().__init__(parent)
        self._icon = get_icon(icon_name)
        self._icon_size = size
        self.setFixedSize(size, size)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        self._icon.paint(painter, self.rect())


def _abort_team_window(win) -> None:
    """回收建窗成功但注册/join 失败的团队窗口（幽灵窗口兜底，E1/E2 共用）。

    优先级：
    1. 若窗口已注册进 Tab 管理器 → remove_window（内部完整清理：
       _windows.pop / removeWidget / remove_tab / 断开闭包 / close /
       deleteLater；close 自动触发 closeEvent → 从 _instances 移除）
    2. remove_window 不可用/异常 → 兜底 win.close()（closeEvent 仍会清理）
    3. 以上全部失败 → 静默（日志已由调用方输出）

    Args:
        win: 待回收的窗口实例（可能未完整初始化，调用方负责防御 None）。
    """
    if win is None:
        return
    try:
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm is not None:
            tm.remove_window(win)
            return
    except Exception:
        pass
    # 兜底：未注册 Tab（_create_fresh_window 中途失败）→ 直接 close
    try:
        close_fn = getattr(win, "close", None)
        if callable(close_fn):
            close_fn()
    except Exception:
        pass


class ToolWindowTitleBar(QWidget):
    """窗口标题栏（原 app/tool_popup.py 定义，随 ToolPopupDialog 下线迁移至此）"""

    popupRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._custom_buttons = []
        self._popup_mode_buttons = []
        self._is_compact = False
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(28)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 2, 0)
        layout.setSpacing(4)

        self._icon_widget = IconWidget(self)
        self._icon_widget.setFixedSize(16, 16)

        self._title_label = QLabel(self)
        self._title_label.setObjectName("titleLabel")

        layout.addWidget(self._icon_widget)
        layout.addWidget(self._title_label)
        layout.addStretch()

        self._action_container = QWidget(self)
        self._action_container.setObjectName("actionContainer")
        self._action_layout = QHBoxLayout(self._action_container)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(3)
        layout.addWidget(self._action_container)

        # 内存显示标签
        self._memory_label = QLabel(self)
        self._memory_label.setObjectName("memoryLabel")
        self._memory_label.setFixedHeight(20)
        from app.utils.design_tokens import Colors

        self._memory_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} font-size: {scale_font_size(11)}px; "
            f"padding: 1px 4px; background-color: transparent; border: none; border-radius: 3px;"
        )
        self._memory_label.hide()  # 默认隐藏，子类可以控制显示
        layout.insertWidget(layout.indexOf(self._action_container) - 1, self._memory_label)

        # 内存刷新定时器
        self._memory_timer = QTimer(self)
        self._memory_timer.setInterval(5000)  # 5秒刷新
        self._memory_timer.timeout.connect(self._update_memory_label)
        self._memory_refreshing = False

        # 设置按钮已移除（移到主窗口内）

        self._min_btn = TransparentToolButton(get_icon("最小化"), self)
        self._min_btn.setFixedSize(28, 28)
        self._min_btn.setToolTip("最小化")

        self._popup_btn = TransparentToolButton(FluentIcon.CLOSE, self)
        self._popup_btn.setFixedSize(28, 28)
        self._popup_btn.setToolTip("关闭")
        self._popup_btn.clicked.connect(self._on_popup_clicked)

        layout.addWidget(self._min_btn)
        layout.addWidget(self._popup_btn)

        try:
            font_name = Settings.get_instance().llm_font_family.value
        except Exception:
            try:
                font_name = Settings.get_instance().canvas_font_selected.value
            except Exception:
                font_name = "Microsoft YaHei"

        # 使用主题颜色
        from app.utils.design_tokens import Colors

        Colors.refresh()
        Colors.refresh()
        title_color = Colors.TEXT_PRIMARY
        btn_hover = Colors.HOVER_BG
        border_color = Colors.BORDER

        self.setStyleSheet(f"""
            ToolWindowTitleBar {{
                background-color: {Colors.CONTENT_BG};
                border-bottom: 1px solid {border_color};
            }}
            #titleLabel {{
                color: {title_color};
                font-size: {scale_font_size(13)}px;
                font-weight: bold;
                font-family: "{font_name}";
                padding: 0 3px;
            }}
            #actionContainer {{
                background-color: transparent;
            }}
            ToolButton {{
                background-color: transparent;
                border: none;
                border-radius: 3px;
                padding: 1px;
            }}
            ToolButton:hover {{
                background-color: {btn_hover};
            }}
            ToolButton:pressed {{
                background-color: {btn_hover};
            }}
        """)

    def set_icon(self, icon):
        self._icon_widget.setIcon(icon)

    def set_title(self, title):
        self._title_label.setText(title)

    def set_title_color(self, color: str):
        """设置标题文字颜色（覆盖默认的 TEXT_PRIMARY）

        传入空字符串 '' 可清除行内颜色样式，恢复默认主题色。
        """
        if color:
            self._title_label.setStyleSheet(f"color: {color};")
        else:
            self._title_label.setStyleSheet("")

    def add_button(self, widget, stretch=0):
        self._action_layout.insertWidget(self._action_layout.count() - 2, widget, stretch=stretch)
        self._custom_buttons.append(widget)

    def insert_button(self, index, widget, stretch=0):
        self._action_layout.insertWidget(index, widget, stretch=stretch)
        self._custom_buttons.append(widget)

    def remove_button(self, widget):
        self._action_layout.removeWidget(widget)
        if widget in self._custom_buttons:
            self._custom_buttons.remove(widget)
        widget.setParent(None)

    def _on_popup_clicked(self):
        self.popupRequested.emit()

    def refresh_style(self):
        """主题/字体变更时刷新标题栏样式"""
        Colors.refresh()
        # 重新读取字体
        try:
            font_name = Settings.get_instance().llm_font_family.value
        except Exception:
            try:
                font_name = Settings.get_instance().canvas_font_selected.value
            except Exception:
                font_name = "Microsoft YaHei"

        title_color = Colors.TEXT_PRIMARY
        btn_hover = Colors.HOVER_BG
        border_color = Colors.BORDER

        # 整体标题栏样式
        self.setStyleSheet(f"""
            ToolWindowTitleBar {{
                background-color: {Colors.CONTENT_BG};
                border-bottom: 1px solid {border_color};
            }}
            #titleLabel {{
                color: {title_color};
                font-size: {scale_font_size(13)}px;
                font-weight: bold;
                font-family: "{font_name}";
                padding: 0 3px;
            }}
            #actionContainer {{
                background-color: transparent;
            }}
            ToolButton {{
                background-color: transparent;
                border: none;
                border-radius: 3px;
                padding: 1px;
            }}
            ToolButton:hover {{
                background-color: {btn_hover};
            }}
            ToolButton:pressed {{
                background-color: {btn_hover};
            }}
        """)

        # 内存标签样式（单独设置，因为其 objectName 与整体样式不冲突）
        self._memory_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} font-size: {scale_font_size(11)}px; "
            f"padding: 1px 4px; background-color: transparent; border: none; border-radius: 3px;"
        )

    def show_memory_label(self):
        """显示内存标签并开始刷新"""
        self._memory_label.show()
        # 每次显示都重新启动定时器，确保新窗口独立刷新
        self._memory_timer.stop()
        self._memory_refreshing = True
        self._update_memory_label()
        self._memory_timer.start()

    def _update_memory_label(self):
        """更新内存显示"""
        try:
            process = psutil.Process()
            mem_info = process.memory_info()
            mem_mb = mem_info.rss / (1024 * 1024)
            self._memory_label.setText(f" {mem_mb:.0f} MB ")
        except Exception:
            self._memory_label.setText(" N/A ")


class ToolWindow(QWidget):
    """工具窗口基类（原 app/tool_popup.py 定义，随 ToolPopupDialog 下线迁移至此）"""

    name: str = "Unnamed"
    icon = None

    def __init__(self, page):
        super().__init__()
        self.homepage = page
        self._title_bar = None
        self._content_widget = None

        self._init_unified_font()
        self._init_title_bar()
        self.setObjectName("OpenAIChatToolWindow")

    def _init_title_bar(self):
        if self._title_bar:
            return

        self._title_bar = ToolWindowTitleBar(self)
        self._title_bar.set_icon(self.icon)
        self._title_bar.set_title(self.name)
        self._title_bar.hide()
        self._setup_title_bar()

    def _setup_title_bar(self):
        pass

    def register_action_button(self, widget):
        if self._title_bar:
            self._title_bar.add_button(widget)

    def get_title_bar(self):
        return self._title_bar

    def _init_unified_font(self):
        try:
            font_name = Settings.get_instance().llm_font_family.value
        except Exception:
            try:
                font_name = Settings.get_instance().canvas_font_selected.value
            except Exception:
                font_name = "Microsoft YaHei"

        font = self.font()
        font.setFamily(font_name)
        self.setFont(font)

        # 只设置字体，不设置背景（背景由子类的 setup_ui 处理）
        self.setStyleSheet(f"""
            ToolWindow {{
                font-family: "{font_name}";
            }}
            QLabel, QPushButton, QLineEdit, QComboBox, QTreeWidget, QTableWidget {{
                font-family: "{font_name}";
            }}
        """)


class _ToolReloadNoticeBridge(QObject):
    """工具热重载风险通知桥：watcher 后台线程 emit → 主线程槽执行

    reloaded 由 watcher 后台线程 emit；桥在主线程创建，reloaded→notified
    跨线程自动 QueuedConnection，确保外部槽在主线程执行。
    """

    reloaded = pyqtSignal()
    notified = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.reloaded.connect(self.notified)


_tool_reload_notice_bridge: Optional[_ToolReloadNoticeBridge] = None


class OpenAIChatToolWindow(ToolWindow):
    name = "飘狐"
    icon = get_icon("drifox")
    # 所有窗口实例列表（用于广播事件）
    _instances: List[OpenAIChatToolWindow] = []
    session_manager = None
    _valid_configs: Dict[str, Dict[str, Any]] = {}
    history_manager = None
    _current_agent: str = "build"
    _current_session_id: Optional[str] = None
    _settings_popup = None
    _is_welcome = False
    _is_searching: bool = False
    _search_results: List[int] = []
    _current_search_index: int = -1
    _is_continuing: bool = False
    _processed_tool_ids: set = set()
    _current_assistant_card = None
    _tool_call_depth: int = 0
    _pending_tool_calls: int = 0
    _first_tool_result: bool = True
    _todo_floating_widget = None
    _question_floating_widget = None
    _question_tool_call_id = None
    _todo_was_visible_before_system: bool = False  # 打开系统卡片前todo的可见状态
    _is_system_card_visible: bool = False  # 当前是否有系统卡片显示
    _system_cards_open: bool = False  # 是否有系统卡片正在打开（用于 _do_hide_input_area 做竞态保护）
    _window_active: bool = True

    # 插件热重载窗口级去重（D 修复）：
    # plugin_changed 信号广播到全部 backend 实例（多窗口），同一事件
    # 每个窗口各执行一遍 _on_plugin_hot_reload。类级指纹 + 短窗抑制，
    # 只让首个窗口执行完整刷新链路，其余窗口直接 return。
    # 指纹 = result 序列化（同一次广播 result 相同）；10s 短窗内同指纹只执行一次。
    _last_hot_reload_fingerprint: Optional[str] = None
    _last_hot_reload_at: float = 0.0

    # 工具热重载风险通知：进程级注册标记（多窗口只注册一次 listener）
    _tool_reload_notice_registered: bool = False

    # 团队模板：新建窗口延后 join team 的延迟（ms），等 backend 初始化完成
    # TODO: 根据用户机器性能动态调整此值
    _TEMPLATE_JOIN_DELAY_MS: int = 300
    # 团队 join 就绪轮询：backend 未就绪时的重试上限与间隔（C4）
    # 30×50ms=1500ms 上限：给 UI 补注册更多机会；重试循环已有
    # _is_destroyed 守卫 + E3 计数收口，无风险
    _TEAM_JOIN_MAX_RETRIES: int = 30
    _TEAM_JOIN_RETRY_INTERVAL_MS: int = 50
    # 新建任务：相邻成员窗口会话创建的交错间隔（C3，避免 N 窗同步链冻结 UI）
    _TEAM_NEW_TASK_STAGGER_MS: int = 50
    # 模板加载时待排列的新窗口计数（延迟 join 完成后递减，归零时触发自动排列）
    _pending_arrange_count: int = 0

    # 系统配置卡片 ID 列表 — 这些卡片打开时自动隐藏输入区域。
    # 列表可由 register_system_card() 动态扩展（UI 插件注册浮动卡片时调用）。
    # 单一真相源：_on_system_card_opened/_on_system_card_closed 通过 instance 属性访问，
    # 避免两处硬编码列表漂移。
    # aboutToQuit 全局注册守卫（仅首个窗口连接一次）
    _about_to_quit_connected: bool = False
    # 子智能体日志全局清理 timer（类级单例，不随窗口销毁）
    _class_subagent_log_cleanup_timer = None

    _BASE_SYSTEM_CARD_IDS = (
        "model_selector",
        "model_config",
        "memory",
        "history",
        "auto_loop_config",
        "auto_loop_running",
        # 以下四张全局卡片已迁移到 TabManagerWindow 的 GLOBAL_WINDOW_ID 作用域，
        # 不再属于 per-window 系统卡片，故不在此列表（它们不再隐藏对话输入区）。
        # "settings",
        # "provider_edit",
        # "mcp_edit",
        # "hook_edit",
        "project_selector",
        "tool_control",
        "share",
        "history_questions",
    )
    # AutoLoop 状态
    _is_auto_loop_running: bool = False
    _auto_loop_config_card: Optional[AutoLoopConfigCard] = None
    _auto_loop_running_card: Optional[AutoLoopRunningCard] = None
    _auto_loop_worker: Optional[AutoLoopWorker] = None
    _history_preview_messages: Optional[List[dict]] = None
    _history_preview_title: str = ""

    # 自动压缩
    _auto_compact_in_progress: bool = False  # 防重入守卫
    # Tab 模式下延迟刷新的标记（主题变更时非可见窗口跳过刷新，激活时补刷）
    _theme_needs_refresh: bool = False
    # P5b：延迟刷新时记录的待刷 scope（theme/font_family/font_size/None），
    # 切回 tab 补刷时按此精确执行，避免漏刷字体字号（V2 方案 A）
    _theme_needs_refresh_scope: str | None = None
    insertResponse = pyqtSignal(str)
    createResponse = pyqtSignal(str)
    contextActionRequested = pyqtSignal(str, str)
    skillExecutionRequested = pyqtSignal(str, dict)
    # 线程安全桥接信号：从后台线程发射，主线程槽函数自动执行
    _topic_summary_ready = pyqtSignal(object, object)
    _interrupt_complete = pyqtSignal(object)  # 中断完成后主线程回调
    userInterventionRequested = pyqtSignal(dict)
    executionResultProduced = pyqtSignal(str)
    toolStartUiSyncRequested = pyqtSignal(str, str, object, str)
    # 桌宠用：AI 状态变化信号（idle / thinking / streaming / question / error）
    ai_state_changed = pyqtSignal(str)
    # OpenCode Zen 免费模型列表异步刷新完成（后台线程 → 主线程）
    _opencode_models_ready = pyqtSignal(object)
    # models.dev 动态模型数据后台刷新完成（后台线程 → 主线程）
    _models_dev_ready = pyqtSignal(object)

    def __init__(self, homepage, source_window=None):
        # 性能优化：标记是否为复制/分支窗口，必须在 super().__init__() 之前设置，
        # 因为父类 __init__ 会触发 setup_ui，其中 _update_branch 需要根据此标志
        # 跳过冗余的 `git branch --show-current` 子进程调用，直接复制源窗口分支状态。
        # 复制/分支窗口与源窗口共享完全相同的项目与工作目录，git 分支必然一致，
        # 无需重复执行同步子进程（最坏可达 3s 阻塞主线程，拖慢窗口出现速度）。
        self._is_duplicate_window = source_window is not None
        self._source_window = source_window
        # 调用父类（会触发 setup_ui -> _create_agent_switch_buttons）
        super().__init__(homepage)
        # 需要在 super().__init__() 之前初始化所有依赖项
        self.homepage = homepage  # 必须在 super() 之前设置，供 backend.initialize 使用
        self.cfg = Settings.get_instance()
        # 初始化当前项目（在 backend.initialize 之前）
        self._current_project = self.cfg.current_project.value or "默认项目"  # 当前项目
        # 多窗口隔离：实例级工作目录缓存（{project: workdir_path}）
        # 优先级：实例缓存 > DB；DB 写入仅作为新窗口的默认恢复值
        self._current_workdir: Dict[str, str] = {}
        # 标记窗口是否已销毁，防止异步回调访问已销毁的 widget
        # 多窗口隔离：实例级模型配置缓存（必须覆盖类变量，防止多窗口共享）
        self._valid_configs: Dict[str, Dict[str, Any]] = {}
        self._is_destroyed = False
        # 多窗口隔离：窗口唯一标识（持久化 ID，跨重启稳定）
        from app.core.team_manager import TeamManager

        self._window_id = TeamManager.get_instance().generate_window_id()
        # 注册由 _rxmb_zbshud_vhmcnvr_sn_sdzl_lzmzfdq() 触发
        self._sync_active_windows_to_team_manager()

        # 系统卡片 ID 集合 — 显示时自动隐藏输入区。
        # 初始化为类常量 _BASE_SYSTEM_CARD_IDS，运行时由 register_system_card()
        # 动态扩展（UI 插件注册浮动卡片时调用）。instance 属性而非类属性，
        # 保证多窗口之间互不影响。
        self._system_card_ids: set = set(self._BASE_SYSTEM_CARD_IDS)

        # 🛡️ 工具权限控制器（per-window 多窗口隔离）
        # 必须在 backend.initialize 之前创建并注入,engine 启动时会读取
        self._tool_permission_controller = ToolPermissionController(self)

        # 创建后端（后端自己创建所有组件）- 需要在 super() 之前创建并初始化
        # 因为 setup_ui() 中会用到 self.backend.get_primary_agents()
        self.backend = ChatBackend(window_id=self._window_id)
        # 注入工具权限控制器（在 initialize 之前,engine 创建时会用到）
        self.backend.set_tool_permission_controller(self._tool_permission_controller)
        # 在 PyInstaller (frozen) 环境中不传默认 workdir，
        # 避免把 _internal 临时目录误当作项目根目录。
        # 实际 workdir 由 _sync_working_directory() 在 showEvent 中设置。
        if getattr(sys, "frozen", False):
            initial_workdir = None
        else:
            initial_workdir = str(Path(__file__).resolve().parent.parent)
        self.backend.initialize(
            get_model_config=self._get_current_model_config,
            workdir=initial_workdir,
        )
        # 🛡️ 将 controller 绑定到工具控制卡片(卡片在 super().__init__ 中已创建,
        # 此时 controller 还没建好,需要延迟绑定)
        if hasattr(self, "_tool_control_card") and self._tool_control_card is not None:
            self._tool_control_card.set_controller(self._tool_permission_controller)
        # 注册子智能体默认模型解析回调（用于 subagent_para/dag 自动使用默认模型）
        # 子智能体默认解析是后台自动流程：解析失败静默回退主模型，不弹 InfoBar 警告
        self.backend.set_subagent_model_resolver(lambda v: self._resolve_subagent_model_config(v, show_error=False))
        # 连接插件热更新信号
        self.backend.plugin_changed.connect(self._on_plugin_hot_reload)
        # 注册工具热重载风险通知监听（进程级一次）
        self._register_tool_reload_notice()
        # 连接自动上下文压缩信号（PostToolUse hook 检测到阈值时触发）
        self.backend.auto_compact_requested.connect(self._on_auto_compact_requested)
        self.backend._current_project = self._current_project
        # 同步项目到 tool_executor，确保 BuiltinTools 使用正确项目名
        if self.backend.tool_executor:
            self.backend.tool_executor.set_current_project(self._current_project)
        # 从后端获取组件（前端只负责 UI 逻辑）
        self.history_manager = self.backend.history_manager
        self.session_store = self.backend.session_store
        self.session_manager = self.backend.session_manager
        # 会话卡片缓存只保存轻量状态快照，不再保存整组 QWidget / QWebEngineView 对象
        self._session_card_cache: Dict[str, Dict[str, Any]] = {}
        self._current_history_project: Optional[str] = None  # 当前历史面板项目过滤
        self._welcome_card_cache: Dict[str, MessageCard] = {}
        self._displayed_session_id: Optional[str] = None
        self._initial_visible_batch_count = 12
        self._incremental_visible_batch_count = 8
        self._history_load_threshold = 48
        # 虚拟滚动：可见范围外前后保留多少个增量批次（缓冲区）
        # 增加缓冲区：保留 1 倍可视区域批次数量的额外卡片，减少 WebEngine 重建
        # 值越大回收越保守（减少 WebEngine 重建），值越小内存越低
        self._virtual_scroll_buffer = 1
        self._message_batch: List[List[Dict[str, Any]]] = []
        # 存储每个batch对应的UI卡片：None表示已回收（只存数据不存UI）
        self._batch_cards: List[Optional[List[MessageCard]]] = []
        # 前缀和缓存：_user_prefix[i] = 前 i 个 batch 中有多少个 user（用于 O(1) 的 round_index 计算）
        self._user_prefix_cache: List[int] = []
        self._visible_batch_start = 0
        self._visible_batch_end = 0
        self._is_loading_history_batches = False
        self._is_virtual_recycling = False
        # 滚动停止后延迟回收
        self._virtual_scroll_timer = QTimer(self)
        self._virtual_scroll_timer.setSingleShot(True)
        self._virtual_scroll_timer.setInterval(500)  # 滚动停止 500ms 后再回收，避免连续滚动反复重建
        self._virtual_scroll_timer.timeout.connect(self._recycle_out_of_view_batches)
        self._suspend_auto_scroll = False
        self._gen_thread_pool = QThreadPool()
        self._gen_thread_pool.setMaxThreadCount(2)
        # 线程安全桥接：后台线程发射信号 → 主线程执行 _on_topic_summary_generated
        self._topic_summary_ready.connect(self._on_topic_summary_generated)
        # 线程安全桥接：中断完成后回到主线程保存会话
        self._interrupt_complete.connect(self._on_finalize_complete)
        # ★ B5：停止/关窗后台 finalize 幂等锁（防止 stop 按钮与 closeEvent 双触发双 finalize）
        self._bg_finalize_lock = threading.Lock()
        self._bg_finalize_started = False
        # ★ B4 温和层：WebEngine 并发页上限（本窗口已渲染未卸载卡片数）
        self._rendered_card_count: int = 0
        self._max_rendered_cards: int = _MAX_RENDERED_CARDS
        self._render_tick: float = 0.0
        self._recycle_lru_call_count: int = 0  # 计数校准（每 20 次重算实际计数）
        # ★ B4 强回收层：已卸载 renderer 进程登记（LRU 淘汰用）
        self._unloaded_pids: List[Tuple[int, float, int]] = []  # (pid, unload_ts, batch_idx)
        self._last_kill_at: float = 0.0
        # ★ 用量聚合（T6）：套餐用量结果由进程级单例 UsageService 广播
        # （全局缓存 + 单例轮询，N tab × 同 provider 只发 1 路请求），
        # 替代旧的 per-window _coding_plan_result_ready 信号桥接。
        from app.core.usage_service import UsageService

        UsageService.get_instance().coding_plan_ready.connect(self._on_coding_plan_result)
        # 线程安全桥接：OpenCode Zen 免费模型异步刷新结果回主线程
        self._opencode_models_ready.connect(self._on_opencode_models_ready)
        # 线程安全桥接：models.dev 动态数据后台刷新结果回主线程
        self._models_dev_ready.connect(self._on_models_dev_ready)
        # 线程安全桥接：后台 git 分支检测结果回主线程
        self._branch_detect_signals = _BranchDetectSignals()
        self._branch_detect_signals.finished.connect(self._on_branch_detected)
        self._branch_detect_request_id = 0
        self._branch_cache: Dict[str, str] = {}  # workdir → branch（按路径缓存，避免重复 git 调用）
        self._pending_scroll_to_bottom = False
        self._bottom_anchor_deadline = 0.0
        self._last_visible_user_pair_index = -1
        # [PERF] 滚底定时器：24ms → 50ms，减少冗余布局重算
        # 50ms 已足够覆盖最快的内容到达速度（约 30 chars/80ms 批处理）
        self._scroll_bottom_timer = QTimer(self)
        self._scroll_bottom_timer.setSingleShot(True)
        self._scroll_bottom_timer.setInterval(50)
        self._scroll_bottom_timer.timeout.connect(self._do_scroll_to_bottom)
        # 团队模式：文件系统监听器（检测新邮件到达）
        self._team_fs_watcher = QFileSystemWatcher(self)
        self._team_fs_watcher.directoryChanged.connect(self._on_team_mailbox_changed)
        self._team_watch_paths: set = set()
        self._team_processing: bool = False  # 串行处理锁
        self._known_mail_ids: set = set()  # 已知邮箱邮件 id 快照（区分新邮件 vs 状态写回，F1 P0-1）
        self._last_stop_time: float = 0.0  # 手动停止时刻（停止后冷却，防止自动重触发，F1 P1-3）
        self._injected_team_mails: list = []  # 流式中 hook 注入的团队邮件（流结束时标记完成）
        self._team_agent_name: str = ""  # 团队模式下的 agent 名称，空=非团队模式
        self._team_name: str = ""  # 团队名（TeamManager 模板名），空=非团队模式；供 Tab 分组使用
        self._team_run_id: str = ""  # 团队运行标识（方案 A：/team --load 生成，团队会话自动保存时落库），空=非团队模式

        # [PERF] 底部锚定定时器：100ms 已足够维持粘性滚底
        self._bottom_anchor_timer = QTimer(self)
        self._bottom_anchor_timer.setSingleShot(True)
        self._bottom_anchor_timer.setInterval(100)
        self._bottom_anchor_timer.timeout.connect(self._maintain_bottom_anchor)
        self._suppress_scroll_sync_count = 0  # 加载历史时抑制滚动同步的计数器
        # 🛡️ 会话切换哨兵：_create_new_session 中置 True，丢弃 stop_streaming 后
        # 仍可能跨线程到达的 worker 旧回调（_on_messages_updated / _do_post_stream_cleanup
        # / _on_finalize_complete），防止把旧会话消息写到新会话再被 save 到新项目。
        # 在 _on_send_clicked 发起新 AI 请求时清零。
        self._session_switched = False
        # 🛡️ 压缩守卫：自动压缩清空会话后置 True，拦截旧 worker 延迟到达的
        # finished_with_messages 全量覆写（防止清空被恢复导致压缩失效+反复触发）。
        # 在 _on_send_clicked 发起新 AI 请求时清零；守卫拦截到旧快照后自清零。
        self._post_compact_guard = False
        # 🛡️ 会话脏标记：会话 messages 自上次保存后是否有过变更。
        # 用于跳过无实际变更的重复持久化（如：流式完成后 _do_post_stream_cleanup
        # 再次调用 _save_current_session_to_history、或新建会话/关闭窗口时
        # _auto_save_current_session 对已保存的无变更会话做无意义保存）。
        # 在 _on_send_clicked / _on_messages_updated / _on_finalize_complete
        # 等会修改会话内容的地方置 True；在成功保存后置 False。
        self._session_dirty: bool = False
        # 🛡️ 会话钩子注入标记：_create_new_session / _clear_session / _on_compact_finished
        # 在触发 SessionStart hook 前置 True，让 _on_messages_updated 能识别出这是
        # 合法的新会话 hook 输出而非旧 worker 的过期回调，避免被 _session_switched 误拦截。
        self._pending_session_hook = False
        self._loading_session = False  # 加载会话标志，用于懒渲染期间保持滚动位置
        self._initial_scroll_to_bottom = False  # 首次滚底标记：只在首次强制滚底，后续只有用户在底部才继续
        self._user_intentionally_away_from_bottom = False  # 用户主动滚上去标记
        # 🆕 流式结束滚底宽限截止（monotonic 时间戳，0 = 无宽限）：
        # 流式刚结束 2s 内忽略 _user_intentionally_away_from_bottom 拦截，
        # 防止用户流式中途上滚导致结束时的兜底滚底被跳过（详见 _ensure_at_bottom）。
        self._stream_finished_grace_until: float = 0.0
        self._pending_lazy_cards: List[MessageCard] = []  # 待处理的懒渲染卡片队列
        self._lazy_batch_timer_active = False  # 懒批量渲染定时器是否已激活，防止重复调度
        # resize 防抖定时器 - 性能优化：32ms（~30fps）已足够覆盖感知刷新率
        # 每帧比 16ms 少一次布局重算，在慢速 resize 拖拽场景下人眼不会感知差异
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(32)  # 32ms 防抖，约30fps视觉刷新
        self._resize_debounce_timer.timeout.connect(self._do_debounced_resize)
        # resize 完成后更新所有卡片的定时器（延迟更新非可见区域卡片）
        self._resize_complete_timer = QTimer(self)
        self._resize_complete_timer.setSingleShot(True)
        self._resize_complete_timer.setInterval(100)  # resize 结束后尽快恢复真实内容
        self._resize_complete_timer.timeout.connect(self._sync_all_cards_width)
        self._pending_resize_sync = False
        self._resize_preview_active = False
        self._last_chat_viewport_width = 0
        # [PERF] 滚动同步定时器：100ms 已足够跟踪滚动停止
        self._scroll_sync_timer = QTimer(self)
        self._scroll_sync_timer.setSingleShot(True)
        self._scroll_sync_timer.setInterval(100)
        self._scroll_sync_timer.timeout.connect(self._sync_visible_cards_on_scroll)
        self.toolStartUiSyncRequested.connect(self._handle_tool_start_ui_sync, type=Qt.QueuedConnection)
        self._is_streaming = False
        self._ai_state = "idle"  # 桌宠用：当前 AI 状态
        self._topic_summary_cancelled = False  # 用于取消正在进行的标题生成任务
        self._response_start_time = None
        self._stop_elapsed = None  # 手动停止时暂存的耗时
        # 使用 try-except 保护 homepage 操作，防止 C++ 对象已删除错误
        try:
            from PyQt5 import sip

            if not sip.isdeleted(homepage):
                homepage.installEventFilter(self)
                self._window_active = homepage.isActiveWindow()
            else:
                self._window_active = False
        except Exception:
            self._window_active = False
        # 初始化属性
        self._pending_permission_tool_call_id: Optional[str] = None
        self._question_tool_call_id: Optional[str] = None
        self._current_assistant_round_index: Optional[int] = None  # 跟踪当前应分配给 assistant 的 round_index
        self._pending_scroll_to_index: Optional[int] = None  # 时间线节点滚动目标索引
        self._pending_scroll_to_batch: Optional[int] = None  # 时间线节点滚动目标 batch 索引
        self._pending_scroll_to_update: Optional[int] = None  # 待更新的节点索引（用于同步高亮和进度）
        # 撤销删除功能：删除消息的缓存栈
        self._undo_delete_stack: List[Dict[str, Any]] = []  # 每个元素包含 deleted_messages, round_index, widgets

        self._current_session_id = self.session_manager.get_current_session().session_id

        # 初始化卡片管理器（注册当前窗口）
        self._card_manager = CardManager.get_instance()
        self._card_manager.register_window(self._window_id)

        # 初始化 UI
        self.setup_ui()

        # 初始化 UI 相关的回调
        self._setup_engine_callbacks()

        # 初始化子智能体信号连接
        self._init_sub_agent_signals()
        # 设置子智能体获取主智能体历史消息的回调
        self.backend.set_sub_agent_history_getter(self._get_current_session_messages_for_tools)
        # [审查 #8r Bug D] SubAgentManager 延迟创建（600ms 批）完成前信号连接会跳过；
        # 监听 sub_agent_ready 信号补连 + 补传历史 getter
        self.backend.sub_agent_ready.connect(self._on_sub_agent_ready)

        # 初始化历史管理器
        self._project_label.setText(self._current_project)

        # 应用退出时自动保存（仅首个窗口注册一次，遍历所有实例批量保存）
        cls = type(self)
        if not cls._about_to_quit_connected:
            app = QApplication.instance()
            if app is not None:
                try:
                    app.aboutToQuit.connect(cls._on_app_about_to_quit)
                    cls._about_to_quit_connected = True
                except Exception:
                    pass

        # 设置文件操作记录的会话上下文
        if self.backend.tool_executor:
            self.backend.set_session_context(self._current_session_id)

        # 自动检查更新（启动时静默检查，全局仅首次窗口触发）
        if not OpenAIChatToolWindow._global_auto_update_checked:
            OpenAIChatToolWindow._global_auto_update_checked = True
            self._init_auto_update_check()
        else:
            logger.debug("[AutoUpdate] 已检查过更新，跳过")

        # 注册到全局实例列表（用于多窗口事件广播）
        OpenAIChatToolWindow._instances.append(self)

        # 【性能优化】延迟构建重型卡片内容（记忆/历史/模型选择等），
        # 让窗口外壳（chat_scroll_area + 输入区域）先出现。
        # 800ms 延迟让首帧绘制完成后再开始填充内容。
        # 卡片内部进一步按 singleShot(0) 链式逐个构建，避免一次冻结 UI。
        # P2 懒加载：窗口未激活（Tab 模式非当前页）时定时器回调只置待建标记，
        # 首次激活（showEvent）时由 _maybe_build_deferred_content 补建，
        # 避免每个后台 tab 都全量构建重型卡片。
        self._deferred_build_pending = False
        self._settings_popup_pending = False
        self._pixel_pet_pending = False
        QTimer.singleShot(800, self._deferred_build_cards)

    # 全局标志：自动更新检查在整个应用生命周期内只触发一次
    _global_auto_update_checked = False

    def _init_auto_update_check(self):
        """启动时静默检查更新（使用延迟确保窗口完全就绪）"""

        # 检查是否启用自动更新
        if not self.cfg.auto_check_update.value:
            return

        # 延迟 500ms 确保 ToolPopupDialog 完全显示后再检查更新
        # 这样 InfoBar 能正确显示在已就绪的窗口上
        QTimer.singleShot(500, lambda: self._do_auto_update_check())

    def _do_auto_update_check(self):
        """执行自动更新检查"""
        from app.update_checker import UpdateChecker

        # 检查窗口是否已销毁（防止多窗口切换时的问题）
        if getattr(self, "_is_destroyed", False):
            return

        # 更新单例的 parent，确保 InfoBar 显示在正确的父窗口上
        checker = UpdateChecker.get_instance(self)
        checker.check_update(silent=True)

    # ── Tab 模式焦点守卫 ──

    def _is_tab_active(self) -> bool:
        """判断当前窗口是否为 Tab 管理器中的活动标签页

        在 Tab 模式下，非活动标签页不应该通过 setFocus 抢夺输入焦点。
        非 Tab 模式始终返回 True（独立窗口正常聚焦）。

        Returns:
            True — 可以安全调用 setFocus（非 Tab 模式 或 是当前活动标签页）
            False — 当前窗口不是活动标签页，应跳过焦点操作
        """
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is not None and tm.isVisible():
                return tm.get_current_window() is self
        except Exception:
            pass
        return True  # 非 Tab 模式：始终允许

    def _focus_input_if_active(self, reason=Qt.OtherFocusReason):
        """仅在活动窗口时聚焦输入框，避免 Tab 模式下焦点被后台窗口劫持"""
        if self._is_tab_active() and self.input_area:
            self.input_area.setFocus(reason)

    def _setup_engine_callbacks(self):
        """设置 ChatEngine 的回调"""
        callbacks = {
            "content_received": self._on_content_received,
            "reasoning_content_received": self._on_reasoning_content_received,
            "thinking_started": self._on_thinking_started,
            "tool_call_started": self._on_tool_call_started,
            "tool_args_updated": self._on_tool_args_updated,
            "tool_call_sync_requested": self._request_tool_start_ui_sync,
            "tool_result_received": self._on_tool_result_received,
            "stream_started": self._on_stream_started,
            "stream_finished": self._on_stream_finished,
            "messages_updated": self._on_messages_updated,
            "error": self._on_engine_error,
            "skill_requested": self._on_skill_requested,
            "question_asked": self._on_question_asked,
            "agent_switched": self._on_agent_switched,
            "retry_status": self._on_retry_status,
            "retry_resolved": self._on_retry_resolved,
            "permission_approval_requested": self._on_permission_approval_requested,
            "context_updated": self._on_context_updated,
        }
        self.backend.set_all_callbacks(callbacks)

    def _init_sub_agent_signals(self):
        """连接子智能体信号到 UI 回调"""
        sub_agent_mgr = self.backend.sub_agent_manager
        if sub_agent_mgr:
            sub_agent_mgr.task_started.connect(self._on_sub_agent_task_started)
            sub_agent_mgr.task_finished.connect(self._on_sub_agent_task_finished)
            # ★ T24：子智能体 ask 权限请求 → 主线程弹窗（用户允许/拒绝）
            sub_agent_mgr.permission_requested.connect(self._on_subagent_permission_requested)

    def _on_sub_agent_ready(self):
        """[审查 #8r Bug D] SubAgentManager 延迟创建完成后的补连入口

        backend 的 SubAgentManager 在 QTimer 600ms 批延迟创建；窗口 __init__
        同步调用 _init_sub_agent_signals 时 manager 为 None 会静默跳过，
        导致子智能体任务信号/权限弹窗永久缺失。此槽在 sub_agent_ready
        信号发出时重连，并补传历史 getter（backend 侧也会补传，双保险）。
        """
        try:
            self._init_sub_agent_signals()
            self.backend.set_sub_agent_history_getter(self._get_current_session_messages_for_tools)
        except Exception as e:
            logger.warning(f"[MainWidget] SubAgent 补连失败: {e}")

    def _init_llm_api_service(self):
        """初始化 LLM API 服务"""
        from app.gateway import (
            APISessionHandler,
            LLMAPIService,
            is_service_running,
        )

        # 注册服务商列表获取回调
        def get_providers_list():
            return [{"name": name} for name in self._valid_configs.keys()]

        # 创建并注册 API 会话处理器（复用 UI 的 ChatEngine 和 SessionManager）
        self._api_session_handler = APISessionHandler(self)
        LLMAPIService.set_session_handler(self._api_session_handler)

        # 根据配置决定是否启动服务
        if self.cfg.llm_api_enabled.value:
            if not is_service_running():
                service = LLMAPIService()
                service.port = self.cfg.llm_api_port.value
                service.start(background=True)
        else:
            # 确保服务未启动
            if is_service_running():
                from app.gateway import (
                    stop_llm_api_service,
                )

                stop_llm_api_service()

        # 锁屏远程：若配置开启则自动生效（保持自动化任务持续运行），开关由设置 UI 控制
        try:
            from app.core.system.lock_screen_remote import (
                get_lock_screen_remote_manager,
            )

            if self.cfg.lock_screen_remote_enabled.value:
                get_lock_screen_remote_manager().enable(lock_now=False, keep_display_on=True)
        except Exception as e:
            logger.warning(f"[MainWidget] 锁屏远程初始化失败: {e}")

    def _register_cards_to_manager(self):
        """注册所有卡片到 CardManager 并添加到容器

        容器分配：
        - TopCardContainer (chatscroll 上方): Todo、MCP Edit、Hook Edit、Settings 等系统配置
        - BottomCardContainer (chatscroll 下方): 长期记忆、历史会话、模型参数、AutoLoop、Tool、Question、SubAgent

        规则：
        - 同位置互斥：Top/Bottom 各自只能显示一个卡片
        - Question 强制覆盖：Question 显示时同时关闭其他所有卡片
        - 不同位置可共存：Top 的卡片和 Bottom 的卡片可以同时显示
        """
        mgr = self._card_manager

        # 绑定容器到 CardManager（传入窗口ID用于隔离）
        self._top_card_container.bind_card_manager(mgr, self._window_id)
        self._bottom_card_container.bind_card_manager(mgr, self._window_id)

        # ===== TopCardContainer (chatscroll 上方) =====
        # 系统配置卡片，互斥显示
        mgr.register_card(self._window_id, ContainerType.TOP, "todo", self._todo_floating_widget)
        self._top_card_container.add_card("todo", self._todo_floating_widget)

        # 注：mcp_edit/provider_edit/hook_edit 三张编辑卡片已改为懒创建，
        # 注册/入容器在 _ensure_xxx_card() 中按需执行，避免 setup_ui 关键路径上构建。

        # 项目选择卡片（Top 容器，与 settings 同容器互斥）
        mgr.register_card(
            self._window_id,
            ContainerType.TOP,
            "project_selector",
            self._project_selector_card,
            system_card=True,
        )
        self._top_card_container.add_card("project_selector", self._project_selector_card)

        # ===== BottomCardContainer (chatscroll 下方) =====
        # Question: 强制覆盖所有其他卡片
        # Tool/SubAgent: 实时卡片
        # History/Memory/ModelConfig/AutoLoop: 系统卡片
        mgr.register_card(
            self._window_id,
            ContainerType.BOTTOM,
            "question",
            self._question_floating_widget,
        )
        self._bottom_card_container.add_card("question", self._question_floating_widget)

        mgr.register_card(
            self._window_id,
            ContainerType.BOTTOM,
            "sub_agent_compact",
            self._sub_agent_compact_widget,
        )
        self._bottom_card_container.add_card("sub_agent_compact", self._sub_agent_compact_widget)

        # 注：history/share/history_questions/memory/model_config/model_selector
        # 六张系统卡片框架已懒创建（P0-1），注册/入容器在各自 _ensure_xxx_card() 中执行，
        # 由 _deferred_build_cards 链预构建 + 打开入口兜底，避免 setup_ui 关键路径开销。

        mgr.register_card(
            self._window_id,
            ContainerType.BOTTOM,
            "tool_control",
            self._tool_control_card,
            system_card=True,
        )
        self._bottom_card_container.add_card("tool_control", self._tool_control_card)

        # 注：auto_loop_config / auto_loop_running 两张卡片已延迟构建（T7），
        # 注册/入容器在 _ensure_auto_loop_config_card() /
        # _ensure_auto_loop_running_card() 中按需执行（_deferred_card_steps 链预构建）。

        # 注：model_selector 卡片框架懒创建，注册/入容器见 _ensure_model_selector_card()

    # ── 六张系统卡片框架懒创建（P0-1 性能优化）──
    # 原 setup_ui 同步段直接创建框架（每窗口 ~160ms），改为惰性创建：
    # 由 _deferred_build_cards 链（800ms 后）预构建 + 卡片打开入口 ensure 兜底，
    # 保证「打开卡片 → 框架已就绪」行为不变。属性名/注册语义与改造前完全一致。

    def _ensure_history_card(self):
        """确保历史会话卡片框架已创建（内容由 _build_deferred_card_history 填充）"""
        if self._history_card is not None:
            return
        self._history_card = BaseSettingsCard("历史会话", "📜", self)
        self._history_card.setMinimumHeight(300)  # 自适应窗口高度
        # 设置历史/归档标签
        self._history_card.setup_tabs(
            [
                ("history", "历史会话"),
                ("archived", "归档"),
            ],
            "history",
        )
        # 强制触发首次 tab 渲染
        self._history_card.tabChanged.connect(self._on_history_tab_changed)
        self._history_card.set_current_tab("history")
        self._history_card.tabChanged.connect(self._on_history_tab_changed)
        self._history_card.setVisible(False)
        self._history_card.closed.connect(
            lambda: (
                self._card_manager.hide_card("history", self._window_id),
                self._restore_after_system_close(),
            )
        )
        self._card_manager.register_card(
            self._window_id, ContainerType.BOTTOM, "history", self._history_card, system_card=True
        )
        self._bottom_card_container.add_card("history", self._history_card)

    def _ensure_share_card(self):
        """确保分享卡片框架已创建（内容由 _build_deferred_card_share 填充）"""
        if self._share_card is not None:
            return
        self._share_card = BaseSettingsCard("分享当前对话", "📤", self)
        self._share_card.set_height_mode("content")
        self._share_card.setVisible(False)
        self._share_card.closed.connect(lambda: self._card_manager.hide_card("share", self._window_id))
        self._card_manager.register_card(
            self._window_id, ContainerType.TOP, "share", self._share_card, system_card=True
        )
        self._top_card_container.add_card("share", self._share_card)

    def _ensure_history_questions_card(self):
        """确保历史问题卡片框架已创建（内容由 _build_deferred_card_history_questions 填充）"""
        if self._history_questions_card is not None:
            return
        self._history_questions_card = BaseSettingsCard("历史问题", "💬", self)
        self._history_questions_card.set_height_mode("content")
        self._history_questions_card.setVisible(False)
        self._history_questions_card.closed.connect(
            lambda: self._card_manager.hide_card("history_questions", self._window_id)
        )
        self._card_manager.register_card(
            self._window_id, ContainerType.TOP, "history_questions", self._history_questions_card, system_card=True
        )
        self._top_card_container.add_card("history_questions", self._history_questions_card)

    def _ensure_memory_card(self):
        """确保记忆管理卡片框架已创建（内容由 _build_deferred_card_memory 填充）"""
        if self._memory_card is not None:
            return
        self._memory_card = BaseSettingsCard("记忆管理", "🧠", self)
        self._memory_card.setMinimumHeight(300)  # 自适应窗口高度
        # 设置记忆管理标签（条目记忆/项目笔记/关键文档）
        self._memory_card.setup_tabs(
            [
                ("entries", "条目记忆"),
                ("notes", "项目笔记"),
                ("docs", "关键文档"),
            ],
            "entries",
        )
        self._memory_card.tabChanged.connect(self._on_memory_tab_changed)
        # 强制触发首次 tab 渲染
        self._memory_card.set_current_tab("entries")
        self._memory_card.setVisible(False)
        self._memory_card.closed.connect(
            lambda: (
                self._card_manager.hide_card("memory", self._window_id),
                self._restore_after_system_close(),
            )
        )
        self._card_manager.register_card(
            self._window_id, ContainerType.BOTTOM, "memory", self._memory_card, system_card=True
        )
        self._bottom_card_container.add_card("memory", self._memory_card)

    def _ensure_auto_loop_config_card(self):
        """确保 AutoLoop 配置卡已创建（延迟构建 T7）。

        原 setup_ui 同步构造（每次新建 tab ~7ms），改为惰性创建：
        - _deferred_card_steps 链（800ms 后）预构建
        - 打开入口 _show_auto_loop_config 兜底 ensure
        信号/注册/容器语义与改造前完全一致。
        """
        if self._auto_loop_config_card is not None:
            return
        from app.widgets.cards.settings.auto_loop_card import AutoLoopConfigCard

        self._auto_loop_config_card = AutoLoopConfigCard()
        self._auto_loop_config_card.startRequested.connect(self._on_auto_loop_start)
        self._auto_loop_config_card.closed.connect(
            lambda: (
                self._card_manager.hide_card("auto_loop_config", self._window_id),
                self._restore_after_system_close(),
            )
        )
        self._auto_loop_config_card.setVisible(False)
        self._card_manager.register_card(
            self._window_id, ContainerType.BOTTOM, "auto_loop_config", self._auto_loop_config_card, system_card=True
        )
        self._bottom_card_container.add_card("auto_loop_config", self._auto_loop_config_card)

    def _ensure_auto_loop_running_card(self):
        """确保 AutoLoop 运行卡已创建（延迟构建 T7，与配置卡同通道）。"""
        if self._auto_loop_running_card is not None:
            return
        from app.widgets.cards.settings.auto_loop_card import AutoLoopRunningCard

        self._auto_loop_running_card = AutoLoopRunningCard()
        self._auto_loop_running_card.stopRequested.connect(self._on_auto_loop_stop)
        self._auto_loop_running_card.archiveRequested.connect(self._on_auto_loop_archive)
        self._auto_loop_running_card.setVisible(False)
        self._card_manager.register_card(
            self._window_id, ContainerType.BOTTOM, "auto_loop_running", self._auto_loop_running_card, system_card=True
        )
        self._bottom_card_container.add_card("auto_loop_running", self._auto_loop_running_card)

    def _ensure_model_config_card(self):
        """确保模型配置卡片框架已创建（内容由 _build_deferred_card_model_config 填充）"""
        if self._model_config_card is not None:
            return
        self._model_config_card = BaseSettingsCard("模型配置", "🔧", self)
        self._model_config_card.setMinimumHeight(250)  # set_config 时 ModelConfigCard 会重新计算
        self._model_config_card.set_height_mode("content")  # 按内容自适应高度
        self._model_config_card.setVisible(False)
        self._model_config_card.closed.connect(
            lambda: (
                self._card_manager.hide_card("model_config", self._window_id),
                self._restore_after_system_close(),
            )
        )
        self._card_manager.register_card(
            self._window_id, ContainerType.BOTTOM, "model_config", self._model_config_card, system_card=True
        )
        self._bottom_card_container.add_card("model_config", self._model_config_card)

    def _ensure_model_config_popup(self):
        """确保模型配置卡片内容已构建（幂等）

        与 _ensure_model_selector_card_content 同理：延迟构建链可能尚未执行，
        用户提前点击模型参数按钮时兜底立即构建，避免
        _load_model_config_to_card 访问 _model_config_popup 为 None。
        """
        if self._model_config_popup is not None:
            return
        self._ensure_model_config_card()
        self._model_config_popup = ModelConfigCard()
        self._model_config_popup.configApplied.connect(self._on_config_applied)
        self._model_config_card.content_layout.addWidget(self._model_config_popup)

    def _ensure_model_selector_card(self):
        """确保模型选择卡片框架已创建（内容由 _build_deferred_card_model_selector 填充）"""
        if self._model_selector_card is not None:
            return
        self._model_selector_card = BaseSettingsCard("", "", self)
        self._model_selector_card.setMinimumHeight(250)  # 自适应窗口高度
        self._model_selector_card.setVisible(False)
        self._model_selector_card.closed.connect(
            lambda: (
                self._card_manager.hide_card("model_selector", self._window_id),
                self._restore_after_system_close(),
            )
        )
        self._card_manager.register_card(
            self._window_id, ContainerType.BOTTOM, "model_selector", self._model_selector_card, system_card=True
        )
        self._bottom_card_container.add_card("model_selector", self._model_selector_card)

    def _ensure_model_selector_card_content(self):
        """确保模型选择卡片内容已构建（幂等）

        延迟构建链（_build_deferred_card_model_selector）可能因 800ms 定时器
        未触发 / P2 懒加载 pending / 构建失败而尚未执行；用户提前点击模型选择
        按钮时本方法兜底立即构建，避免 _load_model_selector_to_card 访问 None。
        """
        if self._model_selector_card_content is not None:
            return
        self._ensure_model_selector_card()
        self._model_selector_card_content = ModelSelectorCardContent()
        self._model_selector_card_content.modelSelected.connect(self._on_model_selected_from_popup)
        self._model_selector_card_content.stickyProviderChanged.connect(self._on_sticky_provider_changed)
        self._model_selector_card.set_search_handler(
            "搜索模型...",
            self._model_selector_card_content.set_search_filter,
        )
        self._model_selector_card.add_header_button(
            FluentIcon.ADD,
            "添加服务商",
            self._on_add_provider_from_card,
        )
        self._model_selector_card.add_header_button(
            get_icon("配置管理"),
            "配置服务商",
            self._on_configure_providers_from_card,
        )
        self._model_selector_card.content_layout.addWidget(self._model_selector_card_content)

    def _deferred_build_cards(self):
        """【性能优化】延迟构建重型卡片内容（分批渐进式）

        在 setup_ui 中仅创建卡片的轻量框架（BaseSettingsCard），
        而卡片内部的重量级内容 widget 在本方法中创建并填充。

        性能优化：每个卡片使用独立的 QTimer.singleShot(0) 调度，
        让 Qt 事件循环在卡片创建之间有机会处理绘制事件，
        避免 6 张卡片连续同步创建导致 UI 冻结数秒。

        P2 懒加载：若窗口尚未激活（Tab 模式非当前页，isVisible() 为 False），
        本回调不构建卡片，仅置 _deferred_build_pending 待建标记；
        待窗口首次变为可见（showEvent）时由 _maybe_build_deferred_content
        重新触发本方法补建，避免每个后台 tab 都全量构建 7 张重型卡片。
        """
        if getattr(self, "_is_destroyed", False):
            return  # 窗口已销毁：800ms 定时器仍可能触发，直接跳过
        if not self.isVisible():
            self._deferred_build_pending = True
            return
        self._deferred_card_build_step = 0
        self._deferred_card_steps = [
            self._build_deferred_card_history,
            self._build_deferred_card_share,
            self._build_deferred_card_history_questions,
            self._build_deferred_card_memory,
            self._build_deferred_card_model_config,
            self._build_deferred_card_model_selector,
            self._build_deferred_card_auto_loop,
        ]
        self._schedule_next_deferred_card()

    def _schedule_next_deferred_card(self):
        """调度下一个卡片构建步骤（每次 yield 给事件循环）"""
        if self._deferred_card_build_step < len(self._deferred_card_steps):
            step = self._deferred_card_steps[self._deferred_card_build_step]
            self._deferred_card_build_step += 1
            QTimer.singleShot(0, step)

    def _build_deferred_card_history(self):
        """── ① 历史会话卡片 ──"""
        try:
            self._ensure_history_card()  # P0-1：框架惰性创建
            self._history_popup_card = HistoryCard()
            self._history_popup_card.sessionSelected.connect(self._on_history_session_selected)
            self._history_popup_card.sessionArchived.connect(self._archive_history_session)
            self._history_popup_card.sessionRenamed.connect(self._rename_history_session)
            self._history_popup_card.refreshRequested.connect(self._refresh_history_toggle_panel)
            self._history_popup_card.sessionImported.connect(self._on_session_imported)
            self._history_popup_card.sessionRestored.connect(self._on_archived_session_restored)
            self._history_popup_card.sessionPermanentlyDeleted.connect(self._on_archived_session_deleted)
            self._history_popup_card.archivedSessionRenamed.connect(self._on_archived_session_renamed)
            self._history_popup_card.teamRestoreRequested.connect(self._on_team_restore_requested)
            self._history_popup_card.teamArchiveRequested.connect(self._on_team_archive_requested)
            self._history_popup_card.memberSelected.connect(self._on_team_member_selected)
            self._history_card.set_extra_button_handler(
                self._history_popup_card.get_import_button_handler(),
                tooltip="导入会话",
            )
            self._history_card.content_layout.addWidget(self._history_popup_card)
            self._history_card.set_search_handler(
                "🔍 搜索会话...",
                lambda text: self._history_popup_card.set_search_filter(text),
            )
        except Exception:
            logger.exception("[DeferredBuild] HistoryCard 构建失败")
        finally:
            self._schedule_next_deferred_card()

    def _build_deferred_card_share(self):
        """── ② 分享卡片 ──"""
        try:
            self._ensure_share_card()  # P0-1：框架惰性创建
            self._share_card_content = ShareCardContent(self)
            self._share_card.content_layout.addWidget(self._share_card_content)
        except Exception:
            logger.exception("[DeferredBuild] ShareCard 构建失败")
        finally:
            self._schedule_next_deferred_card()

    def _build_deferred_card_history_questions(self):
        """── ③ 历史问题卡片 ──"""
        try:
            self._ensure_history_questions_card()  # P0-1：框架惰性创建
            self._history_questions_card_content = HistoryQuestionsCardContent(self)
            self._history_questions_card_content.questionClicked.connect(self._on_history_question_clicked)
            self._history_questions_card_content.questionClicked.connect(
                lambda: self._card_manager.hide_card("history_questions", self._window_id)
            )
            self._history_questions_card.content_layout.addWidget(self._history_questions_card_content)
        except Exception:
            logger.exception("[DeferredBuild] HistoryQuestionsCard 构建失败")
        finally:
            self._schedule_next_deferred_card()

    def _build_deferred_card_memory(self):
        """── ④ 记忆管理卡片 ──"""
        try:
            self._ensure_memory_card()  # P0-1：框架惰性创建
            self._memory_card_popup = MemoryCardContent(self.backend.memory_manager, self)
            self._memory_card_popup.memorySaved.connect(self._on_memory_card_saved)
            self._memory_card_popup.workingDirChanged.connect(self._on_working_dir_changed)
            self._memory_card_popup.set_project(self._current_project)
            self._memory_card.content_layout.addWidget(self._memory_card_popup)
            self._memory_card.set_search_handler(
                "🔍 搜索条目记忆...",
                lambda text: self._memory_card_popup.set_search_filter(text),
            )
        except Exception:
            logger.exception("[DeferredBuild] MemoryCard 构建失败")
        finally:
            self._schedule_next_deferred_card()

    def _build_deferred_card_model_config(self):
        """── ⑤ 模型配置卡片 ──"""
        try:
            self._ensure_model_config_card()  # P0-1：框架惰性创建
            self._ensure_model_config_popup()
        except Exception:
            logger.exception("[DeferredBuild] ModelConfigCard 构建失败")
        finally:
            self._schedule_next_deferred_card()

    def _build_deferred_card_model_selector(self):
        """── ⑥ 模型选择卡片 ──"""
        try:
            self._ensure_model_selector_card()  # P0-1：框架惰性创建
            self._ensure_model_selector_card_content()
        except Exception:
            logger.exception("[DeferredBuild] ModelSelectorCard 构建失败")
        finally:
            self._schedule_next_deferred_card()

    def _build_deferred_card_auto_loop(self):
        """── ⑦ AutoLoop 配置卡 + 运行卡（延迟构建 T7）──"""
        try:
            self._ensure_auto_loop_config_card()
            self._ensure_auto_loop_running_card()
        except Exception:
            logger.exception("[DeferredBuild] AutoLoopCard 构建失败")
        finally:
            self._schedule_next_deferred_card()

    def _setup_title_bar(self):
        """设置标题栏按钮"""
        title_bar = self.get_title_bar()
        # 显示内存标签
        title_bar.show_memory_label()
        # 创建复制窗口按钮
        self._copy_btn = TransparentToolButton(get_icon("新建窗口"), self)
        self._copy_btn.setFixedSize(28, 28)
        self._copy_btn.setToolTip("新建窗口")
        self._copy_btn.clicked.connect(lambda: self._safe_duplicate_window(branch=False))
        title_bar.insert_button(0, self._copy_btn)

        # 创建分支按钮
        self._branch_btn = TransparentToolButton(get_icon("分支"), self)
        self._branch_btn.setFixedSize(28, 28)
        self._branch_btn.setToolTip("分支当前对话")
        self._branch_btn.clicked.connect(lambda: self._safe_duplicate_window(branch=True))
        title_bar.insert_button(1, self._branch_btn)
        # 创建设置按钮
        self._settings_btn = TransparentToolButton(FluentIcon.SETTING, self)
        self._settings_btn.setFixedSize(28, 28)
        self._settings_btn.setToolTip("设置")
        # 设置弹窗已改为懒构建（1500ms 后），
        # 使用 _open_settings_popup 确保若弹窗尚未构建则先构建再切换
        self._settings_btn.clicked.connect(self._open_settings_popup)
        title_bar.insert_button(2, self._settings_btn)

    def _toggle_settings_card(self):
        """切换设置卡片的显示（委托全局卡片控制器）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc.toggle_settings()

    def _open_api_docs(self):
        """打开 API 文档页面"""
        from app.gateway import open_docs

        open_docs()

    def _safe_duplicate_window(self, branch: bool = False):
        """安全包装 _duplicate_window，确保任何异常都不会传播到 PyQt5 信号槽链

        PyQt5 中信号槽内的未捕获异常会调用 pyqt5_err_print() → qFatal() → abort()，
        导致整个进程崩溃（macOS 上的经典崩溃模式）。
        此方法在最外层用 BaseException 兜底，确保异常不会逃逸到 PyQt5 的信号调度器。

        调用链：分支/复制窗口按钮的 clicked 信号直接连接 lambda，lambda 内部通过
        本方法用 BaseException 兜底。如果连本方法都进不去（信号来自已析构的 widget
        等极端情况），异常会由 PyQt5 内部的 C++ 异常处理器捕获 → pyqt5_err_print，
        其中部分 PyQt5 版本会不可阻止地调用 qFatal → abort。
        头尾通过日志确认入口/出口状态。
        """
        logger.debug(f"[_safe_duplicate_window] ENTER branch={branch}")
        try:
            self._duplicate_window(branch=branch)
        except BaseException:
            import traceback

            logger.error(f"[_safe_duplicate_window] 复制窗口时发生异常: {traceback.format_exc()}")
            # 强制刷新日志缓冲区，确保异常信息写入磁盘
            try:
                logger.complete()
            except BaseException:
                pass
            try:
                from qfluentwidgets import InfoBar, InfoBarPosition

                InfoBar.error(
                    "复制失败",
                    "创建窗口时发生异常，请重试",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.BOTTOM,
                )
            except BaseException:
                pass  # InfoBar 也失败时彻底放弃
        logger.debug(f"[_safe_duplicate_window] LEAVE branch={branch}")

    def _duplicate_window(self, branch: bool = False):
        """复制当前窗口并以弹窗方式显示，或从当前会话分支创建新会话

        Args:
            branch: 如果为 True，则复制当前会话的消息到新窗口
        """
        try:
            # 验证 self 和 homepage 是否有效
            from PyQt5 import sip

            try:
                if sip.isdeleted(self) or sip.isdeleted(self.homepage):
                    InfoBar.error(
                        "窗口错误",
                        "主窗口已关闭，无法创建新窗口",
                        parent=TabManagerWindow.get_instance() or self.window(),
                        position=InfoBarPosition.BOTTOM,
                    )
                    return
            except Exception:
                pass  # 忽略检查失败

            # 确保 homepage 有效后再使用
            valid_homepage = self.homepage
            if valid_homepage is None:
                return

            # 创建新的窗口实例（传入 source_window 以标记复制/分支窗口，
            # setup_ui 中将据此跳过 git 子进程、直接复制源窗口分支状态）
            new_instance = OpenAIChatToolWindow(valid_homepage, source_window=self)

            # ── 多窗口隔离：把源窗口的项目上下文原样复制给新窗口 ──
            # 必须在 __init__ 跑完之后立刻覆盖,否则新窗口会从全局 cfg 读到
            # 最近一次 _on_project_selected 写入的"当前最新选择的项目",
            # 导致分支/复制窗口错位显示项目名。
            new_instance._current_project = self._current_project
            # 【按项目定义】不复制源窗口 workdir 实例缓存：复制窗口按当前项目
            # 从 DB 读取项目定义的工作目录（showEvent is_duplicate 分支的
            # _sync_working_directory 完成读取+写回）。避免继承源窗口的临时/
            # 手动路径，确保 project_root 与项目定义一致，且不链式传播。
            # new_instance._current_workdir = dict(self._current_workdir)  # 已移除
            new_instance.backend._current_project = self._current_project
            # 注：此处 tool_executor 可能尚未创建（延迟 200ms），if 分支会跳过；
            # workdir 由 showEvent 的 is_duplicate 分支 _sync_working_directory
            # （500ms 延迟）从 DB 按项目恢复。
            if new_instance.backend.tool_executor:
                new_instance.backend.tool_executor.set_current_project(self._current_project)
            if hasattr(new_instance, "_project_label"):
                new_instance._project_label.setText(self._current_project)

            # ── workdir 不再从源窗口复制：由 _sync_working_directory 按项目从 DB 恢复 ──

            # 同步刷新面包屑样式与 git 分支标签
            if hasattr(new_instance, "_refresh_project_branch_style"):
                new_instance._refresh_project_branch_style()
            # 性能优化：复制/分支窗口与源窗口共享完全相同的项目与工作目录，
            # 分支必然一致。setup_ui 已从源窗口复制了分支标签状态，这里再次从
            # 源窗口(self)复制，跳过 _update_branch 的同步 git 子进程调用。
            if hasattr(new_instance, "_copy_branch_from"):
                new_instance._copy_branch_from(self)
            elif hasattr(new_instance, "_update_branch"):
                new_instance._update_branch()
            # ──────────────────────────────────────────────────

            # 如果是分支模式，传递当前会话的消息
            if branch:
                current_session = self.session_manager.get_current_session()
                if current_session:
                    branch_messages = list(current_session.messages)
                    branch_name = current_session.name + " [分支]"
                    # 设置分支会话数据，新窗口会使用这些消息创建会话
                    new_instance._branch_session_data = {
                        "messages": branch_messages,
                        "name": branch_name,
                        "project": self._current_project,  # 记录源项目，便于历史分组/检索
                    }
                # 分支模式不跳过历史恢复，而是使用传入的分支数据
                new_instance._skip_restore_history = True  # 跳过 _restore_latest_session
            else:
                new_instance._skip_restore_history = True  # 跳过历史会话恢复，创建新会话

            # 复制模型选择（确保两个实例都已初始化 UI）
            try:
                if (
                    hasattr(self, "_current_provider_name")
                    and hasattr(new_instance, "_current_provider_name")
                    and self._current_provider_name
                ):
                    new_instance._current_provider_name = self._current_provider_name
                    new_instance._current_model_name = self._current_model_name
                    # #4 语义：分支/新窗口继承源窗口的"是否手动选过"标志，
                    # 保持会话状态一致（手动选过的分支窗口同步时同样不被云端覆盖）
                    new_instance._user_manually_selected_model = getattr(self, "_user_manually_selected_model", False)
                    # 性能优化：直接复制 _valid_configs，避免新窗口在 showEvent 中
                    # 重新从磁盘加载全部服务商配置（_load_model_configs 很重）
                    new_instance._valid_configs = dict(self._valid_configs)
                    new_instance._update_model_selector_btn()
            except Exception:
                pass  # 忽略模型复制失败

            # 注：_is_duplicate_window 已在 __init__(source_window=self) 中设置，
            # showEvent 据此跳过冗余初始化步骤（_load_model_configs / _sync_working_directory）。

            # ── 多窗口隔离：仅分支会话复制工具权限；新建窗口使用系统默认 ──
            # branch=True（分支当前对话）需要继承原窗口的工具权限；
            # branch=False（新建窗口/标签页）跳过 copy_state_from，保持
            # ToolPermissionController.__init__ 的默认行为，从全局 Settings
            # 加载系统默认工具偏好。
            if branch:
                try:
                    if hasattr(self, "_tool_permission_controller") and hasattr(
                        new_instance, "_tool_permission_controller"
                    ):
                        new_instance._tool_permission_controller.copy_state_from(self._tool_permission_controller)
                except Exception:
                    pass  # 忽略权限复制失败

            # 设置 session 初始化的标志，避免重复创建新 session
            # 并标记为新会话模式，跳过历史会话恢复
            # 注意：不要设置 _session_initialized，让 showEvent 正常执行初始化
            new_instance._skip_restore_history = True  # 跳过历史会话恢复

            # ── 统一路由到 Tab 管理器 ──
            # 多窗口模式已下线，禁止降级为独立 ToolPopupDialog（幽灵窗口）。
            # 若单例未就绪则惰性重建并确保可见。（TabManagerWindow 为模块级导入）
            tm = TabManagerWindow.get_instance() or TabManagerWindow.create_instance()
            if not tm.isVisible():
                tm.show()
            tm.add_window(new_instance)
            logger.debug("[TabMode] 已添加新窗口到 Tab 管理器")
            return
        except Exception as e:
            InfoBar.error(
                "复制失败",
                str(e),
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
        except BaseException:
            # 极端情况（如 KeyboardInterrupt）也要兜底，防止 pyqt5_err_print → qFatal → abort
            import traceback

            from loguru import logger as _log

            _log.error(f"[_duplicate_window] 未预期的异常: {traceback.format_exc()}")

    def _create_fresh_window(
        self,
        branch_data: dict = None,
        team_agent: str = "",
        team_name: str = "",
        team_run_id: str = "",
    ):
        """创建一个全新的空白会话窗口（不复制任何已有窗口的上下文/会话内容）。

        用于团队模板加载等需要纯净新窗口的场景：
        - 不传 source_window（走完整初始化，不继承项目/模型/分支上下文）
        - 跳过历史恢复（创建全新空白会话）
        - Tab 模式：加入 Tab 管理器（纯追加，不动已有标签页）

        Args:
            branch_data: 可选。若提供，则在 add_window 之前赋值给新窗口的
                _branch_session_data，确保 showEvent 触发
                _apply_branch_or_create_session 时分支数据已就绪，
                避免"窗口已显示、分支数据尚未赋值"的竞态导致走了
                _create_new_session（历史会话无法加载的根因之一）。
            team_agent: 可选。团队角色名（D2：标记前置——在 add_window
                **之前**写入窗口，使 Tab 管理器 add_window 时
                _resolve_tab_team_id 直接命中团队分组，消除"创建与注册
                分离"竞态窗口期；空串表示非团队窗口）。
            team_name: 可选。团队显示名（模板名），随 team_agent 一起前置。
            team_run_id: 可选。团队运行标识（分组 key），随 team_agent 一起前置。
        """
        try:
            from PyQt5 import sip

            if sip.isdeleted(self) or sip.isdeleted(self.homepage):
                return None
            valid_homepage = self.homepage
            if valid_homepage is None:
                return None

            # 全新窗口：不带 source_window，走完整初始化（不复制任何上下文）
            new_instance = OpenAIChatToolWindow(valid_homepage)
            # 跳过历史会话恢复，创建全新空白会话
            new_instance._skip_restore_history = True
            # 🛡️ 分支数据在 add_window（触发 showEvent）之前赋值，
            # 消除"分支数据赋值太晚"竞态（见 docstring）。
            if branch_data is not None:
                new_instance._branch_session_data = branch_data
            # 🛡️ D2 根治：团队标记在 add_window **之前**写入，add_window 内
            # _resolve_tab_team_id 直接命中团队分组，不再先落独立区再靠
            # 300ms 后置 refresh_capsule 补救（成员缺位竞态根因）。
            if team_agent:
                new_instance._team_agent_name = team_agent
                new_instance._team_name = team_name or ""
                new_instance._team_run_id = team_run_id or ""

            # 统一路由到 Tab 管理器：多窗口模式已下线（见 main.py 启动强约束），
            # 一律以 Tab 形式承载，禁止降级为独立 ToolPopupDialog，
            # 避免 /team --load 为每个模板角色弹出"幽灵窗口"。
            # 若单例因异常未就绪则惰性重建，保证模板窗口一定进入 Tab 容器。
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance() or TabManagerWindow.create_instance()
            if not tm.isVisible():
                tm.show()
            tm.add_window(new_instance)
            return new_instance
        except Exception as e:
            logger.error(f"[_create_fresh_window] 创建全新窗口失败: {e}")
            # 🛡️ E2 回收：窗口已构造但注册（add_window）抛异常 → 主动回收，
            # 避免窗口残留在 _instances / Tab 容器成为"幽灵窗口"。
            if "new_instance" in locals() and new_instance is not None:
                _abort_team_window(new_instance)
            return None

    def _request_tool_start_ui_sync(self, tool_call_id: str, tool_name: str, arguments: dict, round_id: str = None):
        self.toolStartUiSyncRequested.emit(tool_call_id, tool_name, arguments or {}, round_id or "")

    def _handle_tool_start_ui_sync(self, tool_call_id: str, tool_name: str, arguments: object, round_id: str):
        """工具开始时的 UI 同步处理"""
        self._on_tool_call_started(tool_call_id, tool_name, arguments or {}, round_id)
        # 🔧 处理等待的信号：确保排在前面的 content_received 信号（文本内容）
        # 在工具执行前被主线程处理并渲染到 DOM，避免文本延迟到工具执行完毕才显示
        # 注意：此处的 processEvents 在主线程运行，不会阻塞后台 worker
        # [PERF] 限量版：最多处理 5ms 事件（全量 processEvents 会无界扫描 pending
        # 事件队列，拖慢主线程；5ms 上限足以 flush content_received 等 posted 信号，
        # 同时避免鼠标/动画等高频事件长时间占住主线程）。
        QApplication.processEvents(QEventLoop.AllEvents, 5)

    def _get_chat_cards_for_engine(self):
        cards = []
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if isinstance(widget, MessageCard):
                    cards.append(widget)
        return cards

    def _ensure_thinking_fields(self, config: dict):
        """以 models.dev / 模型能力为准，确保思考字段与模型实际能力一致。

        在 model_overrides 叠加后调用，防止旧覆盖数据回补思考字段。
        """
        if not self._current_model_name:
            return
        from app.core.model_capabilities import get_model_capabilities

        caps = get_model_capabilities(self._current_model_name)
        if not caps.get("supports_thinking", False):
            config.pop("思考模式", None)
            config.pop("思考等级", None)
            config.pop("思考预算", None)

    def _get_current_model_config(self):
        """获取当前选中的模型配置，实时从系统配置读取

        多窗口隔离：使用 _current_model_name 覆盖全局配置中的模型名称，
        确保每个窗口使用自己选中的模型，而非其他窗口最后选择的模型。

        合并顺序（低 → 高）：
            1. _valid_configs (provider 实例 + 服务商默认)
            2. apply_model_defaults (硬编码兜底 + 模型能力)
            3. model_overrides[当前模型名] (用户调过的参数，最高)
        """
        selected_name = (
            self._current_provider_name
            if self._current_provider_name
            else (list(self._valid_configs.keys())[0] if self._valid_configs else "")
        )

        # 优先从 _valid_configs 获取（已合并默认配置）
        if selected_name in self._valid_configs:
            config = self._valid_configs[selected_name].copy()
            # 确保使用当前窗口选中的模型名称，而非全局配置中的模型名称（多窗口隔离）
            if self._current_model_name:
                config["模型名称"] = self._current_model_name
            # 叠加模型默认值（硬编码兜底 + 模型能力，会覆盖 FREE_PROVIDERS 的部分默认值）
            config = apply_model_defaults(config, self._current_model_name)
            # 叠加用户按模型名覆盖的参数（最高优先级）
            # key = "服务商名||模型名"，按服务商隔离同名模型
            model_overrides = getattr(self.cfg, "llm_model_overrides", None)
            if model_overrides and self._current_model_name:
                override_data = model_overrides.value or {}
                overrides = None
                if self._current_provider_name:
                    pname = self._valid_configs.get(self._current_provider_name, {}).get(
                        "provider_name", self._current_provider_name
                    )
                    overrides = override_data.get(f"{pname}||{self._current_model_name}")
                # 向后兼容：旧格式（纯模型名）兜底
                if overrides is None:
                    overrides = override_data.get(self._current_model_name, {})
                if overrides:
                    config.update(overrides)
            # 模型覆盖数据可能回补思考字段，以 models.dev 为准重新检查
            self._ensure_thinking_fields(config)
            return config

        return {}

    def _get_current_session_messages_for_tools(self) -> List[Dict[str, Any]]:
        session = self.session_manager.get_current_session()
        if not session:
            return []
        return list(session.messages or [])

    def _maybe_build_deferred_content(self):
        """【P2 懒加载】窗口首次激活（变为可见）时补建延迟的重型内容

        由 showEvent 触发。仅当本窗口此前因未激活而置了待建标记时才补建：
        - _deferred_build_pending  → 重新触发 _deferred_build_cards（7 张卡片）
        - _settings_popup_pending  → 重新触发 _build_settings_popup
        - _pixel_pet_pending       → 重新触发 _init_pixel_pet

        已构建（无 pending）时直接返回，保证"首次激活后与现有行为等价"，
        不会重复构建、不会改变构建内容与时机。

        销毁守卫：showEvent 是 Qt 事件处理器，其栈内异常会直接 abort（不被
        pytest-qt 捕获）；窗口已销毁（deleteLater 后）触发的补建一律跳过，
        补建也经 _safe_timer_call 调度，避免事件循环中访问已删 C++ 对象。
        """
        if getattr(self, "_is_destroyed", False):
            return
        try:
            from PyQt5 import sip

            if sip.isdeleted(self):
                return
        except Exception:
            pass
        if not self.isVisible():
            return
        if getattr(self, "_deferred_build_pending", False):
            self._deferred_build_pending = False
            QTimer.singleShot(0, lambda: self._safe_timer_call(self._deferred_build_cards))
        if getattr(self, "_settings_popup_pending", False):
            self._settings_popup_pending = False
            QTimer.singleShot(0, lambda: self._safe_timer_call(self._build_settings_popup))
        if getattr(self, "_pixel_pet_pending", False):
            self._pixel_pet_pending = False
            QTimer.singleShot(0, lambda: self._safe_timer_call(self._init_pixel_pet))

    def showEvent(self, event):
        # P2 懒加载：窗口变为可见（Tab 模式被选中 / 独立窗口显示）时，
        # 补建此前因未激活而延迟的重型内容（7 张卡片 / settings / 桌宠）。
        # 已构建的窗口此方法为空操作，不影响现有首次初始化流程。
        self._maybe_build_deferred_content()
        if getattr(self, "_session_initialized", False):
            super().showEvent(event)
            self._connect_opacity_signal()
            return
        self._session_initialized = True
        # 标记正在初始化，防止窗口在初始化完成前被关闭导致竞态条件
        self._initialization_in_progress = True

        # 判断是否为复制/分支窗口（__init__ 中通过 source_window 参数设置）
        is_duplicate = getattr(self, "_is_duplicate_window", False)

        # 如果有分支数据，延迟调用分支会话处理，避免与 _restore_latest_or_create_session 冲突
        # 注：_load_agent_list 由 _create_new_session / _apply_branch_or_create_session 内部调用，此处不需要重复触发
        if getattr(self, "_branch_session_data", None):
            QTimer.singleShot(50, lambda: self._safe_timer_call(self._apply_branch_or_create_session))
        else:
            QTimer.singleShot(0, lambda: self._safe_timer_call(self._create_new_session))

        # 性能优化：复制窗口已从源窗口复制了 _valid_configs 和 workdir，
        # 跳过从磁盘重载模型配置和工作目录，直接进入完成状态
        if is_duplicate:
            QTimer.singleShot(0, lambda: self._safe_timer_call(self._on_initialization_complete))
            # 【按项目定义】复制窗口 workdir 同步：
            # _duplicate_window 不再复制源窗口 workdir（tool_executor 延迟创建
            # 时 set_workdir 也会被跳过），此处延迟到 tool_executor 创建后，
            # 由 _sync_working_directory 按当前项目从 DB 读取项目定义的工作目录
            # （_current_workdir 为空 → get_working_directory 兜底 → 临时目录），
            # 保证 project_root 与项目一致，且复制链不传播错误路径。
            QTimer.singleShot(500, lambda: self._safe_timer_call(self._sync_working_directory))
        else:
            # [PERF] 延迟非关键初始化到窗口首帧绘制之后，让用户先看到可交互的 UI
            # _load_model_configs 遍历所有服务商配置（50-200ms），
            # _sync_working_directory 文件系统检测（20-50ms），
            # 均匀分散到 1.5s-2.5s 窗口内，避免同时爆发导致 UI 冻结
            QTimer.singleShot(1500, lambda: self._safe_timer_call(self._load_model_configs))
            # 初始化当前项目的工作目录
            QTimer.singleShot(2000, lambda: self._safe_timer_call(self._sync_working_directory))
            # 初始化完成后解除保护
            QTimer.singleShot(2500, lambda: self._safe_timer_call(self._on_initialization_complete))
        self._connect_opacity_signal()
        super().showEvent(event)

    def _on_initialization_complete(self):
        """初始化完成后调用，解除保护标志"""
        self._initialization_in_progress = False
        logger.debug("[OpenAIChatToolWindow] Initialization complete")

        # 启动子智能体日志自动清理（每6小时清理一次，保留14天）
        self._start_subagent_log_cleanup()

        # 复制/分支窗口的 _valid_configs（含模型列表）已在 _duplicate_window 中
        # 从源窗口直接复制，不需要再重新拉取 OpenCode 免费模型列表，避免冗余网络请求和日志
        if not getattr(self, "_is_duplicate_window", False):
            QTimer.singleShot(3000, lambda: self._safe_timer_call(self._async_refresh_opencode_models))
            # 设置弹窗在 3500ms 构建，提醒在 5s 后弹（确保弹窗已就绪）
            QTimer.singleShot(5000, lambda: self._safe_timer_call(self._check_gitee_sync_reminder))
        # models.dev 动态数据后台预热（内存缓存未填充才发网络，模块级单飞去重；
        # 早于 1500ms _load_model_configs 的首次主线程读取，UI 路径零阻塞）
        QTimer.singleShot(1000, lambda: self._safe_timer_call(self._start_models_dev_sync))

    @classmethod
    def _on_class_cleanup_timer(cls):
        """类级清理 timer 回调：找任意存活窗口执行清理"""
        for win in getattr(cls, "_instances", []):
            if not getattr(win, "_is_destroyed", False):
                win._do_clean_subagent_logs()
                return

    @classmethod
    def _start_subagent_log_cleanup(cls):
        """定期清理子智能体日志，避免无限堆积（全局仅一个 timer）"""
        if cls._class_subagent_log_cleanup_timer is not None:
            return  # 已有全局 timer
        # 找任意一个存活窗口执行首次清理
        for win in getattr(cls, "_instances", []):
            if not getattr(win, "_is_destroyed", False):
                win._do_clean_subagent_logs()
                break
        cls._class_subagent_log_cleanup_timer = QTimer()
        cls._class_subagent_log_cleanup_timer.setInterval(6 * 60 * 60 * 1000)  # 6小时
        cls._class_subagent_log_cleanup_timer.timeout.connect(cls._on_class_cleanup_timer)
        cls._class_subagent_log_cleanup_timer.start()

    def _do_clean_subagent_logs(self):
        """执行子智能体日志清理（保留14天）"""
        try:
            if self.backend and self.backend.session_store:
                deleted = self.backend.session_store.clear_old_subagent_tasks(14)
                if deleted > 0:
                    logger.info(f"[Cleanup] 已清理 {deleted} 条子智能体日志（保留14天）")
        except Exception as e:
            logger.warning(f"[Cleanup] 子智能体日志清理异常: {e}")

    def _safe_timer_call(self, func):
        """安全执行 QTimer.singleShot 回调，在 widget 已销毁时自动跳过

        防止 QTimer 回调在窗口关闭(deleteLater)后执行导致 segfault。
        必须在所有通过 QTimer.singleShot 调度的回调中使用。
        """
        if getattr(self, "_is_destroyed", False):
            logger.debug(f"[OpenAIChatToolWindow] Skipping timer callback {func.__name__}: widget destroyed")
            return
        try:
            from PyQt5 import sip

            if sip.isdeleted(self):
                logger.debug(f"[OpenAIChatToolWindow] Skipping timer callback {func.__name__}: C++ object deleted")
                return
        except Exception:
            pass
        try:
            func()
        except RuntimeError as e:
            # PyQt5 在访问已删除 C++ 对象时抛出 RuntimeError
            if "C++" in str(e) or "wrapped C/C++" in str(e):
                logger.debug(f"[OpenAIChatToolWindow] Skipping timer callback {func.__name__}: {e}")
                return
            raise

    def eventFilter(self, obj, event):
        """处理窗口大小变化"""
        # 输入卡 wrapper / 容器尺寸变化 → 同步胶囊光晕底层几何，
        # 否则输入框高度自适应（输入多行内容时）会让光晕"卡"在旧位置
        if event.type() == event.Type.Resize and obj in (
            getattr(self, "_input_card_wrapper", None),
            getattr(self, "_bottom_input_container", None),
        ):
            self._position_input_glow_underlay()

        # 拖拽文件到扩展区域（输入卡空白区 / 附件行 / 消息列表）→ 添加 AttachmentChip
        # chat_container = 消息列表区：拖到对话区域任意位置都进入附件；
        # 项目卡片/关键文档卡片等位于独立卡片容器且自带 drop 处理，不在此列。
        if obj in (
            getattr(self, "_input_card", None),
            getattr(self, "_attach_container", None),
            getattr(self, "chat_container", None),
        ):
            etype = event.type()
            if etype == QEvent.DragEnter:
                if event.mimeData().hasUrls():
                    event.acceptProposedAction()
                    return True
            elif etype == QEvent.Drop:
                paths = []
                if event.mimeData().hasUrls():
                    for url in event.mimeData().urls():
                        local_path = url.toLocalFile()
                        if local_path and os.path.exists(local_path):
                            paths.append(local_path)
                if paths:
                    self._on_files_dropped(paths)
                    event.acceptProposedAction()
                    return True

        return super().eventFilter(obj, event)

    def _connect_opacity_signal(self):
        """连接父窗口的透明度变化信号"""
        if getattr(self, "_opacity_signal_connected", False):
            return
        parent = self.parent()
        if parent and hasattr(parent, "globalOpacityChanged"):
            parent.globalOpacityChanged.connect(self._on_global_opacity_changed)
            self._opacity_signal_connected = True

    def _on_global_opacity_changed(self, opacity: float):
        """响应全局透明度变化，更新所有子组件的透明度"""
        self._update_widgets_opacity(opacity)

    def _update_widgets_opacity(self, opacity: float):
        """更新所有需要响应透明度变化的组件"""
        # 更新待办事项悬浮框
        if self._todo_floating_widget:
            self._todo_floating_widget.set_opacity(opacity)
        # 更新模型配置卡片
        if self._model_config_card:
            self._model_config_card.set_opacity(opacity)
        # 更新历史会话卡片
        if self._history_card:
            self._history_card.set_opacity(opacity)
        # 更新记忆管理卡片
        if self._memory_card:
            self._memory_card.set_opacity(opacity)
        # 更新设置卡片
        if self._settings_popup:
            self._settings_popup.set_opacity(opacity)
        # 更新子智能体紧凑悬浮框
        if hasattr(self, "_sub_agent_compact_widget") and self._sub_agent_compact_widget:
            self._sub_agent_compact_widget.set_opacity(opacity)
        # 更新问题悬浮框
        if self._question_floating_widget:
            self._question_floating_widget.set_opacity(opacity)
        # 更新服务商编辑卡片
        if self._provider_edit_card:
            self._provider_edit_card.set_opacity(opacity)
        # 更新主窗口背景透明度
        self._update_window_bg_opacity(opacity)

    def _update_window_bg_opacity(self, opacity: float):
        """更新窗口背景透明度

        对话框背景已完全透明，由外层容器兜底，故此处直接设为透明。
        """
        self.setStyleSheet("background: transparent;")
        self.setAutoFillBackground(False)

    def _apply_branch_or_create_session(self):
        # 检查窗口是否仍然有效，防止在初始化期间窗口被关闭后继续执行
        if getattr(self, "_is_destroyed", False):
            logger.debug("[OpenAIChatToolWindow] Window destroyed before branch session creation, skipping")
            return
        try:
            from PyQt5 import sip

            if sip.isdeleted(self):
                logger.debug("[OpenAIChatToolWindow] C++ object deleted before branch session creation, skipping")
                return
        except Exception:
            pass

        branch_data = getattr(self, "_branch_session_data", None)
        if branch_data:
            # 使用分支数据创建会话（透传 project，恢复团队会话时保持原项目归属）
            self._create_branched_session(
                branch_data.get("messages", []),
                branch_data.get("name", "分支对话"),
                project=branch_data.get("project") or "",
            )
        else:
            # 没有分支数据，创建新会话
            self._create_new_session()

    def _create_branched_session(self, messages: List[Dict], name: str, project: str = ""):
        """创建分支会话并渲染消息

        Args:
            messages: 分支会话消息列表
            name: 会话标题
            project: 分支会话所属项目（空串则回落当前项目 _current_project）
        """
        # 检查窗口是否仍然有效，防止在初始化期间窗口被关闭后继续执行
        if getattr(self, "_is_destroyed", False):
            logger.debug("[OpenAIChatToolWindow] Window destroyed before branched session creation, skipping")
            return
        try:
            from PyQt5 import sip

            if sip.isdeleted(self):
                logger.debug("[OpenAIChatToolWindow] C++ object deleted before branched session creation, skipping")
                return
        except Exception:
            pass

        logger.info("[Branch] 开始创建分支会话")

        # 🛡️ 与 _create_new_session 对齐：切换前先保存被分支的旧会话，
        # 否则旧会话可能未落库，历史面板刷新后仍缺失该条目。
        try:
            self._auto_save_current_session()
        except Exception:
            logger.exception("Failed to auto-save current session before creating a branched session")

        # 停止当前对话并清理
        if self._is_streaming and self.backend.chat_engine:
            self.backend.stop_streaming()
            self._is_streaming = False
            self._topic_summary_cancelled = True  # 🛡️ 取消标题生成重试
            self._toggle_send_stop(False)
        elif self.backend.chat_engine:
            # 即使不在流式输出，也要清理 worker
            self.backend.cleanup_worker()

        # 切换会话前彻底清理卡片
        self._cache_current_session_cards()
        # 清空批量渲染索引，防止虚拟滚动定时器触发的回收访问已移出布局的旧卡片
        self._batch_cards = []
        self._message_batch = []
        self._visible_batch_start = 0
        self._visible_batch_end = 0
        self._virtual_scroll_timer.stop()
        # 只重置会话状态，保留 tool_executor（分支后还需要执行工具）
        if self.backend.tool_executor:
            self.backend.reset_session_state()
        session = self.backend.create_session()
        session.messages = messages
        session.name = name
        session.topic_summary = name  # 同步 topic_summary，避免 _display_current_session 覆盖 title_edit
        # 记录分支所属项目(走 metadata 而非新增字段,保持核心数据模型不变)
        # 🛡️ 支持 project 透传：恢复团队会话时用会话记录的 project；
        # 无透传（空串）回落当前项目。
        resolved_project = project or self._current_project
        session.metadata["project"] = resolved_project
        self._current_project = resolved_project
        self.backend._current_project = resolved_project
        if self.backend.tool_executor:
            try:
                self.backend.tool_executor.set_current_project(resolved_project)
            except Exception:
                pass
        # 🛡️ 同步标题栏项目显示（与 _load_history_session_from_popup 一致），
        # 否则恢复团队会话后 UI 仍显示旧项目名
        if hasattr(self, "_project_label"):
            self._project_label.setText(resolved_project)
        self._current_session_id = session.session_id
        self._load_agent_list()

        # 清空聊天区域
        self._clear_chat_area()
        self.title_edit.setText(name)
        self.node_preview.clear_nodes()

        # 重置输入框高度
        if hasattr(self, "input_area"):
            self.input_area.setFixedHeight(72)
            self.input_area._initializing = False

        # 复用现有的会话显示逻辑
        self._display_current_session()

        # 🛡️ Bug2（恢复窗口落库）：恢复团队会话经本方法注入历史消息后，窗口
        # _team_run_id 已在恢复循环同步设置（_create_fresh_window 返回后立即
        # 赋值，先于本方法执行）。显式置脏，保证窗口关闭时 _auto_save_current_session
        # 落一条带团队元数据（team_run_id/team_name/agent_name/team_members
        # 快照）的会话记录——否则未触发成员窗口关闭不置脏被跳过保存，历史聚合
        # _merge_team_lightweight 漏掉该成员。复制窗口（非团队，_team_run_id 空）
        # 不受影响，保持原"无变更不保存"语义。
        if getattr(self, "_team_run_id", ""):
            self._session_dirty = True

        # 🆕 刷新历史面板：分支创建新会话后，历史面板 UI 需同步最新数据
        # （被分支的旧会话已 autosave 入库；分支新会话尚未落库不显示，属预期）。
        # 仅历史卡片可见时执行（不可见时 0 开销），下次打开面板仍走
        # _toggle_history_card → _refresh_history_toggle_panel 拉取最新数据。
        refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

    def _refresh_cache_stats(self):
        """刷新缓存统计显示（对话完成后调用）"""
        ring = getattr(self, "context_usage_ring", None)
        if not ring:
            return

        stats_dict = self.backend.get_last_cache_stats()

        if not stats_dict:
            worker = self.backend.get_current_worker()
            if worker:
                try:
                    raw = worker.get_cache_stats()
                    if hasattr(raw, "to_dict"):
                        stats_dict = raw.to_dict()
                    elif isinstance(raw, dict):
                        stats_dict = raw
                except Exception:
                    pass

        if not stats_dict:
            return

        try:
            cost_savings = 0.0
            cost_with = stats_dict.get("cost_usd", 0.0)
            cost_without = stats_dict.get("cost_without_cache_usd", 0.0)
            if cost_without > 0:
                cost_savings = cost_without - cost_with

            hit_rate = stats_dict.get("hit_rate", 0.0)
            per_request_hit_rate = stats_dict.get("per_request_hit_rate", 0.0)
            total_input_hit_rate = stats_dict.get("total_input_hit_rate", 0.0)
            read_tokens = stats_dict.get("cache_read_tokens", 0)
            write_tokens = stats_dict.get("cache_creation_5m_tokens", 0) + stats_dict.get("cache_creation_1h_tokens", 0)
            cache_hits = stats_dict.get("cache_hits", 0)
            cache_misses = stats_dict.get("cache_misses", 0)

            logger.info(
                f"[CacheStats] hit_rate={hit_rate:.1%}"
                f" per_req={per_request_hit_rate:.1%}"
                f" total_input={total_input_hit_rate:.1%}"
                f" read={read_tokens} write={write_tokens}"
                f" hits={cache_hits} misses={cache_misses}"
                f" saved=${cost_savings:.4f}"
            )

            ring.set_cache_stats(
                hit_rate=hit_rate,
                read_tokens=read_tokens,
                write_tokens=write_tokens,
                cost_savings=cost_savings,
                per_request_hit_rate=per_request_hit_rate,
                total_input_hit_rate=total_input_hit_rate,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
                requests=stats_dict.get("requests", 0),
            )
        except Exception as e:
            logger.debug(f"[CacheStats] Failed to refresh: {e}")

    def setup_ui(self):
        Colors.refresh()
        # 注册自身为 ThemeManager 的刷新目标，热重载时自动级联
        theme_manager.register_refresh_target(self)
        # 动态更新主题选项
        update_theme_options()

        # 标题栏分组分隔线（1px 竖线，用主题色 DIVIDER_COLOR）
        def _make_vdivider() -> QFrame:
            div = QFrame(self)
            div.setFrameShape(QFrame.VLine)
            div.setFixedHeight(18)
            div.setFixedWidth(1)
            Colors.refresh()
            div.setStyleSheet(f"color: {Colors.DIVIDER_COLOR}; background: {Colors.DIVIDER_COLOR}; border: none;")
            return div

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        # 创建卡片容器
        self._top_card_container = TopCardContainer()
        self._bottom_card_container = BottomCardContainer()

        # ── 对话框背景完全透明 ──
        # 不再为 OpenAIChatToolWindow 叠加独立背景层（palette window_bg +
        # setAutoFillBackground），由外层容器兜底：Tab 内嵌时由
        # TabManagerWindow 的 #chatFrame 半透明背景（+全局背景图）透出；
        # 独立弹窗时由承载者背景垫底。保留 _window_bg_color 字段兼容旧引用。
        colors = theme_manager.get_current_colors()
        window_bg = colors.get("window_bg", "rgba(102, 198, 255, 0.04)")
        self._window_bg_color = window_bg
        self.setAutoFillBackground(False)

        # 字体样式
        self.setStyleSheet("")

        # ===== 像素小狐桌宠 =====
        self.pixel_pet: PixelPetWidget | None = None
        from app.utils.config import Settings

        if Settings.get_instance().pet_enabled.value:
            # 性能优化：桌宠非关键装饰，延迟到首帧后再初始化，
            # 避免在窗口出现的关键路径上加载 spritesheet / 播放入场动画。
            QTimer.singleShot(0, self._init_pixel_pet)

        # 桌宠显示开关的实时响应
        Settings.get_instance().pet_enabled.valueChanged.connect(self._on_pet_enabled_changed)

        session_bar_layout = QHBoxLayout()

        # ===== 项目+分支组合控件（一体感布局） =====
        self._project_branch_container = QFrame(self)
        self._project_branch_container.setObjectName("projectBranchContainer")
        pb_layout = QHBoxLayout(self._project_branch_container)
        pb_layout.setContentsMargins(8, 0, 8, 0)  # 左侧留出 padding，与标题编辑区保持间距
        pb_layout.setSpacing(2)

        # 项目方形 icon（缩写字母，flat design squircle 风格）
        self._project_avatar = _SquareAvatar(
            extract_project_initials(self._current_project), get_project_color(self._current_project), self, size=24
        )
        self._project_avatar.setCursor(Qt.PointingHandCursor)
        self._project_avatar.mousePressEvent = self._on_project_label_clicked
        self._project_avatar.setToolTip("点击切换项目")  # tooltip 在 _update_branch() 中动态更新（含项目名/路径/分支）
        pb_layout.addWidget(self._project_avatar)

        # 项目选择标签（隐藏，仅通过 avatar icon 展示项目缩写）
        self._project_label = QLabel(self._current_project, self)
        self._project_label.setCursor(Qt.PointingHandCursor)
        self._project_label.mousePressEvent = self._on_project_label_clicked
        self._project_label.setToolTip("点击切换项目")
        self._project_label.setVisible(False)

        # 分支分隔符（三角箭头，面包屑风格）
        self._pb_separator = QLabel("▸", self)
        self._pb_separator.setAlignment(Qt.AlignCenter)
        self._pb_separator.setVisible(False)
        pb_layout.addWidget(self._pb_separator)

        # Git 分支标签
        self._branch_widget = PushButton(text="main", parent=self)
        self._branch_widget.setObjectName("_branchWidget")
        self._branch_widget.clicked.connect(self._on_branch_label_clicked)
        self._branch_widget.setToolTip("当前 Git 分支 — 点击打开关键文档")
        self._branch_widget.setAutoDefault(False)  # 防止 QDialog 在 Enter 时误触发
        self._branch_widget.setVisible(False)
        self._refresh_branch_widget_style()
        pb_layout.addWidget(self._branch_widget)

        self._refresh_project_branch_style()
        # 性能优化：复制/分支窗口直接从源窗口复制 git 分支标签状态，
        # 跳过同步 git 子进程（最坏可达 3s），避免重复窗口出现卡顿
        if getattr(self, "_is_duplicate_window", False) and getattr(self, "_source_window", None):
            self._copy_branch_from(self._source_window)
        else:
            self._update_branch()

        # 将组合控件加入布局
        # 标题编辑（行内编辑模式）
        self.title_edit = TitleEditWidget("新对话", self)
        font_css = get_font_family_css()
        Colors.refresh()
        title_style = f"""QLabel {{
            color: {Colors.TEXT_PRIMARY};
            {font_size_css(15)}
            font-weight: bold;
            padding: 6px 4px;
            border-radius: 10px;
            background-color: transparent;
            {font_css}
        }}
        QLabel:hover {{
            background-color: {Colors.HOVER_BG};
        }}
        QLineEdit {{
            color: {Colors.TEXT_PRIMARY};
            {font_size_css(15)}
            font-weight: bold;
            padding: 6px 4px;
            border-radius: 10px;
            background-color: transparent;
            border: none;
            {font_css}
        }}
        QLineEdit:focus {{
            background-color: {Colors.TOOLBAR_BG};
            border: 1px solid {Colors.BORDER};
        }}
    """
        self.title_edit.setStyleSheet(title_style)
        self.title_edit.returnPressed.connect(self._on_title_edit_finished)
        self.title_edit.editingFinished.connect(self._on_title_edit_finished)

        session_bar_layout.addWidget(self._project_branch_container)

        # 标题栏分组分隔线：[项目▸分支] │ [标题]
        session_bar_layout.addWidget(_make_vdivider())

        session_bar_layout.addWidget(self.title_edit, 1)  # 占据剩余空间

        # 先创建余额/用量/上下文组件（稍后添加到底部工具栏，模型选择右侧）
        self.balance_display = BalanceDisplay(self)
        self.coding_plan_ring = CodingPlanRing(self)
        # 圆环隐藏状态（ring 初始隐藏）：_on_coding_plan_result 据此判断
        # 是否打"无数据"日志，避免多标签页下无数据广播刷屏
        self._coding_plan_hidden = True
        self.context_usage_ring = ContextUsageRing(self)

        # 标题栏右侧：分享按钮 + 当前会话历史问题按钮（替代时间线节点）
        right_layout = QHBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(2)
        right_layout.setAlignment(Qt.AlignVCenter)

        # 历史问题按钮（点击弹窗显示当前会话所有用户提问，支持快速跳转）
        self._history_questions_btn = TransparentToolButton(FluentIcon.MESSAGE, self)
        self._history_questions_btn.setFixedSize(28, 28)
        self._history_questions_btn.setToolTip("当前会话的用户提问历史")
        self._history_questions_btn.clicked.connect(self._toggle_history_questions_popup)
        right_layout.addWidget(self._history_questions_btn)
        # 右上角 InfoBadge，显示用户问题总数（自动跟随按钮位置）
        self._history_questions_badge = InfoBadge.attension(
            0, parent=self, target=self._history_questions_btn, position=InfoBadgePosition.LEFT
        )
        self._history_questions_badge.setVisible(False)

        # 分享按钮
        self._share_btn = TransparentToolButton(FluentIcon.SHARE, self)
        self._share_btn.setFixedSize(28, 28)
        self._share_btn.setToolTip("分享当前对话")
        self._share_btn.clicked.connect(self._on_share_clicked)
        right_layout.addWidget(self._share_btn)

        # 差异对比按钮（从右下移到右上）
        self.diff_btn = TransparentToolButton(get_icon("差异对比"), self)
        self.diff_btn.setFixedSize(28, 28)
        self.diff_btn.setToolTip("会话级差异对比")
        self.diff_btn.clicked.connect(self._open_diff_viewer)
        right_layout.addWidget(self.diff_btn)

        right_layout.addSpacing(8)  # 右侧留白

        session_bar_layout.addLayout(right_layout)
        layout.addLayout(session_bar_layout)

        # 时间线节点不再显示为 UI 元素，但保留 widget 及其内部逻辑供历史问题弹窗使用
        self.node_preview = ConversationNodePreview(self)
        self.node_preview.nodeClicked.connect(self._on_node_preview_clicked)
        self.node_preview.setVisible(False)

        # 监听服务商配置变更，确保多窗口同步（全局监听，需尽早连接）
        self.cfg.llm_saved_providers.valueChanged.connect(self._on_providers_config_changed)
        # 监听技能配置变更（启用/禁用），确保多窗口同步
        self.cfg.llm_enabled_skills.valueChanged.connect(self._on_skills_config_changed)

        # gitee 配置同步完成 → 本窗口按云端 llm_selected_model 刷新模型选择。
        # 不用 valueChanged 监听（llm_selected_model 全项目无监听器，且直接监听会破坏
        # 多窗口各自选择独立模型），改为同步服务在配置全部写回内存后一次性通知。
        # 窗口销毁时 PyQt 自动断开连接，不泄漏。
        try:
            from app.core.config_sync import ConfigSyncService

            ConfigSyncService.get_instance().settingsRestored.connect(self._apply_synced_model_selection)
            # Gitee token 真失效（syncDone 含"已失效"）→ 触发全局「重新绑定」提醒
            ConfigSyncService.get_instance().syncDone.connect(self._on_gitee_sync_done)
        except Exception:
            pass

        # 性能优化：设置弹窗（含全部服务商/Hook/MCP/Gateway 子卡片）是隐藏的重型构件，
        # 不再预构建——改为按需构建（首次打开设置时 _build_settings_popup，见
        # global_card_controller.ensure_settings_popup 兜底），消除 3500ms 定时器在
        # tab 切换/空闲期的无谓主线程开销（T22 实测 settings 占首切卡顿 29%）。

        # ── 全局卡片（settings/provider/hook/mcp）已迁移到 Tab 窗口层 ──
        # 实例由 GlobalCardController 持有，本类通过下方只读 property 兼容旧读取点
        # （透明度调节、主题刷新、字体缩放等仍按旧属性名访问）

        self._todo_floating_widget = TodoFloatingWidget(self)
        self._todo_floating_widget.setVisible(False)
        self._todo_floating_widget.closed.connect(self._on_todo_closed)

        self._sub_agent_compact_widget = SubAgentCompactFloatingWidget(self)
        self._sub_agent_compact_widget.setVisible(False)
        self._sub_agent_compact_widget.closed.connect(self._on_sub_agent_compact_closed)
        self._sub_agent_compact_widget.enter_session_requested.connect(self._on_sub_agent_enter_session)
        self._sub_agent_compact_widget.stop_subagent_requested.connect(self._on_sub_agent_stop_requested)

        # 多窗口隔离的 function 命令处理器（每个窗口独立，不被新窗口覆盖）
        self._function_command_handlers = {
            "subagents": self._handle_subagents_command,
            "title-gen": self._handle_title_gen_command,
            "compact": self._handle_compact_command,
            "todos": self._handle_todos_command,
            "team": self._handle_team_command,
            "toggle-window": self._handle_toggle_window_command,
            "clear": self._handle_clear_command,
        }

        # 下方卡片容器 - 添加 SubAgentCompact
        self._bottom_card_container.add_card("sub_agent_compact", self._sub_agent_compact_widget)

        # 上方卡片容器 - 添加 Todo
        self._top_card_container.add_card("todo", self._todo_floating_widget)

        # 注：mcp_edit/hook_edit/provider_edit 三张编辑卡片已改为懒创建，
        # 在 _ensure_xxx_card() 中按需添加至容器，此处跳过避免访问 None。

        layout.addWidget(self._top_card_container)

        self.chat_scroll_area = SingleDirectionScrollArea(self)
        self.chat_scroll_area.setMinimumHeight(0)
        self.chat_scroll_area.setMinimumWidth(320)
        self.chat_scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.chat_scroll_area.setStyleSheet(CHAT_SCROLL_STYLE)
        self.chat_scroll_area.setWidgetResizable(True)
        self.chat_scroll_area.setViewportMargins(2, 2, 10, 2)
        self.chat_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background: transparent;")
        self.chat_container.setAcceptDrops(True)
        self.chat_container.installEventFilter(self)
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(6, 6, 6, 6)
        self.chat_layout.setSpacing(8)
        self.chat_layout.setAlignment(Qt.AlignBottom)
        self.chat_scroll_area.setWidget(self.chat_container)

        # 连接滚动事件，触发虚拟滚动回收
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        scroll_bar.valueChanged.connect(self._on_chat_scrolled)

        layout.addWidget(self.chat_scroll_area, 1)

        # 下方卡片容器
        layout.addWidget(self._bottom_card_container)

        # ── 六张系统卡片框架懒创建（P0-1 性能优化）──
        # 原 setup_ui 同步段直接创建 6 张 BaseSettingsCard 框架（~160ms），
        # 改为 _ensure_xxx_card() 惰性创建：deferred 链预构建 + 打开入口兜底。
        # 属性名保持稳定（None 占位），引用点已有 hasattr/getattr/if 保护。
        self._history_card = None
        self._history_popup_card = None
        self._share_card = None
        self._share_card_content = None
        self._history_questions_card = None
        self._history_questions_card_content = None
        self._memory_card = None
        self._memory_card_popup = None
        self._model_config_card = None
        self._model_config_popup = None
        self._model_selector_card = None
        self._model_selector_card_content = None

        # 工具控制卡片（controller 由 _tool_permission_controller 在后续 set_controller 注入）
        self._tool_control_card = ToolControlCardFrame(self)
        # 🛡️ 如果 controller 已存在（__init__ 中在 super 之前创建时），立即绑定
        if hasattr(self, "_tool_permission_controller") and self._tool_permission_controller is not None:
            self._tool_control_card.set_controller(self._tool_permission_controller)
        self._tool_control_card.setObjectName("toolControlCard")
        self._tool_control_card.setMinimumHeight(250)
        self._tool_control_card.setVisible(False)
        self._tool_control_card.closed.connect(
            lambda: (
                self._card_manager.hide_card("tool_control", self._window_id),
                self._restore_after_system_close(),
            )
        )
        self._tool_control_card.togglesChanged.connect(lambda _: self._refresh_tool_toggle_btn())
        self._bottom_card_container.add_card("tool_control", self._tool_control_card)

        # 模型选择卡片框架懒创建（P0-1）：见上方 _ensure_model_selector_card() 说明

        # 项目选择卡片（Top 卡片，与 settings 同容器）
        self._project_selector_card = BaseSettingsCard("", "", self)
        self._project_selector_card.setMinimumHeight(200)  # 自适应窗口高度
        self._project_selector_card_content = ProjectSelectorCardContent()
        self._project_selector_card_content.projectSelected.connect(self._on_project_selected)
        self._project_selector_card_content.newProjectCreated.connect(self._on_new_project_created)
        self._project_selector_card_content.archiveProject.connect(self._on_archive_project)
        self._project_selector_card_content.exportProject.connect(self._on_export_project)
        self._project_selector_card_content.importProjectRequested.connect(self._on_import_project)
        self._project_selector_card_content.projectFileDropped.connect(self._on_project_file_dropped)
        self._project_selector_card_content.openFolderRequested.connect(self._on_open_project_folder)
        self._project_selector_card_content.folderDropped.connect(self._on_project_folder_dropped)

        self._project_selector_card.content_layout.addWidget(self._project_selector_card_content)
        # ── 新建项目输入放到标题栏 ──
        from PyQt5.QtWidgets import QLineEdit

        Colors.refresh()
        self._project_new_edit = QLineEdit(self._project_selector_card)
        self._project_new_edit.setPlaceholderText("新建/搜索项目...")
        self._project_new_edit.setMaximumWidth(220)
        self._project_new_edit.setMinimumWidth(130)
        self._project_new_edit.setFixedHeight(26)
        self._project_new_edit.setStyleSheet(f"""
            QLineEdit {{
                background: {Colors.HOVER_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                padding: 2px 6px;
                {font_size_css(11)}
                {get_font_family_css()}
            }}
            QLineEdit:focus {{
                border: 1px solid {Colors.TEXT_ACCENT};
            }}
            QLineEdit::placeholder {{
                color: {Colors.INPUT_PLACEHOLDER};
            }}
        """)
        self._project_new_edit.returnPressed.connect(self._on_header_new_project)
        self._project_new_edit.textChanged.connect(self._on_project_filter_changed)

        self._project_new_btn = TransparentToolButton(FluentIcon.ADD, self._project_selector_card)
        self._project_new_btn.setFixedSize(24, 24)
        self._project_new_btn.setToolTip("创建项目")
        self._project_new_btn.clicked.connect(self._on_header_new_project)

        # 选择文件夹按钮（+号右侧）
        self._project_open_folder_btn = TransparentToolButton(FluentIcon.FOLDER, self._project_selector_card)
        self._project_open_folder_btn.setFixedSize(24, 24)
        self._project_open_folder_btn.setToolTip("选择文件夹作为项目根目录")
        self._project_open_folder_btn.clicked.connect(self._on_project_open_folder_btn)

        # 导入项目按钮（从 .drifox_project 压缩包导入）
        self._project_import_btn = TransparentToolButton(get_icon("导入"), self._project_selector_card)
        self._project_import_btn.setFixedSize(24, 24)
        self._project_import_btn.setToolTip("导入项目（从 .drifox_project 压缩包）")
        self._project_import_btn.clicked.connect(self._on_import_project)

        # 插入到标题栏的额外按钮区（关闭按钮之前）
        self._project_selector_card._extra_buttons_container.insertWidget(0, self._project_new_edit)
        self._project_selector_card._extra_buttons_container.insertWidget(1, self._project_new_btn)
        self._project_selector_card._extra_buttons_container.insertWidget(2, self._project_open_folder_btn)
        self._project_selector_card._extra_buttons_container.insertWidget(3, self._project_import_btn)

        self._project_selector_card.setVisible(False)
        self._project_selector_card.closed.connect(
            lambda: (
                self._card_manager.hide_card("project_selector", self._window_id),
                self._restore_after_system_close(),
            )
        )

        # AutoLoop 配置卡 / 运行卡（延迟构建 T7）
        # 由 _deferred_card_steps 链（800ms 后）经 _ensure_auto_loop_config_card /
        # _ensure_auto_loop_running_card 惰性创建，移除 setup_ui 同步路径开销
        # （每次新建 tab 省 ~15ms，setup_ui 的 ~28%）。
        # 打开入口 _show_auto_loop_config 有 ensure 兜底，行为与同步创建一致。
        self._auto_loop_config_card = None
        self._auto_loop_running_card = None

        self._question_floating_widget = QuestionFloatingWidget(self)
        self._question_floating_widget.setVisible(False)
        self._question_floating_widget.answered.connect(self._on_question_answered)
        self._question_floating_widget.cancelled.connect(self._on_question_cancelled)
        self._question_floating_widget.previewRequested.connect(self._on_question_preview_requested)
        self._bottom_card_container.add_card("question", self._question_floating_widget)

        # 注册卡片到 CardManager（优先级：数值越小权限越高）
        self._register_cards_to_manager()

        # 系统卡片打开时隐藏文本输入框（保留按钮栏），关闭时恢复
        # _system_card_ids 在 __init__ 顶部初始化为 _BASE_SYSTEM_CARD_IDS，
        # UI 插件注册浮动卡片后通过 register_system_card() 扩展该集合。
        for _cid in self._system_card_ids:
            self._card_manager.on_card_shown(self._window_id, _cid, lambda cid: self._on_system_card_opened(cid))
            self._card_manager.on_card_hidden(self._window_id, _cid, lambda cid: self._on_system_card_closed(cid))

        # ===== 内置命令先注册（UI 插件命令依赖 CommandManager） =====
        # [PERF] 延迟 100ms 到首帧之后注册，节省 ~200ms 关键路径时间。
        # 为什么是 100ms 而非 singleShot(0)：Qt QTimer 按到期时间排序，
        # singleShot(0) 到期时间 ≈ 创建时间，早于 main.py 中 _show_popup 的
        # singleShot(0)（创建更晚），导致 BuiltinCommands 仍在窗口显示前执行。
        # 100ms 延迟确保到期时间晚于所有 singleShot(0)，在窗口第一次绘制后注册。
        QTimer.singleShot(100, self._init_builtin_commands)

        # ===== UI 插件系统集成（轻量：仅注册 registry 上下文） =====
        # 性能优化：插件加载 + 命令注册 + 浮动卡片处理器注册延迟到首帧后，
        # 让窗口外壳尽快出现，压缩首次启动感知耗时
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            ui_registry = UIPluginRegistry.get_instance()
            ui_registry.set_main_widget(self)
            # 设置上下文提供者：UI 插件首次显示时通过 set_context() 获取当前项目信息
            ui_registry.set_context_provider(self._build_ui_context, self._window_id)
            QTimer.singleShot(0, self._init_ui_plugins_deferred)
        except Exception as e:
            logger.error(f"[MainWidget] UI plugin registry init failed: {e}")

        self.chat_scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)

        # ===== 底部输入区域（输入卡 + 工具栏紧贴拼接）=====
        # 视觉目标：输入框 + toolbar 等宽，无间距，无外 padding，紧贴 chat 区。
        #          上半圆角（输入卡） + 下半圆角（toolbar），中间一条边框线作分隔。
        # 抖动修复：spacing 永久固定 0，不再随 collapsed 切换；toolbar y 位置
        #          只取决于 _input_card 高度的单调变化，无"先下后上"中间帧。
        self._bottom_input_container = QWidget(self)
        self._bottom_input_container.setStyleSheet("QWidget#bottomContainer { background: transparent; }")
        self._bottom_input_container.setObjectName("bottomContainer")
        bottom_layout = QVBoxLayout(self._bottom_input_container)
        self._bottom_input_layout = bottom_layout
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)

        # ===== 输入卡片（上方圆角 + 渐变 + 边框，border-bottom: none）=====
        self._input_card = QWidget(self._bottom_input_container)
        self._input_card.setObjectName("_input_card")
        self._input_card.setAcceptDrops(True)
        self._input_card.installEventFilter(self)
        card_layout = QVBoxLayout(self._input_card)
        card_layout.setContentsMargins(2, 2, 2, 2)
        card_layout.setSpacing(0)

        # 输入卡环境光晕容器（包裹 _input_card，承载宽柔的外层环境光）
        # 实现双层 halo：输入卡自身 = 紧致主光（primary），wrapper = 弥散环境光（ambient）
        self._input_card_wrapper = QWidget(self._bottom_input_container)
        self._input_card_wrapper.setObjectName("_input_card_wrapper")
        self._input_card_wrapper.setAttribute(Qt.WA_TranslucentBackground, True)
        wrapper_layout = QVBoxLayout(self._input_card_wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)
        # 把 _input_card 移入 wrapper
        self._input_card.setParent(self._input_card_wrapper)
        wrapper_layout.addWidget(self._input_card)

        # 附件预览行（拖拽/粘贴文件时显示 AttachmentChip）
        self._attach_container = QWidget(self._input_card)
        self._attach_container.setVisible(False)
        self._attach_container.setAcceptDrops(True)
        self._attach_container.installEventFilter(self)
        self._attach_layout = QHBoxLayout(self._attach_container)
        self._attach_layout.setContentsMargins(6, 6, 6, 0)
        self._attach_layout.setSpacing(3)
        self._attach_layout.addStretch()
        self._attachments: list[str] = []
        self._history_working_attachments: list[str] = []  # 进入历史模式时保存的附件（退出时恢复）
        card_layout.addWidget(self._attach_container)

        # 输入框（融入卡片，无边框）
        self.input_area = SendableTextEdit(self._input_card)
        self.input_area._agent_combo.hide()
        self.input_area._initializing = False
        # self.input_area.setFixedHeight(52)
        setFont(self.input_area, scale_font_size(15))
        self.input_area.sendMessageRequested.connect(self._on_send_clicked)
        self.input_area.stopMessageRequested.connect(self._on_stop_clicked)
        self.input_area.clearRequested.connect(self._on_clear_shortcut)
        self.input_area.agentChanged.connect(self._on_agent_changed)
        # 注意：textChanged 已在 SendableTextEdit 内部连接 _on_text_changed
        # 并触发 _adjust_height_to_content；这里不重复连接，避免一次输入
        # 触发两次布局重算导致抖动。系统卡片开/关路径会显式调用
        # _on_input_area_height_changed。
        self.input_area.slashTriggered.connect(self._on_slash_triggered)
        self.input_area.slashDismissed.connect(self._on_slash_dismissed)
        self.input_area.slashShowHint.connect(self._on_slash_show_hint)
        self.input_area.atTriggered.connect(self._on_at_triggered)
        self.input_area.atDismissed.connect(self._on_at_dismissed)
        self.input_area.files_dropped.connect(self._on_files_dropped)
        self.input_area.enteringHistoryMode.connect(self._on_entering_history_mode)
        self.input_area.historyAttachmentsRestored.connect(self._on_history_attachments_restored)
        self.input_area.historyModeExited.connect(self._on_history_mode_exited)
        # ★ 用户输入时通知桌宠好奇看向输入框
        self.input_area.textChanged.connect(self._on_pet_typing)
        card_layout.addWidget(self.input_area)

        # 加载输入历史
        self._load_input_history()

        # 命令卡片（必须是输入框创建后）
        self._command_card = CommandCard(self._bottom_input_container)
        self._command_card.setVisible(False)
        self.input_area.set_command_card(self._command_card)
        mgr = self._card_manager
        # 命令卡片压制 tool、sub_agent 和 sub_agent_compact
        mgr.register_card(
            self._window_id,
            ContainerType.BOTTOM,
            "command",
            self._command_card,
            suppress_others=["tool", "sub_agent", "sub_agent_compact"],
        )
        self._bottom_card_container.add_card("command", self._command_card)

        # 文件提及卡片（输入 @ 时显示文件列表）
        self._file_mention_card = FileMentionCard(self._bottom_input_container)
        self._file_mention_card.setVisible(False)
        self.input_area.set_file_mention_card(self._file_mention_card)
        self._file_mention_card.fileSelected.connect(self._on_file_mention_selected)
        mgr.register_card(
            self._window_id,
            ContainerType.BOTTOM,
            "file_mention",
            self._file_mention_card,
        )
        self._bottom_card_container.add_card("file_mention", self._file_mention_card)

        # 预缓存文件列表：延迟到事件循环空闲后执行，不阻塞 UI 初始化
        QTimer.singleShot(200, self._ensure_file_mention_cache)

        # 撤销删除卡片
        self._undo_delete_card = UndoDeleteCard(self._bottom_input_container)
        self._undo_delete_card.setVisible(False)
        self._undo_delete_card.restoreRequested.connect(self._restore_deleted_message)
        self._undo_delete_card.dismissed.connect(self._on_undo_delete_dismissed)
        mgr.register_card(self._window_id, ContainerType.BOTTOM, "undo_delete", self._undo_delete_card)
        self._bottom_card_container.add_card("undo_delete", self._undo_delete_card)

        # 初始化撤销删除缓存（只缓存一步）
        self._undo_delete_cache = {}

        # 🛡️ Bug 修复：截断哨兵 — 记录最近一次 session 截断的关键信息，
        # 用于在异步 finalize_stop / messages_updated 回调到达时识别"是否发生了截断"
        # 结构：{"session_id": str, "messages_len": int, "set_at": float} 或 None
        # 时机：撤销/删除消息触发的 _persist_session_after_mutation 末尾设置；
        #       _on_finalize_complete 在覆盖 session.messages 前检查；
        #       若 worker 返回的消息序列比截断后的当前序列长，且不是其前缀，则丢弃覆盖。
        self._truncation_sentinel = None

        # 🛡️ 截断后发送标志：用户撤销消息后又快速发送新消息时置 True，
        # 用于 _on_finalize_complete / _on_messages_updated 识别并丢弃旧 worker 的过期回调。
        self._pending_send_after_truncation = False
        self._pending_send_user_text = None  # 截断后发送的用户消息文本（用于指纹比对）

        # （内置命令已在上方注册）更新命令 --model= 参数描述
        self._update_subagents_param_description()
        self._update_title_gen_param_description()

        # 监听配置变更，配置同步时自动刷新命令卡参数描述和 UI
        from app.utils.config import Settings as _Cfg

        _cfg = _Cfg.get_instance()
        _cfg.llm_subagent_default_model.valueChanged.connect(self._on_subagent_model_config_changed)
        _cfg.llm_title_gen_default_model.valueChanged.connect(self._on_title_gen_model_config_changed)

        # ===== 独立工具栏条（钉在主窗口底部，不受 _input_card 缩放影响）=====
        # 关键：工具栏从 _input_card 中拆出，作为 _input_card 的 sibling
        # 放在主 layout 自己的容器里。这样 _input_card 缩小到 0 时，
        # 工具栏的窗口绝对坐标不变——按钮栏不出现视觉跳动。
        # 视觉上是独立第二张卡：下方圆角 + 渐变 + 边框；颜色使用专属
        # TOOLBAR_STRIP_BG/TOOLBAR_STRIP_BORDER token（与输入卡片解耦，
        # 主题可分别调控）。
        # 工具栏作为 self 的直接子控件（不放在任何 layout 里），
        # 通过 resizeEvent 绝对定位到窗口底部。这样输入卡折叠/展开时
        # 工具栏的窗口绝对 Y 坐标完全不变，不再被 VBoxLayout 推上/推下。
        self._bottom_toolbar_strip = QWidget(self)
        self._bottom_toolbar_strip.setObjectName("bottomToolbarStrip")
        self._bottom_toolbar_strip.setFixedHeight(36)
        strip_layout = QHBoxLayout(self._bottom_toolbar_strip)
        # 上下 3px 留白 + 28px 内容 = 34px，工具栏 28px 居中放置
        strip_layout.setContentsMargins(10, 4, 10, 4)
        strip_layout.setSpacing(8)

        # ===== 工具栏（现在挂在独立 strip 上）=====
        toolbar_widget = QWidget(self._bottom_toolbar_strip)
        # 28px 高度匹配 strip 内部 28px 内容区，配合 VCenter 完美居中
        toolbar_widget.setFixedHeight(28)
        toolbar_widget.setStyleSheet("background: transparent; border: none;")
        toolbar_layout = QHBoxLayout(toolbar_widget)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)
        # 内部子项统一 28px 时无需对齐；当前 26/28/28 混用 → VCenter 兜底
        toolbar_layout.setAlignment(Qt.AlignVCenter)

        # 模型选择（无边框，只保留背景）
        self._model_btn_container = QWidget(toolbar_widget)
        self._model_btn_container.setFixedHeight(26)
        Colors.refresh()
        self._model_btn_container.setStyleSheet(f"""
            background: {Colors.TOOLBAR_BG};
            border: none;
            border-radius: 8px;
        """)
        model_layout = QHBoxLayout(self._model_btn_container)
        model_layout.setContentsMargins(8, 0, 4, 0)
        model_layout.setSpacing(0)
        self.current_model_btn = QWidget(self._model_btn_container)
        self.current_model_btn.setCursor(Qt.PointingHandCursor)
        self.current_model_btn.setStyleSheet(MODEL_BTN_STYLE)
        self.current_model_btn.mousePressEvent = lambda e: self._toggle_model_selector_card()
        btn_layout = QHBoxLayout(self.current_model_btn)
        btn_layout.setContentsMargins(2, 2, 0, 2)
        btn_layout.setSpacing(4)
        self._model_btn_icon = QLabel(self.current_model_btn)
        self._model_btn_icon.setStyleSheet("background: transparent; border: none;")
        self._model_btn_icon.setFixedSize(18, 18)
        self._model_btn_icon.setScaledContents(True)
        btn_layout.addWidget(self._model_btn_icon)
        self._model_btn_text = QLabel("正在加载...", self.current_model_btn)
        self._model_btn_text.setStyleSheet(self._get_model_btn_text_style())
        btn_layout.addWidget(self._model_btn_text)
        model_layout.addWidget(self.current_model_btn, 1)
        self.settings_btn = QWidget(self._model_btn_container)
        self.settings_btn.setObjectName("settingsEffortBtn")
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.setStyleSheet(f"""
            QWidget#settingsEffortBtn {{
                background: transparent;
                border: none;
                border-radius: 8px;
            }}
            QWidget#settingsEffortBtn:hover {{
                background: {Colors.HOVER_BG_STRONG};
            }}
        """)
        self.settings_btn.setToolTip("模型参数配置")
        self.settings_btn.mousePressEvent = lambda e: self._toggle_model_config_card()
        settings_btn_layout = QHBoxLayout(self.settings_btn)
        settings_btn_layout.setContentsMargins(4, 2, 6, 2)
        settings_btn_layout.setSpacing(5)
        self._settings_btn_icon = QLabel(self.settings_btn)
        self._settings_btn_icon.setFixedSize(16, 16)
        self._settings_btn_icon.setScaledContents(True)
        self._settings_btn_icon.setPixmap(get_icon("模型选择").pixmap(16, 16))
        settings_btn_layout.addWidget(self._settings_btn_icon)
        # 思考强度胶囊：模型支持 reasoning_effort 且思考模式开启时显示当前等级
        self._settings_effort_label = QLabel("", self.settings_btn)
        self._settings_effort_label.setStyleSheet(self._get_settings_effort_style())
        settings_btn_layout.addWidget(self._settings_effort_label)
        model_layout.addWidget(self.settings_btn)

        # 余额/用量/上下文放入模型选择胶囊内
        model_layout.addSpacing(4)
        model_layout.addWidget(self.balance_display)
        model_layout.addWidget(self.coding_plan_ring)
        model_layout.addWidget(self.context_usage_ring)
        model_layout.addSpacing(2)

        toolbar_layout.addWidget(self._model_btn_container)

        self._current_provider_name = ""
        self._current_model_name = ""
        # #4 语义：本窗口用户是否手动选过模型（_on_model_selected_from_popup 置位）。
        # 同步跟随判定：True → 保持自身选择；False（首次加载/默认态）→ 跟随云端 SelectedModel。
        self._user_manually_selected_model = False

        # ===== 工具开关双色分段按钮 =====
        self._tool_toggle_btn = QWidget(toolbar_widget)
        self._tool_toggle_btn.setFixedHeight(26)
        self._tool_toggle_btn.setCursor(Qt.PointingHandCursor)
        Colors.refresh()
        self._tool_toggle_btn.setStyleSheet(f"""
            background: {Colors.TOOLBAR_BG};
            border: none;
            border-radius: 8px;
        """)
        self._tool_toggle_btn.mousePressEvent = lambda e: self._toggle_tool_control_card()
        tt_layout = QHBoxLayout(self._tool_toggle_btn)
        tt_layout.setContentsMargins(6, 0, 6, 0)
        tt_layout.setSpacing(0)

        # 图标（主题感知 SVG — 自动适配浅色/深色模式）
        tt_icon = _ThemedIconLabel("工具", 18, self._tool_toggle_btn)
        tt_icon.setStyleSheet("background: transparent; border: none;")
        tt_layout.addWidget(tt_icon)
        tt_layout.addSpacing(4)

        # 左：危险工具数（暗红）
        self._tool_danger_label = QLabel("0")
        self._tool_danger_label.setAlignment(Qt.AlignCenter)
        self._tool_danger_label.setFixedHeight(20)
        self._tool_danger_label.setStyleSheet(f"""
            background: {Colors.STATUS_DANGER_BG_DARK};
            color: white; font-weight: 700;
            border: none; border-top-left-radius: 4px; border-bottom-left-radius: 4px;
            padding: 0 8px;
            {font_size_css(13)} {get_font_family_css()}
        """)
        tt_layout.addWidget(self._tool_danger_label)

        # 右：安全工具数（暗绿）
        self._tool_safe_label = QLabel("0")
        self._tool_safe_label.setAlignment(Qt.AlignCenter)
        self._tool_safe_label.setFixedHeight(20)
        self._tool_safe_label.setStyleSheet(f"""
            background: {Colors.SUCCESS_DARK};
            color: white; font-weight: 700;
            border: none; border-top-right-radius: 4px; border-bottom-right-radius: 4px;
            padding: 0 8px;
            {font_size_css(13)} {get_font_family_css()}
        """)
        tt_layout.addWidget(self._tool_safe_label)

        # 恢复按钮（仅 agent 覆盖时显示，不打开卡片即可恢复）
        self._tool_restore_btn = QPushButton("↺", self._tool_toggle_btn)
        self._tool_restore_btn.setFixedSize(20, 20)
        self._tool_restore_btn.setCursor(Qt.PointingHandCursor)
        self._tool_restore_btn.setToolTip("取消 agent 覆盖，恢复用户工具权限")
        self._tool_restore_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none;
                color: #ff9500; {font_size_css(13)} {get_font_family_css()}
                font-weight: bold; padding: 0;
            }}
            QPushButton:hover {{
                color: #ffb84d;
            }}
        """)
        self._tool_restore_btn.setVisible(False)
        self._tool_restore_btn.clicked.connect(lambda: self._on_tool_restore())
        tt_layout.addWidget(self._tool_restore_btn)

        # 工具权限按钮移到右侧（右对齐）
        toolbar_layout.addStretch(1)

        toolbar_layout.addWidget(self._tool_toggle_btn)

        # 右侧功能按钮组（无边框，间距加宽）
        self._toolbar_capsule = QWidget(toolbar_widget)
        self._toolbar_capsule.setFixedHeight(28)
        Colors.refresh()
        self._toolbar_capsule.setStyleSheet(f"""
            background: {Colors.TOOLBAR_BG};
            border: none;
            border-radius: 10px;
        """)
        capsule_layout = QHBoxLayout(self._toolbar_capsule)
        capsule_layout.setContentsMargins(6, 2, 6, 2)
        capsule_layout.setSpacing(4)

        Colors.refresh()
        btn_capsule_style = f"""
            TransparentToolButton {{ background: transparent; border: none; }}
            TransparentToolButton:hover {{ background: {Colors.HOVER_BG_STRONG}; border-radius: 5px; }}
        """

        self.auto_loop_btn = TransparentToolButton(get_icon("无限"), self._toolbar_capsule)
        self.auto_loop_btn.setFixedSize(24, 24)
        self.auto_loop_btn.setToolTip("AutoLoop")
        self.auto_loop_btn.setStyleSheet(btn_capsule_style)
        self.auto_loop_btn.clicked.connect(self._show_auto_loop_config)
        capsule_layout.addWidget(self.auto_loop_btn)

        self.memory_btn = TransparentToolButton(get_icon("长期记忆"), self._toolbar_capsule)
        self.memory_btn.setFixedSize(24, 24)
        self.memory_btn.setStyleSheet(btn_capsule_style)
        self.memory_btn.setToolTip("长期记忆")
        self.memory_btn.clicked.connect(self._show_soul_memory)
        capsule_layout.addWidget(self.memory_btn)

        # 历史会话按钮（从右上移到右下）
        self.history_btn = TransparentToolButton(get_icon("历史对话"), self._toolbar_capsule)
        self.history_btn.setFixedSize(24, 24)
        self.history_btn.setStyleSheet(btn_capsule_style)
        self.history_btn.setToolTip("历史会话")
        self.history_btn.clicked.connect(self._toggle_history_card)
        capsule_layout.addWidget(self.history_btn)

        # 新建对话按钮（从右上移到右下）
        self.new_session_btn = TransparentToolButton(get_icon("新会话"), self._toolbar_capsule)
        self.new_session_btn.setFixedSize(24, 24)
        self.new_session_btn.setStyleSheet(btn_capsule_style)
        self.new_session_btn.setToolTip("新建对话")
        self.new_session_btn.clicked.connect(self._create_new_session)
        capsule_layout.addWidget(self.new_session_btn)

        # 为工具栏按钮安装自绘 hover tooltip（绕开 QToolTip 样式问题）
        for _tb in [self.auto_loop_btn, self.memory_btn, self.history_btn, self.new_session_btn]:
            install_hover_tooltip(_tb)

        toolbar_layout.addWidget(self._toolbar_capsule)

        # 工具栏挂到独立 strip（不在 _input_card 里了）
        strip_layout.addWidget(toolbar_widget)

        self._bottom_input_container.setAttribute(Qt.WA_TranslucentBackground, True)
        # 统一胶囊光晕底层：跨越输入卡 + 工具栏整个胶囊，由 paintEvent 自绘连贯环绕光，
        # 避免两个独立 widget 各挂 QGraphicsDropShadowEffect 时光晕只走局部轮廓、
        # 接缝处互相遮挡导致"只上半弧形发光"的诡异观感。
        self._input_glow_underlay = InputGlowUnderlay(self)
        # 旧的 input_card 主光 / wrapper 环境光保留为占位但默认关闭：发光统一由 underlay 提供。
        # 之所以不直接删除，是为了保留 setGraphicsEffect 钩子，方便未来需要时复用。
        self._input_card_primary_shadow = QGraphicsDropShadowEffect(self._input_card)
        self._input_card_primary_shadow.setOffset(0, 0)
        self._input_card_primary_shadow.setBlurRadius(0)
        self._input_card_primary_shadow.setColor(QColor(0, 0, 0, 0))
        self._input_card.setGraphicsEffect(self._input_card_primary_shadow)
        self._input_card_ambient_shadow = QGraphicsDropShadowEffect(self._input_card_wrapper)
        self._input_card_ambient_shadow.setOffset(0, 0)
        self._input_card_ambient_shadow.setBlurRadius(0)
        self._input_card_ambient_shadow.setColor(QColor(0, 0, 0, 0))
        self._input_card_wrapper.setGraphicsEffect(self._input_card_ambient_shadow)
        # 工具栏自身只保留失焦态的轻微下投阴影增强"落地"感，聚焦发光交给 underlay 统一处理
        self._bottom_toolbar_shadow = QGraphicsDropShadowEffect(self._bottom_toolbar_strip)
        self._bottom_toolbar_shadow.setBlurRadius(14)
        self._bottom_toolbar_shadow.setOffset(0, 4)
        self._bottom_toolbar_shadow.setColor(QColor(0, 0, 0, 70))
        self._bottom_toolbar_strip.setGraphicsEffect(self._bottom_toolbar_shadow)
        self._input_card_focused = False
        self._input_area_collapsed = False
        self._apply_bottom_input_stack_style()

        bottom_layout.addWidget(self._input_card_wrapper)
        # 预留 36px 空间给工具栏（工具栏本身不在 layout 里，绝对定位）。
        # 输入卡 + 这 36px = 输入容器高度；工具栏钉死在窗口底部 36px，
        # 与输入容器底部对齐（输入卡隐藏时容器仍占 36px，工具栏位置不变）。
        bottom_layout.addSpacing(36)

        layout.addWidget(self._bottom_input_container)

        # 向内发光：underlay 必须在输入容器 / 工具栏 **之上** 才不会被它们
        # 的不透明背景盖住；setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # 已让鼠标事件全部穿透，不影响文本输入 / 按钮点击。
        self._input_glow_underlay.raise_()

        # 输入卡 wrapper / 容器尺寸变化 → 同步胶囊光晕底层几何
        # （输入框高度自适应、系统卡片折叠都会改它们的尺寸）
        self._input_card_wrapper.installEventFilter(self)
        self._bottom_input_container.installEventFilter(self)

        # 初始定位工具栏（resizeEvent 会持续更新）
        self._position_bottom_toolbar()

        # 初始刷新工具开关按钮
        self._refresh_tool_toggle_btn()

        # 统一安装自绘 hover tooltip，替换所有原生 QToolTip
        batch_install_hover_tooltips(self)

    def _build_settings_popup(self):
        """（委托全局卡片控制器 GlobalCardController）

        P2 懒加载：窗口未激活时跳过构建，置待建标记，激活时补建
        （与 _deferred_build_cards 同理，避免后台 tab 全量构建 settings）。
        """
        if getattr(self, "_is_destroyed", False):
            return  # 窗口已销毁：3500ms 定时器仍可能触发，直接跳过
        if not self.isVisible():
            self._settings_popup_pending = True
            return
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc.ensure_settings_popup()

    def _position_bottom_toolbar(self):
        """将底部工具栏绝对定位到窗口底部 36px。

        工具栏是 self 的直接子控件，不在 main layout 里。这样：
        - 输入卡折叠/展开时，工具栏的窗口绝对 Y 坐标完全不变。
        - 系统卡片打开时，工具栏也不会被推上去。
        位置 = 窗口底部 1px margin 内缩 36px，与输入容器底部 36px spacer 对齐。
        """
        if not hasattr(self, "_bottom_toolbar_strip"):
            return
        w = self.width()
        h = self.height()
        toolbar_h = 36
        toolbar_y = max(0, h - 1 - toolbar_h)
        toolbar_w = max(0, w - 2)
        self._bottom_toolbar_strip.setGeometry(1, toolbar_y, toolbar_w, toolbar_h)
        # 工具栏位置 / 大小变了 → 胶囊光晕底层也需要同步
        self._position_input_glow_underlay()

    def _position_input_glow_underlay(self):
        """同步光晕底层的几何到当前输入卡 + 工具栏组合的胶囊轮廓。

        胶囊 = 输入卡 wrapper 的 顶部 ─→ 工具栏条 的 底部，宽度同两者一致。
        underlay 自身比胶囊四周再大出 ``margin`` px，给柔光留出绘制空间，
        否则 paintEvent 里画的 expansion 会被 underlay 自己的边界裁掉。
        """
        if not hasattr(self, "_input_glow_underlay"):
            return
        if not hasattr(self, "_input_card_wrapper") or not hasattr(self, "_bottom_toolbar_strip"):
            return
        wrapper = self._input_card_wrapper
        toolbar = self._bottom_toolbar_strip
        if wrapper.width() <= 0 or toolbar.width() <= 0:
            return

        # 取两者在主窗口坐标系中的几何（wrapper 在 layout 里 → 用 mapTo）
        try:
            wrapper_top_left = wrapper.mapTo(self, wrapper.rect().topLeft())
        except Exception:
            return
        wrapper_top = wrapper_top_left.y()
        toolbar_geo = toolbar.geometry()
        toolbar_bottom = toolbar_geo.bottom() + 1  # geometry().bottom() 是含端点的最后像素

        pill_left = wrapper_top_left.x()
        pill_top = wrapper_top
        pill_width = wrapper.width()
        pill_height = max(0, toolbar_bottom - pill_top)
        if pill_width <= 0 or pill_height <= 0:
            return

        # 给光晕留 margin（要 >= 最大 blur，避免被 underlay 自己的矩形边界裁掉）
        margin = 80
        underlay_x = pill_left - margin
        underlay_y = pill_top - margin
        underlay_w = pill_width + 2 * margin
        underlay_h = pill_height + 2 * margin
        self._input_glow_underlay.setGeometry(underlay_x, underlay_y, underlay_w, underlay_h)
        # 胶囊在 underlay 局部坐标中的位置
        self._input_glow_underlay.set_pill_geometry(margin, margin, pill_width, pill_height, radius=16)

    # ========== 内置命令初始化 ==========

    def _apply_bottom_input_stack_style(self, focused: bool | None = None):
        """刷新输入卡 + 工具栏拼接样式（等宽紧贴，上圆角 + 下圆角合成胶囊）

        视觉结构：
        - _input_card：上方 16px 圆角，下方直角；border 完整除了 border-bottom: none
        - _bottom_toolbar_strip：上方直角，下方 16px 圆角；border 完整含 border-top
          作为分隔线；左右下边框 1px 灰色，永远不变（不跟随焦点）
        - 两张卡严丝合缝拼接，整体看起来是一张胶囊
        - collapsed 时（系统卡片打开，input_card 高=0）：toolbar 切回四角圆角，
          独立成一张完整卡片显示

        抖动修复：
        - spacing 永久 0，toolbar y 位置只随 _input_card 高度单调变化
        - stylesheet 切换（圆角变化）不影响 layout 几何，无中间帧错位
        """
        if focused is not None:
            self._input_card_focused = focused
        if not hasattr(self, "_input_card") or not hasattr(self, "_bottom_toolbar_strip"):
            return

        Colors.refresh()
        collapsed = bool(getattr(self, "_input_area_collapsed", False))
        focused = bool(getattr(self, "_input_card_focused", False)) and not collapsed

        input_border = Colors.INPUT_FOCUS_BORDER if focused else Colors.INPUT_BORDER
        # 上下边框宽度保持一致（1px），不要在焦点时加粗成 2px：
        # 2px 的亮色 border 会形成明显的"边缘高亮"，和工具栏 1px 边框
        # 视觉上不一致；焦点态的差异改由 underlay 的内发光承担。
        input_border_width = 1
        input_bg_start = Colors.INPUT_FOCUS_BG_START if focused else Colors.INPUT_BG_START
        input_bg_end = Colors.INPUT_FOCUS_BG_END if focused else Colors.INPUT_BG_END

        # 输入卡：上圆角 + 下直角 + border-bottom: none（让 toolbar 上 border 兼任分隔线）
        self._input_card.setStyleSheet(f"""
            QWidget#_input_card {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {input_bg_start},
                    stop:1 {input_bg_end});
                border: {input_border_width}px solid {input_border};
                border-bottom: none;
                border-top-left-radius: 16px;
                border-top-right-radius: 16px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }}
        """)

        # toolbar：上方直角（紧贴 input_card 下方）+ 下方 16px 圆角
        # - 不 collapsed：四周边框完整，其中 border-top 1px 灰色作分隔线
        # - collapsed：四角圆角（独立完整卡，主卡已缩到 0）
        toolbar_top_radius = 16 if collapsed else 0
        self._bottom_toolbar_strip.setStyleSheet(f"""
            QWidget#bottomToolbarStrip {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {Colors.TOOLBAR_STRIP_BG},
                    stop:1 {Colors.TOOLBAR_STRIP_BG});
                border: 1px solid {Colors.TOOLBAR_STRIP_BORDER};
                border-top-left-radius: {toolbar_top_radius}px;
                border-top-right-radius: {toolbar_top_radius}px;
                border-bottom-left-radius: 16px;
                border-bottom-right-radius: 16px;
            }}
        """)

        # === 统一胶囊光晕底层 ===
        # 旧实现里两个 widget 各挂 QGraphicsDropShadowEffect，光晕只走各自局部
        # 轮廓 + 接缝相互遮挡 → 看起来"上半弧形发光、下半突兀"。现在改由
        # InputGlowUnderlay 沿整个胶囊轮廓一次性自绘，per-widget shadow 保留壳
        # 但归零，避免叠加污染。
        if hasattr(self, "_input_card_primary_shadow"):
            self._input_card_primary_shadow.setBlurRadius(0)
            self._input_card_primary_shadow.setColor(QColor(0, 0, 0, 0))

        if hasattr(self, "_input_card_ambient_shadow"):
            self._input_card_ambient_shadow.setBlurRadius(0)
            self._input_card_ambient_shadow.setColor(QColor(0, 0, 0, 0))

        if hasattr(self, "_input_glow_underlay"):
            self._input_glow_underlay.set_color(QColor(Colors.INPUT_FOCUS_BORDER))
            if focused:
                # 只要柔和的内发光（环境光层），不要主光那层紧致高亮 ─→
                # 避免边缘出现"亮色描边"的 lit-border 观感，让上下视觉一致。
                # 同时复用下方的 INPUT_GLOW_AMBIENT_* token，主题可微调。
                self._input_glow_underlay.set_glow(
                    primary_alpha=0,
                    primary_blur=0,
                    ambient_alpha=Colors.INPUT_GLOW_AMBIENT_ALPHA,
                    ambient_blur=Colors.INPUT_GLOW_AMBIENT_BLUR,
                )
            else:
                # 失焦态：默认完全关闭；glow preset（如 breath）会通过
                # INPUT_GLOW_UNFOCUSED_* token 保留微光，营造"持续呼吸"的奢华感
                self._input_glow_underlay.set_glow(
                    primary_alpha=0,
                    primary_blur=0,
                    ambient_alpha=Colors.INPUT_GLOW_UNFOCUSED_AMBIENT_ALPHA,
                    ambient_blur=Colors.INPUT_GLOW_UNFOCUSED_AMBIENT_BLUR,
                )
            # 焦点 / 折叠状态变化都可能改变胶囊几何（如 toolbar 圆角切换、
            # input_card 折叠到 0），同步一次确保 underlay 跟上
            self._position_input_glow_underlay()

        # 工具栏：失焦保留轻微下投阴影（offset 0,4）增强"落地"感；
        # 聚焦时光晕由 underlay 接管，工具栏自身阴影关闭，避免与 underlay 叠加。
        if hasattr(self, "_bottom_toolbar_shadow"):
            if focused:
                self._bottom_toolbar_shadow.setBlurRadius(0)
                self._bottom_toolbar_shadow.setOffset(0, 0)
                self._bottom_toolbar_shadow.setColor(QColor(0, 0, 0, 0))
            else:
                self._bottom_toolbar_shadow.setBlurRadius(14)
                self._bottom_toolbar_shadow.setOffset(0, 4)
                self._bottom_toolbar_shadow.setColor(QColor(0, 0, 0, 70))

    def _init_builtin_commands(self):
        """注册并初始化所有内置命令"""
        from app.core.builtin_commands import register_all_commands

        register_all_commands()
        self._register_system_card_commands()
        self._register_command_shortcuts()
        # ⚠️ register_all_commands() 清空并重建了所有命令定义，
        # 导致之前 _update_subagents_param_description() 更新的
        # param.description 被重置为默认值。此处重新应用当前配置。
        self._update_subagents_param_description()
        self._update_title_gen_param_description()

        # ── 全局热键同步 ──
        # TrayManager.__init__() 在命令加载前已调用 _setup_global_hotkey()，
        # 当时 CommandManager 为空，回退读取了系统默认快捷键，未感知 user-custom 的自定义覆盖。
        # 此处重新同步，与 reload_all_commands / _on_plugin_hot_reload 保持一致。
        try:
            from app.tray_manager import TrayManager

            tray = TrayManager.get_instance()
            tray._setup_global_hotkey()
        except Exception:
            pass

    def _register_system_card_commands(self):
        """为顶层系统设置卡片注册 FUNCTION 命令，使其出现在快捷键管理中

        这些卡片原本只能通过按钮打开，注册为命令后：
        - 用户可以在快捷键管理器中为它们分配全局快捷键
        - 也可以通过 /settings、/history 等斜杠命令打开
        """
        from app.core.command_manager import CommandManager, CommandType

        cmd_mgr = CommandManager.get_instance()

        # card_id → (中文描述, 实际 toggle 方法引用)
        # 直接用 toggle 方法而非裸 toggle_card，确保命令路径与按钮路径行为一致
        # （按钮路径的 toggle 方法会同时刷新卡片数据——如 history 刷新列表、
        #   model_selector 加载模型数据、project_selector 加载项目列表等）
        _SYSTEM_CARD_COMMANDS = {
            "settings": ("打开设置面板", self._toggle_settings_card),
            "history": ("打开对话历史", self._toggle_history_card),
            "memory": ("打开记忆管理", self._toggle_memory_card),
            "model_selector": ("选择模型", self._toggle_model_selector_card),
            "tool_control": ("打开工具控制面板", self._toggle_tool_control_card),
            "project_selector": ("选择项目", self._toggle_project_selector_card),
            "share": ("分享对话", self._on_share_clicked),
        }

        for card_id, (description, toggle_method) in _SYSTEM_CARD_COMMANDS.items():
            # 注册 handler：忽略 args 参数，直接调用 toggle 方法
            self._function_command_handlers[card_id] = lambda _args, m=toggle_method: m()
            if cmd_mgr.has_command(card_id):
                continue
            cmd_mgr.register(
                name=card_id,
                command_type=CommandType.FUNCTION,
                description=description,
                argument_hint="",
            )

    def _clear_command_shortcuts(self):
        """清除已注册的命令快捷键"""
        # F1 守卫：窗口 C++ 对象已销毁时静默返回，避免访问 self.window() 抛
        # RuntimeError（wrapped C/C++ object has been deleted）。
        if _is_sip_deleted(self):
            return
        # 清理窗口级去重缓存（当前实例对应的窗口）
        shortcut_parent = self.window() or self
        win_id = id(shortcut_parent)
        # 统计同一窗口下残余的 MainWidget 实例数
        remaining = sum(
            1 for w in OpenAIChatToolWindow._instances if not w._is_destroyed and id(w.window() or w) == win_id
        )
        if remaining <= 1:
            # 最后一个实例销毁时清除窗口级缓存
            OpenAIChatToolWindow._window_shortcut_cache.pop(win_id, None)

        for qs in getattr(self, "_command_shortcuts", []):
            try:
                qs.setEnabled(False)
                qs.deleteLater()
            except RuntimeError:
                pass
        self._command_shortcuts = []

    def _disconnect_command_shortcut_cleanup(self):
        """closeEvent 主动断开 destroyed 清理连接并立即清理快捷键。

        背景：_register_command_shortcuts 首次执行时连接 self.destroyed →
        lambda 调 _clear_command_shortcuts()。窗口 C++ 对象销毁触发 destroyed 时，
        lambda 访问已删除的 self 会抛 RuntimeError（wrapped C/C++ object has
        been deleted），disband 批量关闭团队窗口时每窗报一次。

        修复：关闭路径主动断开该连接 + 立即清理快捷键（此时 C++ 对象仍存活），
        destroyed 触发时不再执行 lambda；即使仍有其他注入路径触发 destroyed，
        F1 的 sip 守卫也会静默兜底。
        """
        try:
            if getattr(self, "_cmd_shortcuts_destroy_connected", False):
                slot = getattr(self, "_cmd_shortcuts_destroy_slot", None)
                if slot is not None:
                    self.destroyed.disconnect(slot)
                else:
                    self.destroyed.disconnect()
                self._cmd_shortcuts_destroy_connected = False
        except TypeError, RuntimeError:
            pass
        try:
            self._clear_command_shortcuts()
        except RuntimeError:
            pass

    def _init_ui_plugins_deferred(self):
        """延迟加载 UI 插件（首帧渲染后执行，避免阻塞窗口出现）"""
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            ui_registry = UIPluginRegistry.get_instance()
            # 加载所有已启用的 UI 插件
            self._load_all_ui_plugins()
            # 确保 UI 插件命令在 CommandManager 中（覆盖 register_all_commands 的清理）
            ui_registry.re_register_all_commands()
            # 多窗口隔离：为每个 UI 插件浮动卡片注册当前窗口的实例级处理器
            for card_id, card_info in ui_registry.get_floating_cards().items():
                if ":" in card_id:
                    cmd_name = card_id
                elif card_info.plugin_name == "system" or card_id == card_info.plugin_name:
                    cmd_name = card_id
                else:
                    cmd_name = f"{card_info.plugin_name}:{card_id}"
                if cmd_name in self._function_command_handlers:
                    continue

                def _make_handler(cid=card_id, mw=self):
                    return lambda args: ui_registry._show_floating_card(cid, main_widget=mw)

                self._function_command_handlers[cmd_name] = _make_handler()
        except Exception as e:
            logger.error(f"[MainWidget] UI plugin deferred init failed: {e}")

    def _load_all_ui_plugins(self):
        """加载所有已启用的 UI 插件"""
        from app.core.ui_plugin_registry import UIPluginRegistry
        from app.core.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        if not pm.is_initialized():
            return
        registry = UIPluginRegistry.get_instance()

        # 🛡️ 多窗口隔离：如果注册表中已有插件（被其他窗口加载），跳过重复加载。
        # UIPluginRegistry 是单例，所有窗口共享同一注册表。第一个窗口已加载的
        # 插件在后续窗口无需重新 load_plugin —— 调用 load_plugin 会触发
        # "先卸载旧版本"逻辑（if self.is_loaded: unload_plugin），导致所有窗口
        # 的浮动卡片 widget 被 deleteLater()，造成界面闪烁 / 状态丢失。
        # 窗口实例级的命令注册由 setup_ui 中后续的 for 循环处理，不受此影响。
        if registry.list_loaded_plugins():
            return

        plugin_dirs = []
        for plugin in pm.get_enabled_plugins():
            if plugin.has_component("ui"):
                plugin_dirs.append((plugin.name, plugin.path))
        logger.info(f"[MainWidget] Found {len(plugin_dirs)} UI-enabled plugins: {[p[0] for p in plugin_dirs]}")
        count = registry.load_all_enabled_plugins(plugin_dirs)
        if count > 0:
            logger.info(f"[MainWidget] Loaded {count}/{len(plugin_dirs)} UI plugins")

    def _build_ui_context(self) -> Dict[str, str]:
        """构建 UI 插件的上下文 dict

        UIPluginRegistry 在首次显示浮动卡片时调用此方法，
        将结果通过 ``widget.set_context(context)`` 注入卡片。

        Returns:
            dict 包含以下字段：
            - project_root: 当前工作目录（git 工作树根）
            - project_name: 当前项目名
            - session_id:   当前会话 ID
            - window_id:    当前窗口 ID
            - theme_id:     当前主题 ID
            - theme_name:   当前主题名称
            - is_dark:      当前是否为深色模式
            - font_family:  全局字体
            - font_size:    UI 基础字号（px）
            - colors:       主题色字典，可直接用: colors["card_bg"]、colors["accent"]、colors["text_primary"] 等
        """
        # ── 工作目录取值优先级 ──
        # 1. tool_executor.get_workdir() — 用户显式设置的项目根目录（最优先）
        # 2. _current_workdir 实例缓存 — 本窗口最后一次设置的工作目录
        # 3. 空串 — 未设置时返回空，绝不回退 os.getcwd()。
        #    【修复】os.getcwd() 是软件启动目录（源码根/exe 目录），若恰好是 git
        #    仓库（如 D:/work/DriFox），UI 插件会把启动目录误当项目根展示其 git 信息。
        #    与 backend._build_worktree_context 修复模式一致：未设置 → 空串。
        from loguru import logger

        workdir = ""
        try:
            if self.backend and self.backend.tool_executor:
                workdir = self.backend.tool_executor.get_workdir() or ""
        except Exception as e:
            logger.warning(f"[_build_ui_context] get_workdir failed: {e}")

        if not workdir:
            project = getattr(self, "_current_project", "")
            workdir = self._current_workdir.get(project, "")
            if workdir:
                logger.debug(f"[_build_ui_context] workdir from _current_workdir cache: {workdir}")

        if not workdir:
            logger.warning(
                f"[_build_ui_context] workdir unset for project '{getattr(self, '_current_project', '')}'; "
                f"returning empty project_root (avoid injecting software startup dir)"
            )

        # 主题信息
        theme_id = ""
        theme_name = ""
        is_dark = True
        font_family = "Segoe UI"
        font_size = 14
        theme_colors = {}
        try:
            from app.utils.theme_manager import theme_manager
            from app.utils.design_tokens import get_ui_font_size, _get_global_font
            from qfluentwidgets import isDarkTheme

            theme_id = theme_manager.get_current_theme_id()
            theme_data = theme_manager.get_current_theme()
            theme_name = theme_data.get("name", "") if theme_data else ""
            is_dark = isDarkTheme()
            font_family = _get_global_font()
            font_size = get_ui_font_size()
            theme_colors = theme_manager.get_current_colors()
        except Exception:
            pass

        return {
            "project_root": workdir,
            "project_name": getattr(self, "_current_project", ""),
            "session_id": getattr(self, "_current_session_id", ""),
            "window_id": getattr(self, "_window_id", ""),
            "theme_id": theme_id,
            "theme_name": theme_name,
            "is_dark": is_dark,
            "font_family": font_family,
            "font_size": font_size,
            "colors": theme_colors,
        }

    def _create_message_widget(self, role: str, content, timestamp=None, **kwargs):
        """统一的创建消息 widget 入口：先让插件工厂尝试处理

        Returns:
            QWidget 实例（可能是 MessageCard 或插件自定义 widget），
            无工厂处理时返回 None（调用方应使用默认逻辑）
        """
        from app.core.ui_plugin_registry import UIPluginRegistry

        message_data = {
            "role": role,
            "content": content,
            "timestamp": timestamp,
            **kwargs,
        }
        registry = UIPluginRegistry.get_instance()
        for factory in registry.get_message_factories():
            try:
                if factory.condition_func(message_data):
                    widget = factory.factory_func(message_data, self)
                    if widget is not None:
                        return widget
            except Exception as e:
                logger.error(f"[MainWidget] Message factory {factory.name} failed: {e}")
        return None

    # ── 快捷键窗口级注册去重 ──────────────────────────────
    # 在 Tab 模式下，多个 MainWidget 共享同一个顶层窗口（TabManagerWindow），
    # 如果每个 MainWidget 都向同一个窗口注册 QShortcut，将产生重复注册导致
    # 快捷键行为 undefined（Qt 官方文档明确指出同一 context 下重复 key sequence 行为未定义）。
    # 此处维护每个窗口已注册的 shortcut key sequences 集合，在注册前检查去重。
    _window_shortcut_cache: Dict[int, set] = {}

    def _register_command_shortcuts(self):
        """为所有有 shortcut 配置的命令注册 QShortcut

        父对象取顶层窗口（self.window()）而非 self(MainWidget)：
        "替换(full)" 类卡片打开时 TabManagerWindow 会切换 QStackedWidget 隐藏对话区
        （MainWidget 位于对话区第 0 页内），Qt 的 QShortcutMap 会跳过父对象不可见的
        快捷键，导致第二次按快捷键无法触发关闭。顶层窗口在覆盖层打开时仍可见，
        快捷键得以持续生效。

        有参数的命令：无论类型，插入 /command 文本到输入框，自动弹出参数卡片。
        无参数的 FUNCTION 命令：有处理器时直接执行，无处理器时回退到插入文本。
        无参数的 PROMPT/AGENT 命令：回退到插入 /command 文本，用户按 Enter 后走正常发送流程。

        handler 在运行时解析当前激活的 MainWidget（Tab 模式取 _content_area 当前页），
        不捕获 self，避免命中被隐藏或已关闭的标签页。
        """
        self._clear_command_shortcuts()

        # 快捷键挂在顶层窗口上，self 被销毁时不会随之自动清理，连接 destroyed 同步清理，避免泄漏
        # F1 守卫：lambda 在 C++ 对象销毁触发 destroyed 时访问 self 会抛 RuntimeError，
        # 先经 _is_sip_deleted 判定，删除态直接跳过清理（closeEvent 已主动清理，此处为兜底）。
        if not getattr(self, "_cmd_shortcuts_destroy_connected", False):
            try:
                self._cmd_shortcuts_destroy_slot = lambda *a: (
                    None if _is_sip_deleted(self) else self._clear_command_shortcuts()
                )
                self.destroyed.connect(self._cmd_shortcuts_destroy_slot)
                self._cmd_shortcuts_destroy_connected = True
            except RuntimeError:
                pass

        from PyQt5.QtGui import QKeySequence
        from PyQt5.QtWidgets import QShortcut

        from app.core.command_manager import CommandManager

        def _resolve_target(parent):
            # 运行时解析当前激活的 MainWidget：Tab 模式取 _content_area 当前页；
            # 单窗口模式 parent 自身即 MainWidget。不捕获 self，避免命中被隐藏/已关闭标签页。
            content = getattr(parent, "_content_area", None)
            if content is not None:
                w = content.currentWidget()
                if w is not None:
                    return w
            if hasattr(parent, "_execute_command"):
                return parent
            return None

        # 顶层窗口在覆盖层打开时仍可见，确保快捷键持续可触发
        shortcut_parent = self.window() or self
        win_id = id(shortcut_parent)

        # Tab 模式去重：同一窗口已注册的 key sequence 跳过（防止多标签页重复注册）
        registered = OpenAIChatToolWindow._window_shortcut_cache.get(win_id, set())

        cmd_mgr = CommandManager.get_instance()
        for entries in cmd_mgr._commands.values():
            for cmd_type, cmd_def in entries.items():
                if not cmd_def.shortcut:
                    continue
                key_seq = cmd_def.shortcut
                # 去重：同一窗口下相同快捷键只需注册一次
                if key_seq in registered:
                    continue
                registered.add(key_seq)

                qs = QShortcut(QKeySequence(key_seq), shortcut_parent)

                name = cmd_def.name

                def _on_shortcut(n=name, parent=shortcut_parent):
                    try:
                        target = _resolve_target(parent)
                        if target is None:
                            return
                        # 命令有参数 → 插入 /cmd 到输入框，自动触发参数卡片
                        if target._command_has_params(n):
                            logger.debug(f"[Shortcut] '{n}' → 插入文本（有参数）")
                            target._insert_command_text_fallback(n)
                        elif target._has_command_handler(n):
                            logger.debug(f"[Shortcut] '{n}' → 直接执行（无参数+有handler）")
                            target._execute_command(n)
                        else:
                            logger.debug(f"[Shortcut] '{n}' → 插入文本（无handler）")
                            target._insert_command_text_fallback(n)
                    except Exception:
                        logger.warning(f"[Shortcut] '{n}' 处理异常", exc_info=True)

                qs.activated.connect(_on_shortcut)
                self._command_shortcuts.append(qs)

        # 更新窗口级缓存
        OpenAIChatToolWindow._window_shortcut_cache[win_id] = registered

    def _command_has_params(self, name: str) -> bool:
        """检查命令是否定义了参数（用于快捷键触发的参数卡片决策）

        命令定义了 parameters（如 --flag、--key=value）时返回 True，
        快捷键触发时走插入文本路径，让参数卡片自动弹出。
        """
        from app.core.command_manager import CommandManager

        cmd_def = CommandManager.get_instance().get_command(name)
        return bool(cmd_def and cmd_def.parameters)

    def _has_command_handler(self, name: str) -> bool:
        """检查命令名是否有对应的 Python 处理器

        用于区分系统内置 function 命令（有 handler）和用户插件 function 命令（无 handler）。
        后者通过快捷键触发时回退到插入命令文本。
        """
        # 窗口级处理器
        handlers = getattr(self, "_function_command_handlers", {})
        if name in handlers:
            return True
        # 全局注册的处理器
        if FunctionCommandHandlers.has(name):
            return True
        # 硬编码内置命令
        if name in ("new", "new-window", "branch", "remember"):
            return True
        return False

    def _insert_command_text_fallback(self, command_name: str):
        """在输入框插入 /command 文本并聚焦，自动弹出参数卡片

        适用场景：
        1. 用户插件命令（type: function + shortcut）无处理器时
        2. 命令有参数（parameters 非空）时，插入后自动弹出参数卡片

        关键：setPlainText 后光标在0位，需 blockSignals 阻止过早的 textChanged，
        移动光标到末尾后再手动触发 _on_slash_trigger_check 进入 detail 参数模式。
        """
        insert = f"/{command_name} "
        self.input_area.blockSignals(True)
        self.input_area.setPlainText(insert)
        cursor = self.input_area.textCursor()
        cursor.movePosition(cursor.End)
        self.input_area.setTextCursor(cursor)
        self.input_area.blockSignals(False)
        self.input_area.setFocus()
        self.input_area._on_slash_trigger_check()

    def _execute_command(self, command_name: str, args: str = "") -> bool:
        """执行内置函数型命令

        优先从 FunctionCommandHandlers 获取处理器，回退到内置处理器。
        处理器执行过程中可抛 `CommandNeedDegrade` 表示需要降级到 prompt 注入
        （如团队模板加载遇缺失成员 / --create= 无 Python handler / 插件命令
        无处理器注册）；本方法捕获后调 select_prompt 取 prompt_sections 对应段、
        写 `_pending_command` 并置 `_team_load_degraded=True`，由 _on_send_clicked
        继续走 engine.send_message 完成注入。

        Args:
            command_name: 命令名（不含 /）
            args: 命令后的参数字符串

        Returns:
            True: handler 实际执行成功（命令消费了输入语义，调用方可清理输入/附件）
            False: 降级到 prompt 注入（命令未真正执行，调用方应保留附件等输入，
                由后续普通发送流程把附件文本拼入 user_text，避免附件静默丢失）
        """
        from app.core.command_manager import CommandNeedDegrade

        try:
            # 多窗口隔离：优先使用当前窗口自己的处理器
            handlers = getattr(self, "_function_command_handlers", {})
            handler = handlers.get(command_name)
            if handler:
                handler(args)
                return True

            # 回退到全局注册的处理器（兼容旧代码路径）
            handler = FunctionCommandHandlers.get(command_name)
            if handler:
                handler(args)
                return True

            # 回退到内置处理器（用于兼容旧命令或未注册的 function 命令）
            if command_name == "new":
                self._create_new_session()
                return True
            elif command_name == "new-window":
                self._duplicate_window(branch=False)
                return True
            elif command_name == "branch":
                self._duplicate_window(branch=True)
                return True
            elif command_name == "remember":
                self._remember_to_memory(args)
                return True
            else:
                # 无 Python 处理器注册 → 降级到 prompt 注入（缺处理器语义）
                raise CommandNeedDegrade(command_name, args)
        except CommandNeedDegrade as exc:
            # 🆕 业务/故障降级统一入口：handler 抛 CommandNeedDegrade →
            # select_prompt 按 remainder 中的参数匹配 prompt_sections 对应段
            # （如 --create= → create 段、--load= → load_missing 段），写
            # _pending_command 由 inject_command_prompt hook 注入，置
            # _team_load_degraded=True 让 _on_send_clicked 继续 send_message。
            _cmd_name = exc.command_name or command_name
            _remainder = exc.remainder or args
            from app.core.command_manager import CommandManager as _CommandManager

            _cmd_mgr = _CommandManager.get_instance()
            # 🛡️ 兜底：select_prompt 匹配不到对应 section 时（如 --create 无等号、
            # 空参数、未知参数）返回空字符串，回退到完整 body，避免注入空提示词。
            # 与 PROMPT/AGENT 分支（`if not selected_text: selected_text = replacement`）一致。
            _selected = _cmd_mgr.select_prompt(_cmd_name, _remainder) or ""
            if not _selected:
                # CommandNeedDegrade 均来自 FUNCTION 命令，取 FUNCTION 类型定义回退
                from app.core.command_manager import CommandType as _CommandType

                _cmd_def = _cmd_mgr._commands.get(_cmd_name, {}).get(_CommandType.FUNCTION)
                if _cmd_def:
                    _selected = _cmd_def.prompt_text
            _session = self.session_manager.get_current_session()
            if _session:
                _session.metadata.pop("_pending_skill", None)
                _session.metadata.pop("_pending_command", None)
                _session.metadata["_pending_command"] = {
                    "prompt_text": _selected,
                    "command_name": _cmd_name,
                    "remainder": _remainder,
                }
            self._team_load_degraded = True
            return False

    def _handle_team_command(self, args: str):
        """处理 /team 命令：团队管理与协作

        用法:
          /team                       显示帮助
          /team --join=<agent>        加入团队，指定角色智能体
          /team --join                弹出子智能体选择列表
          /team --leave               离开团队
          /team --save=<name>         保存当前窗口的 agent 列表为命名模板
          /team --load=<name>         一键应用模板（重新分配所有活跃窗口的身份）
          /team --load                显示所有可用模板
          /team --delete=<name>       删除模板（不指定名称时列出可用模板）
          /team --create=<描述>       走 PROMPT 注入（prompt_sections 自动匹配，无需 Python handler）
        """
        import re

        args = args.strip()

        if not args:
            InfoBar.info(
                "团队命令",
                "  /team --join=<agent>        加入团队（如 --join=build）\n"
                "  /team --join                弹出选择\n"
                "  /team --leave               离开团队\n"
                "  /team --save=<name>         保存当前窗口的 agent 列表为模板\n"
                "  /team --load=<name>         加载模板（不指定名称时列出可用模板）\n"
                "  /team --delete=<name>       删除模板（不指定名称时列出可用模板）\n"
                "  /team --create=<描述>       创建团队模板（AI 自动生成 yaml 文件）\n"
                "（消息发送请直接在 UI 中操作）",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=6000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        # ── --join / --join=xxx ──
        if args.startswith("--join"):
            m = re.match(r"^--join(?:=(.*))?$", args)
            if m:
                agent_name = (m.group(1) or "").strip()
                self._handle_team_join(agent_name)
            else:
                InfoBar.warning(
                    "格式错误",
                    "用法: /team --join=<agent> 或 /team --join",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    duration=3000,
                    position=InfoBarPosition.BOTTOM,
                )
            return

        # ── --leave ──
        if args == "--leave":
            self._handle_team_leave()
            return

        # ── --save=<name> ──
        if args.startswith("--save"):
            m = re.match(r"^--save=(\S+)\s*$", args)
            if m:
                self._handle_team_save(m.group(1))
            else:
                InfoBar.warning(
                    "格式错误",
                    "用法: /team --save=<name>",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    duration=3000,
                    position=InfoBarPosition.BOTTOM,
                )
            return

        # ── --load=<name> / --load ──
        if args.startswith("--load"):
            m = re.match(r"^--load=(\S+)\s*$", args)
            if m:
                self._handle_team_load(m.group(1))
            else:
                # --load 或 --load= 不指定名称 → 显示可用模板列表
                self._handle_team_templates()
            return

        # ── --delete=<name> / --delete ──
        if args.startswith("--delete"):
            m = re.match(r"^--delete=(\S+)\s*$", args)
            if m:
                self._handle_team_delete(m.group(1))
            else:
                # --delete 或 --delete= 不指定名称 → 显示可用模板列表
                self._handle_team_templates()
            return

        # --create=<描述>：无 Python handler → 抛 CommandNeedDegrade 降级到
        # prompt 注入（_execute_command 捕获后 select_prompt 按 --create= 参数
        # 匹配 `<!-- section:create -->` 段，AI 自动生成团队模板）。
        if args.startswith("--create"):
            from app.core.command_manager import CommandNeedDegrade

            raise CommandNeedDegrade("team", args)

        InfoBar.warning(
            "未知参数",
            f"未知的 team 参数: {args}",
            parent=TabManagerWindow.get_instance() or self.window(),
            duration=3000,
            position=InfoBarPosition.BOTTOM,
        )

    # ── 团队操作 ─────────────────────────────────────

    def _handle_team_join(self, agent_name: str):
        """加入团队：--join=<agent> 直接指定，--join 由命令卡片枚举选择"""
        agent_mgr = self.backend.agent_manager
        if not agent_mgr:
            InfoBar.error(
                "未就绪",
                "智能体管理器未初始化",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            return

        if not agent_name:
            InfoBar.warning(
                "未指定角色",
                "请使用 --join=<agent> 指定子智能体角色",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 验证 agent 存在
        all_agents = agent_mgr.list_agents(include_hidden=False)
        subagents = [a for a in all_agents if a.mode in ("subagent", "all")]
        available_names = [a.name for a in subagents]

        if agent_name not in available_names:
            InfoBar.warning(
                "未知智能体",
                f"未找到: {agent_name}\n可用: {', '.join(available_names)}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        self._do_join_team(agent_name)

    def _do_join_team(self, agent_name: str):
        """实际执行加入团队"""
        agent_mgr = self.backend.agent_manager
        if not agent_mgr:
            return
        all_agents = agent_mgr.list_agents(include_hidden=False)
        subagents = [a for a in all_agents if a.mode in ("subagent", "all")]
        available_names = [a.name for a in subagents]

        if agent_name not in available_names:
            InfoBar.warning(
                "未知智能体",
                f"未找到: {agent_name}\n可用: {', '.join(available_names)}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 切换智能体 + 注入 agent 工具权限(与 agent 命令路径一致)
        self._on_agent_changed(agent_name)
        self._apply_agent_command_permissions(agent_name)
        tm = self._get_team_manager()
        # 团队名：优先取当前模板名，无模板回退 default（与 TeamManager.DEFAULT_TEAM 一致）
        self._team_name = (tm.get_template() or {}).get("name") or "default"
        # 方案 A：复用团队已有 run_id（模板加载生成的）；手动加入老团队
        # （无 run_id）时保持空串，团队会话不注入团队元数据（行为与现状一致）
        # 🛡️ F3 可选收尾（根因 B）：手动加入且团队无 run_id → start_team_run()
        # 幂等生成，使纯手动团队也获得 run_id、成员会话自动落团队元数据
        # （_auto_save_current_session 的 _team_run_id 守卫因此生效），
        # 否则手动成员会话不落团队字段、恢复时无法按 run_id 找回。
        run_id = tm.get_team_run_id()
        if not run_id:
            run_id = tm.start_team_run()
        self._team_run_id = run_id
        # 🛡️ M1：手动 join 路径也透传 run_id / team_label 到 TeamManager，
        # 让成员记录带上团队归属（多团队分组、跨团队拦截的前置条件）。
        team_label = tm.get_team_label()
        tm.join_team(
            window_id=self._window_id,
            agent_name=agent_name,
            run_id=run_id,
            team_label=team_label,
        )
        # 🛡️ B1（review 阻断修复）—— self._team_agent_name 必须保持与
        # agent_name 同步：会话保存（agent_name 字段落空）、项目/工作目录广播
        # （getattr 守卫拦截）、恢复路径 join_team 守卫、getattr fallback 链路
        # 等 4 处业务功能均依赖该字段。join_team 仅写 TeamManager 成员表，
        # 不回写窗口实例标记——此处必须显式赋值（与原行为一致）。
        self._team_agent_name = agent_name
        # 应用团队级统一项目（若已设置）：手动加入团队后与团队共享同一项目
        team_project = tm.get_team_project()
        if team_project:
            self._apply_team_project(team_project)
        # 应用团队级统一工作目录/工作树（若已设置）：与团队共享同一工作树
        team_workdir = tm.get_team_workdir()
        if team_workdir:
            self._apply_team_workdir(team_workdir)
        self._refresh_team_ui(agent_name)

        # 同步活跃窗口列表（触发失效成员清理）
        self._sync_active_windows_to_team_manager()

        # 启动邮箱文件监听（检测新任务邮件）
        self._start_team_watcher()

        InfoBar.success(
            "已加入团队",
            f"角色: {agent_name}\n队友可通过 UI 消息界面向你派发任务",
            parent=TabManagerWindow.get_instance() or self.window(),
            duration=4000,
            position=InfoBarPosition.BOTTOM,
        )

    def _handle_team_leave(self, silent: bool = False, batch_disband: bool = False):
        """离开团队

        Args:
            silent: True 时跳过 InfoBar 提示（批量退出场景，如团队关闭按钮
                对每个成员窗口调用此方法时，避免重复弹"已离开团队"提示）。
            batch_disband: True 时（团队批量解散循环，TabManager
                _on_team_close_requested 对每个成员窗口调用）走轻量路径：
                - 跳过第 4 步全局同步（循环结束后由 TabManager 统一同步 1 次，
                  替代原每成员 2 次 O(n×m) 全局清理）；
                - 跳过第 5 步欢迎卡片 QWebEngineView 重建（窗口即将关闭，
                  重建 100-500ms/窗口纯属浪费）；
                - 置位 _batch_disband_in_progress，使该窗口 closeEvent 内的
                  _sync_active_windows_to_team_manager 一并跳过（单窗关闭不受影响）。
        """
        # 0) 批量解散标志：本窗口 closeEvent 据此跳过全局同步
        if batch_disband:
            self._batch_disband_in_progress = True

        # 1) 先停 watcher，避免后续 rmtree 触发 watcher 事件重建目录
        self._stop_team_watcher()

        # 2) 离开团队（清理邮箱目录）+ 恢复用户工具权限
        tm = self._get_team_manager()
        # 🛡️ W3b-2 1a：批量解散循环内挂起落盘（save_now=False），由 TabManager
        # 在解散循环结束后统一 flush_pending_saves() 合并写盘（n 次 json.dump
        # → 1 次）。单窗离开路径 save_now=True 行为不变（立即写盘）。
        tm.leave_team(self._window_id, save_now=not batch_disband)
        self._tool_permission_controller.restore_user()

        # 3) 清除团队标记后刷新 UI
        self._team_agent_name = ""
        self._team_name = ""
        self._team_run_id = ""
        self._refresh_team_ui(is_team=False)

        # 4) 同步活跃窗口列表（触发失效成员清理）
        # 🛡️ 批量解散场景跳过：每个窗口单独同步是 O(n×m) 重复清理，
        # 由 TabManager 在解散循环结束后统一执行 1 次。
        if not batch_disband:
            self._sync_active_windows_to_team_manager()

        # 5) 还原智能体身份到系统 hook 设定
        try:
            default_agent = Settings.get_instance().llm_primary_agent.value or "build"
            if not self.backend.get_agent(default_agent):
                agents = self.backend.get_primary_agents()
                default_agent = agents[0].name if agents else "build"
            # 🛡️ 批量解散场景：窗口即将关闭，跳过 _show_initial_welcome 的
            # QWebEngineView 重建（_on_agent_changed 其余切换逻辑保留）。
            self._on_agent_changed(default_agent, skip_welcome=batch_disband)
        except Exception:
            logger.exception("[_handle_team_leave] 切换默认智能体失败")

        # 6) 恢复模型选择按钮的 tooltip（离开团队后不再显示 agent 信息）
        self._update_model_selector_btn()

        if not silent:
            InfoBar.info(
                "已离开团队",
                "窗口已恢复独立模式",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )

    # ── 团队模板（save / load / list / delete）────────

    def _handle_team_save(self, name: str):
        """保存当前活跃窗口的 agent 列表为命名模板到 user-custom 插件。

        收集所有 OpenAIChatToolWindow._instances 的 _current_agent，
        去重后写入 .drifox/plugins/user-custom/team_templates/<name>.yaml。
        """
        from app.core.team.template_manager import TemplateManager
        from app.core.team.template_schema import Template, TemplateAgent

        # 收集当前所有活跃窗口的 agent 名称（去重，保留顺序）
        # 注意：_instances 可能包含已销毁的窗口（windowClosed=True），需过滤
        seen: set = set()
        agent_names: List[str] = []
        active_windows: List["OpenAIChatToolWindow"] = []
        for win in getattr(OpenAIChatToolWindow, "_instances", []):
            try:
                if getattr(win, "_is_destroyed", False):
                    continue
                # 额外检查 windowClosed（OpenAIChatToolWindow 的关闭标志）
                if getattr(win, "windowClosed", False):
                    continue
                agent = getattr(win, "_current_agent", None)
                if not agent:
                    continue
                active_windows.append(win)
                if agent in seen:
                    continue
                seen.add(agent)
                agent_names.append(agent)
            except Exception:
                continue

        if not agent_names:
            InfoBar.warning(
                "无法保存",
                "当前没有任何活跃窗口持有有效 agent。请先 /team --join=<agent>",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=4000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        # description 用「实际非已关闭窗口数」而非 _instances 长度（含已销毁的会偏大）
        active_count = len(active_windows)
        # 角色描述复用：若当前团队已加载过模板（/team --load），该模板 agents 中
        # 带有的角色描述可直接复用到保存的模板；手动加入的成员无描述则保持旧格式（兼容）
        role_descs = {}
        try:
            _tm_mgr = self._get_team_manager()
            _tpl = _tm_mgr.get_template()
            for item in (_tpl or {}).get("agents") or []:
                if isinstance(item, dict) and item.get("agent_name"):
                    desc = str(item.get("description") or "").strip()
                    if desc:
                        role_descs[item["agent_name"]] = desc
        except Exception:
            role_descs = {}
        template = Template(
            schema_version=1,
            template_name=name,
            description=f"由 {active_count} 个活跃窗口保存（去重 {len(agent_names)} 个角色）",
            agents=[TemplateAgent(agent_name=a, description=role_descs.get(a, "")) for a in agent_names],
        )

        try:
            tm = TemplateManager.get_instance()
            path = tm.save(template)
            InfoBar.success(
                "模板已保存",
                f"  名称: {name}\n  角色: {', '.join(agent_names)}\n  路径: {path}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
        except TemplateError as e:  # noqa: F821 — 由 import 提供
            InfoBar.error(
                "保存失败",
                str(e),
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
        except Exception as e:  # noqa: BLE001
            InfoBar.error(
                "保存失败",
                f"未预期错误: {e}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )

    def _handle_team_load(self, name: str):
        """应用模板：为模板的每个角色新建一个全新空白窗口并加入团队。

        新语义（方案 A）：
        - 模板定义的 N 个角色 → 全部由新建的 N 个全新空白窗口承担，
          每个窗口分配一个模板角色并加入团队。
        - 已有标签页（标题、顺序、会话内容、agent 归属）完全不参与模板、
          完全不动。加载后总窗口 = 已有窗口数 + N（模板角色数）。

        流程：
        1. 读取 template，校验 agent_name 在系统中存在
        2. 为模板的每个角色调用 _create_fresh_window 新建全新空白窗口
        3. 新窗口延后到下一帧（QTimer）切换 + join，确保 backend 初始化完成
        4. 触发 Ctrl+Shift+G 排列窗口

        重要：load 不会调用 leave_team()，避免销毁已有窗口的 mailbox 目录（含未读任务邮件）。
        """
        from app.core.team.template_manager import TemplateManager
        from app.core.team.template_schema import TemplateError

        try:
            tm = TemplateManager.get_instance()
            template = tm.load(name)
        except TemplateError as e:
            InfoBar.error(
                "加载失败",
                str(e),
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 校验 agent_name 在系统中存在
        agent_mgr = self.backend.agent_manager
        if not agent_mgr:
            InfoBar.error(
                "未就绪",
                "智能体管理器未初始化",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            return
        available_names = [a.name for a in agent_mgr.list_agents(include_hidden=False) if a.mode in ("subagent", "all")]
        missing = template.validate_agent_names(available_names)
        if missing:
            # 🆕 缺失角色不再弹 InfoBar 报错 → 抛 CommandNeedDegrade 降级到
            # prompt 注入补全流程：由 _execute_command 捕获后 select_prompt 按
            # --load= 参数匹配 `<!-- section:load_missing -->` 段（详见
            # `plugins/system/commands/team.md`），AI 走补全流程。
            from app.core.command_manager import CommandNeedDegrade

            raise CommandNeedDegrade("team", f"--load={name} 缺失角色: {', '.join(missing)}")

        agents_needed = [a.agent_name for a in template.agents]
        needed_count = len(agents_needed)

        # ⚠ 会新建 needed_count 个窗口：先弹确认框，避免用户误操作
        from app.widgets.common_dialogs import ConfirmDialog

        _confirmed: list[bool] = [False]

        def _on_load_confirm():
            _confirmed[0] = True

        # 角色列表拼接展示（agent_name 为短英文名，单行拼接避免逐行列表超出弹框固定高度）
        role_names = "、".join(a.agent_name for a in template.agents)
        _dialog = ConfirmDialog(
            title="加载团队模板",
            content=(
                f"确定要加载模板「{name}」吗？\n"
                f"将新建 {needed_count} 个全新窗口并加入团队，已有标签页不会被修改或移动。\n\n"
                f"将加载角色：{role_names}"
            ),
            confirm_text="确认",
            cancel_text="取消",
            # parent 取顶层主窗口（Tab 模式下为 TabManagerWindow），而非 self 这个
            # 嵌入 QStackedWidget 的子 widget——MaskDialogBase 用 parent 尺寸铺遮罩，
            # 传子 widget 会导致遮罩只覆盖聊天区、弹窗层级/定位异常。
            parent=self.window(),
        )
        _dialog.confirmed.connect(_on_load_confirm)
        _dialog.exec_()
        if not _confirmed[0]:
            return

        # 记录团队模板上下文（供 SessionStart hook 注入团队描述 + 各成员角色描述）
        tm_mgr = self._get_team_manager()
        tm_mgr.set_template(
            {
                "name": template.template_name,
                "description": template.description,
                "agents": [{"agent_name": a.agent_name, "description": a.description} for a in template.agents],
            }
        )
        # 方案 A：开始一次团队运行（force=True 强制生成新 run_id），
        # 每次新建团队对话都是独立运行，不复用旧 run_id，与一键恢复路径
        # force=True 语义对齐；本次模板加载的所有新窗口共享同一 run_id，
        # 团队会话自动保存时落库。
        # 🛡️ M1：捕获返回值 new_run_id，批量透传给后续 _spawn_team_members，
        # 保证一次 /team --load 创建的所有成员共享同一团队归属（多团队分组）。
        new_run_id = tm_mgr.start_team_run(force=True)

        # 🐛 构建团队默认项目继承：团队级项目是持久化的（team.json 顶层，
        # 任一成员切项目即写入）。若不在此重置，新团队会沿用上次构建残留的
        # 旧项目，而不是继承「执行本次 /team --load 的那个标签页」的项目。
        # 修复：开新团队时无条件把团队级项目重置为源标签页当前项目，使本次
        # 所有新成员窗口（_spawn_team_member_window）在 _apply_team_project
        # 时共享同一项目。
        src_project = self.__dict__.get("_current_project") or ""
        if src_project:
            tm_mgr.set_team_project(src_project)

        # 🐛 构建团队默认工作目录继承：与团队级项目同理，team_workdir 也是
        # 持久化的（team.json 顶层，用户切换工作目录/git worktree 即写入），
        # 若不在此重置，新团队会沿用上次构建残留的旧工作目录，导致新成员窗口
        # 左上角分支标签显示旧工作目录（其他项目/worktree）的分支，而非当前
        # 团队项目。修复：开新团队时把团队级工作目录重置为源标签页当前工作目录，
        # 使本次所有新成员窗口在 _apply_team_workdir 时共享同一工作树。
        src_workdir = self._resolve_project_workdir() or ""
        tm_mgr.set_team_workdir(src_workdir)

        # 为模板的每个角色新建一个全新空白窗口（复用公共创建链路：
        # _create_fresh_window + 同步前置 join + 300ms 延迟 join）。
        # 注意：这是"开新团队"路径，run_id 已在上方 force 生成，
        # _spawn_team_member_window 内部 get_team_run_id() 取到的即新 run_id。
        # 🛡️ M1：透传 run_id + 模板名作为 team_label（多团队并存时按 label
        # 分组展示）；team_label 优先用 template.template_name，无则回退
        # 当前 TeamManager 推断值（get_team_label 会取 template["name"]）。
        team_label = template.template_name or tm_mgr.get_team_label()
        self._spawn_team_members(agents_needed, run_id=new_run_id, team_label=team_label)

    def _spawn_team_member_window(
        self,
        agent_name: str,
        run_id: str = "",
        team_label: str = "",
        team_name: str = "",
    ) -> Optional["OpenAIChatToolWindow"]:
        """为团队新建一个成员窗口（_handle_team_load 与团队框"快速新建成员"共用）。

        创建链路（与 _handle_team_load 原始循环体一致）：
        1. _create_fresh_window 新建全新空白窗口（不复制任何已有窗口上下文），
           团队标记（_team_agent_name/_team_name/_team_run_id）作为参数**前置**
           传入，在 add_window 之前写入（D2）——Tab 管理器 add_window 时
           _resolve_tab_team_id 直接命中团队分组，消除"tab 先落独立区"竞态。
        2. 同步前置 join：返回后立即登记团队成员身份。此时 showEvent 排队的
           QTimer(0) → _create_new_session 仍在事件队列中未执行，join 完成后
           create_session 触发 SessionStart hook 时 is_team_member 已为 True，
           团队 hook（#team_member matcher）才能命中。同时写入团队名/角色名，
           供 Tab 管理器分组与胶囊使用。
        3. 应用团队级统一项目（若已设置，否则继承执行构建的源窗口项目）
        4. 300ms 延迟 _join_new_window_for_template（等 backend.agent_manager
           就绪；C3：该回调退化为纯 UI 补注册，不再重复 join 写盘），
           join 完成递减 _pending_arrange_count。
           🛡️ M2：延迟回调传 keep_team_name=True——保留本方法前置写入的
           权威归属（透传 run_id/team_name），防止回调内 get_team_run_id()
           取到 team.json 顶层 run_id（其他团队/新 run）覆盖导致分组漂移。

        ⚠️ run_id 关键约束：**复用现有 run_id**（get_team_run_id()），
        禁止 start_team_run(force=True) 生成新 run_id——否则新窗口按
        TabManagerWindow._resolve_tab_team_id 分组进**新团队框**而非当前团队框
        （T3 坑 1）。仅"开新团队"路径（/team --load）与"新建任务"
        （_handle_team_new_task）先行 force 生成 run_id。

        🛡️ M1：调用方透传的 run_id / team_label / team_name 优先——多团队
        并存场景下，team.json 顶层 run_id 可能不是调用方期望的归属（典型：
        快速添加成员时 team.json 顶层是其他团队的 run_id，导致新窗口
        "漂"到错误团队框）。_handle_team_load / _handle_team_add_member
        必须各自透传自身期望的目标 run_id。

        ⚠️ 角色重复：**允许同 agent_name 多窗口**（F14 快速新建成员可重复角色）。
        TeamManager.join_team 以 window_id 为 key，同角色多窗口互不冲突；
        Tab 分组 key 为 run_id，同组多 TabItem 渲染正常。

        Args:
            agent_name: 要创建的角色名
            run_id: 目标团队 run_id；空串时回退到 team.json 顶层
            team_label: 团队显示名（写入快照）；空串时回退到当前 TeamManager 推断值
            team_name: 团队名（窗口实例 _team_name）；空串时回退到当前模板名

        Returns:
            新建窗口；创建失败返回 None（调用方负责计数与提示）
        """
        try:
            tm_mgr = self._get_team_manager()
            # 🛡️ D2 根治：团队标记前置传入 _create_fresh_window（add_window
            # 之前写入），Tab 管理器 add_window 时直接命中团队分组，消除
            # "tab 先落独立区、依赖 300ms 后置 refresh 补救"的竞态窗口期。
            # 🛡️ M1：调用方透传的 team_name 优先（_handle_team_add_member 透传
            # self._team_name 锁定目标团队）；空串回退到模板名（_handle_team_load 场景）。
            team_name = team_name or (tm_mgr.get_template() or {}).get("name") or "default"
            # 🛡️ M1：run_id 同样以调用方透传优先；空串回退到 team.json 顶层
            # （兼容历史单团队时代的隐式行为，但多团队并存时应由调用方显式传入）。
            team_run_id = run_id or tm_mgr.get_team_run_id()
            win = self._create_fresh_window(
                team_agent=agent_name,
                team_name=team_name,
                team_run_id=team_run_id,
            )
            if win is None:
                return None
            # 幂等赋值（防御 + 兼容：_create_fresh_window 可能被 mock/子类覆盖，
            # 此处确保标记一定就位，供 SessionStart hook / Tab 分组使用）
            win._team_agent_name = agent_name
            win._team_name = team_name
            # 复用现有 run_id（不 start_team_run，避免新成员分组漂移）
            win._team_run_id = team_run_id
            # 🛡️ M1：把 run_id / team_label 透传到 join_team——**仅当调用方
            # 显式传参（非空）时才追加到 kwargs**，避免老路径（如测试 mock
            # 严格断言）收到意外 kwargs。
            # 🆕 M1'：_handle_team_add_member 也透传 self._team_run_id/
            # team_label，避免新成员加入错误的团队（多团队并存场景）；
            # _handle_team_load 路径透传 new_run_id/team_label → 写入归属字段。
            # _do_join_team 等其他"老路径"调用不传 → 走 TeamManager 默认空归属
            # （向后兼容）。
            join_kwargs = {"window_id": win._window_id, "agent_name": agent_name}
            if team_run_id:
                join_kwargs["run_id"] = team_run_id
            if team_label:
                join_kwargs["team_label"] = team_label
            tm_mgr.join_team(**join_kwargs)
            # 应用团队级统一项目：新窗口与团队共享同一项目。
            # 🐛 构建团队默认项目继承：首次 /team --load / 快速新建成员时
            # 团队级项目尚未设置（get_team_project 为空串），此前新窗口会回落到
            # 全局默认项目，而不是继承「执行本次构建的那个标签页」的项目。
            # 修复：从发起构建的源窗口（self._current_project）复制项目并写入
            # 团队级，使本次所有新成员窗口与后续恢复路径共享同一项目。
            # 注：用 __dict__.get 而非 getattr——测试用 __new__ 构造实例时
            # 访问 Qt 子类实例属性会触发 super().__init__ 检查抛 RuntimeError。
            team_project = tm_mgr.get_team_project()
            if not team_project:
                src_project = self.__dict__.get("_current_project") or ""
                if src_project:
                    team_project = src_project
                    tm_mgr.set_team_project(team_project)
            if team_project:
                win._apply_team_project(team_project)
            # 应用团队级统一工作目录/工作树（若已设置）：新窗口与团队共享同一工作树
            team_workdir = tm_mgr.get_team_workdir()
            if team_workdir:
                win._apply_team_workdir(team_workdir)
            # 延迟 join（确保 backend.agent_manager 已初始化）
            wid = getattr(win, "_window_id", "?")
            # 🛡️ M2（快速新建成员分组漂移根治）：延迟补注册必须传
            # keep_team_name=True——本方法已在创建时前置写入权威归属
            # （_team_run_id/_team_name，透传自调用方，M1/M1' 约定），
            # _join_new_window_for_template 默认 keep_team_name=False 会在
            # 300ms 后用 team.json **顶层** run_id（get_team_run_id()）与
            # 当前模板名无条件覆盖：多团队并存时顶层 run_id 是其他团队的
            # （或期间 /team --load force 生成的新 run_id），新成员窗口
            # 归属被改写 → refresh_capsule_for_window 重分组时落入错误的
            # / 全新的团队框（"快速添加成员漂到独立新团队"bug 根因）。
            # 与恢复路径（_on_team_restore_requested 传 keep_team_name=True）
            # 语义对齐：保留调用方已设的归属字段。
            QTimer.singleShot(
                self._TEMPLATE_JOIN_DELAY_MS,
                lambda w=win, a=agent_name, wid=wid: self._join_new_window_for_template(w, a, wid, keep_team_name=True),
            )
            return win
        except Exception as e:  # noqa: BLE001
            logger.error(f"[_spawn_team_member_window] 创建成员窗口失败: {e}")
            # 🛡️ E1 回收：窗口已构造但 join/标记失败 → 主动回收（_instances
            # 移除 + Tab 注销 + close），避免残留"幽灵窗口"。
            if "win" in locals() and win is not None:
                _abort_team_window(win)
            return None

    def _spawn_team_members(
        self,
        agent_names: List[str],
        run_id: str = "",
        team_label: str = "",
        team_name: str = "",
    ) -> int:
        """批量创建团队成员窗口（去重由调用方保证）。

        Args:
            agent_names: 要创建的角色名列表（不含已存在成员）
            run_id: 🛡️ M1 多团队归属——透传给 join_team；缺省时空串
                （_spawn_team_member_window 回退到 tm_mgr.get_team_run_id()）
            team_label: 🛡️ M1 多团队归属——透传给 join_team；缺省时空串
            team_name: 🛡️ M1' 透传给 _spawn_team_member_window 的窗口实例
                _team_name（_handle_team_add_member 锁定当前团队名，
                避免 team.json 模板名覆盖为别的团队）

        Returns:
            成功创建的窗口数
        """
        new_windows: List["OpenAIChatToolWindow"] = []
        # C1 批量布局：连续 add_tab 期间跳过每次全量重建，结束统一重建一次
        from app.widgets.tab_manager_window import TabManagerWindow

        _tmw = TabManagerWindow.get_instance()
        if _tmw is not None:
            _tmw._tab_panel.begin_batch_add()
        try:
            for agent_name in agent_names:
                # 🛡️ M1：run_id / team_label 透传到 join_team，保证一次 /team --load
                # 生成的所有新窗口共享同一团队归属（多团队分组基础）。
                # 🛡️ M1'：team_name 同样透传，避免窗口实例 _team_name 被 team.json
                # 模板名覆盖为别的团队（多团队并存下模板可能不是当前团队）。
                win = self._spawn_team_member_window(
                    agent_name,
                    run_id=run_id,
                    team_label=team_label,
                    team_name=team_name,
                )
                if win is not None:
                    new_windows.append(win)
        finally:
            if _tmw is not None:
                _tmw._tab_panel.end_batch_add()
        # 🐛 模板加载后 tab 自动切到第一个成员：add_window 每次激活新窗口
        # （批量创建 N 个成员后激活停在最后一个），此处统一切回第一个新窗口。
        # 注：单个成员（快速新建成员 _handle_team_add_member）时切回自身，
        # 行为不变；仅批量场景（/team --load）生效。
        if new_windows and _tmw is not None:
            try:
                _first_idx = _tmw._window_to_index.get(id(new_windows[0]), -1)
                if 0 <= _first_idx < len(_tmw._windows):
                    _tmw._tab_panel.set_active_index(_first_idx)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[_spawn_team_members] 切回首个成员 tab 失败: {e}")
        # 初始化延迟计数（新窗口 join 完成后逐个递减并排列）
        self._pending_arrange_count = len(new_windows)
        if self._pending_arrange_count == 0:
            self._do_team_window_arrange()
        return len(new_windows)

    def _handle_team_add_member(self):
        """团队框"快速新建成员"交互：列出可选角色（可重复），选择后创建成员会话。

        交互（QMenu 以鼠标位置弹出，样式与 TabPanel contextMenuEvent 一致）：
        - 角色列表 = 模板 agents 全部角色（若有模板）∪ 当前成员 agent_name（去重展示）
        - **全部可点击、不去重**（F14：允许重复角色，可建多个 build 等）
        - 点击角色 → 创建该角色成员会话（并入当前 run_id）

        兜底：
        - 无模板且无成员 → InfoBar 提示先 /team --load=<模板> 或先加入成员
        """
        tm_mgr = self._get_team_manager()
        # 可选角色：模板 agents ∪ 当前成员角色（去重展示；选择时可重复创建）
        all_agents: List[str] = []
        template = tm_mgr.get_template()
        if template and template.get("agents"):
            all_agents = [a.get("agent_name") for a in template["agents"] if a.get("agent_name")]
        # 🛡️ M1'：get_members 必须按当前 run_id 过滤——多团队并存下 tm_mgr 全量
        # get_members() 含所有 run 的成员，会把别的团队成员混入快速新建弹窗
        # （"弹窗全部团队显示最新新建的团队的成员"bug 根因）。ref_win 的
        # _team_run_id 即为目标 run_id（团队框 UI 上下文）；空串时回退到
        # team.json 顶层（兼容历史单团队语义）。
        _target_run_id = self._team_run_id or tm_mgr.get_team_run_id()
        member_agents = [
            m.get("agent_name") for m in tm_mgr.get_members(run_id=_target_run_id or None) if m.get("agent_name")
        ]
        for name in member_agents:
            if name not in all_agents:
                all_agents.append(name)
        if not all_agents:
            InfoBar.warning(
                "无法新建成员",
                "当前团队无模板且无成员角色，请先 /team --load=<模板> 或 /team --join=<角色>",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=4000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        from PyQt5.QtGui import QCursor
        from PyQt5.QtWidgets import QMenu

        menu = QMenu(self.window())
        menu.setStyleSheet(
            f"""
            QMenu {{
                background: {Colors.CARD_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(13)}
            }}
            QMenu::item:selected {{
                background: {Colors.HOVER_BG};
            }}
            """
        )
        for agent_name in all_agents:
            menu.addAction(agent_name)  # 可重复选，不置灰不去重（F14）

        chosen = menu.exec_(QCursor.pos())
        if chosen is None:
            return
        agent_name = chosen.text()
        # 🛡️ M1：透传调用方窗口所属团队的 run_id / team_name / team_label，
        # 避免新成员落到 team.json 顶层 run_id 的错误团队框（多团队并存场景：
        # team.json 顶层 run_id 是其他团队时，新成员会"漂"到那个团队）。
        # _handle_team_load 路径透传 new_run_id 同理。
        created = self._spawn_team_members(
            [agent_name],
            run_id=self._team_run_id or "",
            team_label=self._team_name or "",
            team_name=self._team_name or "",
        )
        if created:
            InfoBar.success(
                "已创建成员",
                f"角色 {agent_name} 的成员会话已创建",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=4000,
                position=InfoBarPosition.BOTTOM,
            )

    def _handle_team_new_task(self):
        """团队框"新建任务"：全员内部新建会话 + 团队级生成新 run_id（F14）。

        一轮任务结束后，一键开启新一轮：所有成员窗口清空当前对话（内部
        新建会话，保留窗口），团队 run_id 更新为新值——新任务所有成员共享
        新 run_id，历史独立条目（旧任务归旧 run_id）。

        顺序关键（防历史污染）：
        1. 先对每个成员窗口 _create_new_session（内部 _auto_save_current_session
           用**旧** run_id 落库旧任务历史 ✅）
        2. 再 start_team_run(force=True) 生成新 run_id
        3. 最后更新所有成员窗口 _team_run_id = 新 run_id（后续会话保存落新 run）
        4. 刷新 Tab 分组（run_id 变化 → 窗口移入新团队框分组）
        """
        tm_mgr = self._get_team_manager()
        members = tm_mgr.get_members()
        if not members:
            InfoBar.warning(
                "无法新建任务",
                "当前团队没有成员窗口，无法新建任务",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=4000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 收集团队成员窗口实例（按 _window_id 匹配）
        member_windows: List["OpenAIChatToolWindow"] = []
        member_ids = {m.get("window_id") for m in members if m.get("window_id")}
        for inst in list(OpenAIChatToolWindow._instances):
            wid = getattr(inst, "_window_id", None)
            if wid in member_ids and not getattr(inst, "_is_destroyed", False):
                member_windows.append(inst)
        if not member_windows:
            InfoBar.warning(
                "无法新建任务",
                "未找到团队成员的活跃窗口",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=4000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 1) 全员内部新建会话（先保存旧历史到旧 run_id）。
        #    C3 优化：QTimer 链式交错（每窗 _TEAM_NEW_TASK_STAGGER_MS），
        #    避免 N 个窗口的 auto_save/create_session/UI 清理背靠背占用
        #    主线程导致 UI 冻结；welcome 渲染已由 _schedule_initial_welcome
        #    独立交错（C2），此处不重复。
        #    顺序约束（防历史串台）：全部 _create_new_session 完成（旧 run_id
        #    落库）→ 才 start_team_run(force) → 更新 _team_run_id → 刷新分组。
        from PyQt5.QtCore import QTimer as _QTimer

        def _run_new_task_steps(_idx: int):
            # 🛡️ 窗口销毁守卫：PyQt 实例属性访问可能抛 RuntimeError
            # （未初始化/已销毁），用 __dict__ 检查避免 getattr 触发
            # PyQt __getattr__（getattr 默认值只兜 AttributeError）。
            try:
                if self.__dict__.get("_is_destroyed", False):
                    return
            except Exception:
                pass
            if _idx >= len(member_windows):
                # 2) 生成新 run_id（force：每次新建任务都是独立运行）
                new_run_id = tm_mgr.start_team_run(force=True)
                # 3) 更新所有成员窗口的 run_id（后续会话保存落新 run_id）
                #    🛡️ 修复：新 run_id 必须回写 TeamManager 成员数据两处
                #    run_id（members[wid] / team_members[wid]）。仅更新窗口
                #    实例 _team_run_id 而成员表仍旧，会使窗口实例(new)、
                #    顶层 run_id(new)、成员数据 run_id(旧) 三方不一致——
                #    多团队严格匹配（M1-r'/M1）按 run_id 查询成员
                #    （get_members(run_id=new) / get_team_member_snapshot(new)）
                #    查不到成员，表现为团队收发/添加成员/历史合并断裂。
                for win in member_windows:
                    win._team_run_id = new_run_id
                    try:
                        tm_mgr.update_member_team(
                            win._window_id,
                            run_id=new_run_id,
                            team_label=getattr(win, "_team_name", "") or "",
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[_handle_team_new_task] 同步成员 run_id 失败: {e}")
                # 4) 刷新 Tab 分组：run_id 变化 → 窗口移入新 run_id 团队框分组
                try:
                    from app.widgets.tab_manager_window import TabManagerWindow

                    tm_win = TabManagerWindow.get_instance()
                    if tm_win is not None:
                        for win in member_windows:
                            tm_win.refresh_capsule_for_window(win)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[_handle_team_new_task] 刷新 Tab 分组失败: {e}")
                InfoBar.success(
                    "已新建任务",
                    f"已为 {len(member_windows)} 个成员窗口开启新一轮任务\n新 run: {new_run_id[:8]}…",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    duration=4000,
                    position=InfoBarPosition.BOTTOM,
                )
                return
            win = member_windows[_idx]
            try:
                win._create_new_session()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[_handle_team_new_task] 成员窗口新建会话失败: {e}")
            # 下一窗交错执行：期间事件循环可处理绘制/输入/welcome
            _QTimer.singleShot(self._TEAM_NEW_TASK_STAGGER_MS, lambda: _run_new_task_steps(_idx + 1))

        _run_new_task_steps(0)

    def _join_new_window_for_template(
        self,
        win,
        agent_name: str,
        window_id: str,
        track_arrange: bool = True,
        keep_team_name: bool = False,
    ):
        """为模板新建的窗口延后执行团队 UI 补注册（确保 backend 已初始化）。

        Args:
            track_arrange: False 时旁路模板专用排列计数（恢复路径自行排列，
                避免 _pending_arrange_count 被误递减破坏模板加载计数）
            keep_team_name: True 时保留调用方已设置的 _team_name
                （恢复路径用会话记录里的团队名，不覆盖为模板名）

        语义（C3 写盘合并）：
        - join_team 已由调用方**同步前置**执行（_spawn_team_member_window /
          _on_team_restore_requested 各恰好一次），本方法**不再写盘 join**，
          退化为纯 UI 补注册：agent 切换 + 权限 + 团队项目 + UI 刷新 + watcher。
        - backend 未就绪 → C4 就绪轮询（_TEAM_JOIN_MAX_RETRIES 次 ×
          _TEAM_JOIN_RETRY_INTERVAL_MS 间隔）后放弃。
        - 所有提前 return 路径（窗口销毁 / 重试耗尽 / 异常）统一递减
          _pending_arrange_count（E3 计数兜底），保证排列回调不垛死。
        """
        try:
            if getattr(win, "_is_destroyed", False):
                # E3：窗口已销毁 → 计数无人递减，此处统一收口
                if track_arrange:
                    self._pending_arrange_count = max(0, self._pending_arrange_count - 1)
                    if self._pending_arrange_count == 0:
                        self._do_team_window_arrange()
                return
            if (
                not getattr(win, "backend", None)
                or not win.backend.agent_manager
                # 🐛 团队邮件角色 build 修复：_on_agent_changed 有
                # `not self.backend.chat_engine` 守卫（ChatEngine 400ms 延迟创建），
                # 仅等 agent_manager 会导致回调在 chat_engine 就绪前提前 return，
                # switch_agent/set_team_context 未写入正确角色 → 邮件发件人恒为默认 build。
                # 补查 chat_engine，就绪后再回调，从根上消除竞态。
                or not win.backend.chat_engine
            ):
                # C4 就绪轮询：backend/chat_engine 未就绪 → 重试而非直接放弃
                retries = getattr(win, "_team_join_retries", 0)
                if retries < self._TEAM_JOIN_MAX_RETRIES:
                    win._team_join_retries = retries + 1
                    logger.warning(
                        f"[_join_new_window_for_template] window {window_id} backend/chat_engine 未就绪，"
                        f"重试 {win._team_join_retries}/{self._TEAM_JOIN_MAX_RETRIES}"
                    )
                    QTimer.singleShot(
                        self._TEAM_JOIN_RETRY_INTERVAL_MS,
                        lambda w=win, a=agent_name, wid=window_id, ta=track_arrange, kn=keep_team_name: (
                            self._join_new_window_for_template(w, a, wid, ta, kn)
                        ),
                    )
                    return
                logger.error(f"[_join_new_window_for_template] window {window_id} backend 重试耗尽，放弃")
                # C4a：watcher 不依赖 backend，无条件补启动（缺则任务邮件永不主动触发；
                # 有 _is_destroyed 守卫 + _start_team_watcher 内部 try/except）
                try:
                    if not getattr(win, "_is_destroyed", False):
                        win._start_team_watcher()
                except Exception:
                    pass
                # E3：重试耗尽同样收口计数
                if track_arrange:
                    self._pending_arrange_count = max(0, self._pending_arrange_count - 1)
                    if self._pending_arrange_count == 0:
                        self._do_team_window_arrange()
                return
            if hasattr(win, "_on_agent_changed"):
                win._on_agent_changed(agent_name)
            if hasattr(win, "_apply_agent_command_permissions"):
                win._apply_agent_command_permissions(agent_name)
            win._team_agent_name = agent_name
            tm_mgr = self._get_team_manager()
            if not keep_team_name:
                win._team_name = (tm_mgr.get_template() or {}).get("name") or "default"
                # 🛡️ 恢复路径 keep_team_name=True 时保留调用方已设的 run_id
                # （恢复窗口已设 new_run_id，避免延后期内被中途 start_team_run
                # 刷新覆盖，导致恢复窗口归入错误 run_id 分组漂移）
                win._team_run_id = tm_mgr.get_team_run_id()
            # 🛡️ C3：不再重复 join_team（调用方已同步前置执行，恰好一次写盘）
            # 应用团队级统一项目（若已设置）：延迟补注册后与团队共享同一项目
            team_project = tm_mgr.get_team_project()
            if team_project:
                win._apply_team_project(team_project)
            # 应用团队级统一工作目录/工作树（若已设置）：与团队共享同一工作树
            team_workdir = tm_mgr.get_team_workdir()
            if team_workdir:
                win._apply_team_workdir(team_workdir)
            if hasattr(win, "_refresh_team_ui"):
                try:
                    win._refresh_team_ui(agent_name)
                except Exception:
                    pass
            if hasattr(win, "_start_team_watcher"):
                try:
                    win._start_team_watcher()
                except Exception:
                    pass
            self._sync_active_windows_to_team_manager()
            logger.info(f"[_join_new_window_for_template] {agent_name}@{window_id} 已加入模板团队")

            if track_arrange:
                # 选中该窗口并递减待排列计数
                self._pending_arrange_count = max(0, self._pending_arrange_count - 1)
                try:
                    from app.tray_manager import TrayManager

                    tm_tray = TrayManager.get_instance()
                    dialog = win.window() if hasattr(win, "window") else win
                    if dialog and dialog is not win and hasattr(dialog, "set_selection_indicator"):
                        tm_tray._select_window(dialog)
                except Exception:
                    pass

                if self._pending_arrange_count == 0:
                    self._do_team_window_arrange()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[_join_new_window_for_template] 失败: {e}")
            # 🛡️ C4 兜底：异常时窗口可能已建成，至少刷新胶囊使其归入团队分组
            try:
                if not getattr(win, "_is_destroyed", False):
                    from app.widgets.tab_manager_window import TabManagerWindow

                    tm_win = TabManagerWindow.get_instance()
                    if tm_win is not None:
                        tm_win.refresh_capsule_for_window(win)
            except Exception:
                pass
            if track_arrange:
                self._pending_arrange_count = max(0, self._pending_arrange_count - 1)
                if self._pending_arrange_count == 0:
                    self._do_team_window_arrange()

    def _do_team_window_arrange(self):
        """在模板加载后自动排列已选中的窗口（归零回调）。"""
        try:
            from app.tray_manager import TrayManager

            TrayManager.get_instance().arrange_selected_windows_grid()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[_do_team_window_arrange] 网格排列失败: {e}")

    def _handle_team_templates(self):
        """列出所有已保存模板。"""
        from app.core.team.template_manager import TemplateManager

        try:
            tm = TemplateManager.get_instance()
            templates = tm.list_templates()
        except Exception as e:  # noqa: BLE001
            InfoBar.error(
                "列出失败",
                f"读取模板目录失败: {e}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        if not templates:
            user_dir = tm.user_dir
            dir_hint = str(user_dir) if user_dir else "user-custom 插件"
            InfoBar.info(
                "无模板",
                f"当前没有模板。\n保存: /team --save=<name>\n保存目录: {dir_hint}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        source_labels = {
            tm.SOURCE_USER: "\U0001f464 用户",
            tm.SOURCE_PLUGIN: "\U0001f4e6 插件",
            tm.SOURCE_SYSTEM: "\U00002699 系统",
        }
        groups: Dict[str, List] = {}
        for t in templates:
            src = t.get("source", tm.SOURCE_SYSTEM)
            groups.setdefault(src, []).append(t)

        lines = [f"已保存模板 ({len(templates)} 个):"]
        for src_key in [tm.SOURCE_USER, tm.SOURCE_PLUGIN, tm.SOURCE_SYSTEM]:
            group = groups.get(src_key)
            if not group:
                continue
            label = source_labels.get(src_key, src_key)
            lines.append(f"  \u2500\u2500 {label} \u2500\u2500")
            for t in group:
                desc = t.get("description") or "(无描述)"
                agents = ", ".join(t.get("agent_names", []))
                lines.append(f"    \u2022 {t['name']} \u2014 {t['agent_count']} 个角色 [{agents}]")
                lines.append(f"      {desc}")
        lines.append("\n保存: /team --save=<name>  |  删除: /team --delete=<name>")
        InfoBar.info(
            f"团队模板 ({len(templates)})",
            "\n".join(lines),
            parent=TabManagerWindow.get_instance() or self.window(),
            duration=10000,
            position=InfoBarPosition.BOTTOM,
        )

    def _handle_team_delete(self, name: str):
        """删除模板文件。"""
        from app.core.team.template_manager import TemplateManager, TemplateError

        try:
            tm = TemplateManager.get_instance()
            deleted = tm.delete(name)
        except TemplateError as e:
            InfoBar.error(
                "删除失败",
                str(e),
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
            return
        except Exception as e:  # noqa: BLE001
            InfoBar.error(
                "删除失败",
                f"未预期错误: {e}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        if deleted:
            InfoBar.success(
                "模板已删除",
                f"  名称: {name}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
        else:
            # 检查模板是否存在于其他来源（系统/插件），给出更具体的提示
            source = tm.get_source(name)
            if source:
                source_labels = {
                    tm.SOURCE_SYSTEM: "系统内置",
                    tm.SOURCE_PLUGIN: "插件提供",
                    tm.SOURCE_USER: "用户自定义",
                }
                src_label = source_labels.get(source, source)
                InfoBar.warning(
                    "只读模板",
                    f"模板「{name}」来自 {src_label}，只读不可删除。\n仅用户保存的模板（来源: user）可删除。",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    duration=5000,
                    position=InfoBarPosition.BOTTOM,
                )
            else:
                InfoBar.warning(
                    "模板不存在",
                    f"  名称: {name}",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    duration=3000,
                    position=InfoBarPosition.BOTTOM,
                )

    # ── 邮箱监听与任务处理 ───────────────────────────

    def _start_team_watcher(self):
        """启动邮箱目录的文件系统监听"""
        self._rearm_team_watcher()

        # 🛡️ 启动快照（F1 P0-1）：记录当前已有邮件 id，后续 watcher 事件只对"新 id"响应，
        # 状态写回（mark_mail_running/pending/done 改写同一文件）不会回流重触发。
        self._known_mail_ids = self._snapshot_mail_ids()

        # 启动时立即处理已有未处理邮件
        self._check_and_process_pending()

        # 🛡️ 邮箱目录一旦被外部删除（清理逻辑 / 手工删除），QFileSystemWatcher 会永久
        # 丢失该路径且不再回调，成员将彻底收不到任务邮件。用低频定时器自愈。
        timer = getattr(self, "_team_watch_guard_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(5000)
            timer.timeout.connect(self._rearm_team_watcher)
            self._team_watch_guard_timer = timer
        if not timer.isActive():
            timer.start()

    def _rearm_team_watcher(self):
        """确保邮箱目录存在且处于监听中（目录被删后可自愈重建）"""
        if getattr(self, "_is_destroyed", False):
            return
        if not getattr(self, "_team_agent_name", ""):
            return  # 非团队模式，无需监听
        from app.core.team_manager import TeamManager

        try:
            tm = TeamManager.get_instance()
            mailbox_path = tm._mailbox_dir("default", self._window_id)
            mailbox_path.mkdir(parents=True, exist_ok=True)
            mailbox_dir = str(mailbox_path)
            # Qt 会在路径失效时把它从 directories() 中剔除，需重新 addPath
            if mailbox_dir not in self._team_fs_watcher.directories():
                self._team_fs_watcher.addPath(mailbox_dir)
                self._team_watch_paths.add(mailbox_dir)
                # 目录曾经消失过，重新挂载后补一次轮询，避免漏掉期间到达的邮件。
                # 仅当出现"新邮件 id"才处理：状态写回（running/pending/done 改写
                # 同一文件）不回流，否则停止回滚的 pending 邮件会被 5s 重挂轮询
                # 再次拉起 → 自动重触发（F1 P0-1）。
                current_ids = self._snapshot_mail_ids()
                if current_ids - self._known_mail_ids:
                    self._known_mail_ids = current_ids
                    self._check_and_process_pending()
                else:
                    self._known_mail_ids = current_ids
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[TeamWatcher] 重挂邮箱监听失败: {e}")

    def _stop_team_watcher(self):
        """停止邮箱文件监听"""
        timer = getattr(self, "_team_watch_guard_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        for p in list(self._team_watch_paths):
            try:
                self._team_fs_watcher.removePath(p)
            except Exception:
                pass
        self._team_watch_paths.clear()

    def _snapshot_mail_ids(self) -> set:
        """扫描当前邮箱目录的邮件 id 集合（F1 P0-1）。

        用于区分"新邮件到达"与"状态写回"：TeamManager 状态写回
        （mark_mail_running/pending/done 经 _write_json 改写邮箱目录内
        mail_*.json）也会触发 directoryChanged（Windows 实测），但状态变化
        由调用方直接驱动，watcher 回流只会造成"停止后 pending 邮件立即被
        重新拉起"的自触发循环。id 集合不变 → 纯状态写回；出现新 id → 新邮件。
        """
        try:
            tm = self._get_team_manager()
            return {m.get("id") for m in tm.get_mailbox_mails(self._window_id) if m.get("id")}
        except Exception:  # noqa: BLE001
            return set()

    def _on_team_mailbox_changed(self, path: str):
        """邮箱目录变更 → 检查新邮件，串行处理（F1 P0-1：只响应新邮件文件）"""
        if getattr(self, "_is_destroyed", False):
            self._stop_team_watcher()
            return
        current_ids = self._snapshot_mail_ids()
        if current_ids == self._known_mail_ids:
            return  # 纯状态写回（running/pending/done），不回流处理
        self._known_mail_ids = current_ids
        self._check_and_process_pending()

    def _check_and_process_pending(self):
        """检查并处理待办任务邮件（串行：一次只处理一个）

        流式路径：作为 hook 消息注入到当前消息流，不中断流式。
        LLM 在下一轮 API 调用时自动看到（与子智能体完成信号机制相同）。
        非流式路径：正常走对话流程处理。
        """
        if self._team_processing:
            return  # 正在处理中，跳过

        # 🛡️ 停止后冷却（F1 P1-3）：手动停止后 1s 内不自动拉起 pending 邮件。
        # 停止回滚（mark_mail_pending 写回邮箱 JSON）与 watcher 事件之间的
        # 竞态窗口内，任何 watcher/rearm 触发的本函数都可能把同一封邮件立即
        # 重开新对话（用户视角"停止停不掉"）。用户手动发消息不经过本函数
        # （worker 侧 _inject_pending_hook_messages 注入路径），不受影响。
        if time.monotonic() - getattr(self, "_last_stop_time", 0.0) < 1.0:
            logger.debug("[TeamMail] 停止冷却期内，跳过 pending 自动处理")
            return

        tm = self._get_team_manager()
        pending = tm.get_pending_tasks(self._window_id)
        if not pending:
            return

        mail = pending[0]

        # 🆕 流式路径：作为 hook 消息注入到当前消息流，不中断流式
        if self._is_streaming:
            self._inject_team_mail_as_hook(mail)
        else:
            # 非流式路径：正常走对话流程处理
            self._team_processing = True
            self._process_team_task(mail)

    def _inject_team_mail_as_hook(self, mail: dict):
        """流式中：将团队任务邮件作为 hook 消息注入到当前消息流

        复用 backend._hook_message_queue 机制（与子智能体完成信号相同），
        邮件包装为 <system-reminder> 格式，chat_worker 在下一轮 API 调用前
        自动消费并注入上下文，LLM 在下一轮响应中即可感知任务邮件。
        """
        from app.core.backend import _format_hook_output

        tm = self._get_team_manager()
        tm.mark_mail_running(mail["id"], self._window_id)

        task_desc = mail.get("body", mail.get("subject", ""))
        from_agent = mail.get("from_agent", "?")
        from_window = mail.get("from_window", "?")
        sender_id = f"{from_agent}@{from_window}"

        # 构建 hook 内容
        content = (
            f"📨 **来自 [{sender_id}] 的任务邮件：**\n\n"
            f"{task_desc}\n\n"
            f"（以上是系统自动注入的团队成员任务邮件，请根据上下文酌情处理）"
        )
        hook_content = _format_hook_output("TeamMail", content, wrap_system_reminder=False)

        # 推送到 _hook_message_queue，worker 在下一轮 API 调用前自动消费
        self.backend._hook_message_queue.put(
            {
                "role": "user",
                "content": hook_content,
                "_hook_event": "TeamMail",
            }
        )

        # 记录注入的邮件信息，供流结束时标记完成
        self._injected_team_mails.append(
            {
                "mail_id": mail["id"],
                "mail": mail,
                "injected_at": time.time(),
            }
        )

        logger.info(f"[TeamMail] 流式注入团队邮件: #{mail['id']} from [{sender_id}]")

    def _process_team_task(self, mail: dict):
        """处理任务邮件：标记运行中，插入聊天流走正常对话流程"""
        tm = self._get_team_manager()
        tm.mark_mail_running(mail["id"], self._window_id)

        task_desc = mail.get("body", mail.get("subject", ""))
        from_agent = mail.get("from_agent", "?")

        # 保存邮件上下文（供流完成时清理用）
        self._current_team_mail = {
            "mail": mail,
            "from_agent": from_agent,
        }

        # 显示完整发送方身份（agent@window），方便 member 回调
        from_window = mail.get("from_window", "?")
        sender_id = f"{from_agent}@{from_window}"

        # 像正常对话一样发送任务消息
        # 🔧 hook_event="TeamMail"：给消息打 _hook_event 标记，
        # 使 get_team_first_question 等预览逻辑能识别并跳过邮件内容（R1 源头打标）
        # 🛡️ preserve_input=True：邮件自动发送不得清空用户正在编辑的输入框/附件
        # （Bug 修复：此前复用 _on_send_clicked 会无条件 input_area.clear()，
        # 成员邮件到达时把用户输入框内容抹掉）。
        user_msg = f"📨 **来自 [{sender_id}] 的任务邮件：**\n\n{task_desc}"
        self._on_send_clicked(user_msg, hook_event="TeamMail", preserve_input=True)

        # 🛡️ 团队邮件锁释放（Bug2）：_on_send_clicked 有 8+ 个 send 前提前 return
        # 分支（模型无效无 API_KEY / 命令拦截 / 技能切换 / 无文本等），命中任一分支
        # 都不会进入流式，但 _team_processing 已被 _check_and_process_pending 置 True。
        # 若不释放：后续所有团队邮件被 _team_processing 拦截，且该邮件永久卡 running
        # （团队邮件系统死锁）。_on_send_clicked 在 _do_deferred_send（下一 tick）之前
        # 同步置 _is_streaming=True，因此返回后检查 _is_streaming 可精准区分
        # "被提前 return 拦截" vs "正常进入流式"。
        if not self._is_streaming:
            self._rollback_team_mail_processing()
            return
        # 兜底（Bug2 防线 2）：_do_deferred_send 内 send_message_to_engine 失败会
        # 把 _is_streaming 置回 False（下一 tick 异步发生，同步检查看不到），
        # 1.5s 后若锁仍持有且未流式 → 复位，杜绝任何漏网路径造成永久死锁。
        from PyQt5.QtCore import QTimer as _QTimer

        _QTimer.singleShot(1500, self._delayed_team_mail_lock_guard)

    def _rollback_team_mail_processing(self):
        """团队邮件处理被拦截（未进入流式）时复位锁 + 回滚 pending（Bug2）。

        与 _sync_team_mail_on_stop 的未响应分支一致：mark_mail_pending 回滚，
        由后续 _check_and_process_pending 重新排队处理（宁可重复处理不丢失）。
        """
        tm = self._get_team_manager()
        ctx = getattr(self, "_current_team_mail", None)
        if ctx:
            try:
                tm.mark_mail_pending(ctx["mail"]["id"], self._window_id)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[TeamMail] 回滚 pending 失败: {e}")
            self._current_team_mail = None
        self._team_processing = False

    def _delayed_team_mail_lock_guard(self):
        """延迟兜底（Bug2 防线 2）：1.5s 后锁仍持有且未进入流式 → 复位。

        覆盖 _do_deferred_send 中 send_message_to_engine 失败（异步把 _is_streaming
        置回 False）等同步检查不可见的漏网路径，杜绝团队邮件系统永久死锁。
        """
        if not getattr(self, "_is_destroyed", False) and getattr(self, "_team_processing", False):
            if not self._is_streaming:
                self._rollback_team_mail_processing()

    def _finalize_single_team_mail(self, mail: dict, done_result: str = "") -> bool:
        """按实际响应状态收尾单封团队邮件（Bug3：复用 _mail_was_responded 判定）。

        有响应 → mark_mail_done（结果取 last_assistant_text 或显式 done_result）；
        无响应 → mark_mail_pending 回滚（宁可下次重复处理，不误标终态 done 丢失）。

        Returns:
            True=标 done；False=回滚 pending
        """
        tm = self._get_team_manager()
        session = self.session_manager.get_current_session() if hasattr(self, "session_manager") else None
        if self._mail_was_responded(session, mail):
            result = done_result or self._last_non_hook_assistant_text(session)
            tm.mark_mail_done(mail["id"], self._window_id, result)
            return True
        tm.mark_mail_pending(mail["id"], self._window_id)
        return False

    def _on_task_stream_finished(self):
        """流式完成后标记任务邮件为已完成（Bug3：按实际响应状态收尾）"""
        ctx = getattr(self, "_current_team_mail", None)
        if not ctx:
            return
        self._current_team_mail = None

        mail = ctx["mail"]
        # 🛡️ 修复（Bug3）：LLM 流式结束 ≠ 邮件已响应。若流式仅产生工具调用/
        # 响应被截断/未输出实质内容，无条件 mark_mail_done 会把邮件误标终态
        # 永久丢失（done 不可回退）。复用 _mail_was_responded 判定：
        # 有响应 → done（结果取最后一条非 hook assistant 文本）；
        # 无响应 → mark_mail_pending 回滚，由 _check_and_process_pending 重新排队。
        self._finalize_single_team_mail(mail)
        self._team_processing = False
        self._check_and_process_pending()

    def _mail_was_responded(self, session, mail) -> bool:
        """判断邮件是否被 LLM 实际响应过（修复 T23：收尾不误标 done）。

        标准：session.messages 中存在该邮件对应的 TeamMail user 消息
        （content 含发送方 agent@window 标识 + "任务邮件" 字样），且其后存在
        非 hook 的 assistant 消息（LLM 针对该邮件给出了回复）。
        未定位到邮件消息（可能未注入 session）→ 保守判定未处理（回滚 pending，
        宁可下次重复处理也不丢失）。
        """
        if not session or not session.messages:
            return False
        target_sender = f"{mail.get('from_agent', '?')}@{mail.get('from_window', '?')}"
        mail_id = mail.get("id", "")
        marker_idx = -1
        for i, msg in enumerate(session.messages):
            if msg.get("_hook_event") != "TeamMail":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                from app.core import content_to_text

                content = content_to_text(content)
            text = str(content)
            if (mail_id and mail_id in text) or (target_sender in text and "任务邮件" in text):
                marker_idx = i
        if marker_idx < 0:
            return False
        for msg in session.messages[marker_idx + 1 :]:
            if msg.get("role") == "assistant" and not msg.get("_hook_event"):
                return True
        return False

    @staticmethod
    def _last_non_hook_assistant_text(session) -> str:
        """取最后一条非 hook 的 assistant 消息文本（作邮件处理结果）。"""
        if not session or not session.messages:
            return ""
        for msg in reversed(session.messages):
            if msg.get("role") == "assistant" and not msg.get("_hook_event"):
                content = msg.get("content", "")
                if isinstance(content, list):
                    from app.core import content_to_text

                    content = content_to_text(content)
                return content or ""
        return ""

    def _finalize_injected_team_mails(self):
        """流式结束时，按处理状态收尾所有 hook 注入的团队邮件（修复 T23）。

        覆盖两条注入路径：
        1. main_widget._inject_team_mail_as_hook → _injected_team_mails 跟踪
        2. chat_worker._inject_pending_hook_messages → 邮件状态为 "running"

        关键修复：**注入 ≠ 处理**。收尾阶段注入的邮件可能未被 LLM 响应
        （chat_worker 退出前最后一次注入后直接 return，无新一轮 API），
        无条件 mark_mail_done 会误杀（done 是终态，永久丢失）。改为按
        _mail_was_responded 判断：有响应 → done（结果取最后一条非 hook
        assistant 文本）；无响应 → mark_mail_pending 回滚，由流结束后的
        _check_and_process_pending 重新排队处理。
        """
        tm = self._get_team_manager()

        done = 0
        requeued = 0
        tracked_ids = set()

        # 路径 1：main_widget 跟踪的注入邮件
        injected = getattr(self, "_injected_team_mails", None)
        if injected:
            for ctx in injected:
                mail = ctx["mail"]
                tracked_ids.add(mail["id"])
                if self._finalize_single_team_mail(mail):
                    done += 1
                else:
                    requeued += 1
            self._injected_team_mails.clear()

        # 路径 2：worker 直接注入的 "running" 邮件
        for mail in tm.get_running_tasks(self._window_id):
            if mail["id"] in tracked_ids:
                continue
            tracked_ids.add(mail["id"])
            if self._finalize_single_team_mail(mail):
                done += 1
            else:
                requeued += 1

        if done or requeued:
            logger.info(f"[TeamMail] 流式结束：{done} 封已处理标 done，{requeued} 封未处理回滚 pending")

    def _sync_team_mail_on_stop(self):
        """手动停止时，按处理状态收尾所有团队邮件（修复 T23：未处理回滚 pending）。

        用户停止对话 ≠ 邮件处理完毕：已被 LLM 响应的邮件标 done（结果记录为
        手动停止）；未被响应的回滚 pending，下次对话 _check_and_process_pending
        重新处理，避免误标 done 导致邮件永久丢失。
        """
        tm = self._get_team_manager()

        # 1. 正在处理的团队任务邮件（_process_team_task 路径）
        if getattr(self, "_current_team_mail", None):
            mail = self._current_team_mail["mail"]
            self._finalize_single_team_mail(mail, done_result="用户手动停止")
            self._current_team_mail = None
            self._team_processing = False

        # 2. hook 注入的团队邮件（_inject_team_mail_as_hook + worker 路径）
        tracked_ids = set()
        injected = getattr(self, "_injected_team_mails", None)
        if injected:
            for ctx in injected:
                mail = ctx["mail"]
                tracked_ids.add(mail["id"])
                self._finalize_single_team_mail(mail, done_result="用户手动停止")
            self._injected_team_mails.clear()

        for mail in tm.get_running_tasks(self._window_id):
            if mail["id"] in tracked_ids:
                continue
            self._finalize_single_team_mail(mail, done_result="用户手动停止")

    def _get_model_config_obj(self) -> dict:
        """获取当前模型配置（兜底）"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            return {
                "api_base": cfg.llm_api_base.value or "",
                "api_key": cfg.llm_api_key.value or "",
                "model": cfg.llm_model.value or "",
            }
        except Exception:
            return {}

    def _sync_active_windows_to_team_manager(self):
        """同步当前所有活跃窗口 ID 到 TeamManager，触发失效成员清理"""
        from app.core.team_manager import TeamManager

        tm = TeamManager.get_instance()
        active_ids = set()
        for inst in list(OpenAIChatToolWindow._instances):
            wid = getattr(inst, "_window_id", None)
            if wid and not getattr(inst, "_is_destroyed", False):
                active_ids.add(wid)
        # 🛡️ __init__ 阶段本窗口尚未 append 进 _instances（注册在数百行之后），
        # 若不显式补入：首个窗口会同步出空集合、后续窗口会把自己排除在活跃集之外，
        # 导致 TeamManager 把在册成员判为 stale 并 rmtree 其邮箱目录。
        # 窗口关闭路径已先置 _is_destroyed=True，故此处不会把将死窗口重新算作活跃。
        self_wid = getattr(self, "_window_id", None)
        if self_wid and not getattr(self, "_is_destroyed", False):
            active_ids.add(self_wid)
        tm.set_active_window_ids(active_ids)

    # ── 内部辅助 ─────────────────────────────────────

    def _get_team_manager(self):
        from app.core.team_manager import TeamManager

        return TeamManager.get_instance()

    def _agent_to_color(self, agent_name: str) -> str:
        """用智能体名称的 hash 生成稳定的辨识色

        保证输出的 HSL 颜色有良好的饱和度和亮度，避免过亮/过暗。
        同一 agent_name 始终返回同一颜色。
        """
        # 使用 Python 内置 hash（稳定但进程间不一致，够用）
        h = abs(hash(agent_name)) % 360
        # 固定饱和 65%，亮度 50%（中等亮度）
        return f"hsl({h}, 65%, 50%)"

    def _refresh_team_ui(self, agent_name: str = "", is_team: bool = True):
        """刷新团队模式下的 UI

        - 更新窗口标题（保持会话标题，角色名只进胶囊/边框颜色，不覆盖标题）
        - 更新窗口边框样式
        """
        # 标题始终使用会话标题（让 Windows 任务栏能区分各窗口；
        # 团队模式下角色名通过 Tab 胶囊 / 窗口边框颜色标识，不覆盖标题）
        session = self.session_manager.get_current_session() if self.session_manager else None
        if session:
            title = session.topic_summary or session.name or "飘狐"
        else:
            title = "飘狐"

        if is_team and agent_name:
            # 团队模式：窗口边框 + 标题字体颜色：根据 agent 生成辨识色
            color = self._agent_to_color(agent_name)
            self._set_dialog_border(color)
            if self._title_bar:
                self._title_bar.set_title_color(color)
        else:
            # 独立模式：恢复默认边框与字体颜色
            self._set_dialog_border("none")
            # 恢复默认字体颜色：清除行内颜色样式 + 刷新标题栏样式
            if self._title_bar:
                self._title_bar.set_title_color("")
                if hasattr(self._title_bar, "refresh_style"):
                    self._title_bar.refresh_style()

        try:
            if self._title_bar:
                self._title_bar.set_title(title)
        except Exception:
            pass

        # 同步更新对话框的窗口标题（影响任务栏底部显示的名称）
        try:
            dialog = self.window() if hasattr(self, "window") else None
            if dialog and hasattr(dialog, "setWindowTitle"):
                dialog.setWindowTitle(title)
        except Exception:
            pass

        # 同时更新自身标题（供 Tab 管理器监听 windowTitleChanged）
        try:
            self.setWindowTitle(title)
        except Exception:
            pass

        # Tab 模式下主动同步胶囊状态：加入/离开团队时不依赖 windowTitleChanged 信号
        # （Qt 在标题未变时不发射该信号，新建空白窗口加入团队后标题仍是默认"飘狐"，
        # 导致 _on_win_title_changed 不触发、胶囊不显示）
        try:
            if self.cfg.enable_tab_manager.value:
                from app.widgets.tab_manager_window import TabManagerWindow

                _tm = TabManagerWindow.get_instance()
                if _tm is not None:
                    _tm.refresh_capsule_for_window(self)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[_refresh_team_ui] 同步 Tab 胶囊/分组跳过: {e}")

    def _sync_dialog_title(self):
        """同步对话框窗口标题为当前会话标题，供 Windows 任务栏区分各窗口

        当会话标题变更（用户重命名 / 主题摘要生成 / 切换会话）时调用，
        确保每个窗口在 Windows 任务栏右键菜单中显示不同的会话标题。

        团队模式下窗口标题同样保持会话标题（角色名只进胶囊/边框颜色），
        因此此处不再跳过。
        """
        session = self.session_manager.get_current_session() if self.session_manager else None
        if not session:
            return
        title = session.topic_summary or session.name or "新对话"
        try:
            dialog = self.window() if hasattr(self, "window") else None
            if dialog and hasattr(dialog, "setWindowTitle"):
                dialog.setWindowTitle(title)
        except Exception:
            pass
        # 同时更新自身标题（供 Tab 管理器监听 windowTitleChanged）
        try:
            self.setWindowTitle(title)
        except Exception:
            pass

    def _set_dialog_border(self, color: str):
        """设置外层弹窗的边框颜色"""
        try:
            dialog = self.window() if hasattr(self, "window") else None
            if dialog and hasattr(dialog, "set_border_color"):
                dialog.set_border_color(color)
        except Exception:
            pass

    def _execute_subagent_task(
        self,
        agent_name: str,
        task_description: str,
        with_context: bool = False,
        model_value: str = "",
    ):
        """触发子智能体任务（智能体命令 + --subagent 参数）

        Args:
            agent_name: 智能体名称（来自 agents 目录）
            task_description: 子智能体任务描述
            with_context: 是否传递当前会话历史作为上下文（对应 --with-context）
            model_value: 模型/服务商指定（对应 --model=xxx，支持 "模型名"、"服务商名"、"服务商:模型名"）
        """
        if not agent_name:
            InfoBar.warning(
                "参数错误",
                "缺少智能体名称",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 清空输入框（和函数型命令一样处理）
        self.input_area.clear()

        # 检查 AgentManager 中是否存在该智能体
        agent_mgr = self.backend.agent_manager
        if not agent_mgr:
            InfoBar.error(
                "未就绪",
                "智能体管理器未初始化",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            return

        available_agents = [a.name for a in agent_mgr.list_agents(include_hidden=True)]
        if agent_name not in available_agents:
            InfoBar.warning(
                "未知智能体",
                f"未找到智能体: {agent_name}，可用: {', '.join(available_agents)[:100]}",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            return

        sub_agent_mgr = self.backend.sub_agent_manager
        if not sub_agent_mgr:
            InfoBar.error(
                "未就绪",
                "子智能体管理器未初始化",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 解析模型配置（支持 --model=xxx 覆盖）
        llm_config = self._resolve_subagent_model_config(model_value)

        # 触发子智能体任务
        sub_agent_mgr.execute_task(
            task_id=f"agent_{uuid.uuid4().hex[:8]}",
            agent_name=agent_name,
            task_description=task_description,
            parent_context="",
            share_context=with_context,  # 由 --with-context 控制
            on_finished=None,
            on_error=None,
            llm_config=llm_config,
        )
        logger.info(f"[BuiltinCommands] 触发子智能体任务: agent={agent_name}, task={task_description[:50]}...")

    def _resolve_service_provider(self, name: str) -> Optional[str]:
        """把用户传入的"服务商名"解析成 config_id

        支持以下写法（按优先级）：
          1. config_id（如 "b2c3d4e5"）—— 直接命中 _valid_configs
          2. display_name（如 "OpenCode Zen #2"）—— 查 _display_to_config_id 映射
          3. display_name 遍历 _valid_configs（_display_to_config_id 未就绪时的兜底）
          4. provider_name（如 "OpenCode Zen"，同名时取第一个）—— 遍历匹配
          5. 大小写不敏感的 provider_name 匹配
        返回 None 表示找不到。
        """
        if not name:
            return None
        # 1. config_id 直接命中
        if name in self._valid_configs:
            return name
        # 2. display_name 命中 _display_to_config_id 缓存
        display_to_config = getattr(self, "_display_to_config_id", {})
        if name in display_to_config:
            return display_to_config[name]
        # 3. display_name 遍历 _valid_configs 兜底（缓存未就绪时仍可匹配）
        for cid, info in self._valid_configs.items():
            if info.get("display_name") == name:
                return cid
        # 4. provider_name 精确匹配（同名取第一个）
        for cid, info in self._valid_configs.items():
            if info.get("provider_name") == name:
                return cid
        # 5. provider_name 大小写不敏感匹配
        name_lower = name.lower()
        for cid, info in self._valid_configs.items():
            pname = info.get("provider_name", "")
            if pname and pname.lower() == name_lower:
                return cid
        return None

    def _resolve_subagent_model_config(self, model_value: str, show_error: bool = True) -> Optional[Dict]:
        """解析 --model=xxx 参数，返回覆盖后的 LLM 配置

        支持三种格式：
          - "模型名"          → 仅覆盖当前服务商的模型名称
          - "服务商名"        → 切换到指定服务商的完整配置
          - "服务商名:模型名"  → 切换到指定服务商并覆盖模型名称

        "服务商名" 支持：config_id / display_name / provider_name 三种写法。

        Args:
            show_error: True 时解析失败弹出 InfoBar 警告（用户手动输入场景，
                如 /subagents --model=、/title-gen --model= 命令）；
                False 时静默返回 None（后台自动流程，如标题生成、子智能体
                默认模型解析——解析失败应静默回退主模型，避免打扰用户）。
        """
        if not model_value:
            return None  # 使用默认配置

        if ":" in model_value:
            # 格式: "服务商名:模型名"
            provider, model_query = model_value.split(":", 1)
            config_id = self._resolve_service_provider(provider)
            if config_id is None:
                if show_error:
                    InfoBar.warning(
                        "未知服务商",
                        f"未找到服务商: {provider}",
                        parent=TabManagerWindow.get_instance() or self.window(),
                        position=InfoBarPosition.BOTTOM,
                    )
                return None
            model_query_lower = model_query.lower()
            matched = self._fuzzy_match_model_name(
                config_id,
                model_query_lower,
                lambda: self._get_model_list_for_provider(config_id),
            )
            if matched is not None:
                config = self._valid_configs[config_id].copy()
                config["模型名称"] = matched
                return config
            else:
                available = self._get_model_list_for_provider(config_id)
                if show_error:
                    InfoBar.warning(
                        "模型不存在",
                        f"服务商 {provider} 下未找到以「{model_query}」开头的模型，可用: {', '.join(available)}",
                        parent=TabManagerWindow.get_instance() or self.window(),
                        position=InfoBarPosition.BOTTOM,
                    )
                return None
        # 格式: "服务商名" — 切换到该服务商（取默认模型）
        config_id = self._resolve_service_provider(model_value)
        if config_id is not None:
            return self._valid_configs[config_id].copy()
        # 格式: "模型名" — 在当前服务商模糊匹配
        config = self._get_current_model_config()
        provider = self._current_provider_name
        model_query_lower = model_value.lower()
        matched = self._fuzzy_match_model_name(
            provider,
            model_query_lower,
            lambda: self._get_model_list_for_provider(provider),
        )
        if matched is not None:
            config = dict(config)
            config["模型名称"] = matched
            return config
        else:
            available = self._get_model_list_for_provider(provider)
            if show_error:
                InfoBar.warning(
                    "模型不存在",
                    f"未找到以「{model_value}」开头的模型，可用: {', '.join(available)}",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.BOTTOM,
                )
            return None

    def _get_model_list_for_provider(self, provider: str) -> List[str]:
        """获取指定服务商的模型列表（供模糊匹配用）"""
        config = self._valid_configs.get(provider, {})
        model_list = config.get("模型列表", [])
        if isinstance(model_list, str):
            try:
                import ast

                model_list = ast.literal_eval(model_list)
            except Exception:
                model_list = []
        # 模型列表缺失/为空 → 回退到 merged_provider_models（硬编码 + models.dev），
        # 与 _load_model_selector_to_card 的兜底逻辑一致。默认注入的 OpenCode
        # 免费服务商故意不写"模型列表"键（依赖异步刷新回填），在异步刷新尚未
        # 完成/失败时，若不回退会导致 _resolve_subagent_model_config 匹配不到
        # 用户已保存的免费模型（如 mimo-v2.5-free），弹"模型不存在"警告并回退主模型。
        if not model_list:
            pname = config.get("provider_name", provider)
            merged = get_merged_provider_models()
            if pname in merged:
                model_list = list(merged[pname])
        # 也把当前选中的模型加进去（万一不在列表里）
        current = config.get("模型名称", "")
        if current and current not in model_list:
            model_list = [current] + list(model_list)
        return list(model_list)

    def _fuzzy_match_model_name(
        self, provider: str, query: str, get_model_list: Callable[[], List[str]]
    ) -> Optional[str]:
        """前缀+大小写不敏感模糊匹配模型名

        匹配规则（按优先级）：
        1. 精确匹配（忽略大小写）
        2. 前缀匹配（忽略大小写）

        Args:
            provider: 服务商名（用于日志）
            query: 用户输入（小写）
            get_model_list: 获取模型列表的回调

        Returns:
            匹配到的模型名（原始大小写），无匹配返回 None
        """
        model_list = get_model_list()
        if not model_list:
            return None

        # 1. 精确匹配（忽略大小写）
        for name in model_list:
            if name.lower() == query:
                return name

        # 2. 前缀匹配（忽略大小写）
        matches = [name for name in model_list if name.lower().startswith(query)]

        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            # 多匹配时取第一个（列表顺序已由服务商配置决定）
            return matches[0]

        return None

    def _trigger_context_compaction(self, clear_after: bool = False, user_hint: str = ""):
        """触发上下文压缩：调用 compaction 子智能体压缩当前对话

        Args:
            clear_after: 是否在子智能体执行成功后清空当前会话的所有历史消息
            user_hint: 用户在 /compact 后提供的自由文本（已剥离 --clear 标记），
                       会作为补充说明附加到子智能体的任务描述中
        """
        session = self.session_manager.get_current_session()
        if not session or not session.messages:
            InfoBar.warning(
                "无法压缩",
                "当前会话没有对话内容",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            return

        sub_agent_mgr = self.backend.sub_agent_manager
        if not sub_agent_mgr:
            InfoBar.error(
                "未就绪",
                "子智能体管理器未初始化",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 拼接子智能体任务描述：默认任务 + 用户补充说明（如果有）
        base_task = "请压缩当前对话上下文，生成详细的工作摘要"
        task_description = f"{base_task}。用户补充说明：{user_hint}" if user_hint else base_task

        # 自动压缩时要求子智能体在摘要末尾输出用户最新提问，便于继续执行
        if clear_after and not user_hint:
            task_description += (
                "\n\n## 重要：压缩完成后需继续执行\n"
                "在标准的 5 段输出末尾，追加一节 ## Continue：\n"
                "从对话上下文中找到用户最新的提问，原样输出（不要修改任何文字、不要翻译、不要总结）。\n"
                "这是为了压缩完成后 AI 能紧接用户的提问继续执行，不要停下。"
            )

        # 仅在需要清空时才挂接完成回调，避免污染正常 compact 流程
        on_finished_cb = None
        if clear_after:
            on_finished_cb = lambda tid, result, _sid=session.session_id: self._on_compact_clear_finished(
                tid, result, _sid
            )
        else:
            # 普通 compact 完成后触发 SessionStart state="compact"
            on_finished_cb = lambda tid, result, _sid=session.session_id: self._on_compact_finished(tid, result, _sid)

        success = sub_agent_mgr.execute_task(
            task_id=f"compact_{uuid.uuid4().hex[:8]}",
            agent_name="compaction",
            task_description=task_description,
            parent_context="",
            share_context=True,  # 接入主智能体完整上下文
            on_finished=on_finished_cb,
            on_error=lambda err: InfoBar.error(
                "压缩失败",
                str(err)[:100],
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            ),
            session_id=session.session_id,  # 显式传 session_id，避免回退到 SubAgentManager 内部可能陈旧的值
        )

        if not success:
            # execute_task 返回 False 时显示启动失败（agent 缺失 / LLM 未配置等）
            InfoBar.error(
                "压缩失败",
                "无法启动压缩任务，请检查 LLM 配置",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )

    def _on_auto_compact_requested(self, ratio: float):
        """自动上下文压缩请求处理

        当 PreAssistantMessage hook 检测到上下文使用比例超过阈值时，
        由 backend.auto_compact_requested 信号触发。

        流程：
        1. 停止当前流式输出（与手动 /compact --clear 行为一致）
        2. 保存中断消息到 session（partial 消息不丢失）
        3. 触发上下文压缩子智能体

        防重入：若上一次自动压缩尚未完成，跳过本次触发。
        """
        # 防重入：上一次自动压缩未完成时跳过
        if self._auto_compact_in_progress:
            logger.info(f"[MainWidget] 自动压缩已在执行中，跳过本次触发 (ratio={ratio:.1%})")
            return

        logger.info(f"[MainWidget] 自动上下文压缩触发 (ratio={ratio:.1%})")

        # 先停止当前流式输出，确保会话状态稳定
        # 与手动 /compact --clear 一致：停止后再压缩，避免竞态条件：
        #  - Worker 的 _on_messages_updated 不会在清空后覆盖 session
        #  - UI 不会被 _display_current_session 重建后丢失流式卡片引用
        if self._is_streaming and self.backend.chat_engine:
            self._is_streaming = False
            self._set_ai_state("idle")
            self._toggle_send_stop(False)
            if self._current_assistant_card:
                self._current_assistant_card.stop_streaming_anim()
                self._current_assistant_card.finish_streaming()
            try:
                interrupted = self.backend.stop_streaming()
                if interrupted:
                    self._apply_interrupted_messages_to_session(interrupted)
            except Exception as e:
                logger.warning(f"[MainWidget] 自动压缩停止流式失败: {e}")

        self._auto_compact_in_progress = True
        QTimer.singleShot(0, lambda: self._trigger_context_compaction(clear_after=True))

    def _on_compact_clear_finished(self, task_id: str, result: str, session_id: str):
        """--clear 模式下，compaction 子智能体完成后的清空回调

        仅在子智能体执行成功（无 _execution_error）时清空历史消息；
        失败 / 取消时不清空，避免误删。

        清空语义：**只清空触发压缩时的那个 session 的 messages，不创建新会话**。
        - 保留 session 自身（session_id、name、topic_summary 均不变）
        - 不展示欢迎卡片
        - 不重置标题
        - 即使用户在子智能体执行期间切换到了其他 session，原 session 仍会被清空；
          UI 同步（清空聊天区域）仅在用户当前仍停留在原 session 时执行。

        Args:
            task_id: 子智能体任务 ID
            result: 子智能体返回的摘要内容
            session_id: 触发压缩时锁定的会话 ID（用于按 ID 找到原 session，不依赖 "current"）
        """
        # 重置自动压缩进度标志（即使 early return 也要重置，防止标志位卡死）
        self._auto_compact_in_progress = False

        if getattr(self, "_is_destroyed", False):
            logger.info(f"[DEBUG-cc] _on_compact_clear_finished early-return: _is_destroyed (task={task_id[:8]})")
            return
        # 校验执行结果：仅在无错误时清空
        sub_agent_mgr = self.backend.sub_agent_manager
        executor = sub_agent_mgr._running_tasks.get(task_id) if sub_agent_mgr else None
        if executor is None:
            # executor 可能已被 get_finished_tasks() 从 _running_tasks 移除（竞态条件）
            # 此时检查 _finished_tasks：有明确错误则不清空，否则继续尝试清空
            if sub_agent_mgr and task_id in sub_agent_mgr._finished_tasks:
                task_info = sub_agent_mgr._finished_tasks.get(task_id, {})
                if task_info.get("error"):
                    # 有明确错误（agent 缺失 / mode 不允许 / staled / timeout）— 不清空
                    logger.info(
                        f"[DEBUG-cc] early-return: finished_tasks error (task={task_id[:8]}, "
                        f"error={str(task_info.get('error'))[:80]})"
                    )
                    return
                # 无错误：可能是竞态条件，继续尝试清空
            else:
                # 无任何记录 — 任务从未启动 / 已被丢弃，不清空
                logger.info(
                    f"[DEBUG-cc] early-return: no record (task={task_id[:8]}, "
                    f"running={sub_agent_mgr is not None and task_id in sub_agent_mgr._running_tasks}, "
                    f"finished_keys={list(sub_agent_mgr._finished_tasks.keys())[-5:] if sub_agent_mgr else None})"
                )
                return
        else:
            execution_error = getattr(executor, "_execution_error", None)
            if execution_error:
                logger.info(
                    f"[DEBUG-cc] early-return: execution_error (task={task_id[:8]}, "
                    f"error={str(execution_error)[:80]})"
                )
                return

        # 按 ID 找到触发压缩时的那个 session（不依赖 session_manager.get_current_session，
        # 避免用户在子智能体执行期间切换会话后误清空当前会话）
        target_session = None
        for s in self.session_manager.sessions:
            if s.session_id == session_id:
                target_session = s
                break
        if not target_session or not target_session.messages:
            logger.info(
                f"[DEBUG-cc] early-return: session missing/empty (task={task_id[:8]}, sid={session_id[:8]}, "
                f"target={target_session is not None}, msg_len={len(target_session.messages) if target_session else -1}, "
                f"sessions={len(self.session_manager.sessions)})"
            )
            return
        logger.info(
            f"[DEBUG-cc] 即将清空 (task={task_id[:8]}, sid={session_id[:8]}, "
            f"msg_len={len(target_session.messages)}, result_len={len(result) if result else 0})"
        )

        # 清空目标 session 的 messages（保留 session 自身，topic_summary / name 不变）
        # 用 set_messages 而非 session.clear()，避免把 topic_summary 重置为 ""
        target_session.set_messages([], preserve_compaction=False)
        # 🛡️ 压缩守卫：清空后旧 worker 的 finished_with_messages（取消时 emit 的
        # 旧消息快照）可能延迟到达主线程，_on_messages_updated 全量覆写会恢复
        # 已清空的会话 → 压缩失效 + 反复触发。守卫拦截"消息数远多于当前 session"
        # 的旧快照，新 worker（压缩后启动）消息数≈session 消息数，不受影响。
        self._post_compact_guard = True
        # 弹出卡片缓存，避免对已 deleteLater 的 widget 残留引用
        self._session_card_cache.pop(target_session.session_id, None)

        # 触发 SessionStart hook — state="clear"
        # 标记 _pending_session_hook 避免 _session_switched 残留时误拦截
        try:
            self._pending_session_hook = True
            self.backend.trigger_session_event("clear")
        except Exception:
            pass
        finally:
            self._pending_session_hook = False

        # 仅在用户仍停留在原 session 时才同步更新 UI
        if self._current_session_id == session_id:
            self._display_current_session()

        # 将压缩摘要存入 compaction_cache，供后续对话提供上下文
        if target_session and result:
            target_session.set_compaction_cache(
                {
                    "active": True,
                    "kind": "auto_compact",
                    "summary_message": {"role": "system", "content": str(result)},
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source_message_count": len(target_session.messages),
                }
            )

    def _on_compact_finished(self, task_id: str, result: str, session_id: str):
        """普通 compact 完成后触发 SessionStart state='compact'

        Args:
            task_id: 子智能体任务 ID
            result: 子智能体返回的摘要内容
            session_id: 触发压缩时锁定的会话 ID
        """
        # 重置自动压缩进度标志（与 _on_compact_clear_finished 一致）
        self._auto_compact_in_progress = False

        if getattr(self, "_is_destroyed", False):
            return
        # 校验执行结果：仅在无错误时触发
        sub_agent_mgr = self.backend.sub_agent_manager
        executor = sub_agent_mgr._running_tasks.get(task_id) if sub_agent_mgr else None
        if executor is None:
            return
        execution_error = getattr(executor, "_execution_error", None)
        if execution_error:
            return
        try:
            self._pending_session_hook = True
            self.backend.trigger_session_event("compact")
        except Exception:
            pass
            # 刷新上下文使用率指示器（清空后会回到 0%）
            self._refresh_context_usage_indicator()
        finally:
            self._pending_session_hook = False

    def _remember_to_memory(self, content: str):
        """将内容存入长期记忆"""
        content = content.strip()
        if not content:
            InfoBar.warning(
                "记忆为空",
                "请在 /remember 后输入要记忆的内容",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            return

        from app.core.memory_manager import MemoryManagerCore

        mm = MemoryManagerCore.get_instance()
        success = mm.add_entry_memory(content, source="manual")
        if success:
            InfoBar.success(
                "已记忆",
                f'"{content[:30]}{"..." if len(content) > 30 else ""}" 已存入长期记忆',
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
        else:
            InfoBar.error(
                "记忆失败",
                "无法保存到长期记忆",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )

    # ========== 命令卡片处理 ==========

    def _on_slash_triggered(self, query: str):
        """输入框 / 触发 - 更新命令卡片并显示"""
        if not hasattr(self, "_command_card"):
            return
        # 先让 CardManager 处理互斥和容器展开（仅首次生效，后续因卡片已可见而跳过）
        self._card_manager.show_card("command", self._window_id)
        # 再以正确 query 加载数据（CardManager 内部 show_card 会重置 query 为空）
        self._command_card.show_card(query)
        if self._command_card.filtered_count > 0:
            # 把焦点还给输入框（卡片不抢焦点）
            self.input_area.setFocus(Qt.OtherFocusReason)

    def _on_slash_dismissed(self):
        """输入框 / 触发结束 - 隐藏命令卡片"""
        if not hasattr(self, "_command_card"):
            return
        # 直接调用卡片的 dismiss 方法确保关闭，同时通过 CardManager 通知容器
        self._command_card.dismiss()
        self._card_manager.hide_card("command", self._window_id)

    def _on_slash_show_hint(self, cmd_name: str, selected_type: str = ""):
        """输入框 完整命令 + 空格 - 显示参数提示

        将命令卡片切换到 detail 模式，显示该命令的参数提示信息。
        如果卡片尚未显示，先让 CardManager 展开容器。

        Args:
            cmd_name: 命令名（不含 /）
            selected_type: 选中项的 display_type（"command"/"prompt"/"agent"），
                          用于 detail 模式显示对应类型的 hint
        """
        if not hasattr(self, "_command_card"):
            return
        card = self._command_card
        # 如果卡片还没显示（首次进入 detail 模式），展开容器
        if not card.is_card_visible:
            self._card_manager.show_card("command", self._window_id)
        # 构建数据源（供 detail 模式参数列表使用）
        data_provider = {
            "model_options": self._get_all_model_options_flat(),
            "agent_options": self._get_subagent_names(),
            "template_options": self._get_team_template_names(),
            "plugin_options": self._get_plugin_names(),
        }
        # 提取当前输入文本和光标位置，让 show_command_detail 重建 widgets
        # 后能立即应用 active 状态（修复失焦→重新聚焦时 active 状态丢失）
        full_text = self.input_area.toPlainText()
        cursor_pos = self.input_area.textCursor().position()
        # 切换到 detail 模式（按选中类型显示对应 hint）
        card.show_command_detail(
            cmd_name,
            selected_type,
            data_provider=data_provider,
            full_text=full_text,
            cursor_pos=cursor_pos,
        )
        # 把焦点还给输入框
        self.input_area.setFocus(Qt.OtherFocusReason)

    # ========== 文件提及卡片处理 ==========

    def _get_current_workdir(self) -> str:
        """获取当前工作目录（项目根目录）"""
        # 优先从实例缓存获取
        project = getattr(self, "_current_project", "")
        if project and hasattr(self, "_current_workdir") and self._current_workdir:
            if project in self._current_workdir:
                return self._current_workdir[project]
        # 降级：尝试从 tool_executor 获取
        try:
            workdir = self.backend.tool_executor.get_workdir()
            if workdir:
                return workdir
        except Exception:
            pass
        return ""

    def _on_at_triggered(self, query: str):
        """输入框 @ 触发 - 更新文件提及卡片并显示

        性能保证：缓存已在 ensure_cache 中预填充，
        show_card 仅做 O(n) 内存过滤，无 I/O。
        """
        if not hasattr(self, "_file_mention_card"):
            return

        workdir = self._get_current_workdir()
        if not workdir:
            return

        card = self._file_mention_card
        # ⚠️ 先加载卡片内容并设置正确高度，再让 CardManager 展开容器
        # 若顺序反转（先 CardManager 后 show_card），_do_expand 会在卡片
        # fixedHeight 尚未设置时读取 QScrollArea 默认 sizeHint（≈72px=2 item），
        # 导致容器动画到错误高度且因动画钳制 maximumHeight 不触发 Resize 修正。
        card.show_card(workdir, query)
        self._card_manager.show_card("file_mention", self._window_id)
        # 把焦点还给输入框
        self.input_area.setFocus(Qt.OtherFocusReason)

    def _ensure_file_mention_cache(self):
        """延迟预缓存文件列表（UI 初始化完成后调用）"""
        if not hasattr(self, "_file_mention_card"):
            return
        workdir = self._get_current_workdir()
        if workdir:
            self._file_mention_card.ensure_cache(workdir)

    def _on_at_dismissed(self):
        """输入框 @ 触发结束 - 隐藏文件提及卡片"""
        if not hasattr(self, "_file_mention_card"):
            return
        self._file_mention_card.dismiss()
        self._card_manager.hide_card("file_mention", self._window_id)

    def _on_file_mention_selected(self, file_path: str):
        """文件被选中 - 添加为 attachment chip，移除 @ 文本"""
        if not hasattr(self, "_file_mention_card"):
            return
        # 关闭卡片
        self._file_mention_card.dismiss()
        self._card_manager.hide_card("file_mention", self._window_id)

        # 移除输入框中的 @query 文本
        self.input_area.insert_file_mention(file_path)

        # 添加到附件列表（复用拖拽文件的 chip 机制）
        if file_path not in self._attachments:
            self._attachments.append(file_path)
            from app.widgets.bottom_input_area import AttachmentChip

            chip = AttachmentChip(file_path, self._attach_container)
            chip.removed.connect(lambda path=file_path: self._remove_attachment(path))
            self._attach_layout.insertWidget(self._attach_layout.count() - 1, chip)
        self._attach_container.setVisible(bool(self._attachments))

        # 聚焦输入框
        self.input_area.setFocus(Qt.OtherFocusReason)

    def _get_all_model_options_flat(self) -> list:
        """平展所有服务商:模型名选项列表（带描述）

        条目格式：{"value": "display:model", "description": "..."}，
        描述来自 models.dev / 硬编码能力字典的 note 字段（无则空串）。
        供命令卡片枚举值模式显示当前模型描述。
        """
        from app.core.model_capabilities import get_model_capabilities

        options = []
        for config_id, config in self._valid_configs.items():
            # 用 display_name 给用户看，避免 UUID 出现
            display = config.get("display_name", config.get("provider_name", config_id))
            models = self._get_model_list_for_provider(config_id)
            for model in models:
                caps = get_model_capabilities(model)
                note = (caps.get("note", "") or "").strip()
                options.append({"value": f"{display}:{model}", "description": note})
        return sorted(options, key=lambda x: x["value"])

    def _get_subagent_names(self) -> list:
        """获取所有可作为子智能体的 agent 选项列表（带描述）

        条目格式：{"value": name, "description": "..."}（无描述则空串）。
        供命令卡片枚举值模式显示当前 agent 描述。
        """
        if not self.backend or not self.backend.agent_manager:
            return []
        all_agents = self.backend.agent_manager.list_agents(include_hidden=False)
        items = [
            {"value": a.name, "description": (a.description or "").strip()}
            for a in all_agents
            if a.mode in ("subagent", "all")
        ]
        return sorted(items, key=lambda x: x["value"])

    def _get_team_template_names(self) -> list:
        """获取所有已保存的团队模板选项列表（带描述）

        条目格式：{"value": name, "description": "..."}（无描述则空串）。
        供命令卡片枚举值模式显示当前模板描述。
        """
        try:
            from app.core.team.template_manager import TemplateManager

            templates = TemplateManager.get_instance().list_templates()
            items = [{"value": t["name"], "description": (t.get("description") or "").strip()} for t in templates]
            return sorted(items, key=lambda x: x["value"])
        except Exception:
            return []

    def _get_plugin_names(self) -> list:
        """获取所有已发现的插件选项列表（带描述）

        条目格式：{"value": name, "description": "..."}（无描述则空串）。
        供命令卡片枚举值模式显示当前插件描述。
        """
        try:
            from app.core.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                return []
            plugins = pm.list_plugins()
            items = [{"value": p.name, "description": (p.description or "").strip()} for p in plugins if p.name]
            return sorted(items, key=lambda x: x["value"])
        except Exception:
            return []

    def _toggle_model_selector_card(self):
        """切换模型选择卡片的显示"""
        self._ensure_model_selector_card()  # P0-1：框架懒创建，确保注册/入容器
        self._card_manager.toggle_card("model_selector", self._window_id)
        if self._card_manager.is_card_visible("model_selector", self._window_id):
            self._load_model_selector_to_card()
            # 确保顶层窗口从最小化恢复并激活
            top_window = self.window()
            if top_window:
                if top_window.isMinimized():
                    top_window.showNormal()
                top_window.activateWindow()
                top_window.raise_()

    def _load_model_selector_to_card(self):
        """加载模型数据到模型选择卡片"""
        # 兜底：延迟构建未完成（800ms 未到 / P2 懒加载 pending / 构建失败）时
        # 立即构建内容，避免 _model_selector_card_content 为 None 崩溃
        self._ensure_model_selector_card_content()
        provider_models_data = []
        merged_provider_models = get_merged_provider_models()
        # 维护 display_name → config_id 映射，用于 model_selector 回调时反查
        self._display_to_config_id: dict[str, str] = {}
        # 维护 display_name → provider_name 映射，用于 model_selector 找 icon
        self._display_to_provider_name: dict[str, str] = {}
        for config_id, config in self._valid_configs.items():
            display_name = config.get("display_name", config.get("provider_name", config_id))
            pname = config.get("provider_name", config_id)
            self._display_to_config_id[display_name] = config_id
            self._display_to_provider_name[display_name] = pname
            model_list = []
            if "模型列表" in config:
                saved_models = config["模型列表"]
                if isinstance(saved_models, str):
                    try:
                        import ast

                        saved_models = ast.literal_eval(saved_models)
                    except Exception:
                        saved_models = []
                if isinstance(saved_models, list) and saved_models:
                    model_list = list(saved_models)
                # 模型列表存在但为空（[]）→ 回退到 merged_provider_models，
                # 避免刚注入默认配置时模型选择器一片空白
                if not model_list and pname in merged_provider_models:
                    model_list = list(merged_provider_models[pname])
            elif pname in merged_provider_models:
                model_list = list(merged_provider_models[pname])
            cur_model = config.get("模型名称", "")
            if cur_model and cur_model not in model_list:
                model_list.insert(0, cur_model)
            if not model_list and cur_model:
                model_list = [cur_model]
            is_current = config_id == self._current_provider_name
            # 传给 model_selector 用 display_name（用户看到的名），不要传 config_id
            provider_models_data.append((display_name, model_list, is_current))

        # 找当前选中服务商的 display_name
        current_display = ""
        if self._current_provider_name in self._valid_configs:
            current_display = self._valid_configs[self._current_provider_name].get(
                "display_name", self._current_provider_name
            )

        # 收集模型描述（来自 models.dev / 硬编码能力字典）
        model_notes = {}
        for _, models, _ in provider_models_data:
            for model_name in models:
                if model_name not in model_notes:
                    caps = get_model_capabilities(model_name)
                    note = (caps.get("note", "") or "").strip()
                    model_notes[model_name] = note

        self._model_selector_card_content.set_providers_data(
            provider_models_data,
            current_display,
            self._current_model_name or "",
            self._display_to_provider_name,
            model_notes=model_notes,
        )

        # 更新卡片头部：有服务商时显示服务商图标 + 模型名称，否则显示默认"模型选择"
        self._update_model_selector_header()

    # ──────────────────────────────────────────────
    # models.dev 动态数据后台预热
    # ──────────────────────────────────────────────
    def _start_models_dev_sync(self):
        """后台预热 models.dev 动态模型数据（内存缓存未填充时）。

        主线程只读内存/文件缓存（毫秒级），零阻塞；仅当本进程尚未加载过
        动态数据且本地缓存不可用时，才经 refresh_dynamic_models_async 在
        后台线程发起网络拉取（30s + 15s 超时全部落在后台），完成后经
        _models_dev_ready 信号回主线程刷新 UI。
        """
        from app.core.models_dev_sync import get_dynamic_models, refresh_dynamic_models_async

        # 已加载过动态数据（多窗口/多标签复用模块级内存缓存）→ 无需重复刷新
        cached = get_dynamic_models()
        if cached.provider_models or cached.model_capabilities:
            return

        # 后台线程拉取；模块级单飞去重保证多窗口只发一路网络请求
        refresh_dynamic_models_async(on_done=lambda _r: self._models_dev_ready.emit(_r))

    @pyqtSlot(object)
    def _on_models_dev_ready(self, _result):
        """models.dev 动态数据后台刷新完成（主线程）：刷新依赖动态数据的 UI。"""
        if getattr(self, "_is_destroyed", False):
            return
        # 模型选择按钮 tooltip（价格/多模态/思考开关）依赖动态能力数据
        self._update_model_selector_btn()
        # 模型选择卡片可见则刷新内容（模型列表可能新增动态模型）
        if hasattr(self, "_card_manager") and self._card_manager.is_card_visible("model_selector", self._window_id):
            self._load_model_selector_to_card()
        # 设置弹窗可见则刷新服务商卡片（模型下拉可能新增动态模型）
        if (
            hasattr(self, "_card_manager")
            and self._card_manager.is_card_visible("settings", self._window_id)
            and hasattr(self, "_settings_popup")
            and hasattr(self._settings_popup, "llmProviderCard")
        ):
            self._settings_popup.llmProviderCard._refresh_items()

    # ──────────────────────────────────────────────
    # OpenCode Zen 免费模型异步刷新
    # ──────────────────────────────────────────────
    def _async_refresh_opencode_models(self):
        """后台线程异步刷新内置默认 OpenCode 免费服务商（name="opencode免费模型"）的模型列表。

        只刷新内置默认，不碰用户自己添加的 OpenCode 实例，避免覆盖用户自定义模型列表。
        启动后立即返回，不阻塞 UI。
        网络与解析逻辑在 app.core.models_dev_sync 的
        fetch_opencode_free_models_for_providers，本方法只负责收集实例、
        调度线程、把结果经信号回主线程刷新 UI。
        """
        import threading

        from app.core.models_dev_sync import fetch_opencode_free_models_for_providers

        # 只刷新内置默认的 OpenCode 免费服务商（name="opencode免费模型"），
        # 不动用户自己添加的 OpenCode 实例，防止覆盖用户自定义模型列表。
        targets: list[tuple[str, str, str]] = []
        for config_id, config in self._valid_configs.items():
            if config.get("name") != "opencode免费模型":
                continue
            api_url = config.get("API_URL", "")
            api_key = config.get("API_KEY", "")
            if api_url:
                targets.append((config_id, api_url, api_key))

        if not targets:
            return

        def _do_fetch():
            """后台线程执行：批量拉取各实例免费模型，逐个回传主线程"""
            results = fetch_opencode_free_models_for_providers(targets)
            for config_id, free_models in results.items():
                self._opencode_models_ready.emit((config_id, free_models))

        threading.Thread(target=_do_fetch, daemon=True).start()

    @pyqtSlot(object)
    def _on_opencode_models_ready(self, result: tuple):
        """主线程处理 OpenCode Zen 免费模型异步刷新结果。"""
        config_id, free_models = result

        if config_id not in self._valid_configs:
            return

        # 更新持久化配置
        saved = self.cfg.llm_saved_providers.value
        if isinstance(saved, dict) and config_id in saved:
            saved[config_id]["模型列表"] = free_models
            self.cfg.llm_saved_providers.value = saved
            self.cfg.save()

        # 更新本地缓存
        self._valid_configs[config_id]["模型列表"] = free_models

        # 如果当前模型选择器已打开，刷新显示
        if hasattr(self, "_card_manager") and self._card_manager.is_card_visible("model_selector", self._window_id):
            self._load_model_selector_to_card()

        # 更新模型选择按钮标签
        self._update_model_selector_btn()

    def _update_model_selector_header(self):
        """根据当前服务商/模型状态，更新模型选择卡片的头部（图标 + 初始标题）

        标题用 display_name（人类可读），图标按 provider_name 查找。
        """
        if self._current_provider_name and self._current_provider_name in self._valid_configs:
            config = self._valid_configs[self._current_provider_name]
            display = config.get("display_name", self._current_provider_name)
            pname = config.get("provider_name", display)
            # 用 provider_name 找 icon（PROVIDER_ICONS 按服务商名索引，不是 display_name）
            icon_widget = ProviderIconWidget(pname, 20)
            self._model_selector_card.set_icon_widget(icon_widget)
            self._model_selector_card.set_title_text(display)
        else:
            # 无服务商：显示默认图标 + "模型选择"
            self._model_selector_card.set_icon("🤖")
            self._model_selector_card.set_title_text("模型选择")

    def _on_sticky_provider_changed(self, provider_name: str):
        """滚动时吸顶服务商变化，更新标题栏显示当前服务商名和图标

        provider_name 是从 model_selector 传来的 display_name（不是 config_id）。
        """
        if provider_name:
            self._model_selector_card.set_title_text(provider_name)
            # 用 display_name → config_id → provider_name 链反查，找图标
            config_id = getattr(self, "_display_to_config_id", {}).get(provider_name)
            pname = provider_name
            if config_id and config_id in self._valid_configs:
                pname = self._valid_configs[config_id].get("provider_name", provider_name)
            icon_widget = ProviderIconWidget(pname, 20)
            self._model_selector_card.set_icon_widget(icon_widget)
        elif self._current_provider_name and self._current_provider_name in self._valid_configs:
            # 滚到顶部时恢复显示当前选中的服务商
            config = self._valid_configs[self._current_provider_name]
            display = config.get("display_name", self._current_provider_name)
            pname = config.get("provider_name", display)
            self._model_selector_card.set_title_text(display)
            icon_widget = ProviderIconWidget(pname, 20)
            self._model_selector_card.set_icon_widget(icon_widget)

    def _on_add_provider_from_card(self):
        """从模型选择卡片点击「添加」按钮 - 显示添加服务商卡片"""
        self._card_manager.hide_card("model_selector", self._window_id)
        self._show_provider_add_card()

    def _on_configure_providers_from_card(self):
        """从模型选择卡片点击「配置」按钮 - 直接打开当前服务商配置卡片

        优先：若已有当前选中的服务商，直接进入其编辑卡片（一跳到位）。
        回退：未选中服务商时走原路径（打开设置卡片并展开服务商列表）。
        """
        # 隐藏模型选择卡片
        self._card_manager.hide_card("model_selector", self._window_id)

        # 当前选中的服务商
        current_provider = getattr(self, "_current_provider_name", "")
        if current_provider and current_provider in self._valid_configs:
            # 直接打开当前服务商配置卡片（一跳到位）
            self._show_provider_edit_card(current_provider, self._valid_configs[current_provider])
            return

        # 回退：未选中服务商时显示设置卡片并展开服务商下拉
        # 显示设置卡片（通过 CardManager 保证生命周期一致性，触发容器展开）
        self._card_manager.show_card("settings", self._window_id)
        # 滚动设置卡片内容到顶部
        QTimer.singleShot(100, self._scroll_settings_to_top)
        # 展开服务商下拉
        QTimer.singleShot(200, lambda: self._expand_provider_list_card())

    def _scroll_settings_to_top(self):
        """滚动设置卡片内容到顶部"""
        try:
            # 找到 LLMSettingsCard 内部的 QScrollArea 并滚到顶
            scroll_areas = self._settings_popup.findChildren(QScrollArea)
            if scroll_areas:
                scroll_areas[0].verticalScrollBar().setValue(0)
        except Exception:
            pass

    def _expand_provider_list_card(self):
        """展开服务商列表卡片"""
        try:
            if hasattr(self._settings_popup, "llmProviderCard"):
                self._settings_popup.llmProviderCard.toggleExpand()
        except Exception:
            pass

    def _on_model_selected_from_popup(self, provider_name: str, model_name: str):
        """从弹窗/卡片选中模型后切换

        provider_name 可能是 display_name（如 "OpenCode Zen #2"）或 config_id，
        统一转回 config_id 后再处理。

        多窗口隔离：全局配置保存最后使用的服务高（作为新窗口默认值），
        但窗口实例的 _current_provider_name/_current_model_name 不受其他窗口影响。
        """
        # 关键修复：先转 config_id，避免后续 _valid_configs[display_name] 创建新条目
        display_to_config = getattr(self, "_display_to_config_id", {})
        config_id = display_to_config.get(provider_name, provider_name)
        # #4 语义：用户在本窗口手动选过模型 → 置位，后续 gitee 同步不再覆盖本窗口选择
        self._user_manually_selected_model = True
        self._current_provider_name = config_id
        self._current_model_name = model_name
        # 保存到全局配置（作为新窗口的默认值，不影响当前窗口实例）
        self.cfg.set(self.cfg.llm_selected_model, config_id, save=True)

        # 更新 saved_providers 中的模型名称
        # 注意：必须用 deepcopy！ConfigItem.value 返回内部 dict 引用，原地修改后 set 回同一对象不会触发 valueChanged 信号
        saved_providers = copy.deepcopy(self.cfg.llm_saved_providers.value) or {}
        # 优先用 _display_to_config_id 映射找 config_id；
        # 找不到时回退到遍历查找（兼容旧代码路径）
        if config_id not in saved_providers:
            config_id = None
            for cid, info in saved_providers.items():
                if cid == provider_name or info.get("provider_name") == provider_name:
                    config_id = cid
                    break
        if config_id and config_id in saved_providers:
            saved_providers[config_id]["模型名称"] = model_name
            self.cfg.set(self.cfg.llm_saved_providers, saved_providers, save=True)
            # 更新 _valid_configs 确保 ChatEngine 能读到最新配置
            self._valid_configs[config_id] = saved_providers.get(config_id, {}).copy()
            self._valid_configs[config_id]["模型名称"] = model_name
            # ★ 用量聚合（T6）：配置快照变更后失效用量/余额缓存（幂等）
            try:
                from app.core.usage_service import UsageService

                UsageService.get_instance().invalidate(config_id)
            except Exception:
                pass

        # 重新加载模型配置（_load_model_configs 已修复：保持窗口自身选择优先）
        self._load_model_configs()

        self._update_model_selector_btn()
        self._refresh_context_usage_indicator()
        self._update_balance_display()

        # 隐藏模型选择卡片（如果已打开）
        if hasattr(self, "_card_manager"):
            self._card_manager.hide_card("model_selector", self._window_id)

    def _on_footer_model_label_clicked(self, model_name: str, config_id: str = ""):
        """用户点击消息卡片页脚的模型标签 — 按 UUID 精确切换到对应服务商和模型"""
        if not config_id or config_id not in self._valid_configs:
            InfoBar.warning(
                "未找到服务商",
                f"未找到服务商「{model_name}」，可能已被移除",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            return
        self._on_model_selected_from_popup(config_id, model_name)

    def _get_model_btn_text_style(self) -> str:
        """动态构建模型按钮文字样式（运行时重新计算 font_size_css）"""
        Colors.refresh()
        return f"color: {Colors.TEXT_PRIMARY}; {font_size_css(13)} font-weight: bold; background: transparent;"

    def _update_model_selector_btn(self):
        """更新模型选择按钮的图标和文字显示"""
        if not hasattr(self, "current_model_btn"):
            return
        # 当前服务商的显示名 + provider_name（用于找 icon）
        display = self._current_provider_name
        pname = self._current_provider_name
        if self._current_provider_name in self._valid_configs:
            config = self._valid_configs[self._current_provider_name]
            display = config.get("display_name", self._current_provider_name)
            pname = config.get("provider_name", display)
        # 设置图标（按 provider_name 查 PROVIDER_ICONS，不按 UUID 查）
        icon = None
        if pname:
            icon_name = PROVIDER_ICONS.get(pname, "")
            if icon_name:
                icon = get_icon(icon_name)

        if icon and not icon.isNull():
            pm = icon.pixmap(15, 15)
            if pm.width() != pm.height():
                pm = pm.scaled(15, 15, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._model_btn_icon.setPixmap(pm)
        else:
            self._model_btn_icon.clear()

        # 设置文字（用 display_name 给用户看，避免 UUID 显示）
        if self._current_provider_name and self._current_model_name:
            self._model_btn_text.setText(self._current_model_name)
            caps = get_model_capabilities(self._current_model_name)
            note = caps.get("note", "")
            # 第一行：服务商 · 模型 · 价格(单位在末尾一次) · 多模态/思考
            cost_parts = []
            cost = caps.get("cost") or {}
            for key, label in (("input", "in"), ("output", "out"), ("cache_read", "cache")):
                v = cost.get(key)
                if v is not None:
                    cost_parts.append(f"{label}:{_format_cost_number(v)}")
            extra_parts = []
            if cost_parts:
                extra_parts.append(" ".join(cost_parts) + " $/M")
            if caps.get("supports_vision"):
                extra_parts.append("多模态")
            if caps.get("supports_thinking"):
                extra_parts.append("开关思考")
            tooltip = f"{display} · {self._current_model_name}"
            if extra_parts:
                tooltip += " · " + " ".join(extra_parts)
            if note:
                tooltip += f"\n{note}"
            self.current_model_btn.setToolTip(tooltip)
        elif self._current_provider_name:
            self._model_btn_text.setText(display)
            self.current_model_btn.setToolTip(display)
        else:
            self._model_btn_text.setText("选择模型...")
            self.current_model_btn.setToolTip("")

        self._update_balance_display()
        self._update_settings_effort_btn()

    def _get_settings_effort_style(self) -> str:
        """思考强度胶囊样式（主题感知：背景/文字色随主题刷新）"""
        Colors.refresh()
        return f"""
            QLabel {{
                background: rgba(90, 169, 255, 0.10);
                color: {Colors.RING_NORMAL};
                border: 1px solid rgba(90, 169, 255, 0.30);
                border-radius: 9px;
                padding: 1px 7px;
                {font_size_css(10)}
                font-weight: 600;
                {get_font_family_css()}
            }}
        """

    def _update_settings_effort_btn(self):
        """参数配置按钮显示当前思考强度（模型支持 reasoning_effort 且思考开启时）。

        显示条件：模型能力 thinking_param == "reasoning_effort"（可调强度等级）、
        当前配置里有"思考等级"值、且思考模式为开启。
        开关型思考（thinking）/ 无等级模型 / 思考模式关闭 → 只显图标。
        """
        if not hasattr(self, "_settings_effort_label") or not hasattr(self, "settings_btn"):
            return
        text = ""
        tooltip = "模型参数配置"
        try:
            if self._current_model_name:
                caps = get_model_capabilities(self._current_model_name)
                if caps.get("supports_thinking") and caps.get("thinking_param") == "reasoning_effort":
                    config = self._get_current_model_config()
                    effort = config.get("思考等级", "")
                    thinking_mode = config.get("思考模式", True)
                    # 思考模式关闭 → reasoning_effort 不会随请求发送，不显示强度
                    if effort and self._is_thinking_enabled(thinking_mode):
                        text = str(effort)
                        tooltip = f"模型参数配置 · 思考强度: {effort}"
        finally:
            self._settings_effort_label.setText(text)
            self._settings_effort_label.setVisible(bool(text))
            self.settings_btn.setToolTip(tooltip)

    @staticmethod
    def _is_thinking_enabled(value) -> bool:
        """思考模式开关是否开启（兼容 bool 与字符串存储形态）"""
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() not in ("", "0", "false", "off", "no", "disabled")

    def _on_context_selection_changed(self, _selected_keys=None):
        self._refresh_context_usage_indicator()

    def _refresh_context_usage_indicator(self):
        """刷新上下文使用环。

        性能优化：加入 500ms 节流。
        get_context_usage_snapshot → build_messages 会触发 anyio.run(to_thread.run_sync, compactor.compact)，
        在主线程上阻塞 UI 事件循环。频繁调用（如每次工具执行后）会造成累积卡顿。
        """
        import time

        now = time.monotonic()
        last = getattr(self, "_last_context_refresh_time", 0.0)
        if now - last < 0.5:
            return

        ring = getattr(self, "context_usage_ring", None)
        if not ring:
            return

        # 注：历史上这里在 _is_streaming 时直接 return，以避免 build_messages →
        # compactor.compact 阻塞主线程。但 engine.get_context_usage_snapshot 现在走
        # 快速路径（仅用 session.system_prompt + session.messages + 缓存的 tools 估算，
        # 不触发 build_messages/compact），不再阻塞；故移除该守卫。0.5s 节流已足以
        # 限制刷新频率，使流式(工具迭代)期间也能实时补全各类型上下文占比 breakdown。
        session = self.session_manager.get_current_session()
        llm_config = self._get_current_model_config()
        api_prompt_tokens = int(getattr(session, "last_api_prompt_tokens", 0) or 0)
        api_message_count = int(getattr(session, "last_api_message_count", 0) or 0)
        from_api = bool(getattr(session, "last_api_prompt_from_usage", False))
        snapshot = self.backend.get_context_usage_snapshot(
            session,
            llm_config,
            api_prompt_tokens=api_prompt_tokens,
            api_message_count=api_message_count,
            from_api=from_api,
        )
        ring.set_usage(
            snapshot.get("percent", 0),
            snapshot.get("used_tokens", 0),
            snapshot.get("budget_tokens", 0),
            snapshot.get("compaction", {}),
            snapshot.get("normal_tokens", 0),
            snapshot.get("compacted_tokens", 0),
            breakdown=snapshot.get("breakdown", []),
            pruned_tokens=snapshot.get("pruned_tokens", 0),
        )
        # 防闪：流式期间 session.messages 可能尚未包含本轮新增消息（陈旧），快照
        # used_tokens 会远小于 worker 实时 token_count，导致圆环/卡片闪现异常小的数值。
        # 以 worker 实时值（last_api_prompt_tokens）作为权威总量下限，避免闪现。
        used_for_display = snapshot.get("used_tokens", 0)
        if api_prompt_tokens and used_for_display < api_prompt_tokens:
            used_for_display = api_prompt_tokens
        # 卡片底部 token 显示与上下文圆环同步（同一 used_tokens）
        # 🆕 isdeleted 防线：_current_assistant_card 可能已被 deleteLater 排队销毁
        # （如 send 失败回滚 / 会话切换），C++ 对象已失效时访问 set_meta_info 会抛
        # RuntimeError: wrapped C/C++ object ... has been deleted。
        card = getattr(self, "_current_assistant_card", None)
        if card and not _is_sip_deleted(card) and used_for_display > 0:
            card.set_meta_info(token_usage={"total": used_for_display})
        self._last_context_refresh_time = now

    def _update_balance_display(self):
        """更新余额显示（用量聚合 T6：请求委托 UsageService 全局单例）"""
        balance_display = getattr(self, "balance_display", None)
        if not balance_display:
            return

        # 获取当前选中的服务商配置
        # _current_provider_name 在「单服务商多配置」改造后是 config_id（UUID），
        # 真实的服务商类型名在 config["provider_name"] 里。BalanceDisplay 按
        # provider_name 做白名单判断，所以这里必须用 config["provider_name"]。
        config_id = getattr(self, "_current_provider_name", "")
        if not config_id:
            balance_display.clear()
            return

        config = self._valid_configs.get(config_id, {})
        api_key = config.get("API_KEY", "")
        provider_name = config.get("provider_name", "")

        balance_display.set_provider(provider_name, config_id)

        # 委托 UsageService：缓存命中直接广播，未命中单例后台抓取（全局 1 路）
        from app.core.usage_service import UsageService

        UsageService.get_instance().request_balance(provider_name, config_id, config)

        # 同时刷新套餐用量显示
        self._refresh_coding_plan()

    def _refresh_coding_plan(self):
        """刷新套餐用量同心圆（5小时/每周/每月）— 用量聚合 T6。

        请求委托进程级单例 UsageService：全局缓存命中直接广播，未命中
        单例后台线程抓取后写缓存再广播；active key 由单例 QTimer 统一轮询
        （60s），N tab × 同 provider 只发 1 路请求。未注册该功能的服务商
        由服务 emit None，此处隐藏圆环。
        """
        ring = getattr(self, "coding_plan_ring", None)
        if not ring:
            return

        config_id = getattr(self, "_current_provider_name", "")
        if not config_id:
            ring.clear()
            self._coding_plan_hidden = True
            return

        config = self._valid_configs.get(config_id, {})
        provider_name = config.get("provider_name", "")

        if not provider_name:
            ring.clear()
            self._coding_plan_hidden = True
            return

        from app.core.usage_service import UsageService

        UsageService.get_instance().request_coding_plan(provider_name, config_id, config)

    def _on_coding_plan_result(self, provider_name: str, config_id: str, result):
        """主线程接收套餐用量查询结果（UsageService 广播，全窗口共享）。

        竞态防护：结果可能属于切换前的 config_id，直接丢弃。
        """
        if getattr(self, "_is_destroyed", False):
            return
        # 防竞态：用户已切换服务商，丢弃过期结果
        if config_id != getattr(self, "_current_provider_name", ""):
            return

        ring = getattr(self, "coding_plan_ring", None)
        if not ring:
            return

        # 防刷屏：无数据广播（无 fetcher 的 provider 每次 request 同步 emit None）
        # 会打到所有窗口。_coding_plan_hidden 记录当前隐藏状态（__init__ 初始化），
        # 仅当圆环由显示转为隐藏时才打日志，已隐藏则静默返回。
        if not result:
            if self._coding_plan_hidden:
                return
            logger.debug("[CodingPlan] 无数据，隐藏圆环")
            ring.clear()
            self._coding_plan_hidden = True
            return
        rolling = result.get("rolling")
        weekly = result.get("weekly")
        monthly = result.get("monthly")
        if not rolling and not weekly and not monthly:
            if self._coding_plan_hidden:
                return
            logger.debug("[CodingPlan] 三层均为空，隐藏圆环")
            ring.clear()
            self._coding_plan_hidden = True
            return
        logger.info(f"[CodingPlan] 收到数据: rolling={rolling}, weekly={weekly}, monthly={monthly}")
        ring.set_usage(
            rolling=rolling,
            weekly=weekly,
            monthly=monthly,
        )
        self._coding_plan_hidden = False
        # 轮询由 UsageService 单例统一驱动（60s 周期），窗口不再自建定时器

    def _open_settings_popup(self):
        """打开设置卡片（委托全局卡片控制器）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc.open_settings()

    def _check_gitee_sync_reminder(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc.check_gitee_sync_reminder()

    def _on_gitee_sync_done(self, success: bool, message: str):
        """Gitee 同步完成：token 真失效（含"已失效"）时延迟弹出「重新绑定」提醒

        仅 ConfigSyncService.syncDone(False, "…已失效…") 触发；网络异常
        （"刷新失败（网络异常）"）等其他失败不算失效，不弹。
        延迟 5s 确保设置弹窗已构建（与 _check_gitee_sync_reminder 同一节奏）。
        """
        if success:
            return
        if "已失效" not in message:
            return  # 网络异常/未授权等其他失败不触发失效提醒
        QTimer.singleShot(5000, lambda: self._safe_timer_call(self._check_gitee_token_invalid_reminder))

    def _check_gitee_token_invalid_reminder(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc.check_gitee_token_invalid_reminder()

    def _open_gitee_bind_from_reminder(self, infobar):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc.open_gitee_bind_from_reminder(infobar)

    def _dismiss_gitee_reminder(self, infobar):
        """提醒中点击「不再提醒」：持久化设置并关闭"""
        self.cfg.set(self.cfg.gitee_sync_remind, False, save=True)
        infobar.close()

    def _on_provider_edit_saved(self, provider_name: str, provider_info: dict, is_new: bool = False):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_provider_edit_saved(provider_name, provider_info, is_new)

    def _on_provider_edit_closed(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_provider_edit_closed()

    def _show_provider_add_card(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._show_provider_add_card()

    def _show_hook_add_card(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._show_hook_add_card()

    def _show_hook_edit_card(self, hook_id: str, hook_data: dict):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._show_hook_edit_card(hook_id, hook_data)

    def _on_hook_edit_saved(self, values: dict):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_hook_edit_saved(values)

    def _on_hook_edit_closed(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_hook_edit_closed()

    def _show_provider_edit_card(self, config_id: str, provider_info: dict):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._show_provider_edit_card(config_id, provider_info)

    def _show_mcp_add_card(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._show_mcp_add_card()

    def _show_mcp_edit_card(self, name: str, server_data: dict):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._show_mcp_edit_card(name, server_data)

    def _setup_mcp_edit_mode_buttons(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._setup_mcp_edit_mode_buttons()

    def _refresh_mcp_mode_buttons(self, is_json: bool):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._refresh_mcp_mode_buttons(is_json)

    def _try_toggle_to_form(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._try_toggle_to_form()

    def _try_toggle_to_json(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._try_toggle_to_json()

    def _on_mcp_edit_saved(self, server_data: dict):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_mcp_edit_saved(server_data)

    def _on_mcp_edit_closed(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_mcp_edit_closed()

    def _on_hook_edit_card_closed(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_hook_edit_card_closed()

    def _on_provider_edit_card_closed(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_provider_edit_card_closed()

    def _on_mcp_edit_card_closed(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_mcp_edit_card_closed()

    def _ensure_hook_edit_card(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._ensure_hook_edit_card()

    def _ensure_provider_edit_card(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._ensure_provider_edit_card()

    def _ensure_mcp_edit_card(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._ensure_mcp_edit_card()

    def _on_hook_toggled(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_hook_toggled()

    def _on_hook_toggled_light(self, hook_id: str, enabled: bool):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_hook_toggled_light(hook_id, enabled)

    def _on_mcp_servers_toggled(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_mcp_servers_toggled()

    def _on_gateway_toggled(self):
        """（委托全局卡片控制器 GlobalCardController）"""
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc._on_gateway_toggled()

    def _hide_main_popups(self):
        """隐藏主要的悬浮面板（互斥显示）

        包括：系统设置、模型配置、历史会话、记忆管理、AutoLoop
        现在也保存并隐藏 todo/tool/sub_agent 实时卡片
        """
        # 标记系统卡片打开状态，阻止实时卡片自行显示
        self._is_system_card_visible = True
        # 保存 todo 可见状态，用于系统卡片关闭后恢复
        self._todo_was_visible_before_system = self._todo_floating_widget.isVisible()
        # 通过 CardManager 隐藏所有卡片
        for card_id in [
            "todo",
            "tool",
            "sub_agent",
            "question",
            "model_config",
            "history",
            "memory",
            "auto_loop_config",
            "undo_delete",
        ]:
            self._card_manager.hide_card(card_id, self._window_id)
        if not self._is_auto_loop_running:
            self._card_manager.hide_card("auto_loop_running", self._window_id)
        # 全局卡片（settings/provider_edit/hook_edit/mcp_edit）已迁移到 Tab 窗口层，统一隐藏
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        if cc is not None:
            cc.hide_all_global_cards()

    def register_system_card(self, card_id: str) -> None:
        """将一个卡片 ID 注册为"系统卡片" — 显示时自动隐藏输入区域

        用于 UI 插件的浮动卡片（plugin-marketplace 等）。
        首次调用时同时绑定 on_card_shown/on_card_hidden 回调。

        Args:
            card_id: 卡片唯一 ID（与 CardManager / FloatingCardInfo.card_id 一致）

        Side Effects:
            - 幂等：重复注册不会重复绑定回调
            - 须在 _card_manager 已初始化后调用（即 MainWidget.__init__ 后期）
        """
        # 确保 instance 属性已初始化（防御性：极端情况下 __init__ 未跑完）
        if not hasattr(self, "_system_card_ids"):
            self._system_card_ids = set(self._BASE_SYSTEM_CARD_IDS)
        if card_id in self._system_card_ids:
            return  # 幂等保护
        self._system_card_ids.add(card_id)
        # 注册 CardManager 回调，触发输入区域隐藏/恢复
        self._card_manager.on_card_shown(self._window_id, card_id, lambda cid: self._on_system_card_opened(cid))
        self._card_manager.on_card_hidden(self._window_id, card_id, lambda cid: self._on_system_card_closed(cid))

    def _on_system_card_opened(self, card_id: str):
        """系统卡片打开时隐藏文本输入框（保留按钮栏），腾出空间

        关键修复：工具栏已移出 _input_card，独立放在主 layout 最底部的
        _bottom_toolbar_strip 里。_input_card 缩小或隐藏都不会影响工具栏
        相对窗口的绝对位置——它永远钉死在窗口底部 34px。
        """
        # 窗口拖拽中跳过，防止布局重算干扰窗口管理器
        from app.utils.window_drag_state import any_window_dragging

        if any_window_dragging:
            return
        if hasattr(self, "input_area"):
            # 标记系统卡片处于打开状态
            self._system_cards_open = True

            self.setUpdatesEnabled(False)
            # 隐藏输入区释放空间给系统卡片
            self.input_area.setVisible(False)
            self.input_area.setFocusPolicy(Qt.NoFocus)
            self._input_area_collapsed = True
            self._apply_bottom_input_stack_style(False)
            # 释放 _input_card 的高度约束，让它缩到 0
            self._input_card.setMinimumHeight(0)
            self._input_card.setMaximumHeight(0)
            self.setUpdatesEnabled(True)
            self._input_card.update()

    def _do_hide_input_area(self):
        """保留向后兼容 — 已在 _on_system_card_opened 同步执行 setVisible(False)

        原来通过 QTimer.singleShot(0) 延迟一帧再隐藏 input_area，
        正是这个延迟导致 toolbar 出现"短暂在容器外"的中间帧而视觉抖动。
        修复后改为同步执行，不再需要此方法。保留仅为防止外部引用断裂。
        """
        pass

    def _on_system_card_closed(self, card_id: str):
        """系统卡片关闭时检查是否还有其他同类卡片开着，没有则恢复文本输入框

        关键顺序：必须先 setFixedHeight(91) 恢复 _input_card 高度，
        再 setVisible(True) input_area。理由与 _on_system_card_opened 对称：
        避免 setVisible(True) 时 _input_card 仍 39px，input_area 加入
        layout 后 toolbar 几何被推到 53-87（容器外）造成视觉跳变。

        Args:
            card_id: 卡片 ID。特殊值 "hot_reload_restore" 表示强制恢复，
                     跳过系统卡片可见性检查（用于热重载/卡片强制删除后的兜底恢复）。
        """
        # 窗口拖拽中跳过，防止布局重算干扰窗口管理器
        from app.utils.window_drag_state import any_window_dragging

        if any_window_dragging:
            return
        # 强制恢复模式：跳过系统卡片可见性检查，直接恢复输入区
        # 用于热重载后卡片已被强制删除，回调链可能断裂的场景
        if card_id != "hot_reload_restore":
            # 检查所有系统卡片是否都已关闭（含 UI 插件注册的卡片）。
            # 单一真相源：使用 self._system_card_ids，与 _on_system_card_opened
            # 的注册集合保持一致，确保开/关语义对称。
            for cid in self._system_card_ids:
                if self._card_manager.is_card_visible(cid, self._window_id):
                    return
        # 所有系统卡片已关闭，清除打开标记
        self._system_cards_open = False

        if hasattr(self, "input_area"):
            self.setUpdatesEnabled(False)
            self.input_area.setFocusPolicy(Qt.ClickFocus)
            self._input_area_collapsed = False
            self._apply_bottom_input_stack_style(self.input_area.hasFocus())
            self._input_card.setMinimumHeight(0)
            self._input_card.setMaximumHeight(16777215)
            self._on_input_area_height_changed()
            self.input_area.setVisible(True)
            self.setUpdatesEnabled(True)
            self._input_card.update()

    def _system_cards(self) -> list:
        """返回所有系统卡片的列表，用于检查是否有系统卡片可见

        注意：系统配置/服务商编辑/Hook 编辑/MCP 编辑四张全局卡片已迁移到
        TabManagerWindow 的全局作用域（GLOBAL_WINDOW_ID），不再属于 per-window
        系统卡片，故不在此列表中（它们的显隐由 GlobalCardController 管理）。
        """
        cards = [
            self._model_config_card,
            self._history_card,
            self._memory_card,
            self._auto_loop_config_card,
        ]
        return [c for c in cards if c is not None]

    # ───────────────────────────────────────────────────────────
    # 全局卡片兼容属性（只读委托）
    # 四张全局卡片实例由 GlobalCardController 持有；旧代码中的
    # 透明度调节 / 主题刷新 / 字体缩放等读取点通过这些 property 兼容。
    # ───────────────────────────────────────────────────────────

    @staticmethod
    def _global_card_attr(attr: str):
        from app.widgets.cards.global_card_controller import get_global_card_controller

        cc = get_global_card_controller()
        return getattr(cc, attr, None) if cc is not None else None

    @property
    def _settings_popup(self):
        return self._global_card_attr("_settings_popup")

    @property
    def _provider_edit_card(self):
        return self._global_card_attr("_provider_edit_card")

    @property
    def _provider_edit_popup(self):
        return self._global_card_attr("_provider_edit_popup")

    @property
    def _hook_edit_card(self):
        return self._global_card_attr("_hook_edit_card")

    @property
    def _hook_edit_popup(self):
        return self._global_card_attr("_hook_edit_popup")

    @property
    def _mcp_edit_card(self):
        return self._global_card_attr("_mcp_edit_card")

    @property
    def _mcp_edit_popup(self):
        return self._global_card_attr("_mcp_edit_popup")

    def _is_any_system_card_visible(self) -> bool:
        """检查是否有任何系统卡片可见"""
        for card in self._system_cards():
            if card.isVisible():
                return True
        return False

    def _restore_after_system_close(self):
        """系统卡片关闭后，恢复 todo/sub_agent 实时卡片"""
        if not self._is_any_system_card_visible():
            # 只有当所有系统卡片都关闭时才重置标志
            self._is_system_card_visible = False
        # 恢复 todo（如果之前是显示的且还有内容）
        if self._todo_was_visible_before_system and self._todo_floating_widget._todo_list:
            self._todo_floating_widget.setVisible(True)

    # ══════════════════════════════════════════════════════════════
    # 像素小狐桌宠 — 集中 AI 状态管理
    # ══════════════════════════════════════════════════════════════

    def _set_ai_state(self, state: str) -> None:
        """设置 AI 状态并通知桌宠（仅变化时发射信号）"""
        if state != self._ai_state:
            self._ai_state = state
            self.ai_state_changed.emit(state)
            # 🛡️ 团队实时状态同步：流式/思考/提问 → busy；空闲 → idle
            # （覆盖所有流式/思考阶段，供 team_list_members 查询路径可见）
            self._sync_team_member_runtime_status(state)

    def _sync_team_member_runtime_status(self, state: str) -> None:
        """将 AI 状态同步到团队成员 runtime 状态（仅团队成员生效，非成员静默跳过）"""
        from app.core.team_manager import TeamManager, check_team_member

        if not check_team_member(self._window_id):
            return
        if state in ("thinking", "streaming", "question"):
            TeamManager.get_instance().set_member_runtime_status(self._window_id, "busy")
        elif state == "idle":
            TeamManager.get_instance().set_member_runtime_status(self._window_id, "idle")
        # error 等其余状态不覆盖（重试期保持 busy，避免闪烁）

    def _init_pixel_pet(self) -> None:
        """初始化像素小狐桌宠

        P2 懒加载：窗口未激活（Tab 模式非当前页）时跳过构建，置待建标记，
        激活时由 _maybe_build_deferred_content 补建。手动开启路径
        （_on_pet_enabled_changed）不受影响——用户主动开启时窗口必已激活。
        """
        if getattr(self, "_is_destroyed", False):
            return  # 窗口已销毁：0ms 定时器仍可能触发，直接跳过
        if not self.isVisible():
            self._pixel_pet_pending = True
            return
        try:
            self.pixel_pet = PixelPetWidget(self)
            from app.utils.theme_manager import theme_manager

            theme_manager.register_refresh_target(self.pixel_pet)
            self.pixel_pet.show()
            self.pixel_pet.raise_()
            # 连接 AI 状态信号 → 桌宠自主管理动画
            self.ai_state_changed.connect(self.pixel_pet._on_ai_state_changed)
            # 初始定位到右下角
            self.pixel_pet.resize_handle(self.width(), self.height())
            logger.info("[PixelPet] 桌宠已初始化")
        except Exception as e:
            logger.warning(f"[PixelPet] 初始化失败: {e}")
            self.pixel_pet = None

    def _on_pet_typing(self) -> None:
        """★ 用户输入文字时让桌宠好奇看向输入框"""
        if getattr(self, "_is_destroyed", False):
            return
        if not hasattr(self, "pixel_pet") or self.pixel_pet is None:
            return
        # 只在有实际内容输入时触发（避免清空/加载历史时的误触发）
        text = self.input_area.toPlainText().strip() if hasattr(self, "input_area") else ""
        if text:
            self.pixel_pet.on_user_typing()

    def _on_pet_enabled_changed(self, enabled: bool) -> None:
        """桌宠显示开关实时响应"""
        from app.utils.config import Settings

        if enabled:
            if self.pixel_pet is None:
                self._init_pixel_pet()
            else:
                self.pixel_pet.show()
                self.pixel_pet.raise_()
        else:
            if self.pixel_pet is not None:
                from app.utils.theme_manager import theme_manager

                theme_manager.unregister_refresh_target(self.pixel_pet)
                self.pixel_pet.hide()

    # ══════════════════════════════════════════════════════════════

    def _toggle_model_config_card(self):
        """切换模型配置卡片的显示"""
        self._ensure_model_config_card()  # P0-1：框架懒创建，确保注册/入容器
        if self._card_manager.is_card_visible("model_config", self._window_id):
            # 已可见 → 隐藏
            self._card_manager.hide_card("model_config", self._window_id)
        else:
            # 先加载数据再显示卡片，确保 CardContainer._do_expand()
            # 计算展开高度时内容已填充完毕，不会因参数字段在 ScrollArea
            # 内部增长而错过 Resize 事件，导致卡片高度锁死无法完全展开
            self._load_model_config_to_card()
            self._card_manager.show_card("model_config", self._window_id)
            # 确保顶层窗口从最小化恢复并激活
            top_window = self.window()
            if top_window:
                if top_window.isMinimized():
                    top_window.showNormal()
                top_window.activateWindow()
                top_window.raise_()

    def _toggle_tool_control_card(self):
        """切换工具控制卡片的显示"""
        if not self._card_manager.is_card_visible("tool_control", self._window_id):
            # 通知 card 从 controller 拉取最新状态
            self._tool_control_card.set_toggles(self._tool_permission_controller.get_toggles())
        self._card_manager.toggle_card("tool_control", self._window_id)

    def _refresh_tool_toggle_btn(self):
        """刷新工具开关按钮上的数字和 agent 覆盖指示"""
        # [PERF] 延迟导入，避免模块加载时触发 app.tools 全量导入（~2s）
        from app.tools.tool_classifier import get_tool_counts

        toggles = self._tool_permission_controller.get_toggles()
        dangerous, safe = get_tool_counts(toggles)
        self._tool_danger_label.setText(str(dangerous))
        self._tool_safe_label.setText(str(safe))

        # agent 覆盖 → 整个按钮背景变色
        Colors.refresh()
        agent_name = self._tool_permission_controller.get_active_agent_name()
        if agent_name:
            tooltip = f"🔧 工具权限 | 危险 {dangerous} 安全 {safe}\n🤖 由智能体「{agent_name}」控制，点击查看详情"
            self._tool_toggle_btn.setStyleSheet("""
                background: rgba(255,149,0,0.12);
                border: 1px solid rgba(255,149,0,0.25);
                border-radius: 8px;
            """)
        else:
            tooltip = f"🔧 工具控制 | 危险 {dangerous} 安全 {safe}\n点击查看详情"
            self._tool_toggle_btn.setStyleSheet(f"""
                background: {Colors.TOOLBAR_BG};
                border: none;
                border-radius: 8px;
            """)
        # 给按钮及其所有子 label 挂 tooltip（子控件会阻挡父控件的 tooltip 传播）
        self._tool_toggle_btn.setToolTip(tooltip)
        self._tool_danger_label.setToolTip(tooltip)
        self._tool_safe_label.setToolTip(tooltip)
        # 恢复按钮显隐
        self._tool_restore_btn.setVisible(bool(agent_name))

    def _on_tool_restore(self):
        """工具按钮上的恢复点击：恢复用户权限设置"""
        if not hasattr(self, "_tool_permission_controller") or not self._tool_permission_controller:
            return
        self._tool_permission_controller.restore_user()
        # 刷新卡片和按钮
        if hasattr(self, "_tool_control_card") and self._tool_control_card is not None:
            self._tool_control_card.refresh()
        self._refresh_tool_toggle_btn()

    def _load_model_config_to_card(self):
        """加载当前模型配置到卡片（仅参数配置，不显示连接信息）"""
        # 兜底：延迟构建未完成时立即构建内容，避免 _model_config_popup 为 None 崩溃
        self._ensure_model_config_popup()
        current_name = self._current_provider_name if self._current_provider_name else "无"

        # 关键修复：从 _valid_configs 读取（已通过 _load_model_configs 合并了 FREE_PROVIDERS 默认参数）
        # 而不是从 saved_providers 直接读（绕过默认值合并，会丢"温度"、"最大Token"等字段）
        config = {}
        if current_name in self._valid_configs:
            config = self._valid_configs[current_name].copy()

        # 叠加模型默认值（三层兜底：硬编码 > 模型能力 > 已有配置）
        # 当服务商不在 FREE_PROVIDERS（自定义服务商）时，温度/top_p 等参数仍能有合理默认值
        config = apply_model_defaults(config, self._current_model_name)

        # 叠加用户按模型名保存的覆盖值（最高优先级）
        # 按「服务商名||模型名」隔离同名模型
        model_overrides = getattr(self.cfg, "llm_model_overrides", None)
        if model_overrides and self._current_model_name:
            override_data = model_overrides.value or {}
            overrides = None
            if self._current_provider_name:
                pname = self._valid_configs.get(self._current_provider_name, {}).get(
                    "provider_name", self._current_provider_name
                )
                overrides = override_data.get(f"{pname}||{self._current_model_name}")
            # 向后兼容：旧格式（纯模型名）兜底
            if overrides is None:
                overrides = override_data.get(self._current_model_name, {})
            if overrides:
                config.update(overrides)
        # 模型覆盖数据可能回补思考字段，以 models.dev 为准重新检查
        self._ensure_thinking_fields(config)

        # 移除连接信息、元数据字段和无关的额外字段
        for pop_key in [
            "备注",
            "获取地址",
            "模型名称",
            "API_URL",
            "API_KEY",
            "模型列表",
            "provider_name",
            "name",
            "config_id",
            "display_name",
            "认证方式",
            *QUOTA_EXCLUDE_KEYS,  # 套餐用量查询字段不应出现在模型参数配置中
        ]:
            config.pop(pop_key, None)

        self._model_config_popup.set_config(current_name, config, self._current_model_name)

    def _toggle_history_card(self):
        """切换历史会话卡片的显示"""
        self._ensure_history_card()  # P0-1：框架懒创建，确保注册/入容器
        self._card_manager.toggle_card("history", self._window_id)
        # 显示时刷新历史会话数据
        if self._card_manager.is_card_visible("history", self._window_id):
            self._refresh_history_toggle_panel()

    def _sync_search_box_visibility(self):
        """同步搜索框：两个标签页都显示搜索框"""
        search_input = getattr(self._history_card, "_search_input", None)
        if not search_input:
            return
        search_input.setVisible(True)
        search_input.setFocus()

        # 根据当前标签更新占位文本
        current_tab = self._history_card._current_tab if hasattr(self._history_card, "_current_tab") else "history"
        search_input.setPlaceholderText("🔍 搜索历史会话..." if current_tab == "history" else "🔍 搜索归档会话...")

    def _refresh_history_toggle_panel(self, is_archived: bool = False):
        """刷新历史面板数据"""
        if not self._history_card:
            return

        current_tab = self._history_card._current_tab if hasattr(self._history_card, "_current_tab") else "history"

        if current_tab == "history" or is_archived:
            # 获取当前项目的历史会话列表（M4：merge_team=True 团队会话合并为
            # 单一条目，与普通会话混排；不再注入顶部团队分组区）
            history_list = (
                self.history_manager.get_history_list(self._current_project, merge_team=True)
                if self.history_manager
                else []
            )
            # 在项目过滤后的列表中查找当前会话的位置
            current_idx = None
            if self._current_session_id and self.history_manager:
                for i, session in enumerate(history_list):
                    # 🛡️ 合并条目：当前会话是组内成员之一时命中该合并条目
                    if session.get("team_merged"):
                        members = session.get("members") or []
                        if any(m.get("session_id") == self._current_session_id for m in members):
                            current_idx = i
                            break
                    elif session.get("session_id") == self._current_session_id:
                        current_idx = i
                        break
            # 归档操作后需要清理归档会话列表
            if is_archived:
                self._history_popup_card.set_history(history_list, current_idx, clear_archived=True)
            else:
                self._history_popup_card.set_history(history_list, current_idx)
        else:
            # 刷新归档会话
            self._refresh_archived_sessions()

    def _build_team_groups(self, history_list: List[Dict]) -> List[Dict]:
        """从历史会话列表组装团队对话分组（方案 A）

        # TODO: deprecated, remove with TestBuildTeamGroups
        # M4 混排后无业务调用方（_refresh_history_toggle_panel 改用
        # get_history_list(merge_team=True)），仅保留供旧测试引用。

        按 run_id 聚合：每组包含团队名、成员角色（agent_name 去重）、
        最后活跃时间（组内最新 last_time）、会话数。无 run_id 的会话
        （非团队 / 老团队）跳过。

        Returns:
            按最后活跃时间倒序的团队分组列表
        """
        groups: Dict[str, Dict] = {}
        for session in history_list:
            run_id = (session.get("team_run_id") or "").strip()
            if not run_id:
                continue
            group = groups.setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "team_name": (session.get("team_name") or "").strip() or "团队对话",
                    "agent_names": [],
                    "last_time": "",
                    "session_count": 0,
                },
            )
            agent = (session.get("agent_name") or "").strip()
            if agent and agent not in group["agent_names"]:
                group["agent_names"].append(agent)
            group["session_count"] += 1
            last_time = session.get("last_time") or ""
            if last_time and last_time > group["last_time"]:
                group["last_time"] = last_time

        return sorted(groups.values(), key=lambda g: g["last_time"], reverse=True)

    def _on_history_project_selected(self, project: str):
        """历史面板项目切换（现在和标题栏同步）"""
        # P2-B：捕获切换前项目，供团队广播校验接收方一致性
        prev_project = self._current_project
        self._current_project = project
        self.backend._current_project = project
        self._current_history_project = project
        self._history_popup_card.set_current_project(project)
        self._refresh_history_toggle_panel()
        # 团队模式：历史面板项目切换同样触发团队级同步
        self._broadcast_team_project(project, prev_project)

    def _on_session_dropped_on_project(self, project: str, session_index: int):
        """将会话拖拽到指定项目"""
        if not self.history_manager:
            return
        history_list = self.history_manager.get_history_list(self._current_project) if self.history_manager else []
        if 0 <= session_index < len(history_list):
            # 获取 session_id
            session = history_list[session_index]
            session_id = session.get("session_id")
            if session_id:
                # 更新项目的 session 记录
                idx = self.history_manager.find_index_by_session_id(session_id)
                if idx is not None:
                    self.history_manager.move_to_project(idx, project)
                    # 刷新
                    self._history_popup_card.refreshRequested.emit()
                    InfoBar.success(
                        "已移动",
                        f"会话已移至「{project}」项目",
                        duration=2000,
                        parent=TabManagerWindow.get_instance() or self.window(),
                        position=InfoBarPosition.BOTTOM,
                    )

    def _refresh_archived_sessions(self):
        """刷新归档会话列表（带文件修改时间缓存，避免重复读取）"""
        if not self.history_manager:
            return

        archived_list = self.history_manager.get_archived_sessions()

        # 缓存归档文件预览数据（以文件路径+修改时间为键）
        if not hasattr(self, "_archived_cache"):
            self._archived_cache = {}  # path → (mtime, data_dict)

        enriched_list = []
        need_reparse = False

        for session in archived_list:
            fp = session["path"]
            cached = self._archived_cache.get(fp)

            try:
                current_mtime = os.path.getmtime(fp)
            except OSError:
                current_mtime = 0

            if cached and cached[0] == current_mtime:
                # 缓存有效，直接复用
                session["message_count"] = cached[1].get("message_count", 0)
                session["last_time"] = cached[1].get("last_time", "")
                session["preview"] = cached[1].get("preview", "")
            else:
                # 缓存过期或不存在，读取文件
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = json.loads(f.read())
                    messages = data.get("messages", [])
                    # 🛡️ R7 修复：team 邮件（_hook_event="TeamMail"）计入 user 消息数
                    # → mail-only 会话归档后历史列表不再显示"0 条消息"
                    msg_count = data.get(
                        "message_count",
                        len(
                            [
                                m
                                for m in messages
                                if m.get("role") == "user"
                                and (not m.get("_hook_event") or m.get("_hook_event") == "TeamMail")
                            ]
                        ),
                    )
                    last_time = data.get("last_time", data.get("saved_at", ""))
                    preview = get_message_preview(messages) if messages else ""
                    session["message_count"] = msg_count
                    session["last_time"] = last_time
                    session["preview"] = preview
                    self._archived_cache[fp] = (
                        current_mtime,
                        {
                            "message_count": msg_count,
                            "last_time": last_time,
                            "preview": preview,
                        },
                    )
                    need_reparse = True
                except Exception:
                    pass

            enriched_list.append(session)

        # 清理已不存在的归档文件缓存键（删除/恢复归档后立即生效，
        # 防止 _archived_cache 随文件增删无限增长）
        current_paths = {s["path"] for s in archived_list}
        for stale_key in [k for k in self._archived_cache if k not in current_paths]:
            self._archived_cache.pop(stale_key, None)

        self._history_popup_card.set_archived_sessions(enriched_list)

    def _on_history_tab_changed(self, tab_id: str):
        """处理历史/归档标签切换"""
        self._sync_search_box_visibility()

        # 切换标签时清空搜索
        search_input = getattr(self._history_card, "_search_input", None)
        if search_input:
            search_input.clear()

        self._history_popup_card.switch_tab(tab_id)

        if tab_id == "archived":
            self._refresh_archived_sessions()
        else:
            self._refresh_history_toggle_panel()

    def _on_history_session_selected(self, index: int):
        """从历史面板选择会话"""
        if getattr(self, "_is_destroyed", False):
            return
        if index == -1:
            # 新建会话
            self._create_new_session()
        else:
            self._load_history_session_from_popup(index)
        # 关闭历史会话卡片（通过 CardManager 更新显隐状态）
        self._card_manager.hide_card("history", self._window_id)

    def _on_team_restore_requested(self, run_id: str):
        """从历史面板恢复团队会话（方案 A 一键恢复）

        按 run_id 收集该团队全部成员会话（直接查 SQLite，绕开
        _history_limit=500 截断）→ 按 agent_name 去重（每组取最新会话，
        空消息 agent 也建窗口）→ 为每个角色新建全新窗口、加载对应历史
        会话内容、重新登记为团队成员（join_team）→ 全部共享**全新** run_id
        （独立团队：现有团队不被动，Tab 上独立成另一团队框；多团队并存）。

        恢复语义：内容恢复 + 新 session_id + 新 run_id（恢复是一次新的团队
        运行，产生新会话记录，不覆盖原历史记录）。

        Args:
            run_id: 要恢复的团队运行标识（来自 history_card.teamRestoreRequested）
        """
        if not run_id or getattr(self, "_is_destroyed", False):
            return
        if not self.history_manager:
            return

        # 1) 收集该团队会话（权威数据源 = SQLite）。
        # 🛡️ 直接查 SQLite 绕开 _history_limit=500 截断——团队会话长期运行
        # 可能被挤出内存前 500 条，用 get_history_list() 收集会漏成员
        # （恢复成员不全的根因之一）。
        member_sessions = []
        try:
            member_sessions = self.history_manager.get_team_sessions_by_run_id(run_id)
        except Exception:
            member_sessions = []

        if not member_sessions:
            InfoBar.warning(
                "恢复失败",
                "未找到该团队的会话记录",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 2) 多团队并存（M1）：恢复 = 创建**独立**新团队，不动现有团队——
        #    - 不再调用 disband：现有团队的成员窗口 / 邮箱 / 成员登记全保留，
        #      Tab 上原团队框继续存在；
        #    - 不复用现有 team.json 顶层 run_id：start_team_run(force=True)
        #      会改写顶层 run_id → 后续 /team --join 等读取 get_team_run_id()
        #      的入口会"漂"到新团队，破坏现有团队。
        #    → 改为直接生成 UUID 作为本次恢复的 run_id，仅写入新成员记录
        #      （join_team 透传），不动 team.json 顶层 run_id；恢复窗口的
        #      _team_run_id / members.run_id / team_members.run_id 三处一致
        #      用这个 UUID → Tab 分组 key 唯一，落到独立的新团队框。
        tm_mgr = self._get_team_manager()
        new_run_id = uuid.uuid4().hex

        # 4) 按 agent_name 去重：每组取 last_time 最新一条会话，
        #    避免同 agent 多轮会话建重复窗口（恢复成员 4→1 的修复之一）。
        #    空消息 agent 不跳过：窗口照常创建（仅消息为空），
        #    保证恢复窗口数 = agent 数。
        by_agent: Dict[str, Dict] = {}
        for session in member_sessions:
            agent_name = (session.get("agent_name") or "").strip()
            if not agent_name:
                continue
            cur = by_agent.get(agent_name)
            if cur is None or (session.get("last_time") or "") > (cur.get("last_time") or ""):
                by_agent[agent_name] = session

        # 🛡️ F3/T3/T18（第 3 层收口）：恢复窗口集合 = last_time 最新会话的
        # team_members 快照（快照 = 最近一次团队状态，含全部活跃成员 wid）。
        # - T3：快照含 window_id → 同角色多成员各建独立窗口（逐条计）；
        #   无 wid 老格式（纯名字快照）→ 每 agent 计 1 窗口（无法区分多成员）
        # - T18：只取最新会话快照，不并集全部会话——每次恢复都会产生携带
        #   新 wid 快照的新会话，旧会话快照保留历史 wid；并集会随恢复次数
        #   累积（2→4→6…）导致窗口数线性膨胀。所有成员会话都写全量快照
        #   （_get_team_members_snapshot_json 取 team.json 全量），最新会话
        #   快照即最近一次团队状态，天然收敛且不丢成员（F3 找回语义保留）。
        from app.utils.history_manager import parse_team_members_snapshot

        _latest_session = max(member_sessions, key=lambda s: s.get("last_time") or "", default=None)
        snapshot_members: List[Dict] = []
        if _latest_session:
            for rec in parse_team_members_snapshot((_latest_session.get("team_members") or "").strip()):
                if rec not in snapshot_members:
                    snapshot_members.append(rec)

        # 4) 为每个 agent 创建窗口并注入恢复数据。
        # E3（T3）：会话按 agent 分组（last_time 降序），同 agent 多窗口
        # （同角色多成员）依次取会话——每窗口最多 1 条，不够则空窗口仍创建。
        sessions_by_agent: Dict[str, List[Dict]] = {}
        for session in member_sessions:
            agent_name = (session.get("agent_name") or "").strip()
            if not agent_name:
                continue
            sessions_by_agent.setdefault(agent_name, []).append(session)
        for lst in sessions_by_agent.values():
            lst.sort(key=lambda s: s.get("last_time") or "", reverse=True)
        # 窗口计划（Bug 3 修复）：快照成员逐条建窗时，模板 agents（无 wid）
        # 不再与同 agent 的 wid 记录重复建窗——get_team_member_snapshot 合并
        # 模板 agents（无 wid）与快照成员（有 wid）落库会话快照，若逐条全建，
        # 恢复窗口数随恢复次数线性膨胀（模板 3+成员 3 → 6→9→12…）。
        # 语义保持：
        # - 有 wid 记录：逐条建窗（T3 同角色多成员，如两个 build 各建 1 窗）
        # - 无 wid 记录（模板 agents / 老格式快照）：仅当该 agent 没有任何 wid
        #   记录时才兜底建 1 窗（F3 找回语义——模板角色无实际成员也建窗）
        # - 最新快照为空（老数据无快照）→ by_agent 兜底（每 agent 1 窗口）
        window_plan: List[tuple] = []
        _wid_agents: set = set()
        if snapshot_members:
            for rec in snapshot_members:
                agent = rec["agent_name"]
                if rec.get("window_id"):
                    window_plan.append((agent, rec["window_id"]))
                    _wid_agents.add(agent)
            _plain_seen: set = set()
            for rec in snapshot_members:
                agent = rec["agent_name"]
                if rec.get("window_id"):
                    continue
                if agent in _wid_agents:
                    continue
                if agent not in _plain_seen:
                    _plain_seen.add(agent)
                    window_plan.append((agent, ""))
        else:
            window_plan = [(agent, "") for agent in by_agent]

        restored_count = 0
        restored_windows = []
        # 🛡️ 恢复窗口团队名直接用会话记录里的 team_name（set_template 仅保留
        # 给 /team --load 模板加载路径，恢复路径不再篡改模板上下文）。
        # 遍历取第一个非空 team_name，避免 member_sessions[0] 恰好无团队名时
        # 兜底失败。
        team_display_name = next(
            (s.get("team_name") for s in member_sessions if (s.get("team_name") or "").strip()),
            "团队对话",
        )
        _agent_window_index: Dict[str, int] = {}
        # C1 批量布局：连续 add_tab 期间跳过每次全量重建，结束统一重建一次
        from app.widgets.tab_manager_window import TabManagerWindow as _TMW

        _tmw = _TMW.get_instance()
        if _tmw is not None:
            _tmw._tab_panel.begin_batch_add()
        try:
            for agent_name, _snap_wid in window_plan:
                idx = _agent_window_index.get(agent_name, 0)
                _agent_window_index[agent_name] = idx + 1
                agent_sessions = sessions_by_agent.get(agent_name, [])
                session = agent_sessions[idx] if idx < len(agent_sessions) else {}
                session_id = session.get("session_id", "")
                try:
                    messages = self.history_manager.get_session_messages(session_id) or []
                except Exception:
                    messages = []
                try:
                    # 🛡️ 分支数据在 _create_fresh_window 内部 add_window（触发
                    # showEvent）之前赋值，消除"分支数据赋值太晚"竞态——避免
                    # showEvent 先走 _create_new_session 导致历史消息无法加载。
                    # project 透传保持会话原项目归属（历史会话"无法加载"修复）。
                    # 🛡️ D2：团队标记随调用前置传入（add_window 之前写入），
                    # 恢复窗口 Tab 直接命中团队分组（同 run_id 团队框）。
                    win = self._create_fresh_window(
                        branch_data={
                            "messages": messages,
                            "name": session.get("title") or session.get("name") or f"团队对话 {agent_name}",
                            "project": session.get("project") or "",
                        },
                        team_agent=agent_name,
                        team_name=team_display_name,
                        team_run_id=new_run_id,
                    )
                    if win is None:
                        continue
                    # 团队标记 + 重新登记成员（join_team 幂等；标记由 D2 前置赋值，
                    # 此处幂等重写保持与 _spawn_team_member_window 一致的防御语义）
                    win._team_agent_name = agent_name
                    win._team_name = team_display_name
                    win._team_run_id = new_run_id
                    # 🛡️ M1：恢复路径也补回团队归属——run_id 沿用局部变量
                    # new_run_id（用户期望恢复后归属原 run_id），team_label
                    # 沿用 team_display_name（恢复窗口与原团队同名）。
                    tm_mgr.join_team(
                        window_id=win._window_id,
                        agent_name=agent_name,
                        run_id=new_run_id,
                        team_label=team_display_name,
                    )
                    restored_windows.append(win)
                    restored_count += 1
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[_on_team_restore_requested] 恢复成员 {agent_name} 失败: {e}")
        finally:
            if _tmw is not None:
                _tmw._tab_panel.end_batch_add()

        # 🛡️ T18：重建顶层 team_members 快照——仅保留本次实际恢复的窗口，
        # 清除历史残留 wid（T3 key=window_id 后快照只增不删，残留会导致
        # 下次恢复按 snap_counts 计数 → 窗口数随恢复次数线性膨胀 2→4→6…）。
        # 重建后快照 = 实际窗口集合，多次恢复窗口数恒定（= 成员数）。
        # 🛡️ M1：传 4 元组 (wid, agent, run_id, team_label)——把团队归属一并
        # 写入快照，恢复路径不依赖后续 update_member_team 二次回填（已
        # 在 join_team 写入 + 重建确认）。
        try:
            tm_mgr.rebuild_team_members_snapshot(
                [(w._window_id, w._team_agent_name, new_run_id, team_display_name) for w in restored_windows]
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[_on_team_restore_requested] 重建成员快照失败: {e}")

        # 5) 延后为每个恢复窗口执行完整初始化（切换 agent + 注册成员 +
        #    刷新 UI + 启动 watcher）。与模板加载路径共用
        #    _join_new_window_for_template，但：
        #    - track_arrange=False：旁路模板专用排列计数（恢复路径自行排列，
        #      避免 _pending_arrange_count 被误递减破坏模板加载计数）
        #    - keep_team_name=True：保留会话记录里的团队名（不覆盖为模板名，
        #      保护 test_restore_uses_team_name_from_session）
        for win in restored_windows:
            QTimer.singleShot(
                self._TEMPLATE_JOIN_DELAY_MS,
                lambda w=win: self._join_new_window_for_template(
                    w,
                    getattr(w, "_team_agent_name", ""),
                    getattr(w, "_window_id", "?"),
                    track_arrange=False,
                    keep_team_name=True,
                ),
            )

        # 6) 延后排列恢复的窗口（等窗口初始化完成后统一网格排列）
        if restored_windows:
            QTimer.singleShot(self._TEMPLATE_JOIN_DELAY_MS, self._do_team_window_arrange)

        if restored_count > 0:
            InfoBar.success(
                "团队已恢复",
                f"已恢复 {restored_count} 个成员会话",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=4000,
                position=InfoBarPosition.BOTTOM,
            )
        else:
            # 🛡️ S-3：恢复失败（无可建窗）但未触碰现有团队——独立新团队语义下
            # 现有团队完整保留，无需"重新发起恢复"措辞；只提示本次未恢复。
            InfoBar.warning(
                "团队会话恢复失败",
                "未创建任何恢复窗口（可能为空会话）",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=4000,
                position=InfoBarPosition.BOTTOM,
            )
        # 关闭历史会话卡片
        self._card_manager.hide_card("history", self._window_id)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 极小窗口时对话区让出横向空间给左右 dock：
        # 卡片渲染下限 320 由 sync_width(target_width=max(320, ...)) 兜底，
        # 对话区被压窄/遮挡没关系。判据：
        # - Tab 模式（宿主窗口有 dockSplitter）：用窗口总宽 - 左右 dock 最小
        #   需求（含 handle），剩余不足 320 时让位为 0。不能只看 self.width()
        #   ——溢出时对话区窗格会被 clamp 在 min 320，永远 ≥320 造成死锁。
        # - 无 dock（多窗口模式）：按自身可用宽判断。
        if hasattr(self, "chat_scroll_area"):
            target_min_w = 320
            host = self.window()
            sp = host.findChild(QSplitter, "dockSplitter") if host is not None else None
            if sp is not None and sp.count() >= 3:
                win_w = host.width()
                dock_min = sp.widget(0).minimumWidth() + sp.widget(2).minimumWidth() + sp.handleWidth() * 2
                if win_w - dock_min < 320:
                    target_min_w = 0
            elif self.width() < 320:
                target_min_w = 0
            if self.chat_scroll_area.minimumWidth() != target_min_w:
                self.chat_scroll_area.setMinimumWidth(target_min_w)
        self._set_cards_resize_preview_mode(True)
        # resize 期间持续重置防抖，避免在拖拽过程中提前批量重排
        self._pending_resize_sync = True
        self._resize_debounce_timer.stop()
        self._resize_debounce_timer.start()
        self._resize_complete_timer.stop()
        self._resize_complete_timer.start()
        # 重新定位底部工具栏（绝对定位，不在 layout 里）
        self._position_bottom_toolbar()
        # 桌宠跟随窗口大小修正位置
        if self.pixel_pet:
            self.pixel_pet.resize_handle(self.width(), self.height())

    def _set_cards_resize_preview_mode(self, enabled: bool):
        if enabled == self._resize_preview_active:
            return

        self._resize_preview_active = enabled
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not (item and item.widget() and isinstance(item.widget(), MessageCard)):
                continue
            item.widget().set_resize_preview_mode(enabled)

    def _do_debounced_resize(self):
        """防抖执行卡片宽度同步 — resize 期间仅同步可见卡片。

        关键修复：使用 viewport 宽度直接计算目标宽度，避免循环依赖：
        卡片 minimumWidth 阻止 chat_container 缩小 → parent.width() 卡在旧值
        → sync_width 算出旧宽度 → 死锁。绕过方式：从视口直接推算。
        """
        self._pending_resize_sync = False

        scroll_area = getattr(self, "chat_scroll_area", None)
        if not scroll_area:
            return
        viewport_width = scroll_area.viewport().width()
        if viewport_width <= 0:
            return
        self._last_chat_viewport_width = viewport_width

        viewport_rect = scroll_area.viewport().rect()
        viewport_top = scroll_area.verticalScrollBar().value()
        viewport_bottom = viewport_top + viewport_rect.height()

        # 预计算各类卡片的 margin，避免 per-card 重复
        welcome_margin = 20
        user_margin = 180
        assistant_margin = 20

        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not (item and item.widget() and isinstance(item.widget(), MessageCard)):
                continue

            card = item.widget()

            # 仅同步可见区域附近（缓冲 400px）的卡片，大幅降低 resize 期间 CPU 开销
            card_rect = card.geometry()
            if card_rect.bottom() < viewport_top - 400 or card_rect.top() > viewport_bottom + 400:
                continue

            if card.role == "welcome":
                margin = welcome_margin
            elif card.role == "user":
                margin = user_margin
            else:
                margin = assistant_margin

            # 从视口宽度直接推算目标宽度，绕过循环依赖
            card.sync_width(target_width=max(320, viewport_width - margin))

    def _sync_all_cards_width(self):
        """resize 完成后分批恢复卡片，避免所有 WebEngineView 同时分配 GPU 缓冲区"""
        scroll_area = getattr(self, "chat_scroll_area", None)
        viewport_width = 0
        if scroll_area:
            viewport_width = scroll_area.viewport().width()
            if viewport_width > 0:
                self._last_chat_viewport_width = viewport_width

        # 第一步：全量同步所有卡片宽度（轻量，仅 setMinimumWidth/setMaximumWidth，无 GPU 分配）
        # 🐛 修复：不能只同步视口 ±buffer 内卡片——离屏卡片残留旧宽度(尤其窗口被拉宽又缩小后)
        # 会锁死 chat_container 无法缩小（parent.width()→旧宽度→死锁），滚动补同步也用错 parent 宽。
        # 宽度同步本身轻量，全量遍历开销可接受；真正昂贵的是第二步 GPU preview 恢复(已分批)。
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not (item and item.widget() and isinstance(item.widget(), MessageCard)):
                continue
            card = item.widget()
            try:
                if viewport_width > 0:
                    margin = 20 if card.role != "user" else 180
                    card.sync_width(force=True, target_width=max(320, viewport_width - margin))
                else:
                    card.sync_width(force=True)
            except RuntimeError:
                pass

        # 第二步：收集卡片，分批退出 preview 模式
        self._restore_queue = []
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), MessageCard):
                self._restore_queue.append(item.widget())

        if not self._restore_queue:
            self._resize_preview_active = False
            return

        self._restore_batch_idx = 0
        self._process_restore_batch()

    def _process_restore_batch(self):
        """分批恢复卡片 viewer（触发 GPU 分配，必须分批以避免峰值）"""
        BATCH_SIZE = 5
        INTERVAL_MS = 80
        end = min(self._restore_batch_idx + BATCH_SIZE, len(self._restore_queue))
        for i in range(self._restore_batch_idx, end):
            card = self._restore_queue[i]
            try:
                card.set_resize_preview_mode(False)
            except RuntimeError:
                pass
        self._restore_batch_idx = end
        if end < len(self._restore_queue):
            QTimer.singleShot(INTERVAL_MS, self._process_restore_batch)
        else:
            self._restore_queue = []
            self._resize_preview_active = False

    def _sync_single_card_width(self, card, force: bool = True):
        """按当前滚动区 viewport 宽度同步单张卡片宽度（统一宽度来源）。"""
        scroll_area = getattr(self, "chat_scroll_area", None)
        viewport_width = 0
        if scroll_area:
            viewport_width = scroll_area.viewport().width()
            if viewport_width > 0:
                self._last_chat_viewport_width = viewport_width
        try:
            if viewport_width > 0:
                margin = 20 if card.role != "user" else 180
                card.sync_width(force=force, target_width=max(320, viewport_width - margin))
            else:
                card.sync_width(force=force)
        except RuntimeError:
            pass

    def _sync_visible_cards_on_scroll(self):
        """滚动时更新新进入可见区域的卡片"""
        scroll_area = getattr(self, "chat_scroll_area", None)
        if not scroll_area:
            return

        viewport_rect = scroll_area.viewport().rect()
        viewport_top = scroll_area.verticalScrollBar().value()
        viewport_bottom = viewport_top + viewport_rect.height()

        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not (item and item.widget() and isinstance(item.widget(), MessageCard)):
                continue

            card = item.widget()
            card_rect = card.geometry()
            card_top = card_rect.top()
            card_bottom = card_rect.bottom()

            # 只更新可见区域附近（缓冲200px）的卡片
            if card_bottom < viewport_top - 200 or card_top > viewport_bottom + 200:
                continue

            # 🐛 修复：必须用 viewport 宽度直接推算 target，
            # 不能走 card.sync_width()（内部用 parent.width()）——
            # chat_container 会被偏大的卡片最小宽撑宽，parent 返回旧大值 → 循环。
            # 滚动入视口时用 parent 推算会持续覆盖掉 resize 已修正的正确宽度。
            self._sync_single_card_width(card)

    def _on_config_applied(self, new_config: dict):
        if getattr(self, "_is_destroyed", False):
            return
        current_name = self._current_provider_name
        if not current_name:
            return
        if not new_config:
            return

        # 区分模型级与连接级字段
        model_keys = MODEL_LEVEL_KEYS
        model_fields = {}
        conn_fields = {}
        current_model_name = new_config.get("模型名称", self._current_model_name or "")

        for key, value in new_config.items():
            if key in model_keys:
                model_fields[key] = value
            else:
                conn_fields[key] = value

        # 注意：必须用 deepcopy！ConfigItem.value 返回内部 dict 引用
        saved_providers = copy.deepcopy(self.cfg.llm_saved_providers.value) or {}
        model_overrides = copy.deepcopy(self.cfg.llm_model_overrides.value) or {}

        old_config = saved_providers.get(current_name, self._valid_configs.get(current_name, {}))

        # 1. 连接级字段 → 写入 saved_providers[config_id]
        if conn_fields:
            old_config.update(conn_fields)
            self._valid_configs[current_name] = old_config
            saved_providers[current_name] = old_config
            self.cfg.set(self.cfg.llm_saved_providers, saved_providers, save=True)
            logger.debug(
                f"[_on_config_applied] 连接级字段 -> saved_providers[{current_name}]: {list(conn_fields.keys())}"
            )

        # 2. 模型级字段 → 写入 model_overrides[服务商名||模型名]
        if model_fields and current_model_name:
            provider_name = self._valid_configs.get(current_name, {}).get("provider_name", current_name)
            override_key = f"{provider_name}||{current_model_name}"
            existing = model_overrides.get(override_key, {}).copy()
            existing.update(model_fields)
            model_overrides[override_key] = existing
            self.cfg.set(self.cfg.llm_model_overrides, model_overrides, save=True)
            logger.debug(
                f"[_on_config_applied] 模型级字段 -> model_overrides[{override_key}]: {list(model_fields.keys())}"
            )

        self._load_model_configs()
        logger.debug(f"[_on_config_applied] saved: conn={list(conn_fields.keys())}, model={list(model_fields.keys())}")

        # 思考等级可能已变更 → 刷新参数配置按钮上的思考强度显示
        self._update_settings_effort_btn()

        # ★ 用量聚合（T6）：配置字段（API_KEY/cookie 等）变更后失效用量/余额缓存，
        # 下次请求强制重拉（幂等，可重复调用）。
        try:
            from app.core.usage_service import UsageService

            UsageService.get_instance().invalidate(current_name)
        except Exception:
            pass

    def refresh_theme(self):
        """ThemeManager 统一刷新入口（dispatch_refresh 调用）"""
        # dispatch_refresh = 主题发生变更 → scope="theme"
        self._apply_runtime_ui_settings(scope="theme")

    # ── 多窗口批处理：避免每个窗口重复执行全局操作 ──
    _theme_batch_timer = None
    _theme_batch_scope = None

    def _on_settings_config_changed(self):
        """外观设置变更 → 读取 LLMSettingsCard 标记的变更类型，按需刷新

        多窗口优化：使用 debounce timer 批量处理所有窗口的刷新。
        全局操作（Colors.refresh / setTheme）只执行一次，
        然后逐个窗口执行 per-window 样式更新，避免竞态和重复开销。
        """
        self._load_model_configs()
        # 从 LLMSettingsCard 读取变更类型（消双刷后，这是唯一触发路径）
        from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

        scope = LLMSettingsCard._last_change_type
        LLMSettingsCard._last_change_type = None

        # 合并 scope：theme 优先级最高（涵盖颜色+字体），其次保留具体类型
        cls = type(self)
        previous = cls._theme_batch_scope
        if scope == "theme" or previous == "theme":
            cls._theme_batch_scope = "theme"
        elif scope:
            cls._theme_batch_scope = scope

        # ── Debounce: 30ms 内的多次配置变更合并为一次刷新 ──
        if cls._theme_batch_timer is not None:
            cls._theme_batch_timer.stop()
        from PyQt5.QtCore import QTimer

        if cls._theme_batch_timer is None:
            cls._theme_batch_timer = QTimer()  # 无 parent，跨窗口存活
            cls._theme_batch_timer.setSingleShot(True)
            cls._theme_batch_timer.timeout.connect(cls._execute_batched_theme_refresh)
        cls._theme_batch_timer.start(30)

    @classmethod
    def _execute_batched_theme_refresh(cls):
        """批量执行跨窗口主题刷新（debounce timer 回调）

        1. 全局操作：Colors.refresh / setTheme / on_theme_changed 只执行一次
        2. Per-window：迭代所有窗口执行样式更新
        """
        from app.utils.theme_refresh import ThemeRefreshCoordinator

        ThemeRefreshCoordinator.timer_start("batched_total")

        cls._theme_batch_timer = None
        final_scope = cls._theme_batch_scope
        cls._theme_batch_scope = None

        # ── 全局操作：只执行一次 ──
        ThemeRefreshCoordinator.timer_start("global")
        Colors.refresh()
        theme_manager.on_theme_changed()
        try:
            from qfluentwidgets import Theme, setTheme

            if theme_manager.is_light_theme():
                setTheme(Theme.LIGHT)
            else:
                setTheme(Theme.DARK)
        except Exception:
            pass
        ThemeRefreshCoordinator.timer_end("global")

        # ── Per-window：所有窗口执行样式更新 ──
        # Tab 模式下仅刷新可见窗口，非可见窗口标记延迟刷新（切换到该 tab 时补刷）
        ThemeRefreshCoordinator.timer_start("windows")
        _tab_active_win = None
        try:
            from app.widgets.tab_manager_window import TabManagerWindow as _TMW

            _tm = _TMW.get_instance()
            if _tm is not None and _tm.isVisible():
                _tab_active_win = _tm.get_current_window()
        except Exception:
            pass
        for win in getattr(OpenAIChatToolWindow, "_instances", []):
            if getattr(win, "_is_destroyed", False):
                continue
            # Tab 模式下非可见窗口跳过刷新，标记延迟
            if _tab_active_win is not None and win is not _tab_active_win:
                win._theme_needs_refresh = True
                # P5b：记录待刷 scope，切回 tab 补刷时精确执行（V2 方案 A）
                win._theme_needs_refresh_scope = final_scope
                continue
            try:
                win._apply_runtime_ui_settings(scope=final_scope, _skip_global=True)
                win._theme_needs_refresh = False
            except Exception as e:
                logger.warning(f"[batched theme refresh] window {win._window_id}: {e}")
        ThemeRefreshCoordinator.timer_end("windows")

        # ── Tab 模式：刷新 TabManagerWindow 样式 ──
        # 主题变更路径不走 theme_manager.dispatch_refresh()，而是手动遍历
        # OpenAIChatToolWindow._instances，导致 TabManagerWindow 注册的
        # refresh_target 回调从未触发。此处直接更新。
        try:
            from app.widgets.tab_manager_window import TabManagerWindow as _TabManagerWindow

            _tm = _TabManagerWindow.get_instance()
            if _tm is not None:
                _tm._on_theme_changed()
        except Exception as e:
            logger.warning(f"[batched theme refresh] TabManagerWindow: {e}")

        ThemeRefreshCoordinator.timer_end("batched_total")

    def _on_providers_config_changed(self):
        """服务商配置变更时的回调（多窗口同步）

        当一个窗口添加/修改/删除了服务商，所有窗口都会收到此通知。
        刷新本地 _valid_configs 并更新 UI。
        """
        self._load_model_configs()
        # 如果设置卡片当前可见（同窗口），刷新服务商列表
        if (
            hasattr(self, "_card_manager")
            and self._card_manager.is_card_visible("settings", self._window_id)
            and hasattr(self, "_settings_popup")
            and hasattr(self._settings_popup, "llmProviderCard")
        ):
            self._settings_popup.llmProviderCard._refresh_items()
        # 如果模型选择卡片当前可见，刷新内容
        if hasattr(self, "_card_manager") and self._card_manager.is_card_visible("model_selector", self._window_id):
            self._load_model_selector_to_card()

    def _execute_skill_toggle(self, skill_name: str, enable: bool):
        """执行技能启用/禁用（FUNCTION 命令，不发送消息给 LLM）"""
        from qfluentwidgets import InfoBar, InfoBarPosition

        from app.utils.config import Settings

        cfg = Settings.get_instance()
        enabled_skills = cfg.llm_enabled_skills.value.copy() if cfg.llm_enabled_skills.value else []
        changed = False
        if enable:
            if skill_name not in enabled_skills:
                enabled_skills.append(skill_name)
                changed = True
                msg = f"「{skill_name}」已添加到系统提示词"
            else:
                msg = f"「{skill_name}」已是启用状态"
        else:
            if skill_name in enabled_skills:
                enabled_skills.remove(skill_name)
                changed = True
                msg = f"「{skill_name}」已从系统提示词中移除"
            else:
                msg = f"「{skill_name}」已是禁用状态"

        if changed:
            cfg.set(cfg.llm_enabled_skills, enabled_skills, save=True)
            # 保存后 cfg.set 会触发 valueChanged → _on_skills_config_changed → 自动同步 UI
            InfoBar.success(
                title="技能" + ("已启用" if enable else "已禁用"),
                content=msg,
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=2000,
                position=InfoBarPosition.BOTTOM,
            )
        else:
            InfoBar.info(
                title="技能" + ("已启用" if enable else "已禁用"),
                content=msg,
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=1500,
                position=InfoBarPosition.BOTTOM,
            )
        self.input_area.clear()
        if not self._is_streaming:
            self.input_area.toggle_send_button(True)

    def _sync_skill_list_cards(self):
        """同步所有窗口的技能列表卡片状态（无条件同步，widget 隐藏时也更新）"""
        for win in getattr(OpenAIChatToolWindow, "_instances", []):
            if win._is_destroyed:
                continue
            try:
                popup = getattr(win, "_settings_popup", None)
                if popup and hasattr(popup, "llmSkillsCard"):
                    card = popup.llmSkillsCard
                    card._sync_skill_states()
                    card._update_skill_token_count()
            except RuntimeError, AttributeError:
                pass

    def _on_skills_config_changed(self, enabled_skills):
        """技能配置变更时的回调（多窗口同步）

        当一个窗口启用/禁用了技能，轻量同步开关状态，不重建列表。
        """
        self._sync_skill_list_cards()

    def _reload_plugin_system(self):
        """运行时重载所有插件子系统（设置中点击「重载插件」时调用）"""
        if hasattr(self, "backend") and self.backend:
            result = self.backend.reload_plugin_subsystems()
            from qfluentwidgets import InfoBar, InfoBarPosition

            InfoBar.success(
                title="插件已重载",
                content=f"智能体: {result.get('agents', 0)}个, "
                f"命令: {'✓' if result.get('commands') else '✗'}, "
                f"主题: {'✓' if result.get('themes') else '✗'}, "
                f"技能: {'✓' if result.get('skills') else '✗'}, "
                f"MCP: {'✓' if result.get('mcp') else '✗'}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )

    def _register_tool_reload_notice(self):
        """注册工具热重载风险通知监听（进程级一次，多窗口只注册首个）

        PluginToolWatcher 检测到插件工具目录变更并完成重扫后，后台线程触发
        listener → 桥接信号 QueuedConnection 到主线程 → 弹 MaskDialog 风险通知。
        仅 watcher 轮询路径触发；启动对齐 / 插件启停的重扫不触发（用户已知情）。
        """
        if OpenAIChatToolWindow._tool_reload_notice_registered:
            return
        OpenAIChatToolWindow._tool_reload_notice_registered = True
        global _tool_reload_notice_bridge
        try:
            from app.tools.plugin_tool_loader import ensure_plugin_tool_watcher

            watcher = ensure_plugin_tool_watcher()
            if watcher is None:
                # watcher 未就绪（watchfiles 未安装等）→ 解除注册，下次窗口再试
                OpenAIChatToolWindow._tool_reload_notice_registered = False
                return
            if _tool_reload_notice_bridge is None:
                _tool_reload_notice_bridge = _ToolReloadNoticeBridge()
                _tool_reload_notice_bridge.notified.connect(OpenAIChatToolWindow._on_tool_reload_notice)
            watcher.on_tools_reloaded(_tool_reload_notice_bridge.reloaded.emit)
            logger.debug("[ToolReloadNotice] 工具热重载风险通知监听已注册")
        except Exception as e:
            logger.warning(f"[ToolReloadNotice] 注册热重载监听失败: {e}")
            OpenAIChatToolWindow._tool_reload_notice_registered = False

    @staticmethod
    def _on_tool_reload_notice():
        """主线程槽：工具热重载完成 → 弹风险通知（MaskDialog 风格）"""
        try:
            from app.widgets.common_dialogs import InfoDialog

            cfg = Settings.get_instance()
            if not cfg.tool_reload_risk_notice.value:
                return  # 用户已选择「不再提醒」
            # 找主窗口作 parent（Tab 管理器优先，否则首个存活窗口）
            parent = TabManagerWindow.get_instance()
            if parent is None:
                for win in OpenAIChatToolWindow._instances:
                    if not getattr(win, "_is_destroyed", False):
                        parent = win
                        break
            if parent is None:
                return
            dialog = InfoDialog(
                title="工具热重载完成",
                content=(
                    "检测到工具插件已被热重载，模型可用的工具定义已更新。\n\n"
                    "请注意：\n"
                    "• 正在进行的对话仍使用重载前的工具列表，新工具需新开对话才生效\n"
                    "• 历史会话中已记录的工具调用可能与新定义存在差异\n"
                    "• 若工具不可用或调用异常，请检查插件代码后重试"
                ),
                confirm_text="知道了",
                dismiss_text="不再提醒",
                parent=parent,
            )
            dialog.dismissed.connect(lambda: cfg.set(cfg.tool_reload_risk_notice, False, save=True))
            dialog.exec_()
        except Exception as e:
            logger.warning(f"[ToolReloadNotice] 弹框失败: {e}")

    def _on_plugin_hot_reload(self, result: dict):
        """插件热更新完成时的回调（watchfiles 自动触发）

        多窗口广播：遍历所有窗口实例，逐一失效命令卡片缓存。
        如有窗口当前命令卡片可见，立即重建内容。
        """
        if not hasattr(self, "backend") or not self.backend:
            return
        # 窗口级去重：plugin_changed 广播到全部 backend 实例（多窗口各一个 backend），
        # 同一事件每个窗口各执行一遍。类级指纹 + 10s 短窗抑制：只让首个窗口
        # 执行完整刷新链路（其内部遍历 _instances 覆盖所有窗口），其余窗口直接跳过。
        _fingerprint = repr(sorted(result.items()))
        _now = time.time()
        if (
            _fingerprint == OpenAIChatToolWindow._last_hot_reload_fingerprint
            and _now - OpenAIChatToolWindow._last_hot_reload_at < 10.0
        ):
            return
        OpenAIChatToolWindow._last_hot_reload_fingerprint = _fingerprint
        OpenAIChatToolWindow._last_hot_reload_at = _now
        # 不弹 InfoBar，仅日志记录
        logger.debug(
            f"[HotReload] plugin reloaded: agents={result.get('agents', 0)}, "
            f"commands={result.get('commands')}, themes={result.get('themes')}, "
            f"skills={result.get('skills')}, mcp={result.get('mcp')}, "
            f"ui={result.get('ui')}"
        )

        # UI 插件可能注册了浮动卡片命令，需要一并失效命令缓存
        needs_invalidation = (
            result.get("commands") or result.get("skills") or result.get("agents", 0) > 0 or result.get("ui")
        )

        # 广播给所有窗口实例
        for win in OpenAIChatToolWindow._instances:
            if not hasattr(win, "_command_card"):
                continue
            if win._is_destroyed:
                continue

            if needs_invalidation:
                # 使用 refresh_if_visible 替代原先的 invalidate_cache + show_card
                # 修复点：
                # 1. 原代码强制调用 show_card(query) 会触发 _reset_detail_mode()，
                #    导致用户正在查看的 detail 模式参数提示突然消失
                # 2. 原代码从 input_area.toPlainText() 提取 query 会覆盖当前的过滤上下文
                # refresh_if_visible 在 detail 模式下保留参数视图，列表模式下保留过滤
                try:
                    win._command_card.refresh_if_visible()
                except RuntimeError, AttributeError:
                    # 多窗口竞态：窗口已被销毁
                    pass

        # 命令变更：同步刷新快捷键绑定
        if result.get("commands"):
            # 清除窗口级快捷键缓存，允许重新注册
            OpenAIChatToolWindow._window_shortcut_cache.clear()
            for win in OpenAIChatToolWindow._instances:
                if win._is_destroyed:
                    continue
                try:
                    win._register_command_shortcuts()
                except RuntimeError, AttributeError:
                    pass
            # toggle-window 可能被用户插件覆盖 → 同步更新全局热键
            try:
                from app.tray_manager import TrayManager

                tray = TrayManager.get_instance()
                tray._setup_global_hotkey()
            except Exception:
                pass
            logger.debug("[HotReload] command shortcuts re-registered")

        # 技能变更：刷新技能列表（settings popup + 卡片 token 估算）
        # 注意：settings popup 是全局共享单例（所有窗口通过 property 访问同一实例），
        # 遍历窗口时每个窗口都会命中同一实例 → 只处理一次即 break，避免重复刷新。
        if result.get("skills"):
            for win in OpenAIChatToolWindow._instances:
                if win._is_destroyed:
                    continue
                try:
                    if not hasattr(win, "_settings_popup") or not win._settings_popup:
                        continue
                    if not hasattr(win._settings_popup, "llmSkillsCard"):
                        continue
                    win._settings_popup.llmSkillsCard._refresh_skills()
                    break
                except RuntimeError, AttributeError:
                    # 多窗口竞态：窗口已被销毁
                    pass
            logger.debug("[HotReload] skills list re-discovered")

        # Hooks 变更：刷新 hook 设置卡片（settings popup 全局单例 → 只刷一次）
        if result.get("hooks"):
            for win in OpenAIChatToolWindow._instances:
                if win._is_destroyed:
                    continue
                try:
                    if not hasattr(win, "_settings_popup") or not win._settings_popup:
                        continue
                    if not hasattr(win._settings_popup, "hookListCard"):
                        continue
                    card = win._settings_popup.hookListCard
                    if card._hook_manager:
                        card._hook_manager.reload_global_hooks(str(card._hooks_config_file))
                    card._refresh(reload=True)
                    break
                except (RuntimeError, AttributeError) as e:
                    # 多窗口竞态：窗口已被销毁
                    pass
            logger.debug("[HotReload] hooks card refreshed")

        # 主题变更：刷新主题下拉列表（settings popup 全局单例，只刷一次）
        if result.get("themes"):
            for win in OpenAIChatToolWindow._instances:
                if win._is_destroyed:
                    continue
                try:
                    if not hasattr(win, "_settings_popup") or not win._settings_popup:
                        continue
                    win._settings_popup.refresh_theme_options()
                    break
                except RuntimeError, AttributeError:
                    pass
            logger.debug("[HotReload] settings theme dropdown refreshed")

        # MCP 配置变更：刷新全局 MCP 服务器列表
        # 注意：settings popup 是全局唯一共享卡片（所有窗口通过 property 访问同一实例），
        # 且 consume_hot_reload() 的抑制标记是单次消费——若遍历窗口时每个窗口都消费/刷新，
        # 多窗口下第一个窗口消费掉自触发标记后，其余窗口会重复全量重建列表。
        # 因此整轮广播只处理一次：自触发（开关操作）→ consume 返回 True → 跳过刷新；
        # 外部修改 .mcp.json 或插件被热重载 → consume 返回 False → 执行一次 _refresh()。
        #
        # 关键修复（2026-08）：PluginManager.rescan_plugin 在【每次】插件热重载时都会失效
        # MCP 服务器缓存（invalidate_mcp_cache），但触发热重载的未必是 .mcp.json（例如插件
        # Python 代码变更会被归类到其它 component），此时 result['mcp'] 为 False，下方列表行
        # （仅由 _refresh() 重建）便残留旧数据；而 x/x 计数由 3s 定时器读取新缓存会自动更新，
        # 于是出现「计数已更新、列表未更新」的现象。故只要发生了插件重载（任一组件标志为真）
        # 就刷新一次 MCP 列表。
        _mcp_reload = bool(result.get("mcp")) or any(
            result.get(k) for k in ("agents", "commands", "themes", "skills", "lsp", "ui")
        )
        if _mcp_reload:
            mcp_card = None
            for win in OpenAIChatToolWindow._instances:
                if win._is_destroyed:
                    continue
                try:
                    if not hasattr(win, "_settings_popup") or not win._settings_popup:
                        continue
                    if not hasattr(win._settings_popup, "mcpListCard"):
                        continue
                    mcp_card = win._settings_popup.mcpListCard
                    # 自触发（开关操作）→ consume 返回 True → 跳过刷新（避免整卡闪烁）；
                    # 外部修改 .mcp.json / 插件被热重载 / 插件被删除 → consume 返回 False → 刷新一次。
                    if mcp_card.consume_hot_reload():
                        logger.debug("[HotReload] MCP server list: suppress self-triggered refresh")
                    else:
                        # 强制失效 MCP 缓存（rescan 已失效，此处兜底），确保 _refresh 读到最新列表
                        try:
                            mcp_card._get_pm().invalidate_mcp_cache()
                        except Exception:
                            pass
                        mcp_card._refresh()
                        # 补连新增/未连接的已启用服务器：
                        # 热重载路径只刷新列表 + 断开孤儿连接（disconnect_missing），
                        # 从不主动连接新出现的服务器——而唯一连接入口 _init_mcp_connections
                        # 只在启动时跑一次。导致插件热重载安装（带 .mcp.json）后，
                        # 配置显示开启但 MCP 实际未启动。refresh_connections 幂等：
                        # 已连接的跳过、全局开关关闭时跳过。
                        mcp_card.refresh_connections()
                        logger.debug("[HotReload] MCP server list refreshed")
                    break
                except (RuntimeError, AttributeError) as e:
                    # 多窗口竞态：窗口已被销毁
                    pass
            # 插件删除 / 服务器移除 / 禁用后，断开已不在启用列表中的运行连接（避免子进程残留）。
            # 后端删除分支已置 result['mcp']=True（若插件含 MCP 组件），可触发到此分支。
            if mcp_card is not None:
                try:
                    pm = mcp_card._get_pm()
                    # 直接以 PluginManager 取最新启用列表（不依赖卡片存活），断开孤儿连接
                    pm.invalidate_mcp_cache()
                    servers = pm.get_mcp_servers()
                    valid_names = {s.get("name", "") for s in servers if s.get("enabled", True)}
                    mgr = mcp_card._get_mcp_manager()
                    mgr.disconnect_missing(valid_names)
                except Exception as e:
                    logger.debug(f"[HotReload] MCP 断开失效连接失败: {e}")

        # LSP 配置变更：刷新 LSP 状态列表（settings popup 全局单例 → 只刷一次）
        if result.get("lsp"):
            for win in OpenAIChatToolWindow._instances:
                if win._is_destroyed:
                    continue
                try:
                    if not hasattr(win, "_settings_popup") or not win._settings_popup:
                        continue
                    if not hasattr(win._settings_popup, "lspListCard"):
                        continue
                    card = win._settings_popup.lspListCard
                    if hasattr(card, "_rebuild"):
                        card._rebuild()
                    break
                except RuntimeError, AttributeError:
                    pass
            logger.debug("[HotReload] LSP server list refreshed")

        # UI 组件变更：热重载可能已强制删除 UI 插件卡片，
        # 检查并恢复输入区（兜底：防止 _on_system_card_closed 回调链断裂）
        if result.get("ui"):
            for win in OpenAIChatToolWindow._instances:
                if win._is_destroyed:
                    continue
                try:
                    if not getattr(win, "_system_cards_open", False):
                        # 系统卡片未处于打开状态，无需恢复
                        continue
                    if not hasattr(win, "_card_manager") or not hasattr(win, "_window_id"):
                        continue
                    cm = win._card_manager
                    wid = win._window_id
                    # 检查是否所有系统卡片都已关闭
                    all_closed = True
                    for cid in list(getattr(win, "_system_card_ids", set())):
                        if cm.is_card_visible(cid, wid):
                            all_closed = False
                            break
                    if all_closed:
                        win._on_system_card_closed("hot_reload_restore")
                        logger.debug("[HotReload] UI 组件变更后兜底恢复输入区")
                except RuntimeError, AttributeError:
                    pass
            logger.debug("[HotReload] UI 组件变更后系统卡片状态检查完成")

            # Tab 模式下刷新共享 Launcher（热重载可能新增 / 卸载了 UI 插件）
            try:
                from app.widgets.tab_manager_window import TabManagerWindow

                _tm = TabManagerWindow.get_instance()
                if _tm is not None and _tm.isVisible():
                    _tm._update_shared_launcher()
            except Exception:
                logger.exception("[HotReload] 刷新共享 Launcher 异常")
            logger.debug("[HotReload] UI 插件列表已刷新")

    def _apply_runtime_ui_settings(self, scope=None, _skip_global=False):
        """
        统一刷新 UI 外观，按变更类型分流。

        scope 取值：
          None          — 全量刷新（未知变化）
          "theme"       — 仅颜色/主题相关，跳过字体操作
          "font_family" — 仅字体族相关，跳过颜色/主题操作
          "font_size"   — 仅字号相关，跳过颜色/主题操作

        _skip_global   — 批处理模式：全局操作（Colors.refresh/setTheme）
                         已由协调器执行，跳过来自本窗口的重复调用。
        """
        from app.utils.theme_refresh import ThemeRefreshCoordinator

        ThemeRefreshCoordinator.timer_start("total")

        # ── 幂等跳过：同一主题重复刷新直接 return ──
        is_color = scope in (None, "theme")
        is_font_family = scope in (None, "font_family")
        is_font_size = scope in (None, "font_size")
        is_font = scope in (None, "font_family", "font_size")

        # ── 幂等跳过：同一主题直接跳过颜色/主题块 ──
        current_theme_id = theme_manager.get_current_theme_id()
        if is_color and getattr(self, "_last_color_theme_id", None) == current_theme_id:
            is_color = False  # 主题未变，跳过颜色块；字体相关块照常执行
        else:
            self._last_color_theme_id = current_theme_id

        # ── 幂等短路：无任何块需要执行（theme 且主题未变）→ 直接返回 ──
        # scope="theme" 且主题未变时 is_color/is_font 全 False，
        # findChildren 全树扫描 + 公共块 setStyleSheet 均为无意义开销（实测 ~110ms）。
        if not is_color and not is_font:
            ThemeRefreshCoordinator.timer_end("total")
            return

        # Colors.refresh() 在 preamble 中无条件执行，
        # 保证后续公共块中所有 Colors.* 引用使用最新值。
        # 批处理模式下跳过（已由协调器执行）。
        if not _skip_global:
            Colors.refresh()

        # ── 0. 按需扫描 widget 树，按类型缓存 ──
        # [PERF] 单次 findChildren(QWidget) 全树遍历 + isinstance 分类，
        # 替代多次独立类型 findChildren（每次都是完整树遍历，实测 5 次 ≈ 80ms）。
        # SystemCardFrame 单独扫：_settings_popup 由 GlobalCardController 持有，
        # 不在 self 的 widget 树内。
        ThemeRefreshCoordinator.timer_start("findChildren")
        from PyQt5.QtWidgets import QWidget as _QWidget

        _all_widgets = self.findChildren(_QWidget) if (is_color or is_font) else []
        if is_color or is_font_family:
            _message_cards = [w for w in _all_widgets if isinstance(w, MessageCard)]
        else:
            _message_cards = []
        _base_settings = [w for w in _all_widgets if isinstance(w, BaseSettingsCard)]
        from qfluentwidgets import SettingCard

        if is_font_size:
            _setting_cards = [w for w in _all_widgets if isinstance(w, SettingCard)]
        else:
            _setting_cards = []
        from app.widgets.worktree_section import WorktreeSectionWidget

        if is_font:
            _worktree_widgets = [w for w in _all_widgets if isinstance(w, WorktreeSectionWidget)]
        else:
            _worktree_widgets = []
        _popup_frames = self._settings_popup.findChildren(SystemCardFrame) if self._settings_popup else []
        ThemeRefreshCoordinator.timer_end("findChildren")

        # ── 1. 颜色/主题相关块 ──
        if is_color:
            if not _skip_global:
                theme_manager.on_theme_changed()

                # 同步 qfluentwidgets 基础主题
                try:
                    from qfluentwidgets import Theme, setTheme

                    if theme_manager.is_light_theme():
                        setTheme(Theme.LIGHT)
                    else:
                        setTheme(Theme.DARK)
                except Exception:
                    pass

            # 对话框背景完全透明，由外层容器兜底，不叠加独立背景层。
            self.setStyleSheet("background: transparent;")
            self.setAutoFillBackground(False)

            # 分支标签
            if hasattr(self, "_project_label"):
                self._refresh_project_branch_style()

            # 分支标签（git worktree）
            if hasattr(self, "_update_branch"):
                self._update_branch()

            # 时间线节点
            if hasattr(self, "node_preview") and hasattr(self.node_preview, "refresh_theme"):
                self.node_preview.refresh_theme()

            # 上下文圆环 + 编码计划圆环（轨道颜色随深浅模式变化）
            for ring_attr in ("context_usage_ring", "coding_plan_ring"):
                ring = getattr(self, ring_attr, None)
                if ring and hasattr(ring, "refresh_theme"):
                    ring.refresh_theme()

            # 消息卡片主题
            ThemeRefreshCoordinator.timer_start("msg_cards")
            for card in _message_cards:
                if hasattr(card, "refresh_theme"):
                    card.refresh_theme()
            ThemeRefreshCoordinator.timer_end("msg_cards")

            # 自绘 hover tooltip 主题刷新
            from app.widgets.simple_hover_tooltip import refresh_all_tooltips

            refresh_all_tooltips()

        # ── 2. 字体相关块（font_family + font_size + 全量） ──
        if is_font:
            # 输入区字体：QSS 中 font-family/font-size 声明优先级高于 setFont，
            # 仅 setFont 无法让字体变化生效，必须重建 QSS（refresh_style）。
            # is_color 时 5a 块已调 refresh_style，这里跳过避免重复重建。
            if hasattr(self, "input_area") and hasattr(self.input_area, "refresh_style"):
                if not is_color:
                    self.input_area.refresh_style()
            elif hasattr(self, "input_area"):
                setFont(self.input_area, scale_font_size(15))

            # 递归刷新所有 qfluentwidgets 组件字体大小
            apply_font_size_to_widget(self, 14)

            # 设置弹窗字体
            if self._settings_popup:
                apply_font_size_to_widget(self._settings_popup, 14)

            # WorktreeSectionWidget 主题（含字体）
            for wt_widget in _worktree_widgets:
                wt_widget.refresh_style()

            # 上下文圆环 + 编码计划圆环 tooltip 字号随字号变化
            for ring_attr in ("context_usage_ring", "coding_plan_ring"):
                ring = getattr(self, ring_attr, None)
                if ring and hasattr(ring, "refresh_font_size"):
                    ring.refresh_font_size()

            # 自绘 hover tooltip 字号刷新
            from app.widgets.simple_hover_tooltip import refresh_all_tooltips

            refresh_all_tooltips()

            # 分支标签（包含字号 + 颜色的样式，字体变化时需同步刷新）
            if hasattr(self, "_project_label"):
                self._refresh_project_branch_style()

        # ── 2.5 会话标题（颜色+字体，主题切换或字体变化时都要更新） ──
        if is_color or is_font:
            if hasattr(self, "title_edit"):
                font_css = get_font_family_css()
                title_style = f"""QLabel {{
                    color: {Colors.TEXT_PRIMARY};
                    {font_size_css(15)}
                    font-weight: bold;
                    padding: 6px 4px;
                    border-radius: 10px;
                    background-color: transparent;
                    {font_css}
                }}
                QLabel:hover {{
                    background-color: {Colors.HOVER_BG};
                }}
                QLineEdit {{
                    color: {Colors.TEXT_PRIMARY};
                    {font_size_css(15)}
                    font-weight: bold;
                    padding: 6px 4px;
                    border-radius: 10px;
                    background-color: transparent;
                    border: none;
                    {font_css}
                }}
                QLineEdit:focus {{
                    background-color: {Colors.HOVER_BG_STRONG};
                    border: 1px solid {Colors.BORDER};
                }}
                """
                self.title_edit.setStyleSheet(title_style)

        # ── 3. 字号专属块（仅 font_size + 全量，不涉及字族变化） ──
        if is_font_size:
            # 所有 SettingCard 图标大小随字号缩放
            icon_sz = scale_icon_size(16)
            for card in _setting_cards:
                card.setIconSize(icon_sz, icon_sz)

        # ── 4. 消息卡 viewer 渲染（仅 font_family + 全量，字族变化需重渲） ──
        if is_font_family:
            for card in _message_cards:
                viewer = getattr(card, "viewer", None)
                if viewer and hasattr(viewer, "_schedule_render"):
                    viewer._schedule_render(immediate=True)

        # ── 5. 公共块 ──
        # [PERF] 拆分颜色/字体：字体变化时（is_color=False）跳过纯颜色刷新，
        # 避免数百次 setStyleSheet 重复设置相同颜色样式（实测 ~30ms 纯浪费）。
        if is_color:
            # ── 5a. 颜色相关公共块（仅主题/深浅变化时执行） ──
            # 窗口标题栏
            title_bar = self.get_title_bar()
            if hasattr(title_bar, "refresh_style"):
                title_bar.refresh_style()
            # 输入卡片 + 底部工具栏条背景
            if hasattr(self, "_input_card"):
                self._apply_bottom_input_stack_style()
            if hasattr(self, "_bottom_toolbar_strip"):
                self._apply_bottom_input_stack_style()
            # 模型按钮容器
            if hasattr(self, "_model_btn_container"):
                self._model_btn_container.setStyleSheet(f"""
                    background: {Colors.TOOLBAR_BG};
                    border: none;
                    border-radius: 8px;
                """)
            if hasattr(self, "_toolbar_capsule"):
                self._toolbar_capsule.setStyleSheet(f"""
                    background: {Colors.TOOLBAR_BG};
                    border: none;
                    border-radius: 8px;
                """)
            # 输入区样式（含文本框 + 下拉框，主题色敏感 → 每次必刷）
            if hasattr(self, "input_area") and hasattr(self.input_area, "refresh_style"):
                self.input_area.refresh_style()
            # 发送按钮
            if hasattr(self, "input_area") and hasattr(self.input_area, "_apply_send_btn_style"):
                self.input_area._apply_send_btn_style()
            # 智能体切换按钮
            if hasattr(self, "_agent_switch_widget"):
                self._agent_switch_widget.setStyleSheet(f"""
                    background: {Colors.TOOLBAR_BG};
                    border: none;
                    border-radius: 8px;
                """)
            # 设置弹窗 — 子卡片主题样式
            if self._settings_popup:
                for frame in _popup_frames:
                    if hasattr(frame, "refresh_style"):
                        frame.refresh_style()
                # 补充刷新设置弹窗中的命名子卡片（不在 findChildren 范围的子类）
                for card_name in (
                    "uiFontSizeCard",
                    "uiLightModeCard",
                    "uiThemeStyleCard",
                    "llmFontCard",
                    "llmSkillsCard",
                    "llmProviderCard",
                    "mcpListCard",
                    "lspListCard",
                ):
                    card = getattr(self._settings_popup, card_name, None)
                    if card is not None and hasattr(card, "refresh_style"):
                        card.refresh_style()
                # 刷新设置弹窗分隔标签
                if hasattr(self._settings_popup, "_refresh_sep_labels"):
                    self._settings_popup._refresh_sep_labels()
            # 设置卡片（全窗口递归）
            for card in _base_settings:
                if hasattr(card, "refresh_style"):
                    card.refresh_style()
            if self._settings_popup and hasattr(self._settings_popup, "refresh_style"):
                self._settings_popup.refresh_style()
            # AutoLoop 卡片
            if self._auto_loop_config_card and hasattr(self._auto_loop_config_card, "_refresh_theme_style"):
                self._auto_loop_config_card._refresh_theme_style()
            if self._auto_loop_running_card and hasattr(self._auto_loop_running_card, "_refresh_theme_style"):
                self._auto_loop_running_card._refresh_theme_style()
            # 浮动卡片
            for card in (
                self._todo_floating_widget,
                self._question_floating_widget,
                self._sub_agent_compact_widget,
                self._share_card_content,
                self._history_questions_card_content,
                self._undo_delete_card,
            ):
                if card and hasattr(card, "refresh_style"):
                    card.refresh_style()
            # 卡片容器
            if (
                hasattr(self, "_top_card_container")
                and self._top_card_container
                and hasattr(self._top_card_container, "refresh_style")
            ):
                self._top_card_container.refresh_style()
            if (
                hasattr(self, "_bottom_card_container")
                and self._bottom_card_container
                and hasattr(self._bottom_card_container, "refresh_style")
            ):
                self._bottom_card_container.refresh_style()
            # 命令卡片
            if hasattr(self, "_command_card") and self._command_card and hasattr(self._command_card, "refresh_style"):
                self._command_card.refresh_style()
            # 文件提及卡片（滚动条颜色随主题）
            if (
                hasattr(self, "_file_mention_card")
                and self._file_mention_card
                and hasattr(self._file_mention_card, "refresh_style")
            ):
                self._file_mention_card.refresh_style()
            # 模型选择卡片
            if hasattr(self, "_model_selector_card_content") and self._model_selector_card_content:
                self._model_selector_card_content.refresh_style()
            # 🛡️ 懒创建框架下 _model_selector_card 可能仍为 None（deferred 链未执行），
            # 必须判 None 而非 hasattr（属性恒存在，值为 None 时 hasattr 返回 True）
            if getattr(self, "_model_selector_card", None) is not None:
                self._model_selector_card.refresh_style()
                self._update_model_selector_header()
            # 项目选择卡片
            if hasattr(self, "_project_selector_card_content"):
                self._project_selector_card_content.refresh_style()
            if hasattr(self, "_project_selector_card"):
                self._project_selector_card.refresh_style()
            # 记忆卡片
            if hasattr(self, "_memory_card_popup") and hasattr(self._memory_card_popup, "refresh_style"):
                self._memory_card_popup.refresh_style()
            # 工具控制卡片
            if hasattr(self, "_tool_control_card") and hasattr(self._tool_control_card, "refresh_style"):
                self._tool_control_card.refresh_style()

        # ── 5b. 字体相关公共块（颜色或字体变化时都执行） ──
        # 模型按钮文字（含 font_size_css，颜色+字体双敏感）
        if hasattr(self, "_model_btn_text"):
            self._model_btn_text.setStyleSheet(self._get_model_btn_text_style())
        # 参数配置按钮的思考强度胶囊（颜色+字体双敏感）
        if hasattr(self, "_settings_effort_label"):
            self._settings_effort_label.setStyleSheet(self._get_settings_effort_style())
        # 项目新建输入框（含 font_size_css + 颜色）
        if hasattr(self, "_project_new_edit"):
            self._project_new_edit.setStyleSheet(f"""
                QLineEdit {{
                    background: {Colors.HOVER_BG};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 4px;
                    color: {Colors.TEXT_PRIMARY};
                    padding: 2px 6px;
                    {font_size_css(11)}
                    {get_font_family_css()}
                }}
                QLineEdit:focus {{
                    border: 1px solid {Colors.TEXT_ACCENT};
                }}
                QLineEdit::placeholder {{
                    color: {Colors.INPUT_PLACEHOLDER};
                }}
            """)

        ThemeRefreshCoordinator.timer_end("total")

    def _apply_synced_model_selection(self):
        """gitee 配置同步完成后：把本窗口模型选择刷新为云端 llm_selected_model。

        仅在同步恢复路径调用（由 ConfigSyncService.settingsRestored 信号驱动），
        满足"模型选择跟随同步"；不影响多窗口各自的独立切换（正常切换走
        _on_model_selected_from_popup，不改 _current_provider_name 的窗口隔离语义）。

        语义（#4 修正）：仅"本窗口用户从未手动选过模型"的窗口在同步时跟随云端；
        手动选过模型的窗口保持自身选择，不被覆盖（见 _user_manually_selected_model）。
        """
        if getattr(self, "_is_destroyed", False):
            return
        if getattr(self, "_user_manually_selected_model", False):
            # 本窗口用户已手动选过模型 → 保持自身选择：仅重建 _valid_configs
            # （"窗口自身优先"分支会保留 old_provider），不跟随云端 SelectedModel。
            self._load_model_configs()
        else:
            # 本窗口从未手动选过模型 → 跟随云端：force_global 跳过"窗口自身优先"，
            # 改从云端 llm_selected_model 取默认（无效则由 _load_model_configs 回退列表第一项）。
            self._load_model_configs(force_global=True)
        self._update_model_selector_btn()
        self._refresh_context_usage_indicator()

    def _load_model_configs(self, force_global: bool = False):
        # 检查窗口是否仍然有效，防止在初始化期间窗口被关闭后继续执行
        if getattr(self, "_is_destroyed", False):
            return

        # 保存当前窗口的实例级选择状态（多窗口隔离的关键：优先保持自身选择）
        old_provider = self._current_provider_name
        old_model = self._current_model_name

        self._valid_configs.clear()

        saved_providers = self.cfg.llm_saved_providers.value or {}
        # 计算同名分组的后缀映射（与 ProviderListSettingCard._refresh_items 一致）：
        # 唯一配置：无后缀；多个同名：第 1 个无后缀，第 2/3/... 个显示 #2/#3/...
        name_groups: dict[str, list[str]] = {}
        for cid, info in saved_providers.items():
            pname = info.get("provider_name", cid)
            name_groups.setdefault(pname, []).append(cid)
        suffix_map: dict[str, int] = {}
        for pname, cids in name_groups.items():
            if len(cids) == 1:
                suffix_map[cids[0]] = 0
            else:
                for idx, cid in enumerate(cids):
                    suffix_map[cid] = idx

        for config_id in saved_providers:
            config = saved_providers[config_id].copy()
            config.pop("备注", None)
            config.pop("获取地址", None)
            # 关键修复：用 provider_name 字段（人类可读名）合并默认配置，
            # 而不是用 config_id（UUID）—— 否则新字段（如思考模式）无法被默认配置补充
            pname = config.get("provider_name", config_id)
            default_config = FREE_PROVIDERS.get(pname, {})
            for default_key, default_value in default_config.items():
                if default_key not in config:
                    config[default_key] = default_value
            # 附加 display_name（含后缀）供 UI 显示使用，不持久化
            # 优先使用用户填的"配置名称"（name），空则回退到 provider_name
            base_name = config.get("name", "") or pname
            idx = suffix_map.get(config_id, 0)
            config["display_name"] = base_name if idx == 0 else f"{base_name} #{idx + 1}"
            self._valid_configs[config_id] = config

        # 恢复或设置当前选中的服务商和模型
        # 优先级：窗口自身选择 > 全局默认 > 列表第一个
        # 关键修复：多窗口场景下，不应让全局配置覆盖窗口自己的选择
        if old_provider and old_provider in self._valid_configs and not force_global:
            # 优先保持当前窗口已有的选择（多窗口独立）
            self._current_provider_name = old_provider
            self._current_model_name = old_model
        else:
            # 当前选择无效（可能被删除），回退到全局默认或列表第一个
            saved_model = self.cfg.llm_selected_model.value
            if saved_model and saved_model in self._valid_configs:
                self._current_provider_name = saved_model
            else:
                self._current_provider_name = list(self._valid_configs.keys())[0] if self._valid_configs else ""

            # 回退时从配置中获取默认模型名称
            if self._current_provider_name:
                provider_config = self._valid_configs.get(self._current_provider_name, {})
                self._current_model_name = provider_config.get("模型名称", "")
            else:
                self._current_model_name = ""

        self._update_model_selector_btn()
        self._refresh_context_usage_indicator()

    def _load_agent_list(self):
        """加载智能体列表到按钮组（仅显示 primary agents）"""
        # 防重复调用：showEvent 和 session 创建都可能触发，避免无意义重复
        if getattr(self, "_loading_agent_list", False):
            return

        # 检查窗口是否仍然有效，防止在初始化期间窗口被关闭后继续执行
        if getattr(self, "_is_destroyed", False):
            return

        if not self.backend.agent_manager:
            return

        # ⚠️ 即使按钮组不存在（工具开关模式下智能体切换 UI 已移除），
        # 仍需同步 _current_agent 到 ChatEngine，否则 PermissionResolver 会使用错误的默认 agent
        if not hasattr(self, "_agent_btn_group"):
            # 🐛 团队邮件角色 build 修复：团队窗口加入时 _on_agent_changed 可能因
            # chat_engine 未就绪提前 return，_current_agent 保持默认 "build"；
            # 此处优先取团队角色（_team_agent_name），团队窗口不再回退到 build，
            # 保证 set_current_agent → set_team_context 写入正确成员名。
            team_agent = getattr(self, "_team_agent_name", "") or ""
            self._current_agent = team_agent or getattr(self, "_current_agent", "build")
            if self.backend.chat_engine:
                self.backend.set_current_agent(self._current_agent)
                logger.info(
                    f"[_load_agent_list] No button group, synced ChatEngine._current_agent = {self._current_agent}"
                )
            return

        # 以上为防护性提前返回，以下为实际主逻辑
        self._loading_agent_list = True
        try:
            self._suppress_agent_intro = True
            agents = self.backend.get_primary_agents()
            buttons = self._agent_btn_group.buttons()
            default_agent = getattr(self, "_current_agent", "build")

            # 更新按钮文本和提示
            for i, agent in enumerate(agents):
                if i < len(buttons):
                    btn = buttons[i]
                    btn.setText(agent.name)
                    btn.setToolTip(agent.description)

            # 根据当前智能体选中对应按钮
            found = False
            for i, agent in enumerate(agents):
                if i < len(buttons) and agent.name == default_agent:
                    buttons[i].setChecked(True)
                    self._update_agent_button_style(default_agent)
                    found = True
                    logger.info(f"[_load_agent_list] Found match for {default_agent}, btn_id={i}")
                    break

            if not found:
                # 如果没找到匹配的，默认选中第一个
                logger.warning(
                    f"[_load_agent_list] {default_agent} not found, using agents[0]={agents[0].name if agents else 'None'}"
                )
                if buttons:
                    buttons[0].setChecked(True)
                    self._current_agent = agents[0].name if agents else "build"
                    self._update_agent_button_style(self._current_agent)

            # 同步 ChatEngine 的 agent
            if self.backend.chat_engine:
                self.backend.set_current_agent(self._current_agent)
                logger.info(f"[_load_agent_list] Synced ChatEngine._current_agent = {self._current_agent}")

            self._suppress_agent_intro = False
        finally:
            self._loading_agent_list = False

    def _update_agent_button_style(self, active_agent: str):
        """更新智能体按钮样式"""
        if not hasattr(self, "_agent_buttons"):
            return
        for name, data in self._agent_buttons.items():
            btn = data["btn"]
            if name == active_agent:
                btn.setStyleSheet(data["selected_style"])
            else:
                btn.setStyleSheet(data["style"])

    def _on_agent_changed(self, agent_name: str, skip_welcome: bool = False):
        """智能体切换处理

        Args:
            agent_name: 目标智能体名
            skip_welcome: True 时跳过欢迎卡片重建（批量解散场景：窗口即将
                关闭，重建 QWebEngineView 100-500ms/窗口纯属浪费）。
        """
        if getattr(self, "_is_destroyed", False):
            return
        if not agent_name or not self.backend.chat_engine:
            return

        logger.info(f"[_on_agent_changed] Switching from {self._current_agent} to {agent_name}")

        self._current_agent = agent_name
        self.backend.switch_agent(agent_name)
        self._update_agent_status(agent_name)
        # 🛡️ 失效欢迎卡片缓存：卡片内容依赖 _current_agent（智能体名称/描述）。
        # 切换智能体不影响当前会话消息，但若欢迎卡片正在显示需重建。
        self._invalidate_welcome_card()
        if self._displayed_session_id is None and not skip_welcome:
            # 当前正显示欢迎卡片（空会话），立即重建
            self._show_initial_welcome()

    def _on_input_area_height_changed(self):
        """输入框高度变化时同步调整卡片高度

        textChanged 触发的路径已由 input_area._adjust_height_to_content()
        内部一次完成（只动输入框高度，父卡片由布局自动撑大，无抖动）。
        此函数仅作为入口，实际工作转发给 _adjust_height_to_content，
        避免在两处各自手动 setFixedHeight 父卡片导致重复布局重算。
        """
        if not hasattr(self, "input_area"):
            return
        if getattr(self, "_is_destroyed", False):
            return
        try:
            self.input_area._adjust_height_to_content()
        except Exception:
            pass

    def _show_agent_intro(self, agent_name: str):
        """显示智能体介绍卡片"""
        if not self.backend.agent_manager:
            return
        agent = self.backend.get_agent(agent_name)
        if not agent:
            return

        intro_md = f"""\
### 🤖 已切换到智能体：{agent.name}

{agent.description}

"""
        card = MessageCard(parent=self, role="assistant", timestamp="系统")
        card.update_content(intro_md)
        card.finish_streaming()
        self._add_chat_widget(card)
        self._scroll_to_bottom()

    def _update_agent_status(self, agent_name: str):
        """更新智能体状态显示（按钮组模式下主要更新按钮提示）"""
        if getattr(self, "_is_destroyed", False):
            return
        agent = self.backend.get_agent(agent_name)
        if agent:
            mode = agent.mode or "(未声明)"
            hidden = "hidden" if agent.hidden is True else "visible"
            tooltip = f"{agent.name}: {agent.description}\nMode: {mode}, {hidden}"
            # 更新按钮组的 tooltip
            if hasattr(self, "_agent_buttons") and agent_name in self._agent_buttons:
                self._agent_buttons[agent_name]["btn"].setToolTip(tooltip)

    def _create_new_session(self):
        import time as _time

        _t0 = _time.perf_counter()
        # 检查窗口是否仍然有效，防止在初始化期间窗口被关闭后继续执行
        if getattr(self, "_is_destroyed", False):
            logger.debug("[OpenAIChatToolWindow] Window destroyed before session creation, skipping")
            return
        try:
            from PyQt5 import sip

            if sip.isdeleted(self):
                logger.debug("[OpenAIChatToolWindow] C++ object deleted before session creation, skipping")
                return
        except Exception:
            pass

        # 🛡️ 失效欢迎卡片缓存（必须在 _clear_chat_area 之前同步执行，
        # 避免 sip.isdeleted 竞态导致 _show_initial_welcome 命中旧缓存）。
        # 缓存内容依赖 _current_project/_current_agent，新建会话时这两个字段
        # 可能因项目切换 / 智能体切换而变化，必须重建。
        self._invalidate_welcome_card()

        if self._is_auto_loop_running:
            InfoBar.warning(
                "AutoLoop",
                "运行中无法新建会话，请先停止 AutoLoop",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            return
        if self._is_streaming and self.backend.chat_engine:
            # 🐛 修复（切换项目/新建会话打断对话）：
            # 1) stop_streaming 返回的中断消息（partial 回复快照）必须应用回 session，
            #    否则被打断的会话缺最后部分回复（对齐 _on_auto_compact_requested /
            #    _on_stop_clicked 的成熟写法）。
            # 2) 主动复位 AI 状态为 idle：stop 后 worker 的 stream_finished 会被
            #    _on_worker_finished 因 is_streaming=False 忽略，_on_stream_finished
            #    永不触发 → _set_ai_state("idle") 永不执行 → TabPanel 边框动画
            #    停留在 streaming（"正在对话"模式不消失）。
            try:
                interrupted = self.backend.stop_streaming()
                if interrupted:
                    self._apply_interrupted_messages_to_session(interrupted)
                    # 中断消息已应用到 session，确保后续 _auto_save_current_session 不跳过
                    self._session_dirty = True
            except Exception as e:
                logger.warning(f"[MainWidget] 新建会话停止流式失败: {e}")
            self._set_ai_state("idle")
            # 🐛 修复：新建任务打断流式时收尾团队邮件，避免 _team_processing 死锁。
            # 取消路径下 worker 不发射 finished_with_content → 绑定其的 _on_stream_finished
            # 永不触发 → _team_processing 卡 True、正在处理的邮件卡 running、A 后续发来
            # 的新邮件被 _check_and_process_pending 的 _team_processing 守卫跳过，B 完全
            # 收不到。复用手动停止的成熟收尾逻辑（必须在 create_session 前，旧 session
            # 判定口径正确）。非团队窗口 _current_team_mail 为 None 且 get_running_tasks
            # 返回空，调用安全。
            try:
                self._sync_team_mail_on_stop()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[MainWidget] 新建会话收尾团队邮件失败: {e}")

        # 🛡️ 标记会话切换：stop_streaming 后 worker 仍可能在跨线程事件循环里
        # 投递 _on_messages_updated / _on_stream_finished / _do_post_stream_cleanup
        # 等旧回调。此时 _current_session_id 已被下方 create_session 更新到新会话，
        # 这些回调若继续执行会把旧会话的消息写到新会话，再被 _do_post_stream_cleanup
        # 错误地保存到切换后的新项目下，出现「同一对话在两个项目下都出现」的副本 bug。
        # 哨兵一直保持 True，直到下一次 _on_send_clicked 真正发起新会话的 AI 请求时清零。
        self._session_switched = True

        self._is_streaming = False
        self._topic_summary_cancelled = True  # 🛡️ 取消标题生成重试
        self._toggle_send_stop(False)

        if self._sub_agent_compact_widget:
            self._sub_agent_compact_widget.clear()
            self._sub_agent_compact_widget.setVisible(False)

        try:
            self._auto_save_current_session()
        except Exception:
            logger.exception("Failed to auto-save current session before creating a new session")
        _t1 = _time.perf_counter()

        self._cache_current_session_cards()
        _t2 = _time.perf_counter()
        # 清空批量渲染索引，避免虚拟滚动定时器触发时遍历到已移出布局的旧卡片产生虚假警告
        self._batch_cards = []
        self._message_batch = []
        self._visible_batch_start = 0
        self._visible_batch_end = 0
        # 停止虚拟滚动定时器，防止 processEvents 或其他时机触发回收旧卡片
        self._virtual_scroll_timer.stop()

        # 💡 内存优化：新建会话时清理全局 LRU 缓存，释放旧会话渲染/估算数据
        _cleanup_global_lru_caches()

        # 🛡️ 标记待注入：create_session 内部会同步触发 SessionStart hook 并
        # 通过 _hook_messages_updated → _on_messages_updated 回传，此时
        # _session_switched=True 会误拦截合法输出。置此标记让 _on_messages_updated 放行。
        self._pending_session_hook = True
        try:
            session = self.backend.create_session()
        finally:
            self._pending_session_hook = False
        _t3 = _time.perf_counter()

        # 💡 内存优化：释放旧会话在 HistoryManager 中的消息数据（可被 SQLite 恢复）
        # 在 create_session 之后执行，确保 _evict_if_needed 已淘汰旧会话
        self._current_session_id = session.session_id
        self._history_preview_messages = None
        self._clear_chat_area()
        self.title_edit.setText("新对话")
        self.node_preview.clear_nodes()
        # 新会话无用户消息，徽章应隐藏（clear_nodes 绕过了 _update_node_preview）
        self._update_history_questions_badge()
        if self._todo_floating_widget:
            self._todo_floating_widget.clear()
        self.backend.clear_todo_list()
        self.backend.set_session_context(self._current_session_id)
        if self._question_floating_widget:
            self._question_floating_widget.clear()
        self._question_tool_call_id = None
        self._load_agent_list()
        self._release_inactive_session_messages()
        _t4 = _time.perf_counter()

        logger.info(
            f"[Perf-CreateSession] "
            f"auto_save={(_t1 - _t0) * 1000:.0f}ms "
            f"cache_cards={(_t2 - _t1) * 1000:.0f}ms "
            f"backend_create={(_t3 - _t2) * 1000:.0f}ms "
            f"ui_cleanup={(_t4 - _t3) * 1000:.0f}ms "
            f"total={(_t4 - _t0) * 1000:.0f}ms"
        )

        # 同步对话框窗口标题（新会话默认名称如"对话 07-25 14:19"）
        self._sync_dialog_title()
        # 欢迎卡片渲染（QWebEngineView 100-500ms 主线程占用）改为交错时间片调度：
        # 并发新建 N 个会话时避免 N 个 welcome 在同一事件批次连续渲染卡死 UI（C2）。
        self._schedule_initial_welcome()
        self._refresh_context_usage_indicator()
        # 🆕 刷新历史面板：建新会话时旧会话已被 _auto_save_current_session 入库，
        # 但历史面板 UI 未收到信号 → 列表停留在保存前快照（旧会话缺失）。
        # 仅历史卡片可见时执行（不可见时 0 开销）。
        refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

    def _release_inactive_session_messages(self):
        """释放非活跃会话在 HistoryManager 中的消息数据。

        从长对话新建会话后，旧会话的消息数据仍缓存在 HistoryManager._history_sessions
        中占用内存。将其标记为可释放（messages 置空），后续访问时从 SQLite 按需重载。
        """
        if not hasattr(self, "session_manager") or not self.session_manager:
            return
        active_ids = {s.session_id for s in self.session_manager.get_all_sessions()}
        released = 0
        try:
            for session in self.history_manager._history_sessions:
                sid = session.get("session_id")
                if sid and sid not in active_ids and session.get("messages"):
                    session["messages"] = []
                    released += 1
            if released:
                from loguru import logger

                logger.debug(f"[Memory] 已释放 {released} 个非活跃会话的消息数据")
        except Exception:
            pass

    def _display_current_session(self):
        session = self.session_manager.get_current_session()
        if not session:
            self._clear_chat_area()
            return

        self.title_edit.setText(session.topic_summary or session.name or "新对话")
        # 同步对话框窗口标题（便于 Windows 任务栏区分各窗口）
        self._sync_dialog_title()

        # 关键修复：同步 _current_session_id 与实际显示的会话
        self._current_session_id = session.session_id

        if self._restore_cached_session_cards(session):
            self._update_node_preview()
            self._refresh_context_usage_indicator()
            # 恢复缓存卡片后，多次滚动确保在底部
            self._scroll_to_bottom(sticky_ms=900)
            # 滚动完成后同步时间线节点到最后一个
            QTimer.singleShot(100, self._sync_node_preview_to_last)
            return

        self._clear_chat_area()
        self._message_batch = group_messages_for_display(session.messages)
        # 初始化 batch_cards：每个batch对应一个卡片列表，None表示已回收
        self._batch_cards = [None for _ in self._message_batch]
        # 重建 user 前缀和缓存（用于 O(1) 的 round_index 计算）
        self._build_user_prefix_cache()
        self._visible_batch_end = len(self._message_batch)
        self._visible_batch_start = max(0, self._visible_batch_end - self._initial_visible_batch_count)

        if not self._message_batch:
            self._show_initial_welcome()
            return

        self._load_message_batch(initial=True)
        # 同步 batch 结构：确保 _batch_cards 和 _message_index 与布局一致
        self._sync_batch_structures()

    def _show_initial_welcome(self):
        """仅在UI上显示欢迎卡片，不改动Session数据"""
        self._clear_chat_area(delete_widgets=False)
        welcome_card = self._get_or_create_welcome_card()
        self._displayed_session_id = None
        self._add_chat_widget(welcome_card)

    _WELCOME_SLOT_MS = 50  # 相邻窗口 welcome 渲染的最小间隔
    _WELCOME_SLOT_COUNT = 20  # 槽位数：50ms × 20 = 1000ms 上限轮转

    def _schedule_initial_welcome(self):
        """QTimer 交错调度欢迎卡片渲染（C2：并发会话创建不卡 UI）

        背景：_get_or_create_welcome_card 缓存未命中时重建 QWebEngineView，
        每窗占用主线程 100-500ms。团队新建任务/恢复会并发触发 N 个
        _create_new_session，若全部用 singleShot(0) 会在同一事件循环批次内
        连续渲染 N 个 welcome，界面卡死数十秒。

        做法：类级计数器分配 0/50/100/.../950ms 槽位（模 20 轮转），让 N 个
        窗口的渲染请求均匀散开。单窗场景槽位为 0ms（立即，与原来 singleShot(0)
        无感知差异）；类级而非实例级，确保不同窗口拿到递增槽位。
        """
        cls = type(self)
        slot = getattr(cls, "_welcome_slot", 0) + 1
        setattr(cls, "_welcome_slot", slot)
        delay = ((slot - 1) % self._WELCOME_SLOT_COUNT) * self._WELCOME_SLOT_MS
        QTimer.singleShot(delay, lambda: self._safe_timer_call(self._show_initial_welcome))

    def _hide_welcome_cards(self):
        """从布局中移除所有欢迎卡片（widget 不删除，由 _welcome_card_cache 管理）"""
        for i in range(self.chat_layout.count() - 1, -1, -1):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if getattr(widget, "_is_welcome", False):
                    self.chat_layout.removeWidget(widget)
                    widget.hide()

    def _rerender_welcome_card(self):
        """同 mode 重渲染欢迎卡片 body（不重建 QWebEngineView）

        场景：workdir 延迟同步（启动 2s / 项目切换）完成后，看板类 tab
        （project-dashboard）依赖 project_root，需强制重渲染才能拿到新值。
        set_welcome_mode(mode) 同 mode 也会走 _render_welcome_with_body，
        仅重写 markdown，不重建 QWebEngineView（省 100-500ms 主线程）。
        """
        card = self._welcome_card_cache.get(self._window_id)
        if card is None:
            return
        try:
            from PyQt5 import sip

            if sip.isdeleted(card):
                return
        except Exception:
            pass
        mode = getattr(card, "_welcome_mode", "")
        if not mode:
            return
        if mode in ("sessions", "changelog"):
            # 内置 mode 不依赖 project_root/workdir，跳过重渲染。
            # 否则每次 workdir 同步（启动 2s / 项目切换）都会全量重渲染：
            # _render_welcome_with_body 每次生成随机 greeting → markdown/HTML
            # 必然变化 → updateContent 重建 DOM → sessions 卡片进入动画
            # 重复播放一遍（stagger fade-in 出现"播两遍"）。
            return
        try:
            card.set_welcome_mode(mode)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[_rerender_welcome_card] re-render failed: {e}")

    def _invalidate_welcome_card(self):
        """显式失效欢迎卡片缓存（pop + delete widget）

        修复 bug：_welcome_card_cache 仅按 _window_id 缓存，但卡片内容依赖
        _current_project 和 _current_agent。切换项目/智能体/会话数据时必须
        主动失效，否则会展示上一个项目/智能体的陈旧数据。

        性能：失效后下次显示会重建 QWebEngineView（~100-500ms 主线程占用）。
        同窗口内无变化的重复调用仍走缓存命中路径，性能优化保留。
        之所以不能仅靠 sip.isdeleted 兜底：_clear_chat_area 之后
        QTimer.singleShot(0, _show_initial_welcome) 存在竞态——
        若 singleShot 回调先于 deleteLater 真正执行，sip.isdeleted 仍为 False，
        缓存命中即返回旧卡片，导致「新建/切项目后欢迎卡片数据不刷新」。

        调用点：
        - _on_project_selected 切换 _current_project 之后
        - _on_agent_changed 切换 _current_agent 之后
        - _create_new_session 入口（确保 _show_initial_welcome 走重建路径）
        - _on_archived_session_deleted / _renamed 之后（recent_sessions 列表已变化）
        """
        cached = self._welcome_card_cache.pop(self._window_id, None)
        if cached is None:
            return
        try:
            from PyQt5 import sip

            if sip.isdeleted(cached):
                return
        except Exception:
            pass
        # 从父控件摘除（如果还在布局里）。这里不调 removeWidget，因为
        # _clear_chat_area/deleteLater 路径会处理；摘 setParent 已经能断
        # 干净引用，避免下一帧布局刷新时再访问一个已被本流程标记为"待删"的 widget。
        try:
            if cached.parent() is not None:
                cached.setParent(None)
        except Exception:
            pass
        cached.deleteLater()

    def _load_message_batch(self, initial: bool = False):
        """按当前可见窗口加载消息。"""
        session = self.session_manager.get_current_session()
        if session:
            self._displayed_session_id = session.session_id
            # 关键修复：同步 _current_session_id 与实际显示的会话
            self._current_session_id = session.session_id

        visible_batches = self._message_batch[self._visible_batch_start : self._visible_batch_end]
        self._suspend_auto_scroll = not initial
        self._loading_session = True  # 标记加载状态，懒渲染期间保持滚动
        self._initial_scroll_to_bottom = False  # 重置滚底标记，让首次懒渲染强制滚底
        try:
            self._render_message_to_card(
                visible_batches,
                batch_offset=self._visible_batch_start,
            )
        finally:
            self._suspend_auto_scroll = False

        # 节点预览和滚动同步在懒渲染完成后处理
        self._update_node_preview()
        QTimer.singleShot(100, self._sync_node_preview_to_last)
        self._refresh_context_usage_indicator()
        QTimer.singleShot(500, lambda: gc.collect())

    def _has_more_history_batches(self) -> bool:
        return self._visible_batch_start > 0

    def _load_more_history_batches(self):
        if self._is_loading_history_batches or not self._has_more_history_batches():
            return

        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        previous_value = scroll_bar.value()
        previous_height = self.chat_container.sizeHint().height()

        new_start = max(0, self._visible_batch_start - self._incremental_visible_batch_count)
        prepend_batches = self._message_batch[new_start : self._visible_batch_start]
        # 🛡️ B9 防御：回收路径只清 _batch_cards 保留 _message_batch，
        # 但极端情况（旧数据/切会话竞态）可能残留 None，过滤兜底防崩溃
        prepend_batches = [b for b in prepend_batches if b is not None]
        if not prepend_batches:
            return

        self._is_loading_history_batches = True
        self._visible_batch_start = new_start
        self._render_message_to_card(
            prepend_batches,
            insert_at_top=True,
            batch_offset=new_start,
        )
        self._sync_node_preview_to_scroll()

        def restore_anchor():
            try:
                new_height = self.chat_container.sizeHint().height()
                scroll_bar.setValue(previous_value + max(0, new_height - previous_height))
            finally:
                self._is_loading_history_batches = False

        QTimer.singleShot(0, restore_anchor)

    def _recycle_out_of_view_batches(self):
        """回收超出可视缓冲区范围的批次UI，只保留数据，节省内存
        同时确保当前可视范围内的批次都已经懒渲染完成

        防闪烁设计：
        - 回收前记录滚动位置，回收上方卡片后补偿偏移量，防止视口跳动
        - 使用 delete_widgets_from_layout 立即从布局移除，避免延迟导致高度突变
        - 跳过当前流式输出中的卡片
        """
        if self._is_virtual_recycling or len(self._batch_cards) == 0:
            return

        self._is_virtual_recycling = True
        try:
            # 计算可视缓冲区范围
            buffer_batches = self._incremental_visible_batch_count * self._virtual_scroll_buffer
            active_start = (
                0 if self._visible_batch_start <= buffer_batches else self._visible_batch_start - buffer_batches
            )
            active_end = self._visible_batch_end + buffer_batches

            # 第一步：确保当前激活范围内所有卡片都已经懒渲染完成
            lazy_render_count = 0
            for batch_idx in range(active_start, active_end):
                if batch_idx >= len(self._batch_cards):
                    continue
                cards = self._batch_cards[batch_idx]
                if not cards:
                    continue
                for card in cards:
                    if isinstance(card, MessageCard) and not getattr(card, "_lazy_rendered", True):
                        card.ensure_rendered()
                        lazy_render_count += 1

            # 第二步：回收超出缓冲区的批次
            recycled_count = 0
            recycled_card_ids = set()

            # ── 收集上方（历史方向）需要回收的卡片 ──
            above_widgets = []
            above_removed_height = 0
            for batch_idx in range(0, active_start):
                if self._batch_cards[batch_idx] is not None:
                    cards = self._batch_cards[batch_idx]
                    # 如果批次包含当前流式输出的助手卡片，跳过整个批次
                    if cards and self._current_assistant_card in cards:
                        continue
                    if cards:
                        for card in cards:
                            if isinstance(card, MessageCard) and self._is_widget_alive(card):
                                above_removed_height += card.height()
                                recycled_card_ids.add(id(card))
                                above_widgets.append(card)
                        recycled_count += 1
                    # 🛡️ B9 修复：只卸载 UI（_batch_cards=None），
                    # 保留 _message_batch 数据（上滚回可重建，数据不丢失）
                    self._batch_cards[batch_idx] = None

            # ── 收集下方（未来方向）需要回收的卡片 ──
            below_widgets = []
            for batch_idx in range(active_end, len(self._batch_cards)):
                if self._batch_cards[batch_idx] is not None:
                    cards = self._batch_cards[batch_idx]
                    if cards and self._current_assistant_card in cards:
                        continue
                    if cards:
                        for card in cards:
                            if isinstance(card, MessageCard) and self._is_widget_alive(card):
                                recycled_card_ids.add(id(card))
                                below_widgets.append(card)
                        recycled_count += 1
                    # 🛡️ B9 修复：只卸载 UI，保留数据
                    self._batch_cards[batch_idx] = None

            # ── 执行回收（从布局移除 + deleteLater）──
            if above_widgets:
                # 滚动位置补偿：回收上方卡片后，容器高度减少，需同步降低滚动值
                scroll_bar = self.chat_scroll_area.verticalScrollBar()
                old_scroll = scroll_bar.value()
                delete_widgets_from_layout(above_widgets, self.chat_layout)
                # 补偿滚动值：减去已移除的上方卡片总高度
                scroll_bar.setValue(max(0, old_scroll - above_removed_height))

            if below_widgets:
                delete_widgets_from_layout(below_widgets, self.chat_layout)

            # 从懒渲染队列中移除已回收的卡片，避免对已销毁的 widget 调用 ensure_rendered
            if recycled_card_ids:
                self._pending_lazy_cards = [
                    c for c in self._pending_lazy_cards if id(c) not in recycled_card_ids and self._is_widget_alive(c)
                ]

            # 回收完成，如果有回收触发GC
            if recycled_count > 0 or lazy_render_count > 0:
                logger.debug(
                    f"[virtual-scroll] 懒渲染 {lazy_render_count}，回收 {recycled_count} 个离屏批次（含数据清理）"
                )
                if recycled_count > 0:
                    QTimer.singleShot(100, lambda: gc.collect())

        finally:
            self._is_virtual_recycling = False

    # ── B4 温和层：WebEngine 并发页上限（T12 蓝图） ──

    def _unload_batch(self, batch_idx: int) -> int:
        """卸载一个批次的 UI（释放 WebEngine renderer），保留 _message_batch 数据。

        B4 强回收层：卸载前收集本批 renderer PID 入 _unloaded_pids 队列
        （温和层只登记不 kill；内存超阈值时由 _maybe_strong_recycle 统一 kill）。

        Args:
            batch_idx: 批次索引

        Returns:
            被移除卡片的总高度（用于滚动补偿）
        """
        if not (0 <= batch_idx < len(self._batch_cards)):
            return 0
        cards = self._batch_cards[batch_idx]
        if not cards:
            return 0
        # 强回收：卸载前收集存活卡片的 renderer PID（PID>0 才登记）
        now = time.time()
        for card in cards:
            pid = getattr(card, "_renderer_pid", 0) or 0
            if pid > 0 and self._is_widget_alive(card):
                self._unloaded_pids.append((pid, now, batch_idx))
        removed_h = 0
        alive_cards = []
        for card in cards:
            if isinstance(card, MessageCard) and self._is_widget_alive(card):
                try:
                    removed_h += card.height()
                except RuntimeError:
                    pass
                alive_cards.append(card)
        if alive_cards:
            delete_widgets_from_layout(alive_cards, self.chat_layout, call_cleanup=True)
        # 🛡️ B9：只置 _batch_cards=None（卸载 UI），保留 _message_batch 数据
        self._batch_cards[batch_idx] = None
        # 只减已渲染的卡数（_rendered_card_count 语义 = 已渲染未卸载）
        rendered_in_batch = sum(1 for c in cards if getattr(c, "_lazy_rendered", False))
        self._rendered_card_count = max(0, self._rendered_card_count - rendered_in_batch)
        return removed_h

    def _batch_is_protected(self, batch_idx: int) -> bool:
        """判断批次是否受保护（不可淘汰）：
        - 可视区 ±1 批（刚滚出/即将滚入，重建成本高）
        - 包含当前流式输出助手卡片的批次
        - 欢迎卡片所在批次
        """
        if not (0 <= batch_idx < len(self._batch_cards)):
            return True
        cards = self._batch_cards[batch_idx]
        if not cards:
            return False
        # 可视区 ±1
        if batch_idx >= max(0, self._visible_batch_start - 1) and batch_idx <= self._visible_batch_end + 1:
            return True
        # 当前流式卡
        if self._current_assistant_card is not None and self._current_assistant_card in cards:
            return True
        # 欢迎卡
        for card in cards:
            if getattr(card, "_is_welcome", False):
                return True
        return False

    def _batch_distance(self, batch_idx: int) -> int:
        """批次到可视区的距离（批次数，0 = 在可视区）。"""
        if batch_idx < self._visible_batch_start:
            return self._visible_batch_start - 1 - batch_idx
        if batch_idx > self._visible_batch_end:
            return batch_idx - (self._visible_batch_end + 1)
        return 0

    def _recycle_lru_batches(self):
        """B4 温和层：并发页超限时按「距可视区最远优先」淘汰批次 UI。

        仅回收超过 _max_rendered_cards 的部分；受保护批次跳过；
        每卸一批做滚动补偿（setValue(值 - removed_h)）防止视口跳动。
        """
        if self._is_virtual_recycling or not self._batch_cards:
            return
        # 计数校准（可选增强）：每 20 次调用重算一次实际计数，防漂移
        self._recycle_lru_call_count += 1
        if self._recycle_lru_call_count % 20 == 1:
            # 计数校准：只统计已懒渲染的卡（_batch_cards 可能含已创建未渲染卡）
            actual = 0
            for cards in self._batch_cards:
                if cards:
                    actual += sum(1 for c in cards if getattr(c, "_lazy_rendered", False))
            self._rendered_card_count = actual

        if self._rendered_card_count <= self._max_rendered_cards:
            return

        over = self._rendered_card_count - self._max_rendered_cards
        # 候选：所有非空批次（跳过受保护），按距离降序（最远先淘汰）
        candidates = []
        for idx, cards in enumerate(self._batch_cards):
            if not cards:
                continue
            if self._batch_is_protected(idx):
                continue
            candidates.append((self._batch_distance(idx), idx))
        candidates.sort(key=lambda x: x[0], reverse=True)

        self._is_virtual_recycling = True
        try:
            scroll_bar = self.chat_scroll_area.verticalScrollBar()
            removed_total = 0
            for dist, idx in candidates:
                if self._rendered_card_count <= self._max_rendered_cards:
                    break
                removed_h = self._unload_batch(idx)
                removed_total += removed_h
            if removed_total > 0:
                try:
                    scroll_bar.setValue(max(0, scroll_bar.value() - removed_total))
                except RuntimeError:
                    pass
                logger.debug(
                    f"[B4-recycle] 淘汰 {len(candidates)} 候选中的超限批次，"
                    f"_rendered_card_count={self._rendered_card_count}/{self._max_rendered_cards}"
                )
        finally:
            self._is_virtual_recycling = False

        # ★ B4 强回收层：温和淘汰后接续检查内存阈值，超限则 kill 离屏 renderer
        self._maybe_strong_recycle()

    # ── B4 强回收层：内存超阈值时 kill 离屏 renderer 进程（T13 蓝图） ──

    @staticmethod
    def _kill_renderer(pid: int) -> bool:
        """终止指定 renderer 进程（PID 复用误杀防护：校验 cmdline 含
        QtWebEngineProcess 且 --type=renderer 才 kill）。

        Returns:
            True = 已成功终止（或进程已不存在）
        """
        if pid <= 0:
            return False
        try:
            import psutil

            p = psutil.Process(pid)
            cmd = " ".join(p.cmdline() or [])
            if "QtWebEngineProcess.exe" not in cmd or "--type=renderer" not in cmd:
                return False  # PID 复用误杀防护
            p.terminate()
            try:
                p.wait(timeout=1.0)
            except psutil.TimeoutExpired:
                p.kill()
            return True
        except psutil.NoSuchProcess, psutil.AccessDenied:
            return False
        except Exception:
            return False

    def _is_active_window_for_recycle(self) -> bool:
        """多标签护栏：本窗口处于激活状态时不 kill（用户正在查看/即将滚动回来）。

        Tab 模式下当前窗口 isActiveWindow → 活跃不 kill；其他窗口活跃时，
        本窗口的离屏 renderer 可以安全 kill。
        """
        try:
            return self.isActiveWindow()
        except Exception:
            return True

    def _web_children_rss_mb(self) -> float:
        """统计当前进程 WebEngine 相关子进程（qwebengine/QtWebEngineProcess）总 RSS。

        T30 双判据的 WebEngine 侧：renderer/GPU/network 子进程各自数百 MB，
        累计 RSS 反映 WebEngine 内存压力是否值得强回收（kill 离屏 renderer）。
        无 psutil 或找不到子进程时返回 0.0（退化：不触发 WebEngine 判据）。
        """
        try:
            import psutil

            self_proc = psutil.Process()
            total = 0.0
            for child in self_proc.children(recursive=True):
                try:
                    name = (child.name() or "").lower()
                    if "qwebengine" in name or "webengine" in name or "chrome" in name:
                        total += child.memory_info().rss / (1024 * 1024)
                except Exception:
                    continue
            return total
        except Exception:
            return 0.0

    def _over_memory_threshold(self) -> bool:
        """内存阈值判定（T30 双判据）：主进程 RSS > _MEM_THRESHOLD_TOTAL_MB
        且（WebEngine 子进程 RSS > _WEB_MEM_THRESHOLD_MB 或子进程统计不可用时
        退化单判据）→ True。

        无 psutil 时退化判定：并发页 > _MAX_RENDERED_CARDS 且存在
        距可视区 ≥ _OFFSCREEN_BATCHES_FOR_KILL 的已卸载批次（说明内存压力来自 WebEngine）。
        """
        try:
            import psutil

            rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
            if rss_mb <= _MEM_THRESHOLD_TOTAL_MB:
                return False
            # 主进程已超总阈值：再核对 WebEngine 侧，避免误杀
            web_mb = self._web_children_rss_mb()
            if web_mb > 0 and web_mb < _WEB_MEM_THRESHOLD_MB:
                return False
            return True
        except Exception:
            # 退化：有已卸载 PID 且存在远距批次 + 并发页仍超限
            if not self._unloaded_pids:
                return False
            offscreen = any(
                (self._batch_distance(idx) if 0 <= idx < len(self._batch_cards) else 0) >= _OFFSCREEN_BATCHES_FOR_KILL
                for _, _, idx in self._unloaded_pids
            )
            return offscreen and self._rendered_card_count > self._max_rendered_cards

    def _kill_lru_unloaded_renderers(self, keep: int = None) -> int:
        """按卸载时间升序（最老在前）kill 超 keep 部分，每轮 ≤ _KILL_BATCH_MAX。

        Returns:
            成功 kill 的数量
        """
        if keep is None:
            keep = _LRU_RENDERER_KEEP  # 默认值运行时解析（类定义时模块常量未就绪）
        if not self._unloaded_pids:
            return 0
        # 按卸载时间升序（最老在前）→ kill 最老的「超出 keep」部分（LRU 语义）
        sorted_pids = sorted(self._unloaded_pids, key=lambda x: x[1])
        kill_count = max(0, len(sorted_pids) - keep)
        kill_list = sorted_pids[:kill_count]
        killed = 0
        remaining = []
        kill_set = set()
        for entry in kill_list[:_KILL_BATCH_MAX]:
            pid, ts, idx = entry
            kill_set.add(pid)
            if self._kill_renderer(pid):
                killed += 1
        # 重写队列：保留 keep 内 + 不在本轮 kill 集合的
        for entry in sorted_pids:
            if entry[0] in kill_set:
                continue
            remaining.append(entry)
        self._unloaded_pids = remaining
        if killed:
            logger.debug(f"[B4-strong] kill {killed} 个离屏 renderer，队列剩余 {len(self._unloaded_pids)}")
            # [B4-2] 强回收计数日志：warning 级便于观测；带总阈值/WebEngine 阈值上下文
            try:
                web_mb = self._web_children_rss_mb()
                logger.warning(
                    f"[B4-2] strong-recycle kill {killed} renderer(s) (queue {len(self._unloaded_pids)}), "
                    f"web_rss={web_mb:.0f}MB threshold={_WEB_MEM_THRESHOLD_MB}MB"
                )
            except Exception:
                logger.warning(f"[B4-2] strong-recycle kill {killed} renderer(s), queue {len(self._unloaded_pids)}")
        return killed

    def _maybe_strong_recycle(self):
        """强回收触发入口（T13）：_unloaded_pids 非空 + 非活跃窗口 + 冷却就绪 + 超阈值。

        kill 后更新 _last_kill_at 冷却时间戳。
        """
        if not self._unloaded_pids:
            return
        if self._is_active_window_for_recycle():
            return
        now = time.time()
        if now - self._last_kill_at < _KILL_COOLDOWN_S:
            return
        if not self._over_memory_threshold():
            return
        self._last_kill_at = now
        self._kill_lru_unloaded_renderers(keep=_LRU_RENDERER_KEEP)

    def _open_diff_viewer(self):
        """打开差异查看窗口，显示当前会话修改文件的 git diff"""
        try:
            # 获取当前会话 ID
            session_id = self._current_session_id
            if not session_id:
                InfoBar.warning(
                    "提示",
                    "当前没有活动会话",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.TOP_RIGHT,
                )
                return

            # 从 ToolExecutor 获取当前会话的文件操作记录
            if not self.backend.tool_executor:
                InfoBar.warning(
                    "提示",
                    "工具执行器未初始化",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.TOP_RIGHT,
                )
                return

            from app.utils.file_operation_recorder import FileOperationRecorder

            file_recorder = FileOperationRecorder(self.session_store)

            # 获取当前会话的所有文件操作
            operations = file_recorder.get_all_operations_for_session(session_id)

            if not operations:
                InfoBar.warning(
                    "提示",
                    "当前会话没有文件修改记录",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.TOP_RIGHT,
                )
                return

            # 提取文件路径列表（去重）
            file_paths = list({op.get("file_path") for op in operations if op.get("file_path")})

            if not file_paths:
                InfoBar.warning(
                    "提示",
                    "未找到修改的文件",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.TOP_RIGHT,
                )
                return

            # 生成 git diff
            try:
                from app.utils.diff_viewer import DiffHtmlGenerator

                diff_output = DiffHtmlGenerator.get_diff_for_files(file_paths, session_id)
            except Exception as e:
                logger.warning(f"[DiffViewer] 获取 git diff 失败: {e}")
                diff_output = ""

            # 生成 HTML 报告
            html = DiffHtmlGenerator.generate_html_report(diff_output or "", session_id)

            # 内嵌显示差异（覆盖右侧对话区域，类似系统设置）
            from app.widgets.cards.global_card_controller import get_global_card_controller

            controller = get_global_card_controller()
            if controller is not None:
                controller.show_diff_viewer(html)
                logger.info(f"[DiffViewer] 已内嵌显示差异面板，文件数: {len(file_paths)}")
            else:
                # 回退：弹窗模式
                from app.utils.diff_viewer import DiffViewerWindow

                viewer = DiffViewerWindow(parent=self)
                viewer.load_html(html)
                viewer.show()
                logger.info(f"[DiffViewer] 已打开差异查看窗口，文件数: {len(file_paths)}")

        except ImportError as e:
            logger.error(f"[DiffViewer] 导入模块失败: {e}")
            InfoBar.warning(
                "错误",
                f"功能加载失败: {str(e)}",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.TOP_RIGHT,
            )
        except Exception as e:
            logger.exception(f"[DiffViewer] 打开差异查看器失败: {e}")
            InfoBar.warning(
                "错误",
                f"打开差异查看器失败: {str(e)}",
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.TOP_RIGHT,
            )

    def _schedule_gc_hook(self):
        """GC 钩子（T9）：防抖合并触发全局缓存清理 + 堆回收。

        150ms singleShot 合并高频路径（清空聊天区/关闭窗口/清空快捷键），
        只在窗口相关大块内存释放后触发一次，避免每帧同步执行。
        """
        global _gc_hook_pending
        if _gc_hook_pending:
            return
        _gc_hook_pending = True
        QTimer.singleShot(150, _run_gc_hook)

    def _clear_chat_area(self, delete_widgets: bool = True):
        self._current_assistant_card = None
        self._displayed_session_id = None
        self._visible_batch_start = 0
        self._visible_batch_end = 0
        self._is_loading_history_batches = False
        self._pending_lazy_cards.clear()
        self._lazy_batch_timer_active = False
        # ★ B4：清空聊天区 = 本窗口已渲染卡片数归零（_batch_cards 由调用方重建）
        self._rendered_card_count = 0
        # 先收集所有 widget（不能在迭代中修改 layout），再统一处理
        widgets = []
        while self.chat_layout.count():
            item = self.chat_layout.takeAt(0)
            if item and item.widget():
                widgets.append(item.widget())
        for widget in widgets:
            if delete_widgets:
                # cleanup 释放 WebEngine renderer（viewer.cleanup → setHtml("")）；
                # removeWidget + 解除父引用后 deleteLater 才能让 renderer 自然退出
                if hasattr(widget, "cleanup"):
                    try:
                        widget.cleanup()
                    except Exception:
                        pass
                self.chat_layout.removeWidget(widget)
                try:
                    widget.setParent(None)
                except Exception:
                    pass
                widget.deleteLater()
            else:
                widget.hide()
        # 清理全局 LRU 渲染缓存
        if delete_widgets:
            clear_global_render_cache()
        # GC 钩子：防抖触发全局缓存清理 + 堆回收
        if delete_widgets:
            self._schedule_gc_hook()

    def _take_chat_widgets(self) -> List[QWidget]:
        """从布局中取出所有 widgets，返回列表（不删除，由调用方负责删除）"""
        widgets: List[QWidget] = []
        self._current_assistant_card = None
        self._displayed_session_id = None
        while self.chat_layout.count():
            item = self.chat_layout.takeAt(0)
            if item and item.widget():
                w = item.widget()
                # 跳过欢迎卡片：它由 _welcome_card_cache 独立管理，
                # 不应被 _cache_current_session_cards 的 deleteLater 误删。
                if getattr(w, "_is_welcome", False):
                    w.hide()
                    continue
                w.hide()
                widgets.append(w)
        return widgets

    def _cache_current_session_cards(self):
        """
        切换会话时彻底清理当前会话的卡片，释放内存。

        性能优化：移除 QApplication.processEvents()（实测在切换项目中阻塞 UI 事件循环 ~100ms+）；
        deleteLater() 本身是延迟删除的，由 Qt 下一轮事件循环自然消费即可。
        全局渲染缓存清理 / 进程堆压缩也推到 singleShot(0) 下一帧，避免同步阻塞主线程。
        """
        # 从布局中取出所有 widgets
        widgets = self._take_chat_widgets()
        self._pending_lazy_cards.clear()
        self._lazy_batch_timer_active = False

        current_session = self.session_manager.get_current_session()
        if current_session:
            self._session_card_cache[current_session.session_id] = {
                "batch_count": len(self._message_batch),
                "visible_batch_start": self._visible_batch_start,
                "visible_batch_end": self._visible_batch_end,
                "has_widgets": bool(widgets),
                "cached_at": time.time(),
            }

        # 缓存淘汰：写入后立即按 MAX_SESSION_CARD_CACHE_SIZE 淘汰过期条目，
        # 防止 _session_card_cache 随会话切换无限增长（复活 _cleanup_session_card_cache）。
        try:
            self._cleanup_session_card_cache()
        except Exception:
            pass

        # 彻底删除所有卡片及其子资源
        for widget in widgets:
            if isinstance(widget, MessageCard) and self._is_widget_alive(widget):
                # 调用卡片自己的 cleanup 方法
                widget.cleanup()
            # 解除父引用：takeAt 仅摘除布局项，widget 仍挂在父控件树下，
            # QWebEngineView 的 renderer 进程不会自然退出；setParent(None)
            # 结束父引用后 deleteLater 才能释放 renderer
            try:
                if widget.parent() is not None:
                    widget.setParent(None)
            except Exception:
                pass
            widget.deleteLater()

        # 推迟到下一事件循环：清理 Markdown 渲染 LRU 缓存 + 压缩进程堆。
        # 这两个操作虽各自不重，但 merge 进主线程后会切走 ~50-100ms；
        # 切项目这一帧只先释放 widget 引用，把重活放到下一帧。
        QTimer.singleShot(
            0,
            lambda: (
                clear_global_render_cache(),
                _cleanup_global_lru_caches(),
                _compact_process_heap_after_cleanup(),
            ),
        )

    def _cleanup_session_card_cache(self):
        from app.constants import (
            MAX_SESSION_CARD_CACHE_SIZE,
        )

        all_session_ids = {s.session_id for s in self.session_manager.get_all_sessions()}
        cleanup_stale_card_cache(self._session_card_cache, all_session_ids, MAX_SESSION_CARD_CACHE_SIZE)

    def _is_widget_alive(self, widget: Optional[QWidget]) -> bool:
        """检查 widget 是否存活（保留向后兼容）"""
        return is_widget_alive(widget)

    def _restore_cached_session_cards(self, session: ChatSession) -> bool:
        if not session.messages:
            return False

        cache_entry = self._session_card_cache.get(session.session_id)
        if not cache_entry:
            return False
        if not isinstance(cache_entry, dict):
            self._session_card_cache.pop(session.session_id, None)
            return False
        cached_cards = cache_entry.pop("cards", None)
        if cached_cards:
            for card in cached_cards:
                if hasattr(card, "cleanup"):
                    try:
                        card.cleanup()
                    except Exception:
                        pass
                if hasattr(card, "deleteLater"):
                    try:
                        card.deleteLater()
                    except Exception:
                        pass
            self._session_card_cache.pop(session.session_id, None)
            return False
        batch_count = len(group_messages_for_display(session.messages))
        if cache_entry.get("batch_count") != batch_count:
            self._session_card_cache.pop(session.session_id, None)
            return False

        # 轻量快照只保留元信息，实际恢复仍由 session.messages 重建
        return False

        alive_cards, removed = filter_alive_cards(cached_cards)
        if removed:
            self._session_card_cache.pop(session.session_id, None)
        if not alive_cards:
            return False

        self._clear_chat_area(delete_widgets=False)
        for card in alive_cards:
            self._add_chat_widget(card)
        self._displayed_session_id = session.session_id
        # 关键修复：同步 _current_session_id 与实际显示的会话
        self._current_session_id = session.session_id
        self._current_assistant_card = alive_cards[-1] if alive_cards and alive_cards[-1].role == "assistant" else None
        self._message_batch = group_messages_for_display(session.messages)
        # 重建 user 前缀和缓存（用于 O(1) 的 round_index 计算）
        self._build_user_prefix_cache()
        # 同步 _batch_cards 长度
        self._sync_batch_structures()
        # 从缓存卡片的 _message_index 重建 _batch_cards 引用（缓存卡片已有正确的索引）
        for card in alive_cards:
            mi = getattr(card, "_message_index", None)
            if mi is not None and 0 <= mi < len(self._batch_cards):
                if self._batch_cards[mi] is None:
                    self._batch_cards[mi] = []
                if card not in self._batch_cards[mi]:
                    self._batch_cards[mi].append(card)
        self._visible_batch_start = max(0, int(cache_entry.get("visible_batch_start", 0)))
        self._visible_batch_end = min(
            len(self._message_batch),
            int(cache_entry.get("visible_batch_end", len(self._message_batch))),
        )
        # 延迟恢复所有助手卡片的差异统计（避免文件 I/O 阻塞首屏）
        for card in alive_cards:
            if card.role == "assistant":
                QTimer.singleShot(0, lambda c=card: self._update_card_diff_stats(c))
        return True

    def _get_or_create_welcome_card(self) -> MessageCard:
        # P1-4：按 _window_id 缓存 welcome 卡片（窗口维度复用，同窗口重复调用不重建）。
        # 不能做跨窗口全局单例：recent_sessions/top_by_count 按当前项目过滤，
        # 多窗口共享会串数据。缓存命中时跳过 QWebEngineView 重建（省 100-500ms 主线程占用）。
        cached = self._welcome_card_cache.get(self._window_id)
        if cached is not None:
            try:
                from PyQt5 import sip

                if not sip.isdeleted(cached):
                    return cached
            except Exception:
                pass
            self._welcome_card_cache.pop(self._window_id, None)

        agent = self.backend.get_agent(self._current_agent)
        agent_name = agent.name if agent else ""
        agent_desc = agent.description if agent else ""

        # 获取最近会话和最多消息的会话用于欢迎卡片（按当前项目过滤）
        history_list = self.history_manager.get_history_list(self._current_project)
        # 🛡️ 过滤团队会话：欢迎卡片展示的是"可继续的独立对话"，团队会话
        # （team_run_id 非空）由历史面板团队分组/合并条目统一管理，逐条混入
        # 欢迎卡片会导致成员会话重复展示且点击后团队上下文丢失。与历史面板
        # get_history_list(merge_team=True) 的语义对齐：团队会话不进入推荐列表。
        history_list = [s for s in history_list if not (s.get("team_run_id") or "").strip()]

        # 最近会话（按时间排序，取前6；欢迎卡片首屏 2×2 + 折叠区「展开更多」）
        recent_sessions = []
        for session in history_list[:6]:
            recent_sessions.append(
                {
                    "title": session.get("title"),
                    "last_time": session.get("last_time"),
                    "session_id": session.get("session_id"),
                    "message_count": session.get("message_count", 0),
                }
            )

        # 最多消息的会话（取前6，双列 3 行）
        top_sessions = heapq.nlargest(6, history_list, key=lambda x: x.get("message_count", 0))
        top_by_count = []
        for session in top_sessions:
            top_by_count.append(
                {
                    "title": session.get("title"),
                    "last_time": session.get("last_time"),
                    "session_id": session.get("session_id"),
                    "message_count": session.get("message_count", 0),
                }
            )

        # 首次构建后按窗口缓存；会话数据变更点（删除/重命名等）可调用失效（见遗留）
        # 模式：sessions（默认）/ changelog / 插件注册 tab
        from app.utils.config import Settings

        from app.core.ui_plugin_registry import UIPluginRegistry

        cfg = Settings.get_instance()
        welcome_mode = resolve_initial_welcome_mode(
            cfg.welcome_mode.value,
            cfg.welcome_plugin_tab.value,
            UIPluginRegistry.get_instance().get_welcome_tabs(),
        )

        welcome_card = create_welcome_card(
            self,
            agent_name,
            agent_desc,
            recent_sessions,
            top_by_count,
            mode=welcome_mode,
            context_provider=self._build_ui_context,
        )
        welcome_card._is_welcome = True
        welcome_card.contextActionRequested.connect(self.handle_recommended_question)
        # PyQt 层模式切换 → 持久化到 app.config（不重建卡片，避免 QWebEngine 重建开销）
        welcome_card.welcomeModeChanged.connect(self._on_welcome_mode_changed)
        self._welcome_card_cache[self._window_id] = welcome_card
        return welcome_card

    def _on_welcome_mode_changed(self, new_mode: str):
        """欢迎卡片右上角 segmented tabs 切换回调：写 QSettings"""
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        if new_mode in ("sessions", "projects", "changelog"):
            # 内置 mode：写 welcome_mode 并清空插件 tab 记忆
            if cfg.welcome_plugin_tab.value:
                cfg.welcome_plugin_tab.value = ""
            if cfg.welcome_mode.value == new_mode:
                return
            cfg.welcome_mode.value = new_mode
        else:
            # 插件注册的 welcome tab：welcome_mode 的 OptionsValidator.correct
            # 会把非法值纠正回 sessions，插件 tab 存独立字段。重启后仅当该 tab
            # 仍注册时恢复（见 resolve_initial_welcome_mode），插件卸载/停用则
            # 回退内置 mode，不影响正常启动。
            if cfg.welcome_plugin_tab.value == new_mode:
                return
            cfg.welcome_plugin_tab.value = new_mode
        cfg.save()

    def _sanitize_user_message_for_display(self, content: str) -> str:
        """清理用户消息用于显示（保留向后兼容）"""
        return sanitize_user_message_for_display(content)

    def _get_user_round_index_for_batch_index(self, batch_index: int, batch_offset: int = 0) -> int:
        """
        计算给定 batch 索引对应的 round_index（user 轮次索引）

        逻辑：
        - 对于 user batch：round_index = 前面有多少个 user batch
        - 对于 assistant batch：round_index = 前面有多少个 user batch - 1

        优化：使用前缀和缓存实现 O(1) 复杂度（每次 _message_batch 更新时重建缓存）

        Args:
            batch_index: batch 在 _message_batch 中的索引
            batch_offset: 当前加载批次的起始偏移量（用于分批加载历史消息）

        Returns:
            round_index：从 0 开始的用户轮次索引
        """
        global_batch_index = batch_index

        # 边界检查
        if global_batch_index >= len(self._message_batch):
            return 0

        # 使用前缀和缓存 O(1) 获取 user 数量
        user_count = self._user_prefix_cache[global_batch_index]

        # 对于 assistant/tool batch，round_index 需要减 1
        batch = self._message_batch[global_batch_index]
        if not batch:
            return user_count
        current_role = batch[0].get("role")
        if current_role != "user":
            user_count = max(0, user_count - 1)

        return user_count

    def _build_user_prefix_cache(self) -> None:
        """
        构建 user 数量的前缀和缓存数组

        _user_prefix_cache[i] = 前 i 个 batch 中有多少个 user
        prefix[0] = 0（表示"前 0 个 batch 中的 user 数量"）
        prefix 长度 = len(_message_batch) + 1
        """
        self._user_prefix_cache = [0]
        for batch in self._message_batch:
            is_user = batch and batch[0].get("role") == "user"
            self._user_prefix_cache.append(self._user_prefix_cache[-1] + (1 if is_user else 0))

    def _refresh_all_cards_round_index(self):
        """
        删除/撤销操作后，重新同步所有存活 user 卡片的 _round_index。

        ⚠️ 不能只遍历 chat_layout，因为懒加载/回收的卡片不在布局中。
        必须基于 _batch_cards 遍历，使用 _message_batch 计算正确的 round_index。

        注意：清理 _batch_cards 中对已删除卡片的引用，防止 RuntimeError。
        """
        cleaned_any = False
        for batch_idx, cards in enumerate(self._batch_cards):
            if not cards:
                continue
            # 防止 _batch_cards 比 _message_batch 长（删除操作后未同步）
            if batch_idx >= len(self._message_batch):
                continue

            # 过滤掉已删除的卡片（sip.isdeleted）
            alive_cards = [w for w in cards if not sip.isdeleted(w)]
            if len(alive_cards) != len(cards):
                self._batch_cards[batch_idx] = alive_cards if alive_cards else None
                cleaned_any = True
                if not alive_cards:
                    continue
                cards = alive_cards

            for widget in cards:
                if not isinstance(widget, MessageCard):
                    continue
                if getattr(widget, "_is_welcome", False):
                    continue
                if widget.role == "user":
                    widget._round_index = self._get_user_round_index_for_batch_index(batch_idx)

        if cleaned_any:
            logger.debug("[RefreshRound] Cleaned up deleted card references from _batch_cards")

    # ==================== Batch 结构同步 ====================

    def _sync_batch_structures(self):
        """
        同步 _message_batch 与 session.messages 的当前状态。
        仅在发送新消息或流式完成后调用。
        不改变任何卡片上的 _message_index（渲染代码已正确设置），
        只保持 _message_batch / _batch_cards 长度与 session 一致。
        """
        session = self.session_manager.get_current_session()
        if not session:
            return

        new_batch = group_messages_for_display(session.messages)
        self._message_batch = new_batch
        self._build_user_prefix_cache()

        # 扩展/裁剪 _batch_cards 到新长度
        new_len = len(new_batch)
        if new_len > len(self._batch_cards):
            self._batch_cards.extend([None] * (new_len - len(self._batch_cards)))
        elif new_len < len(self._batch_cards):
            self._batch_cards = self._batch_cards[:new_len]

    def _rebuild_batch_cards_from_layout(self) -> None:
        """
        从 chat_layout 中存活卡片按顺序重建 _batch_cards。

        删除中间 round 后，_message_batch 已被 group_messages_for_display 截断为新长度，
        但 chat_layout 中后面 round 的卡片（如 c_u2）仍存活。

        不能用 _sync_batch_structures 的裁剪逻辑（_batch_cards[:new_len]），
        因为这会丢掉仍存活但 batch_idx 已偏移的后面 round 卡片，
        导致后续它们的 _round_index 永远无法被 _refresh_all_cards_round_index 更新。

        策略：
        1. 重置 _batch_cards 为 len(_message_batch) 个 None
        2. 遍历 chat_layout 中所有存活的非 welcome MessageCard（按 layout 顺序）
        3. 按顺序分配到 _batch_cards，同步更新每个 card 的 _message_index
        """
        new_len = len(self._message_batch)
        new_batch_cards: List[Optional[List[MessageCard]]] = [None] * new_len

        # 收集 layout 中所有存活的非 welcome MessageCard（按 layout 顺序）
        alive_cards: List[MessageCard] = []
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if sip.isdeleted(widget):
                continue
            alive_cards.append(widget)

        # 按 layout 顺序分配到 _batch_cards
        for batch_idx, card in enumerate(alive_cards):
            if batch_idx >= new_len:
                # 防御：layout 卡片数不应超过 _message_batch 长度
                logger.warning(
                    f"[Delete] _rebuild_batch_cards_from_layout: alive cards "
                    f"({len(alive_cards)}) > _message_batch length ({new_len}), "
                    f"dropping card at layout position {batch_idx}"
                )
                break
            new_batch_cards[batch_idx] = [card]
            card._message_index = batch_idx

        self._batch_cards = new_batch_cards
        logger.debug(
            f"[Delete] _rebuild_batch_cards_from_layout: {len(alive_cards)} alive cards → {new_len} batch slots"
        )

    def _fix_new_card_message_index(self, user_text: str = None):
        """
        为布局中尚未设置 _message_index 的卡片分配正确的 batch index。
        在发送新消息后调用，因为 _append_user_message / _append_assistant_message
        不设置 _message_index（渲染路径 _render_message_to_card 才设置）。

        关键：必须处理所有无 _message_index 的卡片，而非只处理第一个。
        """
        # 收集所有无 _message_index 的卡片（按布局顺序，即时间顺序）
        unassigned = []
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if getattr(widget, "_message_index", None) is not None:
                continue
            unassigned.append(widget)

        if not unassigned:
            return

        # 构建 _message_batch 中每个 batch 的分配状态：
        # 已有 _batch_cards 引用且非 None 的 batch 表示已被占用
        assigned_batch_indices = set()
        for batch_idx, cards in enumerate(self._batch_cards):
            if cards is not None and len(cards) > 0:
                # 检查 cards 中是否有存活的 widget
                for c in cards:
                    if self._is_widget_alive(c):
                        assigned_batch_indices.add(batch_idx)
                        break

        # 对每个未分配的卡片，按角色从后向前在 _message_batch 中找到
        # 未被占用的同角色 batch
        for widget in unassigned:
            target_batch_idx = -1
            for batch_idx in range(len(self._message_batch) - 1, -1, -1):
                if batch_idx in assigned_batch_indices:
                    continue
                batch = self._message_batch[batch_idx]
                if not batch:
                    continue
                if batch[0].get("role") == widget.role:
                    target_batch_idx = batch_idx
                    break

            if target_batch_idx >= 0:
                # 找到了未被占用的同角色 batch
                widget._message_index = target_batch_idx
                assigned_batch_indices.add(target_batch_idx)
                if target_batch_idx < len(self._batch_cards):
                    if self._batch_cards[target_batch_idx] is None:
                        self._batch_cards[target_batch_idx] = []
                    if widget not in self._batch_cards[target_batch_idx]:
                        self._batch_cards[target_batch_idx].append(widget)
            else:
                # 没有可匹配的 batch，在末尾追加独占槽位
                new_batch_idx = len(self._message_batch)
                self._message_batch.append([])
                self._batch_cards.append([])
                widget._message_index = new_batch_idx
                self._batch_cards[new_batch_idx].append(widget)
                assigned_batch_indices.add(new_batch_idx)

    def _restore_meta_from_batch(self, card, batch: list) -> None:
        """从历史消息 batch 中恢复元信息（耗时和 token 消耗）到卡片底部栏"""
        if card.role != "assistant":
            return
        elapsed = None
        for msg in batch:
            if msg.get("role") == "assistant":
                if msg.get("elapsed") is not None:
                    elapsed = msg["elapsed"]
        # 卡片 token 显示与上下文圆环同步：直接用圆环快照的 used_tokens（同一来源），
        # 不再读取落库的 msg["token_usage"]——后者是 worker 侧估算（可能基于压缩后的
        # current_messages），会远小于圆环真实占用，导致重载后卡片显示异常小的数值。
        used = getattr(self, "_reload_ctx_used_tokens", None)
        _reload_sid = getattr(self, "_reload_ctx_sid", None)
        session = self.session_manager.get_current_session()
        sid = getattr(session, "session_id", None) if session else None
        if used is None or _reload_sid != sid:
            try:
                used = self.backend.get_context_usage_snapshot(session, self._get_current_model_config()).get(
                    "used_tokens", 0
                )
            except Exception:
                used = 0
            self._reload_ctx_used_tokens = used
            self._reload_ctx_sid = sid
        token_usage = {"total": used} if used > 0 else None
        if elapsed is not None or token_usage is not None:
            card.set_meta_info(elapsed=elapsed, token_usage=token_usage)
        # 延迟刷新分隔点：等父级布局完成后再检查 isVisible()，避免加载时父级隐藏导致误判
        from PyQt5.QtCore import QTimer

        QTimer.singleShot(0, card._refresh_footer_separators)

    def _render_message_to_card(
        self,
        batches: List[List[Dict[str, Any]]],
        insert_at_top: bool = False,
        batch_offset: int = 0,
    ):
        insert_index = 0 if insert_at_top else None
        for local_index, batch in enumerate(batches):
            role = batch[0].get("role")
            timestamp = batch[0].get("timestamp") or get_default_timestamp()
            model_name = batch[0].get("model_name")
            global_batch_index = batch_offset + local_index
            round_index = self._get_user_round_index_for_batch_index(global_batch_index, batch_offset)

            # 检查该batch是否已经渲染过并且被回收了
            if self._batch_cards[global_batch_index] is not None:
                # 已经渲染过，卡片已经存在，不需要重新创建
                # 但是需要确保已经添加到布局（如果是滚动回来加载更多）
                cards = self._batch_cards[global_batch_index]
                if cards and insert_index is not None:
                    for card in cards:
                        if self._is_widget_alive(card):
                            if role == "user":
                                self.chat_layout.insertWidget(insert_index, card, 0, Qt.AlignRight)
                            else:
                                self.chat_layout.insertWidget(insert_index, card)
                            insert_index += 1
                continue

            # 需要重新创建
            cards = []
            if role == "user":
                content = self._sanitize_user_message_for_display(batch[0].get("content", ""))
                user_card = self._append_user_message(
                    content,
                    timestamp=timestamp,
                    scroll=False,
                    insert_index=insert_index,
                    user_round_index=round_index,
                    update_preview=not insert_at_top,
                )
                if user_card:
                    # 设置 message_index 用于卡片差异功能
                    user_card._message_index = global_batch_index
                    cards.append(user_card)
                if insert_index is not None and user_card:
                    insert_index += 1

            if role == "assistant" or role == "tool":
                # 🛡️ batch[0] 可能是 tool 消息，不含 provider_name/model_name
                # 此时需要从 batch 中的 assistant 消息提取
                if role == "assistant":
                    provider_name = batch[0].get("provider_name")
                    effective_model_name = model_name
                    history_config_id = batch[0].get("config_id")
                else:
                    provider_name = None
                    effective_model_name = model_name  # 可能在前面已从 assistant 消息提取
                    history_config_id = None
                    for m in batch:
                        if m.get("role") == "assistant":
                            if m.get("provider_name"):
                                provider_name = m["provider_name"]
                            if m.get("config_id"):
                                history_config_id = m["config_id"]
                            if not effective_model_name and m.get("model_name"):
                                effective_model_name = m["model_name"]
                            break
                assistant_card = self._append_assistant_message(
                    timestamp=timestamp,
                    scroll=False,
                    insert_index=insert_index,
                    round_index=round_index,
                    model_name=effective_model_name,
                    provider_name=provider_name,
                    config_id=history_config_id,
                )
                if assistant_card:
                    # 设置 message_index 用于卡片差异功能
                    assistant_card._message_index = global_batch_index
                    cards.append(assistant_card)
                    # 使用辅助函数渲染消息
                    render_batch_to_assistant_card(assistant_card, batch)
                    # 从 batch 中恢复元信息（耗时和 token）
                    self._restore_meta_from_batch(assistant_card, batch)
                    # 延迟恢复差异统计（避免文件 I/O 阻塞首屏渲染）
                    QTimer.singleShot(0, lambda c=assistant_card: self._update_card_diff_stats(c))
                if insert_index is not None and assistant_card:
                    insert_index += 1

            # 保存卡片引用到 batch_cards
            self._batch_cards[global_batch_index] = cards if cards else None

        # 批量处理懒渲染：渲染完所有卡片后再统一触发，避免每次都触发滚动
        # 收集需要懒渲染的卡片
        pending_lazy_cards = []
        for batch_idx in range(batch_offset, batch_offset + len(batches)):
            if batch_idx >= len(self._batch_cards):
                break
            cards = self._batch_cards[batch_idx]
            if cards:
                for card in cards:
                    if isinstance(card, MessageCard) and not getattr(card, "_lazy_rendered", False):
                        pending_lazy_cards.append(card)

        # 批量触发懒渲染，使用延迟加载减少卡顿
        # 同一卡片可能已在队列中（如滚动回来触发的重新加载），需去重
        existing_ids = {id(c) for c in self._pending_lazy_cards}
        for card in pending_lazy_cards:
            if id(card) not in existing_ids:
                self._pending_lazy_cards.append(card)
                existing_ids.add(id(card))
        if pending_lazy_cards and not self._lazy_batch_timer_active:
            self._lazy_batch_timer_active = True
            QTimer.singleShot(0, self._process_next_lazy_batch)

    def _get_rendered_message_cards(self) -> List[MessageCard]:
        def is_user_or_assistant(widget):
            return widget.role in ("user", "assistant")

        return collect_message_cards_from_layout(self.chat_layout, is_user_or_assistant)

    def _process_next_lazy_batch(self):
        """批量懒渲染：16ms 时间片内处理尽量多卡片，减少 WebEngine 创建开销

        批量处理期间合并 heightChanged 信号，批次完成后统一触发布局更新和滚底。
        [PERF] 由固定 BATCH_SIZE 改为时间片预算：单批最多运行 16ms，超时即停，
        未渲染的卡片放回队首下批继续——避免大量卡片排队时单批无界撑爆主线程
        （T22 实测 lazy batch 占首切卡顿兜底项）。
        """
        TIME_SLICE_MS = 16

        if not self._pending_lazy_cards:
            self._loading_session = False
            self._lazy_batch_timer_active = False
            return

        # 时间片内取出尽量多有效卡片（单张 ensure_rendered 通常 <1ms）
        batch = []
        deadline = time.monotonic() + TIME_SLICE_MS / 1000.0
        while self._pending_lazy_cards:
            if time.monotonic() >= deadline:
                break
            card = self._pending_lazy_cards.pop(0)
            if self._is_widget_alive(card) and not getattr(card, "_lazy_rendered", False):
                batch.append(card)

        if not batch:
            # 没有需要渲染的有效卡片，继续下一批
            if self._pending_lazy_cards:
                QTimer.singleShot(0, self._process_next_lazy_batch)
            else:
                self._loading_session = False
                self._lazy_batch_timer_active = False
            return

        # 批量渲染所有卡片（渲染同样受时间片保护：超时未消费的放回队首）
        consumed = 0
        for card in batch:
            if time.monotonic() >= deadline:
                break
            card.ensure_rendered()
            consumed += 1
        if consumed < len(batch):
            self._pending_lazy_cards[:0] = batch[consumed:]

        # ★ B4 温和层：渲染后统一计数，超限时下一帧触发 LRU 淘汰
        # 只统计实际消费渲染的卡片（batch[:consumed]），避免 _rendered_card_count 虚高
        new_count = sum(1 for c in batch[:consumed] if getattr(c, "_lazy_rendered", False))
        if new_count > 0:
            self._rendered_card_count += new_count
            global _global_rendered_pages
            _global_rendered_pages += new_count
            if self._rendered_card_count > self._max_rendered_cards:
                QTimer.singleShot(0, lambda: self._recycle_lru_batches())

        # 批次全部渲染完成，统一更新滚动
        # 🐛 修复：用户已主动滚离底部时禁止在此强制置底——否则向上滚动
        # 到顶加载历史批次时，懒渲染完成会把视口拉回底部（滚轮自动置底回归）。
        if self._loading_session and not self._user_intentionally_away_from_bottom:
            scroll_bar = self.chat_scroll_area.verticalScrollBar()
            if not self._initial_scroll_to_bottom or scroll_bar.value() >= scroll_bar.maximum() - 50:
                scroll_bar.setValue(scroll_bar.maximum())
                self._initial_scroll_to_bottom = True

        # [PERF] 继续处理下一批：80ms 间隔给 Chromium 进程喘息空间，避免批量
        # 创建 QWebEngineView 时进程初始化压力集中导致卡顿
        # 同时增大间隔让 Qt 事件循环有机会处理悬而未决的绘制事件
        if self._pending_lazy_cards:
            QTimer.singleShot(80, self._process_next_lazy_batch)
        else:
            self._loading_session = False
            self._lazy_batch_timer_active = False
            # 🐛 修复：所有懒渲染批次完成后，卡片仍在异步报告高度，
            # layout 持续扩展但不再触发滚底。用 sticky 模式在接下来
            # 900ms 内持续滚动到底部，覆盖卡片异步高度上报期。
            # 但用户已主动滚离底部时跳过——否则向上滚动加载历史批次
            # 完成后会把视口强制拉回底部（滚轮自动置底回归）。
            if self._initial_scroll_to_bottom and not self._user_intentionally_away_from_bottom:
                self._scroll_to_bottom(sticky_ms=900)

    def _get_current_user_round_index(self) -> int:
        """获取当前 user message 应该是第几个 user（从 0 开始）
        基于session消息计算，而非布局中渲染的卡片数量，避免动态加载导致索引错误

        口径（与全仓统一，Bug1）：TeamMail 算作 user round（渲染为卡片），
        其他 hook（SessionStart 等）不算。
        """
        session = self.session_manager.get_current_session()
        if session:
            return sum(
                1
                for msg in session.messages
                if msg.get("role") == "user" and (not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail")
            )
        # fallback: 从布局计数
        return count_user_cards_in_layout(self.chat_layout)

    def _find_user_round_index_for_card(self, card: MessageCard) -> Optional[int]:
        """
        通过遍历布局找到 user card 对应的 round_index
        """
        # 优先通过 _batch_cards 查找（避免懒加载/回收导致布局遍历计数错误）
        for batch_idx, cards in enumerate(self._batch_cards):
            if not cards:
                continue
            if batch_idx >= len(self._message_batch):
                continue
            for widget in cards:
                if widget is card:
                    return self._get_user_round_index_for_batch_index(batch_idx)

        # 降级：通过布局遍历（可能因懒加载/回收导致计数错误）
        user_card_idx = 0
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if widget is card:
                return user_card_idx
            if widget.role == "user":
                user_card_idx += 1
        return None

    def findRoundIndexForCard(self, card: MessageCard) -> Optional[int]:
        """
        供 MessageCard 回调使用，根据 assistant card 查找对应的 round_index。
        通过遍历布局找到该 assistant card 前面的 user card 数量来确定 round_index。
        """
        if not card or card.role != "assistant":
            return None
        # 遍历布局，统计该 assistant card 之前有多少 user card
        round_index = 0
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if widget is card:
                # 找到了，返回当前 round_index
                return round_index
            if widget.role == "user":
                round_index += 1
        return None

    def _find_user_round_index_from_session(
        self,
        session,
        user_text: str,
        timestamp: str,
    ) -> Optional[int]:
        """
        从 session 数据中找到 user 消息对应的 round_index。

        通过在 session.messages 中定位 user 消息，然后计算它是第几个 user。

        Args:
            session: ChatSession 对象
            user_text: 用户消息的纯文本内容
            timestamp: 用户消息的时间戳

        Returns:
            round_index 或 None
        """
        return find_user_round_index(session, user_text, timestamp)

    def _remove_cards_for_round(self, round_index: int) -> bool:
        session = self.session_manager.get_current_session()
        if not session:
            return False

        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)
        if round_index < 0 or round_index >= len(round_ranges):
            return False

        start_idx, end_idx = round_ranges[round_index]
        cards_to_remove = end_idx - start_idx

        user_card_idx = 0
        removed = 0
        removing = False
        widgets_to_remove = []

        # 遍历 chat_layout
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if widget.role not in ("user", "assistant"):
                continue

            if widget.role == "user":
                if user_card_idx == round_index:
                    widgets_to_remove.append(widget)
                    removed += 1
                    removing = True
                else:
                    removing = False
                user_card_idx += 1
            elif widget.role == "assistant" and removing:
                widgets_to_remove.append(widget)
                removed += 1

            if removed >= cards_to_remove:
                break

        logger.info(f"[DELETE] Cards to remove: {len(widgets_to_remove)}, cards_to_remove: {cards_to_remove}")

        # 使用辅助函数执行删除
        delete_widgets_from_layout(widgets_to_remove, self.chat_layout)
        return removed > 0

    def _remove_cards_from_round(self, round_index: int) -> bool:
        """从指定 round 开始删除所有卡片（包括后续卡片）"""
        # 计算预期删除的卡片数量
        session = self.session_manager.get_current_session()
        cards_to_remove_hint = 0
        if session:
            from app.core import consolidate_messages, get_user_round_ranges

            canonical_messages = consolidate_messages(session.messages)
            round_ranges = get_user_round_ranges(canonical_messages)
            if round_index < len(round_ranges):
                start_idx, end_idx = round_ranges[round_index]
                cards_to_remove_hint = end_idx - start_idx

        widgets_to_remove = find_widgets_to_remove_from_round(self.chat_layout, round_index, cards_to_remove_hint)
        delete_widgets_from_layout(widgets_to_remove, self.chat_layout)

        # 关键修复：如果 UI 删除的卡片数量少于预期，清空整个聊天区域并重新渲染
        if cards_to_remove_hint > 0 and len(widgets_to_remove) < cards_to_remove_hint:
            from loguru import logger

            logger.warning(
                f"[UNDO] UI cards incomplete: deleting {len(widgets_to_remove)}/{cards_to_remove_hint}. "
                f"Clearing and re-rendering session view."
            )
            self._clear_chat_area()
            self._display_current_session()
            return False
        return len(widgets_to_remove) > 0

    def _invalidate_current_session_card_cache(self):
        invalidate_session_card_cache(self.session_manager.get_current_session(), self._session_card_cache)

    def _persist_session_after_mutation(self):
        session = self.session_manager.get_current_session()
        if not session:
            return

        session.set_messages(session.messages, preserve_compaction=False)

        if is_session_empty(session):
            if self._current_session_id is not None and self.history_manager:
                idx = self.history_manager.find_index_by_session_id(self._current_session_id)
                if idx is not None:
                    self.history_manager.archive_history(idx)
                self._current_session_id = None
            self._session_dirty = False
            return

        if self.history_manager:
            # 使用辅助函数保存会话
            # 🛡️ 传入 _current_project 作为兜底（仅当该会话从未在 SQLite/内存中出现过时生效）；
            # 已存在的会话以原有 project 为准，避免被默认项目兜底覆盖。
            self._current_session_id = save_or_archive_session(
                self.history_manager,
                session,
                self._current_session_id,
                project_fallback=self._current_project,
            )
            # 🛡️ 直接保存已处理，清除脏标记防冗余
            self._session_dirty = False

        # 🛡️ 记录截断哨兵：用于 _on_finalize_complete 识别"是否在异步 finalize 等待
        # 期间发生过截断"，避免 worker 返回的旧消息序列复活已被撤销的内容。
        self._truncation_sentinel = {
            "session_id": session.session_id,
            "messages_len": len(session.messages),
            "set_at": time.time(),
        }

    def _refresh_session_view_after_mutation(self):
        # 使用辅助函数刷新视图
        refresh_session_view(
            self,
            self._invalidate_current_session_card_cache,
            self._display_current_session,
            self._refresh_context_usage_indicator,
        )

    def _sync_current_assistant_card_ref(self):
        self._current_assistant_card = find_last_assistant_card(self.chat_layout)

    def _finalize_local_session_mutation(self):
        self._invalidate_current_session_card_cache()
        self._history_preview_messages = None
        session = self.session_manager.get_current_session()

        # [DEBUG-diagnose-welcome] 诊断欢迎卡片显示问题
        _diag_session_empty = is_session_empty(session)
        _diag_msg_count = len(session.messages) if session else 0
        logger.info(
            f"[DEBUG-diagnose-welcome] _finalize_local_session_mutation: session_empty={_diag_session_empty}, msg_count={_diag_msg_count}"
        )

        if is_session_empty(session):
            logger.info("[DEBUG-diagnose-welcome] Session is empty, will show welcome card")
            self._clear_chat_area()
            self.node_preview.clear_nodes()
            self._current_assistant_card = None
            # 🛡️ 失效欢迎卡片缓存：会话被改空（撤销/截断）后历史数据已变化，
            # 否则 _get_or_create_welcome_card 命中旧缓存 → "最近会话" 停留在旧快照。
            self._invalidate_welcome_card()
            logger.info("[DEBUG-diagnose-welcome] Before _show_initial_welcome")
            self._show_initial_welcome()
            logger.info("[DEBUG-diagnose-welcome] After _show_initial_welcome")
            self._refresh_context_usage_indicator()
            # 会话被删/撤销至空：此分支不经过 _update_node_preview，需显式刷新
            # 历史问题徽章，否则徽章会残留删除前的旧计数（count=0 时应隐藏）。
            self._update_history_questions_badge()
            return

        logger.info("[DEBUG-diagnose-welcome] Session is NOT empty, skipping welcome card")
        self._sync_current_assistant_card_ref()
        self._update_node_preview()
        self._refresh_context_usage_indicator()
        self._sync_node_preview_to_scroll()

    def _on_clear_shortcut(self):
        if getattr(self, "_is_destroyed", False):
            return
        # 🛡️ 失效欢迎卡片缓存：清空会话后历史数据已变化，避免 _get_or_create_welcome_card
        # 命中旧缓存导致"最近会话"停留在清空前的旧快照（与 _create_new_session 一致）。
        self._invalidate_welcome_card()
        # 使用辅助函数清空并显示欢迎
        clear_and_show_welcome(
            session=self.session_manager.get_current_session(),
            session_card_cache=self._session_card_cache,
            clear_chat_func=self._clear_chat_area,
            clear_preview_func=self.node_preview.clear_nodes,
            get_welcome_func=self._get_or_create_welcome_card,
            add_widget_func=lambda w: QTimer.singleShot(0, lambda: self._add_chat_widget(w)),
        )
        # GC 钩子：清空聊天区后防抖触发全局缓存清理 + 堆回收
        self._schedule_gc_hook()

    def _add_chat_widget(self, widget: QWidget, insert_index: Optional[int] = None):
        if getattr(self, "_is_destroyed", False):
            return
        if insert_index is None:
            add_message_to_layout(widget, self.chat_layout, is_widget_alive)
        else:
            if is_widget_alive(widget):
                widget.setParent(self.chat_container)
                if isinstance(widget, MessageCard) and widget.role == "user":
                    self.chat_layout.insertWidget(insert_index, widget, 0, Qt.AlignRight)
                elif isinstance(widget, MessageCard):
                    self.chat_layout.insertWidget(insert_index, widget, 0, Qt.AlignLeft)
                else:
                    self.chat_layout.insertWidget(insert_index, widget)
                widget.show()
        if isinstance(widget, MessageCard):
            try:
                widget.heightChanged.disconnect(self._on_message_card_height_changed)
            except Exception:
                pass
            widget.heightChanged.connect(self._on_message_card_height_changed)
            if self._resize_preview_active:
                widget.set_resize_preview_mode(True)
            # 🐛 修复：新增卡片宽度同样用 viewport 直接推算，避免 parent.width()
            # 被 chat_container 撑大后返回旧大值（与 _sync_visible_cards_on_scroll 一致）。
            self._sync_single_card_width(widget)

        # 🔧 内存修复：添加新卡片后触发虚拟滚动回收，
        # 否则离屏的旧 MessageCard（含 QWebEngineView）仅在手动滚动时才被回收，
        # 长时间主动聊天会导致大量卡片堆积在布局中，WebEngine 内存持续增长
        self._virtual_scroll_timer.start()

        # [PERF] 非懒渲染卡片加入懒渲染队列（如欢迎卡片 P0 优化后走此路径）
        if isinstance(widget, MessageCard) and not getattr(widget, "_lazy_rendered", True):
            existing_ids = {id(c) for c in self._pending_lazy_cards}
            if id(widget) not in existing_ids:
                self._pending_lazy_cards.append(widget)
            if not self._lazy_batch_timer_active:
                self._lazy_batch_timer_active = True
                QTimer.singleShot(200, self._process_next_lazy_batch)

    def _archive_history_session(self, index: int):
        ## 触发警示动画
        if self.pixel_pet:
            self.pixel_pet.set_state("warning")

        # 🛡️ 使用历史面板缓存的 _all_history 列表查找 session_id，
        # 避免因流式保存导致排序变化后 index 偏移指向错误会话。
        session_record = self._history_popup_card.get_history_at_index(index)
        if not session_record:
            if self.pixel_pet:
                self.pixel_pet.set_state("idle")
            return
        session_id = session_record.get("session_id")
        if not session_id:
            if self.pixel_pet:
                self.pixel_pet.set_state("idle")
            return
        # 通过 session_id 找到全量列表中的真实 index
        full_index = self.history_manager.find_index_by_session_id(session_id)
        if full_index is None:
            # 会话不存在，恢复状态
            if self.pixel_pet:
                self.pixel_pet.set_state("idle")
            return

        archived_current = self._current_session_id is not None and session_id == self._current_session_id

        old_session_manager = self.session_manager
        old_chat_engine = self.backend.chat_engine

        # 清理归档会话的文件操作记录和备份
        if self.backend.tool_executor and self.backend.file_recorder:
            self.backend.file_recorder.clear_session(session_id)
            logger.info(f"[FileRecorder] 已清理归档会话的文件操作记录: {session_id}")

        archived = self.history_manager.archive_history(full_index)

        if archived_current and archived:
            # 使用辅助函数创建新会话状态
            new_state = create_new_session_state(old_session_manager, old_chat_engine)
            init_new_session_after_archive(
                self,
                new_state,
                self.backend,
                self._clear_chat_area,
                self._show_initial_welcome,
            )

        # 手术式删除卡片 vs 全量刷新
        current_tab = (
            self._history_popup_card._current_tab if hasattr(self._history_popup_card, "_current_tab") else "history"
        )
        if archived_current:
            # 归档的是当前会话 → UI 变化大（新会话创建、活跃标记变更），需要全量刷新
            if current_tab == "archived":
                refresh_history_card_if_visible(
                    self._history_card,
                    lambda: self._refresh_history_toggle_panel(is_archived=True),
                )
            else:
                refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)
        else:
            # 归档的是非当前会话 → 可以直接手术式删除卡片，避免全量刷新
            if current_tab == "archived":
                # 归档标签页下，需要刷新归档列表
                refresh_history_card_if_visible(
                    self._history_card,
                    lambda: self._refresh_history_toggle_panel(is_archived=True),
                )
            else:
                # 历史标签页下，手术式删除卡片
                removed = (
                    self._history_popup_card.remove_session_card(session_id)
                    if hasattr(self._history_popup_card, "remove_session_card")
                    else False
                )
                if not removed:
                    # 回退：手术式删除失败，走全量刷新
                    refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

        # 操作完成，恢复正常状态
        if self.pixel_pet:
            self.pixel_pet.set_state("idle")

    def _on_team_archive_requested(self, run_id: str):
        """归档团队会话（合并条目「归档」按钮）

        按 run_id 收集全部成员会话 → archive_sessions_by_run_id 逐条归档
        （写 JSON + 从内存/SQLite 删除，D4 归档区逐条显示成员会话）。
        若当前会话属于该团队，归档后自动切换新会话（复用 _archive_history_session
        的 archived_current 分支语义）。
        """
        if not run_id or not self.history_manager:
            return
        ## 触发警示动画
        if self.pixel_pet:
            self.pixel_pet.set_state("warning")

        try:
            members = self.history_manager.get_team_sessions_by_run_id(run_id)
        except Exception:
            members = []
        if not members:
            if self.pixel_pet:
                self.pixel_pet.set_state("idle")
            InfoBar.warning(
                "归档失败",
                "未找到该团队的会话记录",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        member_ids = {s.get("session_id") for s in members if s.get("session_id")}
        archived_current = self._current_session_id in member_ids

        # 归档当前会话时需先保存旧会话管理器/引擎引用（归档后切换新会话用）
        old_session_manager = None
        old_chat_engine = None
        if archived_current:
            old_session_manager = self.session_manager
            old_chat_engine = self.backend.chat_engine
            # 清理归档会话的文件操作记录和备份
            if self.backend.tool_executor and self.backend.file_recorder:
                for sid in member_ids:
                    try:
                        self.backend.file_recorder.clear_session(sid)
                    except Exception:
                        pass

        count = self.history_manager.archive_sessions_by_run_id(run_id)

        if archived_current and count > 0:
            # 复用 create_new_session_state + init_new_session_after_archive
            new_state = create_new_session_state(old_session_manager, old_chat_engine)
            init_new_session_after_archive(
                self,
                new_state,
                self.backend,
                self._clear_chat_area,
                self._show_initial_welcome,
            )

        # 刷新历史面板（团队合并条目消失 / 成员卡片移除）
        refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

        if count > 0:
            InfoBar.success(
                "团队已归档",
                f"已归档 {count} 个成员会话",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=4000,
                position=InfoBarPosition.BOTTOM,
            )
        else:
            InfoBar.warning(
                "归档失败",
                "没有可归档的成员会话",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
        # 操作完成，恢复正常状态
        if self.pixel_pet:
            self.pixel_pet.set_state("idle")

    def _rename_history_session(self, index: int, new_title: str):
        if not self.history_manager:
            return
        # 🛡️ 使用历史面板缓存的 _all_history 列表查找 session_id，
        # 避免因流式保存导致排序变化后 index 偏移指向错误会话。
        session_record = self._history_popup_card.get_history_at_index(index)
        if not session_record:
            return
        session_id = session_record.get("session_id")
        if not session_id:
            return
        idx = self.history_manager.find_index_by_session_id(session_id)
        if idx is None:
            return
        # 1. 更新 history_manager 内存缓存
        self.history_manager.update_session_title(idx, new_title)
        self.history_manager.set_user_edited_title(idx, True)
        # 2. 持久化到 DB
        if self.session_store:
            self.session_store.update_session_title(session_id, new_title)
        # 3. 同步当前 session 对象（若正在查看该会话）
        current_session = self.session_manager.get_current_session() if self.session_manager else None
        if current_session and current_session.session_id == session_id:
            current_session.set_topic_summary(new_title)
            current_session.set_user_edited_title(True)
            # 4. 同步窗口标题 → Tab 标题（windowTitleChanged 信号自动传播）
            self._sync_dialog_title()
        # 5. 刷新历史会话卡片
        refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

    def _on_session_imported(self, data: dict):
        """处理导入的会话文件"""
        if not self.history_manager:
            return

        file_path = data.get("file_path")
        if not file_path:
            return

        imported_session = self.history_manager.import_from_json(file_path)
        if imported_session:
            # 刷新历史会话卡片
            self._refresh_history_toggle_panel()
            # 显示提示信息
            InfoBar.success(
                title="导入成功",
                content=f"已导入会话：{imported_session.get('title', '新对话')}",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            logger.info(f"[导入会话] 成功: {file_path}")
        else:
            InfoBar.error(
                title="导入失败",
                content="无法解析会话文件，请确认文件格式正确",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            logger.warning(f"[导入会话] 失败: {file_path}")

    def _on_archived_session_restored(self, file_path: str):
        """恢复归档会话到历史会话"""
        if not self.history_manager:
            return

        # 导入归档的会话
        imported_session = self.history_manager.import_from_json(file_path)
        if imported_session:
            # 删除归档文件
            try:
                import os

                os.remove(file_path)
                logger.info(f"[恢复会话] 已删除归档文件: {file_path}")
            except Exception as e:
                logger.warning(f"[恢复会话] 删除归档文件失败: {e}")

            # 刷新归档列表
            self._refresh_archived_sessions()

            InfoBar.success(
                title="恢复成功",
                content=f"已恢复会话：{imported_session.get('title', '新对话')}",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            logger.info(f"[恢复会话] 成功: {file_path}")
        else:
            InfoBar.error(
                title="恢复失败",
                content="无法恢复该会话，文件可能已损坏",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )

    def _on_archived_session_deleted(self, file_path: str):
        """彻底删除归档会话"""
        ## 触发警示动画
        if self.pixel_pet:
            self.pixel_pet.set_state("warning")
        from app.widgets.common_dialogs import ConfirmDialog

        # 确认对话框
        _confirmed: list[bool] = [False]

        def _on_delete_confirm():
            _confirmed[0] = True

        _dialog = ConfirmDialog(
            title="确认删除",
            content="确定要彻底删除这个归档会话吗？此操作不可恢复。",
            confirm_text="删除",
            cancel_text="取消",
            # parent 取顶层主窗口（Tab 模式下为 TabManagerWindow），而非 self 这个
            # 嵌入 QStackedWidget 的子 widget——MaskDialogBase 用 parent 尺寸铺遮罩，
            # 传子 widget 会导致遮罩只覆盖聊天区、弹窗层级/定位异常。
            parent=self.window(),
        )
        _dialog.confirmed.connect(_on_delete_confirm)
        _dialog.exec_()
        if not _confirmed[0]:
            # 取消操作，恢复正常状态
            if self.pixel_pet:
                self.pixel_pet.set_state("idle")
            return

        try:
            import os

            os.remove(file_path)
            logger.info(f"[彻底删除] 成功: {file_path}")

            # 清理归档缓存键（防止已删除文件的预览数据驻留 _archived_cache）
            if hasattr(self, "_archived_cache"):
                self._archived_cache.pop(file_path, None)

            # 刷新归档列表
            self._refresh_archived_sessions()
            # 🛡️ 失效欢迎卡片：归档删除会让 recent_sessions / top_by_count 顺序变化
            self._invalidate_welcome_card()
            if self._displayed_session_id is None:
                self._show_initial_welcome()

            InfoBar.success(
                title="删除成功",
                content="归档会话已彻底删除",
                position=InfoBarPosition.BOTTOM,
                duration=2000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
        except Exception as e:
            logger.error(f"[彻底删除] 失败: {e}")
            InfoBar.error(
                title="删除失败",
                content=f"无法删除文件：{str(e)}",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
        finally:
            # 操作完成（不论成功或失败），恢复正常状态
            if self.pixel_pet:
                self.pixel_pet.set_state("idle")

    def _on_archived_session_renamed(self, file_path: str, new_title: str):
        """重命名归档会话"""
        try:
            # 读取文件
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                data = json.loads(content)

            # 更新标题
            data["title"] = new_title

            # 写回文件
            with open(file_path, "wb") as f:
                f.write(json.dumps(data, option=json.OPT_INDENT_2))

            logger.info(f"[归档会话重命名] 成功: {file_path} -> {new_title}")

            # 刷新归档列表
            self._refresh_archived_sessions()
            # 🛡️ 失效欢迎卡片：归档重命名会让 recent_sessions 标题变化
            self._invalidate_welcome_card()
            if self._displayed_session_id is None:
                self._show_initial_welcome()

            InfoBar.success(
                title="重命名成功",
                content=f"已更名为：{new_title}",
                position=InfoBarPosition.BOTTOM,
                duration=2000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
        except Exception as e:
            logger.error(f"[归档会话重命名] 失败: {e}")
            InfoBar.error(
                title="重命名失败",
                content=str(e),
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )

    def _load_history_session(self, index: int):
        self._load_history_session_from_popup(index)

    def _sync_team_markers_from_record(self, session_record: Dict):
        """从会话记录同步窗口团队标记（F4 公共逻辑，两个加载路径共用）

        - _load_session_from_record：历史面板 index / 成员直选加载
        - _switch_to_session_by_id：欢迎卡片会话标签 / 跨窗口切换

        判定依据：team_run_id 非空 = 团队会话（权威字段；普通会话恒为空串）。
        - 团队会话 → 设置团队上下文（run_id/team_name/agent_name），后续
          保存（_save_current_session_to_history team_kwargs 守卫）继续保留团队分组
        - 普通会话 → 清空团队标记，避免团队窗口加载普通会话后 _team_run_id
          残留，导致保存时普通会话被写入团队字段、混入团队合并条目
        """
        record_run_id = (session_record.get("team_run_id") or "").strip()
        if record_run_id:
            # 🛡️ Bug3 守卫：窗口已是团队成员（is_team_member 判定）且
            # _team_run_id 非空（处于当前团队运行）→ 保留当前 run_id，
            # 仅同步 team_name/agent_name——避免恢复窗口(R2)加载旧记录(R1)
            # 后被记录 run_id 覆盖脱钩当前团队分组，导致后续对话保存丢
            # 团队元数据（被存为普通会话）。仅非团队窗口才采用记录 run_id
            # （此时加载团队会话 = 进入该团队上下文，登记为新成员）。
            try:
                from app.core.team_manager import TeamManager

                _member_guard = bool(self._window_id) and TeamManager.get_instance().is_team_member(self._window_id)
            except Exception:
                _member_guard = False
            # 读取窗口当前 run_id：git 测试 stub（__new__ 绕过 __init__ 的 PyQt 对象）
            # 上 getattr 默认值可能因 C++ 对象未初始化抛 RuntimeError，try/except 兜底
            try:
                _current_run_id = self._team_run_id
            except Exception:
                _current_run_id = ""
            if not (_member_guard and _current_run_id):
                self._team_run_id = record_run_id
            self._team_name = (session_record.get("team_name") or "").strip()
            self._team_agent_name = (session_record.get("agent_name") or "").strip()
            # 🆕 恢复团队会话即重新登记成员（否则 leader 的 team_list_members 查不到
            # 本窗口，发任务报"未找到目标"）。历史会话恢复原本只设置 UI 标记，
            # 未调用 join_team → 成员表缺失；此处补注册 + 启动 watcher + 同步活跃
            # 窗口（防 _cleanup_stale_members 误清）。仅 window_id 与 agent_name
            # 均非空时执行，不破坏普通会话加载语义。
            if self._window_id and self._team_agent_name:
                from app.core.team_manager import TeamManager

                tm = TeamManager.get_instance()
                # 🆕 review#13-#1：守卫从「未注册」升级为「未注册 OR 已注册但 agent_name 不一致」。
                # 原 `is_team_member` 守卫的漏洞：窗口已注册（如 build）但本次恢复的会话
                # agent_name 不同（如 review）时返回 True → join_team 被拦截 → 团队数据里
                # agent_name 仍是旧值，与 UI 实际 agent 漂移，leader team_list_members 不一致。
                # join_team 幂等覆盖，仅在需要时触发（已注册且 agent_name 一致则跳过，免写盘）。
                _existing = None
                try:
                    for _m in tm.get_members() or []:
                        if _m.get("window_id") == self._window_id:
                            _existing = _m
                            break
                except Exception:
                    _existing = None
                if _existing is None or _existing.get("agent_name") != self._team_agent_name:
                    tm.join_team(window_id=self._window_id, agent_name=self._team_agent_name)
                    self._start_team_watcher()
                    self._sync_active_windows_to_team_manager()
        else:
            # 🛡️ F4 修复：仅非团队成员窗口清空标记；已登记成员（is_team_member）
            # 保留团队标记，避免"查看普通历史会话"清空成员身份导致后续保存落普通。
            # 语义：成员身份由 join_team 决定——成员窗口后续编辑/产出会话归团队
            # （保存时带团队字段入团队合并条目），此为有意设计，非污染。
            try:
                from app.core.team_manager import TeamManager

                is_member = bool(self._window_id) and TeamManager.get_instance().is_team_member(self._window_id)
            except Exception:
                is_member = False
            if not is_member:
                self._team_run_id = ""
                self._team_name = ""
                self._team_agent_name = ""
                # 🛡️ F4 补充：清空团队标记后刷新 UI，恢复独立模式边框/标题栏配色，
                # 消除团队窗口加载普通会话后的团队配色残留（对齐 :3924 用法；
                # try/except 防御，与下方 Tab 同步分支风格一致）
                try:
                    self._refresh_team_ui(is_team=False)
                except Exception:
                    pass

    def _load_session_from_record(self, session_record: Dict):
        """从 session_record 加载历史会话（公共逻辑，供面板 index 与成员直选复用）

        提取自 _load_history_session_from_popup：面板 index 定位 session_record 后
        的加载逻辑（哨兵、reset、消息加载、create_session、project/worktree 同步、
        显示刷新）。成员进入会话（_on_team_member_selected）直传 record 调用，
        不依赖面板 index，规避合并条目 index 漂移。
        """
        if not session_record:
            return
        session_id = session_record.get("session_id")
        if not session_id:
            return

        if self._is_streaming:
            self._on_stop_clicked()
            # 🐛 修复（加载历史打断对话 partial 丢失）：_on_stop_clicked 是异步
            # 两阶段停止（cancel + deferred finalize），deferred finalize 在本方法
            # 设置 _session_switched 哨兵**之后**才执行，_on_finalize_complete 会
            # 因哨兵丢弃中断消息 → 最后 partial 回复永久丢失。
            # 此处同步补齐：立即 finalize_stop 拿中断消息应用回 session（幂等——
            # _finalize_worker 单次消费，deferred 再跑拿到空列表无害），
            # 确保下方 _auto_save_current_session 保存的是完整会话。
            try:
                interrupted = self.backend.finalize_stop() if self.backend.chat_engine else []
                if interrupted:
                    self._apply_interrupted_messages_to_session(interrupted)
                    self._session_dirty = True
            except Exception as e:
                logger.warning(f"[MainWidget] 加载历史前同步 finalize 失败: {e}")

        try:
            self._auto_save_current_session()
        except Exception:
            logger.exception("Failed to auto-save current session before loading history")

        # 🛡️ 标记会话切换：_on_stop_clicked 采用两阶段停止（cancel + deferred finalize），
        # old worker 的 finished_with_messages / _on_finalize_complete 等跨线程回调
        # 仍可能在后续事件循环中到达。若不设置哨兵，这些回调会将会话 A 的（部分）消息
        # 通过 _on_messages_updated / _on_finalize_complete 写入刚加载的会话 B，
        # 再被后续 save 错误持久化到会话 B 的记录中，造成"当前会话内容覆盖目标会话"的 bug。
        # 哨兵在 _on_send_clicked 发起新 AI 请求时清零。
        self._session_switched = True

        self.backend.reset_session_state()

        # 💡 内存优化：加载历史会话时清理 LRU 缓存
        _cleanup_global_lru_caches()

        # 通过 session_id 获取会话消息，确保即使列表顺序变化也能加载正确的会话
        messages = self.history_manager.get_session_messages(session_id)
        if not messages:
            return

        title = session_record.get("title") or session_record.get("name") or "历史对话"

        # 使用辅助函数创建会话
        restored = create_session_from_record(session_record, messages, title)

        # 使用辅助函数初始化
        init_after_loading_session(self, restored, session_id, title, self.backend)

        # 如果会话有自己的项目，显示在标题上
        session_project = session_record.get("project", "默认项目") or "默认项目"
        self._current_project = session_project
        self.backend._current_project = session_project
        self._project_label.setText(session_project)
        self._refresh_project_branch_style()
        # 同步到 tool_executor，确保 stage_files 等工具写入正确的项目
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_current_project(session_project)

        # 🛡️ F4：加载历史会话后同步窗口团队标记，防止普通/团队会话互相污染。
        # 判定依据：team_run_id 非空 = 团队会话（权威字段；普通会话恒为空串）。
        # - 团队会话 → 设置团队上下文（run_id/team_name/agent_name），后续
        #   保存（_save_current_session_to_history team_kwargs 守卫）继续保留团队分组
        # - 普通会话 → 清空团队标记，避免团队窗口加载普通会话后 _team_run_id
        #   残留，导致保存时普通会话被写入团队字段、混入团队合并条目
        self._sync_team_markers_from_record(session_record)
        # Tab 模式：团队标记已变，同步胶囊/分组（对齐 _refresh_team_ui 的
        # refresh_capsule_for_window 调用；无 Tab 管理器时静默跳过）
        try:
            if self.cfg.enable_tab_manager.value:
                from app.widgets.tab_manager_window import TabManagerWindow

                _tm = TabManagerWindow.get_instance()
                if _tm is not None:
                    _tm.refresh_capsule_for_window(self)
        except Exception:
            pass

        # 自动切换到该会话关联的 worktree
        # 规则：会话有 worktree_path → 切到该 worktree
        #       会话没有 worktree_path → 切回主仓库（如果当前在 worktree 中）
        worktree_path = session_record.get("worktree_path", "") or ""
        current_wt = self._get_current_worktree_path()
        if worktree_path and os.path.isdir(worktree_path) and worktree_path != current_wt:
            self._switch_to_worktree(worktree_path)
        elif not worktree_path and current_wt:
            self._restore_main_repo()

        self._display_current_session()
        self._release_inactive_session_messages()

        # 刷新历史会话卡片（P0-1：卡片懒创建，需判空）
        if getattr(self, "_history_card", None) and self._history_card.isVisible():
            self._refresh_history_toggle_panel()

        # 刷新 UI 插件命令卡片缓存（插件可能注册了新命令）
        try:
            from app.core.command_manager import CommandManager
            from app.core.ui_plugin_registry import UIPluginRegistry

            CommandManager.get_instance().reload_all_commands()
            UIPluginRegistry.get_instance().re_register_all_commands()
        except Exception:
            pass

    def _on_team_member_selected(self, member_record: dict):
        """团队合并条目成员行被点击 → 直接进入该成员会话（不依赖面板 index）"""
        if not member_record:
            return
        session_id = member_record.get("session_id")
        if not session_id:
            return
        # 补全完整记录（成员轻量记录可能缺 messages 字段）
        full_record = self.history_manager.get_session_by_session_id(session_id) if self.history_manager else None
        record = full_record or member_record
        self._load_session_from_record(record)
        # 关闭历史会话卡片
        if self._card_manager:
            self._card_manager.hide_card("history", self._window_id)

    def _load_history_session_from_popup(self, index: int):
        # 🛡️ 使用历史面板缓存的 _all_history 列表来查找 session_id，
        # 而非重新调用 get_history_list()。原因是：流式对话结束时当前会话被保存
        # 到历史列表头部，若在此处重新获取列表，保存的 index 会因新会话插入而
        # 偏移指向错误的会话（点击会话 C 却加载了会话 B）。
        # 使用面板内缓存的列表保证 index 与渲染时一致，再通过 session_id
        # 从 history_manager 获取完整数据。
        session_record = self._history_popup_card.get_history_at_index(index)
        if not session_record:
            return
        self._load_session_from_record(session_record)

    def _append_user_message(
        self,
        content: str,
        timestamp: str = None,
        scroll: bool = True,
        insert_index: Optional[int] = None,
        user_round_index: Optional[int] = None,
        update_preview: bool = True,
    ):
        session = self.session_manager.get_current_session()
        if session:
            self._displayed_session_id = session.session_id
            # 🛡️ 锁定首发项目快照：用户首次发消息时记录当前项目。
            # 一旦锁定，后续即使切换项目，落盘时仍归属首发项目，
            # 避免"对话完成后立即切换项目导致会话错存到切换后项目"bug。
            if not session.originating_project and self._current_project:
                session.originating_project = self._current_project

        # 计算当前 user message 的 round_index
        if user_round_index is None:
            user_round_index = self._get_current_user_round_index()

        card = MessageCard(parent=self, role="user", timestamp=timestamp)
        card._round_index = user_round_index
        card.update_content(content)
        card.finish_streaming()

        # 设置卡片信号
        setup_user_card_signals(card, self._delete_message, self._undo_from_message, self._on_code_action)

        self._add_chat_widget(card, insert_index=insert_index)
        if scroll and not self._suspend_auto_scroll:
            self._scroll_to_bottom()

        # 使用辅助函数后处理
        post_append_user_message(
            self,
            user_round_index,
            self._update_node_preview if update_preview else None,
        )
        return card

    def _append_assistant_message(
        self,
        timestamp: str = None,
        scroll: bool = True,
        insert_index: Optional[int] = None,
        round_index: Optional[int] = None,
        model_name: str = None,
        provider_name: str = None,
        config_id: str = None,
    ) -> MessageCard:
        session = self.session_manager.get_current_session()
        if session:
            self._displayed_session_id = session.session_id

        # 🛡️ 解析 config_id：如果未传入则从 _valid_configs 反查。
        # 整个导航逻辑只依赖 UUID，不依赖 display_name / provider_name。
        if not config_id and self._valid_configs:
            if self._current_provider_name and self._current_provider_name in self._valid_configs:
                cfg = self._valid_configs[self._current_provider_name]
                if cfg.get("模型名称") == model_name or model_name in (cfg.get("模型列表") or []):
                    config_id = self._current_provider_name
        if not config_id and model_name and self._valid_configs:
            for c, info in self._valid_configs.items():
                if info.get("模型名称") == model_name or model_name in (info.get("模型列表") or []):
                    if config_id is None:
                        config_id = c
                    elif config_id != c:
                        config_id = None
                        break
        if not config_id and self._current_provider_name and self._current_provider_name in self._valid_configs:
            config_id = self._current_provider_name

        # 使用辅助函数创建卡片
        def on_context_action(action, context):
            self.handle_recommended_question(action, context)
            self.contextActionRequested.emit(action, context)

        # ===== UI 插件消息工厂钩子 =====
        # 让插件工厂先尝试处理这条消息（高优先级先尝试）
        custom_widget = self._create_message_widget(
            role="assistant",
            content=None,
            timestamp=timestamp,
            round_index=(round_index if round_index is not None else self._current_assistant_round_index),
            model_name=model_name,
            provider_name=provider_name,
        )
        if custom_widget is not None:
            self._add_chat_widget(custom_widget, insert_index=insert_index)
            if scroll and not self._suspend_auto_scroll:
                self._scroll_to_bottom()
            return custom_widget

        card = create_assistant_card_widget(
            parent=self,
            timestamp=timestamp,
            round_index=(round_index if round_index is not None else self._current_assistant_round_index),
            model_name=model_name,
            provider_name=provider_name,
            config_id=config_id,
            on_action=self._on_code_action,
            on_context_action=on_context_action,
            on_tool_diff=self._on_tool_diff_requested,
            on_card_diff=self._on_card_diff_requested,
            on_save_file=self._on_save_file_requested,
            on_subagent_log=self._on_subagent_log_requested,
            on_review=self._on_review_requested,
            immediate_render=scroll,  # 流式(scroll=True)立即渲染，加载(scroll=False)走懒渲染队列
        )

        # 连接模型标签点击信号到切换逻辑
        card.modelLabelClicked.connect(self._on_footer_model_label_clicked)

        self._add_chat_widget(card, insert_index=insert_index)
        if scroll and not self._suspend_auto_scroll:
            self._scroll_to_bottom()
        return card

    def _update_assistant_message(self, card: MessageCard, new_content: str):
        card.update_content(new_content)
        # [PERF] 流式中合并滚底调用：仅在 _scroll_bottom_timer 未激活时调度，
        # 避免高频 content_received 信号导致大量 singleShot(0) 堆积在事件队列中。
        # timer 激活后会在 50ms 后自动滚底，合并期间所有 content_received 的请求。
        # 卡片高度变化到 layout 更新有至少 1-2 帧延迟，用 timer 合并足够。
        if self._is_streaming and not self._scroll_bottom_timer.isActive():
            # 延迟到下一事件循环再滚底，等卡片高度变化完成后读取 scroll_bar.maximum()
            QTimer.singleShot(0, lambda: scroll_to_bottom_if_streaming(self.chat_scroll_area, self._is_streaming))

    def _update_node_preview(self):
        session = self.session_manager.get_current_session()
        if not session:
            return

        # 🛡️ Bug1 守卫：_save_current_session_to_history 可能由后台 finalize 线程
        # （_launch_background_finalize）调用——窗口关闭后 node_preview 的 C++
        # 对象随窗口树销毁，后台线程再访问抛 RuntimeError
        # （wrapped C/C++ object of type ConversationNodePreview has been deleted）。
        # 与项目既有 _is_sip_deleted 守卫模式一致：已销毁则跳过 UI 更新
        # （node_preview 仅是时间线展示，后台持久化路径无需刷新 UI）。
        if not hasattr(self, "node_preview") or _is_sip_deleted(self.node_preview):
            return

        # 使用辅助函数构建 node preview 数据
        node_data = build_node_preview_from_session(session, content_to_text, max_len=30)

        self.node_preview.update_nodes(node_data)
        self._sync_node_preview_to_scroll()
        # 同步更新历史问题徽章
        self._update_history_questions_badge()

    def _sync_node_preview_to_scroll(self):
        """根据当前滚动位置同步时间线节点的高亮和进度条
        新逻辑：进度条显示节点与节点之间的进度，和实际卡片高度绑定
        """
        # 加载历史时抑制滚动同步
        if self._suppress_scroll_sync_count > 0:
            self._suppress_scroll_sync_count -= 1
            return

        if not hasattr(self, "chat_scroll_area") or not hasattr(self, "node_preview"):
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        total_nodes = len(self.node_preview._nodes) if hasattr(self.node_preview, "_nodes") else 0
        if total_nodes == 0:
            self._last_visible_user_pair_index = -1
            self.node_preview.set_visible_node(-1)
            self.node_preview.set_progress_position(-1)
            return

        # 如果有待更新的目标索引（点击跳转中），直接使用它
        if self._pending_scroll_to_update is not None:
            highlighted_index = self._pending_scroll_to_update
            self._pending_scroll_to_update = None
            highlighted_index = max(0, min(highlighted_index, total_nodes - 1))
            self.node_preview.set_progress_position(highlighted_index)
            if highlighted_index != self._last_visible_user_pair_index:
                self._last_visible_user_pair_index = highlighted_index
                self.node_preview.set_visible_node(highlighted_index)
            return

        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        visible_top = scroll_bar.value()

        # 收集所有已渲染用户卡片的位置信息
        user_card_info = []

        # 使用节点-批次映射（与 build_node_preview_data 对齐），只遍历有节点的 user batch
        node_to_batch = self._build_node_to_batch_mapping()
        for node_idx, batch_idx in enumerate(node_to_batch):
            if node_idx >= total_nodes:
                break

            # 尝试从 batch_cards 中找到对应的卡片
            cards = self._batch_cards[batch_idx] if batch_idx < len(self._batch_cards) else None
            if cards:
                for card in cards:
                    if sip.isdeleted(card):
                        continue
                    if isinstance(card, MessageCard) and card.role == "user":
                        user_card_info.append(
                            {
                                "index": node_idx,
                                "y": card.y(),
                                "bottom": card.y() + card.height(),
                            }
                        )
                        break

        # 如果没有找到任何已渲染卡片，fallback到估算
        if not user_card_info and scroll_bar.maximum() > 0:
            scroll_ratio = visible_top / scroll_bar.maximum()
            highlighted_index = int(round(scroll_ratio * (total_nodes - 1)))
            progress_position = highlighted_index
        else:
            # 找到当前可视区域所在的节点区间
            # 找出当前可视top落在哪个区间 [user_i, user_{i+1}]
            current_segment_index = -1
            for i in range(len(user_card_info)):
                if i < len(user_card_info) - 1:
                    # 当前区间是从这个user卡片开始，到下一个user卡片之前
                    # 包含这个user问题和它对应的所有大模型回答卡片
                    segment_start_y = user_card_info[i]["y"]
                    segment_end_y = user_card_info[i + 1]["y"]

                    if visible_top < segment_end_y:
                        current_segment_index = i
                        break
                else:
                    # 最后一个节点，一直到最后
                    current_segment_index = i
                    break

            # 计算进度
            if current_segment_index >= 0:
                start_node_index = user_card_info[current_segment_index]["index"]
                start_y = user_card_info[current_segment_index]["y"]

                if current_segment_index < len(user_card_info) - 1:
                    end_y = user_card_info[current_segment_index + 1]["y"]
                    end_node_index = user_card_info[current_segment_index + 1]["index"]
                else:
                    # 最后一个区间，使用最后一个卡片的bottom作为终点
                    if len(user_card_info) > 0:
                        last_item = user_card_info[-1]
                        end_y = last_item["bottom"] + 500  # 增加一点余量
                        end_node_index = last_item["index"]
                    else:
                        end_y = start_y
                        end_node_index = start_node_index

                # 计算在当前区间的比例
                if end_y > start_y:
                    segment_progress = (visible_top - start_y) / (end_y - start_y)
                else:
                    segment_progress = 0

                # 转换到节点坐标
                progress_position = start_node_index + segment_progress
                visible_node_index = start_node_index
            else:
                # 都没找到，fallback
                progress_position = 0
                visible_node_index = 0
                highlighted_index = 0

            highlighted_index = visible_node_index

        # 确保在有效范围内
        progress_position = max(0, min(progress_position, total_nodes - 1))
        highlighted_index = max(0, min(highlighted_index, total_nodes - 1))

        # 更新进度条和高亮节点
        self.node_preview.set_progress_position(progress_position)
        if highlighted_index != self._last_visible_user_pair_index:
            self._last_visible_user_pair_index = highlighted_index
            self.node_preview.set_visible_node(highlighted_index)

    def _sync_node_preview_to_last(self):
        """滚动完成后同步到最后一个节点"""
        if not hasattr(self, "node_preview"):
            return

        # 直接使用 node_preview 的节点数（与 build_node_preview_data 保持一致）
        node_count = len(self.node_preview._nodes) if hasattr(self.node_preview, "_nodes") else 0
        if node_count > 0:
            last_index = node_count - 1
            self.node_preview.set_visible_node(last_index)
            self.node_preview.set_progress_position(last_index)
            self._last_visible_user_pair_index = last_index

    def _scroll_to_target_node_index(self, target_index: int):
        """
        滚动到指定节点索引的位置。如果目标卡片未渲染，先加载历史批次。

        Args:
            target_index: 目标节点索引（0-based）
        """
        session = self.session_manager.get_current_session()
        if not session:
            return

        # 将节点索引转换为 _message_batch 索引（与 build_node_preview_data 对齐）
        target_batch_index = self._get_batch_index_from_node_index(target_index)
        if target_batch_index < 0:
            logger.warning(f"[NodePreview] Cannot find batch for target_index={target_index}")
            return

        # 检查目标 batch 是否已经加载（可见）
        if target_batch_index >= self._visible_batch_start:
            # 已加载，直接滚动到目标位置
            self._scroll_to_batch_index(target_batch_index, node_index=target_index)
            return

        # 需要加载更多历史批次
        # 计算需要加载的起始位置（预留一些缓冲批次）
        new_start = max(0, target_batch_index - self._incremental_visible_batch_count // 2)

        # 标记要滚动的目标索引，以便加载完成后使用
        self._pending_scroll_to_index = target_index
        self._pending_scroll_to_batch = target_batch_index

        # 加载历史批次到目标位置
        self._load_history_to_index(new_start)

    def _load_history_to_index(self, target_batch_start: int):
        """
        加载历史批次直到到达目标 batch 起始位置

        Args:
            target_batch_start: 目标批次起始索引
        """
        if target_batch_start >= self._visible_batch_start:
            # 已经到达目标位置，滚动到目标节点
            self._scroll_to_pending_target()
            return

        # 计算需要加载多少批次
        batch_count = self._visible_batch_start - target_batch_start
        if batch_count > 0:
            # 触发分批加载
            self._render_message_to_card(
                self._message_batch[target_batch_start : self._visible_batch_start],
                insert_at_top=True,
                batch_offset=target_batch_start,
            )

            # 更新可见范围
            self._visible_batch_start = target_batch_start

            # 延迟检查是否需要继续加载（使用 QTimer.singleShot 避免重复）
            QTimer.singleShot(100, lambda: self._load_history_to_index(target_batch_start))

    def _scroll_to_pending_target(self):
        """滚动到待处理的目标节点"""
        if self._pending_scroll_to_index is None:
            return

        target_index = self._pending_scroll_to_index
        target_batch_index = self._pending_scroll_to_batch
        self._pending_scroll_to_index = None
        self._pending_scroll_to_batch = None

        self._pending_scroll_to_update = target_index

        # 如果 target_batch_index 未提供，从 _message_batch 查找
        if target_batch_index is None:
            target_batch_index = self._get_batch_index_from_node_index(target_index)

        if target_batch_index < 0:
            return

        # 优先从 _batch_cards 查找（更可靠）
        if 0 <= target_batch_index < len(self._batch_cards):
            cards = self._batch_cards[target_batch_index]
            if cards:
                for card in cards:
                    if self._is_widget_alive(card) and isinstance(card, MessageCard):
                        if card.role == "user":
                            self.chat_scroll_area.verticalScrollBar().setValue(card.y())
                            return

        # 回退：遍历布局查找
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if widget.role == "user" and getattr(widget, "_message_index", None) == target_batch_index:
                self.chat_scroll_area.verticalScrollBar().setValue(widget.y())
                return

        logger.warning(f"[NodePreview] Card not found after history load, index={target_index}")

    def _find_user_card_in_batch(self, target_batch_index):
        """从 _batch_cards 中查找指定 batch 的 user card"""
        if target_batch_index is None or target_batch_index < 0:
            return None
        if target_batch_index >= len(self._batch_cards):
            return None
        cards = self._batch_cards[target_batch_index]
        if not cards:
            return None
        for card in cards:
            if sip.isdeleted(card):
                continue
            if isinstance(card, MessageCard) and card.role == "user":
                return card
        return None

    def _find_user_card_by_message_index(self, target_batch_index):
        """在布局中查找 _message_index 等于目标 batch 的 user card"""
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if isinstance(widget, MessageCard) and widget.role == "user":
                if getattr(widget, "_message_index", None) == target_batch_index:
                    return widget
        return None

    def _scroll_to_batch_index(self, batch_index: int, node_index: int = -1):
        """
        滚动到指定 batch 索引的位置。
        优先使用 _batch_cards 查找，如果找不到再遍历布局作为回退。
        """
        if node_index >= 0:
            self._pending_scroll_to_update = node_index

        # 优先从 _batch_cards 查找（更可靠，避免虚拟回收后布局遍历失效）
        if 0 <= batch_index < len(self._batch_cards):
            cards = self._batch_cards[batch_index]
            if cards:
                for card in cards:
                    if self._is_widget_alive(card) and isinstance(card, MessageCard):
                        # 确保是 user card
                        if card.role == "user":
                            self.chat_scroll_area.verticalScrollBar().setValue(card.y())
                            return

        # 回退：遍历布局查找（处理边界情况，如刚创建但尚未加入 _batch_cards）
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if widget.role == "user" and getattr(widget, "_message_index", None) == batch_index:
                self.chat_scroll_area.verticalScrollBar().setValue(widget.y())
                return

        logger.warning(f"[NodePreview] Cannot find card for batch_index={batch_index}, node_index={node_index}")

    def _build_node_to_batch_mapping(self) -> list:
        """
        构建 timeline 节点索引 → _message_batch 索引的映射列表。

        与 build_node_preview_data 的节点创建逻辑保持一致：
        - user batch 后面跟着 assistant/tool batch → 生成节点
        - 连续 user 中只有最后一个有 assistant 配对的生成节点
        - _message_batch 中最后一个 user batch 也生成节点（trailing check）

        返回: mapping[node_index] = batch_index
        """
        mapping = []
        last_user_batch_idx = -1

        for idx, batch in enumerate(self._message_batch):
            if not batch:
                continue
            if batch[0].get("role") != "user":
                continue

            last_user_batch_idx = idx

            # 检查当前 user batch 之后是否有配对的 assistant/tool batch
            has_paired = False
            for next_idx in range(idx + 1, len(self._message_batch)):
                next_batch = self._message_batch[next_idx]
                if not next_batch:
                    continue
                next_role = next_batch[0].get("role")
                if next_role in ("assistant", "tool"):
                    has_paired = True
                    break
                if next_role == "user":
                    break  # 在 assistant 前遇到另一个 user → 无配对

            if has_paired:
                mapping.append(idx)

        # 最后一个 user（即使是无配对的）也生成节点
        if last_user_batch_idx >= 0 and (not mapping or mapping[-1] != last_user_batch_idx):
            mapping.append(last_user_batch_idx)

        return mapping

    def _get_batch_index_from_node_index(self, node_index: int) -> int:
        """将 timeline 节点索引转换为 _message_batch 索引。"""
        mapping = self._build_node_to_batch_mapping()
        if 0 <= node_index < len(mapping):
            return mapping[node_index]
        return -1

    def _on_share_clicked(self):
        """分享当前对话：切换分享卡片显示"""
        self._ensure_share_card()  # P0-1：框架懒创建（原 hasattr 检查改为惰性创建）

        # 如果卡片正在关闭中（状态为 visible），直接 toggle 关闭
        if self._card_manager.is_card_visible("share", self._window_id):
            self._card_manager.hide_card("share", self._window_id)
            return

        session = self.session_manager.get_current_session()
        if not session:
            from qfluentwidgets import InfoBar, InfoBarPosition

            InfoBar.warning(
                title="",
                content="没有当前会话，无法分享",
                duration=2000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self.window(),
            )
            return

        from app.core import consolidate_messages

        messages = consolidate_messages(session.messages)
        if not messages:
            from qfluentwidgets import InfoBar, InfoBarPosition

            InfoBar.warning(
                title="",
                content="当前会话无消息，无法分享",
                duration=2000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self.window(),
            )
            return

        # 注入消息数据后显示（构建与归档一致的完整 session 记录）
        record = self._build_share_record(session, messages)
        if self._share_card_content:
            self._share_card_content.set_messages(record, session.name or "")
        self._card_manager.toggle_card("share", self._window_id)

    def _build_share_record(self, session, merged_messages: list) -> dict:
        """构建与归档（archive）一致的完整 session 记录字典，供分享导出（JSON/HTML）使用。

        与 history_manager._build_session_record 产出相同的字段结构，
        因此分享的 JSON 即为「归档生成的完整 JSON」，而非仅消息列表。
        """
        if self.history_manager is not None:
            try:
                return self.history_manager._build_session_record(
                    merged_messages=merged_messages,
                    title=session.name or "",
                    session_id=session.session_id,
                    compaction_state=session.compaction_state,
                    compaction_cache=session.compaction_cache,
                    system_prompt=session.system_prompt,
                    project=session.originating_project or "默认项目",
                    worktree_path=(session.metadata or {}).get("worktree_path", ""),
                )
            except Exception:
                pass
        # 兜底：history_manager 不可用时构造基础记录
        return {
            "session_id": getattr(session, "session_id", ""),
            "title": getattr(session, "name", "") or "",
            "project": getattr(session, "originating_project", "") or "默认项目",
            "last_time": getattr(session, "last_updated", ""),
            "messages": merged_messages,
            "message_count": len(merged_messages),
            "compaction_state": getattr(session, "compaction_state", {}) or {},
            "compaction_cache": getattr(session, "compaction_cache", {}) or {},
            "system_prompt": getattr(session, "system_prompt", "") or "",
            "user_edited_title": getattr(session, "user_edited_title", False),
            "context_usage": getattr(session, "context_usage", 0) or 0,
        }

    def _toggle_history_questions_popup(self):
        """切换历史问题卡片的显示"""
        self._ensure_history_questions_card()  # P0-1：框架懒创建（原 hasattr 检查改为惰性创建）

        if self._card_manager.is_card_visible("history_questions", self._window_id):
            self._card_manager.hide_card("history_questions", self._window_id)
            return

        # 获取当前会话的用户问题
        session = self.session_manager.get_current_session()
        if not session:
            return

        from app.core import consolidate_messages, content_to_text

        messages = consolidate_messages(session.messages)
        questions = []
        for msg in messages:
            if msg.get("role") == "user":
                # 🛡️ R7 修复：风格与其他 3 处统一为"正向包含"——
                # team 邮件（_hook_event="TeamMail"）视为真实用户问题，
                # 非 TeamMail 的 hook 事件（SessionStart / vision_inject /
                # SubAgentFinished 等）跳过。
                if not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail":
                    text = content_to_text(msg.get("content", ""))
                    if len(text) > 60:
                        text = text[:60] + "…"
                    questions.append((len(questions), text))

        self._history_questions_card_content.set_questions(questions)
        self._card_manager.toggle_card("history_questions", self._window_id)

    def _update_history_questions_badge(self):
        """更新历史问题徽章计数（当前会话用户问题总数）"""
        if not hasattr(self, "_history_questions_badge"):
            return
        session = self.session_manager.get_current_session()
        if not session:
            self._history_questions_badge.setVisible(False)
            return

        messages = consolidate_messages(session.messages)
        # 🛡️ R6 修复：team 邮件（_hook_event="TeamMail"）计入用户问题数
        count = sum(
            1
            for msg in messages
            if msg.get("role") == "user" and (not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail")
        )

        if count > 0:
            self._history_questions_badge.setNum(count)
            self._history_questions_badge.adjustSize()
            # 重新定位（badge 尺寸变化后需刷新位置）
            if self._history_questions_badge.manager:
                self._history_questions_badge.move(self._history_questions_badge.manager.position())
            self._history_questions_badge.setVisible(True)
        else:
            self._history_questions_badge.setVisible(False)

    def _on_history_question_clicked(self, index: int):
        """历史问题弹窗条目点击，跳转到对应位置（复用时间线节点跳转逻辑）"""
        self._on_node_preview_clicked(index)

    def _on_node_preview_clicked(self, index: int):
        """
        点击时间线节点，滚动到对应的 user 卡片。
        """
        target_batch_index = self._get_batch_index_from_node_index(index)
        if target_batch_index < 0:
            return

        # 优先从 _batch_cards 查找（更可靠，避免虚拟回收后布局遍历失效）
        if 0 <= target_batch_index < len(self._batch_cards):
            cards = self._batch_cards[target_batch_index]
            if cards:
                for card in cards:
                    if self._is_widget_alive(card) and isinstance(card, MessageCard):
                        if card.role == "user":
                            self._pending_scroll_to_update = index
                            self.chat_scroll_area.verticalScrollBar().setValue(card.y())
                            return

        # 回退：遍历布局查找（处理边界情况）
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if widget.role == "user" and getattr(widget, "_message_index", None) == target_batch_index:
                self._pending_scroll_to_update = index
                self.chat_scroll_area.verticalScrollBar().setValue(widget.y())
                return

        # 目标 batch 未渲染，触发加载
        self._scroll_to_target_node_index(index)

    def _is_card_index_valid(self, card, expected_batch_index: int) -> bool:
        """
        验证卡片是否真的对应目标 batch index。
        用于防止虚拟回收后计数错位返回错误卡片。
        """
        if card is None or expected_batch_index < 0:
            return False
        card_index = getattr(card, "_message_index", None)
        return card_index == expected_batch_index

    def _on_chat_scrolled(self, value):
        """聊天区域滚动时，触发虚拟滚动回收并通知所有 MessageCard 更新浮动头"""
        self._virtual_scroll_timer.start()
        # for card in self.findChildren(MessageCard):
        #     card._scroll_position_changed(value)

    def _on_scroll_changed(self, value):
        self._sync_node_preview_to_scroll()
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        if self._bottom_anchor_deadline > 0:
            if value < scroll_bar.maximum():
                self._bottom_anchor_deadline = 0.0
                self._bottom_anchor_timer.stop()
        # 检测用户是否主动滚离底部（距离底部超过 30px）；
        # 滚回底部附近时复位，保证 away 标志状态机闭合（置 True 处见上）
        if value < scroll_bar.maximum() - 30:
            self._user_intentionally_away_from_bottom = True
        elif scroll_bar.maximum() > 0:
            self._user_intentionally_away_from_bottom = False
        if value <= self._history_load_threshold:
            self._load_more_history_batches()
        # 滚动时复用单个防抖定时器，避免堆积大量 singleShot 回调
        self._scroll_sync_timer.stop()
        self._scroll_sync_timer.start()

    def _truncate_session_from_user_round(self, round_index: int, card: MessageCard = None) -> bool:
        """
        截断 session 数据到指定 round 之前，并删除 UI 卡片

        UI 删除策略：基于 card widget 对象在 chat_layout 中的位置精准删除，
        不依赖 round_index 遍历（解决懒加载时卡片序号对不上的问题）
        """

        session = self.session_manager.get_current_session()
        if not session:
            return False

        # === 1. 删除 UI 卡片：从 card 到末尾 ===
        if card is not None:
            # 找到 card 在 chat_layout 中的索引
            card_layout_idx = -1
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                if item and item.widget() is card:
                    card_layout_idx = i
                    break

            if card_layout_idx >= 0:
                # 收集要删除的 widgets：从 card 到末尾（撤销 = 删除之后所有）
                widgets_to_remove = []
                for i in range(card_layout_idx, self.chat_layout.count()):
                    item = self.chat_layout.itemAt(i)
                    if item and item.widget():
                        w = item.widget()
                        if hasattr(w, "_is_welcome") and w._is_welcome:
                            continue
                        widgets_to_remove.append(w)

                from app.widgets.ui_helpers import delete_widgets_from_layout

                # 注意：不调用 cleanup，因为撤销操作需要在删除后仍能访问卡片数据
                deleted_count = delete_widgets_from_layout(widgets_to_remove, self.chat_layout, call_cleanup=False)

                # 清理 _batch_cards 中对已删除卡片的引用，防止后续遍历时 RuntimeError
                for batch_idx in range(len(self._batch_cards)):
                    batch = self._batch_cards[batch_idx]
                    if not batch:
                        continue
                    alive = [w for w in batch if not sip.isdeleted(w)]
                    if len(alive) != len(batch):
                        self._batch_cards[batch_idx] = alive if alive else None

        # === 2. 基于 session.messages 计算截断位置 ===
        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)

        if round_index < 0 or round_index >= len(round_ranges):
            return False

        cutoff_index = round_ranges[round_index][0]

        # === 3. 截断 session.messages ===
        # 🛡️ 使用 canonical_messages 而非 session.messages，确保 cutoff_index（来自
        # canonical 的 round_ranges）与截断目标一致。consolidate_messages 可能过滤掉
        # 非标准消息，直接对 session.messages 切片会导致取错位置。
        session.set_messages(canonical_messages[:cutoff_index], preserve_compaction=False)
        self._session_dirty = True  # 🛡️ 截断修改了消息列表，脏标记兜底

        # === 4. 同步 _message_batch 和 _batch_cards 到 session 的新状态 ===
        self._message_batch = group_messages_for_display(session.messages)
        # 同步裁剪 _batch_cards 长度，防止旧引用残留
        new_len = len(self._message_batch)
        if new_len > len(self._batch_cards):
            self._batch_cards.extend([None] * (new_len - len(self._batch_cards)))
        elif new_len < len(self._batch_cards):
            self._batch_cards = self._batch_cards[:new_len]
        # 重建 user 前缀和缓存
        self._build_user_prefix_cache()

        # === 5. 保存 session ===
        self._persist_session_after_mutation()

        # === 6. 刷新剩余卡片的 round_index ===
        self._refresh_all_cards_round_index()

        # === 7. 收尾 ===
        self._finalize_local_session_mutation()

        return True

    def _delete_message(self, card: MessageCard):
        if card.role != "user":
            return

        session = self.session_manager.get_current_session()
        was_streaming = self._is_streaming

        # 🛡️ 设置截断哨兵，必须领先于 _on_stop_clicked，防止 worker 的 finished_with_messages
        # 信号在 _delete_user_round 截断 session 后到达并覆盖新消息。
        # 哨兵会在 _on_finalize_complete（流式场景）或下方（非流式场景）清除。
        if was_streaming and session:
            self._truncation_sentinel = {
                "session_id": session.session_id,
                "messages_len": len(session.messages),
                "set_at": time.time(),
            }
            self._on_stop_clicked()

        # 🛡️ 先清理 CardManager 中残留的可见状态（上次恢复时 _on_restore_clicked
        # 直接调 setVisible(False) 绕过了 CardManager），再设新缓存，防止后续
        # hide_card → dismissed → _on_undo_delete_dismissed 清空刚设好的缓存。
        if self._card_manager.is_card_visible("undo_delete", self._window_id):
            self._card_manager.hide_card("undo_delete", self._window_id)

        # === 缓存删除数据，用于撤销恢复（只缓存一步）===
        if session and card._round_index is not None:
            try:
                canonical_messages = consolidate_messages(session.messages)
                round_ranges = get_user_round_ranges(canonical_messages)
                if 0 <= card._round_index < len(round_ranges):
                    start_idx, end_idx = round_ranges[card._round_index]
                    msg_count = end_idx - start_idx
                    self._undo_delete_cache = {
                        "session_id": session.session_id,
                        "messages": list(canonical_messages[start_idx:end_idx]),
                        "insert_index": start_idx,
                        "count": msg_count,
                    }
                    logger.debug(
                        "[DELETE] Cache set: "
                        f"session_id={session.session_id!r}, "
                        f"start_idx={start_idx}, end_idx={end_idx}, "
                        f"msg_count={msg_count}, "
                        f"session_messages_len={len(session.messages)}, "
                        f"canonical_len={len(canonical_messages)}"
                    )
            except Exception:
                self._undo_delete_cache = {}

        # 执行删除（清理状态后才设缓存，此时 hide_card 的清空效果对本次缓存无害）
        self._delete_user_round(card)

        # 非流式场景：_on_finalize_complete 不会运行，手动清除哨兵
        if not was_streaming:
            self._truncation_sentinel = None

        # 显示撤销卡片（先隐藏再显示，绕过 CardManager 的"已可见"检查）
        if self._undo_delete_cache:
            self._undo_delete_card.set_count(self._undo_delete_cache.get("count", 0))
            if self._card_manager.is_card_visible("undo_delete", self._window_id):
                self._card_manager.hide_card("undo_delete", self._window_id)
            self._card_manager.show_card("undo_delete", self._window_id)

    def _restore_deleted_message(self):
        """恢复被撤销删除的消息"""
        from loguru import logger

        if not self._undo_delete_cache:
            return

        cache = self._undo_delete_cache
        self._undo_delete_cache = {}  # 立即清空，防止重复恢复

        session = self.session_manager.get_current_session()
        if not session or session.session_id != cache["session_id"]:
            logger.warning(
                "[RESTORE] Session changed, cannot restore: "
                f"cache_session_id={cache['session_id']!r}, "
                f"current_session_id={session.session_id if session else None!r}, "
                f"session_exists={session is not None}, "
                f"cache_insert_index={cache.get('insert_index')}, "
                f"cache_msg_count={cache.get('count')}, "
                f"session_messages_len={len(session.messages) if session else 0}"
            )
            return

        # 恢复消息到 session
        messages = list(session.messages)
        insert_at = min(cache["insert_index"], len(messages))
        messages[insert_at:insert_at] = cache["messages"]
        session.set_messages(messages, preserve_compaction=False)
        self._session_dirty = True  # 🛡️ 消息被恢复，脏标记兜底

        # 保存并刷新视图
        if self._current_session_id != session.session_id:
            self._current_session_id = session.session_id

        # 直接保存到 history_manager，确保数据不丢失
        try:
            if self.history_manager:
                from app.widgets.ui_helpers import get_session_compaction_info

                compaction_info = get_session_compaction_info(session)
                idx = self.history_manager.find_index_by_session_id(self._current_session_id)
                worktree_path = self._get_current_worktree_path()
                worktree_kwargs = {"worktree_path": worktree_path or ""}
                if idx is not None:
                    # 🛡️ 更新已有会话时不传 project，保留该会话原有的项目归属
                    self.history_manager.update_session(
                        idx,
                        session.messages,
                        **compaction_info,
                        **worktree_kwargs,
                    )
                else:
                    # 🛡️ 罕见路径（恢复时历史被截断）：用 originating_project 优先的
                    # fallback 链，避免被当前 _current_project 错误覆盖
                    resolved_project = self._resolve_session_project_fallback(
                        session.session_id, self._current_project, session=session
                    )
                    self.history_manager.save_session(
                        session.messages,
                        session_id=session.session_id,
                        project=resolved_project,
                        **compaction_info,
                        **worktree_kwargs,
                    )
        except Exception as e:
            logger.error(f"[RESTORE] Failed to persist session: {e}")

        # 恢复文件操作（重写 AI 编辑后的文件内容）
        # （write_file 兼容分支已删：DB 实证 0 记录；fr_op 通用结构保留，
        #   含 content 的条目即按内容重写）
        file_restore_ops = cache.get("file_restore_ops", [])
        for fr_op in file_restore_ops:
            try:
                fp = fr_op.get("file_path")
                content = fr_op.get("content", "")
                if fp and content:
                    Path(fp).parent.mkdir(parents=True, exist_ok=True)
                    with open(fp, "w", encoding="utf-8") as f:
                        f.write(content)
                    logger.info(f"[RESTORE] 已恢复文件编辑: {fp}")
            except Exception as e:
                logger.error(f"[RESTORE] 文件恢复失败: {fr_op.get('file_path')} - {e}")

        # 刷新视图
        self._invalidate_current_session_card_cache()
        try:
            self._display_current_session()
        except Exception as e:
            logger.error(f"[RESTORE] _display_current_session failed: {e}")
            # fallback: 强制重建
            try:
                self._clear_chat_area()
                self._message_batch = group_messages_for_display(session.messages)
                self._batch_cards = [None for _ in self._message_batch]
                self._build_user_prefix_cache()
                if self._message_batch:
                    self._visible_batch_end = len(self._message_batch)
                    self._visible_batch_start = max(0, self._visible_batch_end - self._initial_visible_batch_count)
                    self._load_message_batch(initial=True)
                    self._sync_batch_structures()
            except Exception as e2:
                logger.error(f"[RESTORE] Fallback render also failed: {e2}")

        # 🛡️ 验证恢复是否成功：检查 _display_current_session 后布局中是否有消息卡片
        # 若 chat_layout 中没有消息卡片（可能被某些边缘情况重置），强制重建
        if self.chat_layout.count() == 0 or not any(
            isinstance(self.chat_layout.itemAt(i).widget(), MessageCard)
            for i in range(self.chat_layout.count())
            if self.chat_layout.itemAt(i) and self.chat_layout.itemAt(i).widget()
        ):
            if session.messages and not getattr(self, "_is_destroyed", False):
                logger.warning("[RESTORE] Display session produced no cards, forcing rebuild")
                try:
                    self._clear_chat_area()
                    self._message_batch = group_messages_for_display(session.messages)
                    self._batch_cards = [None for _ in self._message_batch]
                    self._build_user_prefix_cache()
                    if self._message_batch:
                        self._visible_batch_end = len(self._message_batch)
                        self._visible_batch_start = max(0, self._visible_batch_end - self._initial_visible_batch_count)
                        self._load_message_batch(initial=True)
                        self._sync_batch_structures()
                except Exception as e:
                    logger.error(f"[RESTORE] Force rebuild failed: {e}")

        # 恢复后消息数变化，显式刷新历史问题徽章（不依赖 _display_current_session
        # 的内部分支路由，保证 badge 计数与恢复后的会话一致）
        self._update_history_questions_badge()

        # 确保用户看到恢复后的最后一条消息
        QTimer.singleShot(200, self._scroll_to_bottom)

        # 🛡️ 恢复成功后同步 CardManager 状态：_on_restore_clicked 直接调了
        # setVisible(False) 绕过 CardManager，这里通知 CardManager 更新状态，
        # 防止下次删除时 hide_card → dismissed 清空新缓存。
        if self._card_manager.is_card_visible("undo_delete", self._window_id):
            self._card_manager.hide_card("undo_delete", self._window_id)

    def _on_undo_delete_dismissed(self):
        """撤销删除卡片自动消失或被关闭时，清空缓存"""
        if self._undo_delete_cache:
            logger.debug(
                "[UNDO-DISMISS] Cache cleared without restore: "
                f"session_id={self._undo_delete_cache.get('session_id')!r}, "
                f"count={self._undo_delete_cache.get('count')}"
            )
        self._undo_delete_cache = {}

    def _delete_user_round(self, card: MessageCard):
        """
        删除单个 round：找到 card 在 chat_layout 中的位置，
        删除该 user card 及其后直到下一个 user card 之间的所有卡片
        """
        from loguru import logger

        logger.info(f"[DELETE] Starting deletion for card at round_index={card._round_index}")

        # [DEBUG-diagnose-welcome] 记录删除前状态
        logger.info(
            f"[DEBUG-diagnose-welcome] _delete_user_round BEFORE: session.messages count = {len(self.session_manager.get_current_session().messages) if self.session_manager.get_current_session() else 0}"
        )
        logger.info(
            f"[DEBUG-diagnose-welcome] _delete_user_round BEFORE: chat_layout.count = {self.chat_layout.count()}"
        )

        # === 1. 删除 UI 卡片：基于 card widget 对象在 layout 中的位置 ===
        # 找到 card 在 chat_layout 中的索引
        card_layout_idx = -1
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget() is card:
                card_layout_idx = i
                break

        if card_layout_idx < 0:
            logger.warning("[DELETE] Card not found in layout")
            return

        # 收集要删除的 widgets：从 card 开始，直到下一个 user card 或末尾
        widgets_to_remove = [card]
        for i in range(card_layout_idx + 1, self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            w = item.widget()
            # 遇到下一个 user card 就停止
            if hasattr(w, "role") and w.role == "user" and not getattr(w, "_is_welcome", False):
                break
            widgets_to_remove.append(w)

        from app.widgets.ui_helpers import delete_widgets_from_layout

        delete_widgets_from_layout(widgets_to_remove, self.chat_layout)
        logger.info(f"[DELETE] Removed {len(widgets_to_remove)} cards from UI")

        # [DEBUG-diagnose-welcome] 记录删除 UI 后状态
        logger.info(
            f"[DEBUG-diagnose-welcome] _delete_user_round AFTER UI delete: chat_layout.count = {self.chat_layout.count()}"
        )

        # === 2. 更新 session 数据 ===
        session = self.session_manager.get_current_session()
        if not session:
            logger.error("[DELETE] No session found")
            return

        # 从 card._round_index 计算 session 截断位置
        round_index = card._round_index
        if round_index is None:
            logger.error("[DELETE] Card has no _round_index")
            return

        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)

        if round_index < 0 or round_index >= len(round_ranges):
            logger.warning(f"[DELETE] Invalid round_index: {round_index}")
            # [DEBUG-diagnose-welcome] 记录无效 round_index
            logger.info(
                "[DEBUG-diagnose-welcome] _delete_user_round: INVALID round_index, will return without showing welcome"
            )
            # 仍显示撤销卡片（缓存已设置）
            if self._undo_delete_cache:
                self._card_manager.show_card("undo_delete", self._window_id)
            return

        success, old_count, new_count = truncate_and_remove_round(session, round_index, round_ranges)
        if not success:
            # [DEBUG-diagnose-welcome] 记录 truncate 失败
            logger.info("[DEBUG-diagnose-welcome] _delete_user_round: truncate_and_remove_round FAILED")
            return

        log_deletion_stats(round_index, len(widgets_to_remove), old_count, new_count)

        # [DEBUG-diagnose-welcome] 记录 truncate 成功后的 session 状态
        logger.info(
            f"[DEBUG-diagnose-welcome] _delete_user_round AFTER truncate: session.messages count = {len(session.messages)}, new_count = {new_count}"
        )

        # === 3. 同步 _message_batch 和 _batch_cards 到 session 的新状态 ===
        self._message_batch = group_messages_for_display(session.messages)
        # 重建 _batch_cards：从 layout 中存活卡片按顺序分配到对应 batch 位置
        # 不能简单裁剪（_batch_cards[:new_len]），因为删除中间 round 后，
        # 后面 round 的卡片（如 c_u2）仍在 layout 中但会被裁剪掉，
        # 导致后续这些卡片的 _round_index 永远无法被 _refresh_all_cards_round_index 更新。
        self._rebuild_batch_cards_from_layout()
        # 重建 user 前缀和缓存
        self._build_user_prefix_cache()

        # === 4. 保存 session ===
        if self._current_session_id != session.session_id:
            self._current_session_id = session.session_id

        try:
            self._persist_session_after_mutation()
        except Exception as e:
            logger.error(f"[DELETE] Failed to persist session: {e}")

        # 同步剩余卡片的 _round_index（删除后后面卡片的 round 会偏移）
        self._refresh_all_cards_round_index()
        self._finalize_local_session_mutation()

        # 显式更新历史问题徽章（_finalize_local_session_mutation 通过 _update_node_preview
        # 间接调用，但异常路径可能跳过，此处确保徽章同步）
        self._update_history_questions_badge()

    def _undo_from_message(self, card: MessageCard):
        if card.role != "user":
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        # 同步 _current_session_id（与 _delete_user_round 保持一致）
        if self._current_session_id != session.session_id:
            self._current_session_id = session.session_id

        # 🛡️ 先清理 CardManager 中残留的可见状态（与 _delete_message 同理，
        # _on_restore_clicked 直接调 setVisible(False) 绕过了 CardManager），
        # 防止后续显示撤销卡片时 hide_card → dismissed 清空缓存。
        if self._card_manager.is_card_visible("undo_delete", self._window_id):
            self._card_manager.hide_card("undo_delete", self._window_id)

        # 获取当前 session 的 round_ranges，用于验证 round_index
        canonical_now = consolidate_messages(session.messages)
        round_ranges_now = get_user_round_ranges(canonical_now)

        # 优先使用 card._round_index，但需要验证是否仍然有效
        # （删除/撤销前面的 round 会导致后面卡片的 round_index 偏移）
        round_index = card._round_index
        if round_index is not None and (round_index < 0 or round_index >= len(round_ranges_now)):
            # round_index 已过时，降级重算
            round_index = None

        if round_index is None:
            # 降级方法 1：通过布局遍历确定卡片位置（最可靠）
            round_index = self._find_user_round_index_for_card(card)
            if round_index is not None and (round_index < 0 or round_index >= len(round_ranges_now)):
                round_index = None

        if round_index is None:
            # 降级方法 2：使用 _message_index 估算
            if card._message_index is not None:
                round_index = 0
                for idx in range(card._message_index):
                    if (
                        idx < len(self._message_batch)
                        and self._message_batch[idx]
                        and self._message_batch[idx][0].get("role") == "user"
                    ):
                        round_index += 1
                if round_index >= len(round_ranges_now):
                    round_index = None

        if round_index is None:
            # 最终降级：使用 session 文本匹配
            user_text = card.get_plain_text()
            timestamp = card.timestamp
            round_index = self._find_user_round_index_from_session(session, user_text, timestamp)

        if round_index is None or round_index < 0 or round_index >= len(round_ranges_now):
            logger.warning("[UNDO] Cannot determine valid round_index for card")
            return

        # === 缓存撤销数据，用于恢复（只缓存一步）===
        try:
            # 撤销：删除从该 round 到末尾的所有消息
            # 🛡️ 使用 canonical_now 而非 session.messages，确保索引一致。
            # consolidate_messages 可能过滤掉非标准消息，导致 session.messages
            # 与 canonical_now 长度不一致，直接使用 session.messages 切片会取错位置。
            start_idx = round_ranges_now[round_index][0]
            msg_count = len(canonical_now) - start_idx
            self._undo_delete_cache = {
                "session_id": session.session_id,
                "messages": list(canonical_now[start_idx:]),  # 使用 canonical 消息
                "insert_index": start_idx,
                "count": msg_count,
            }
            logger.debug(
                "[UNDO] Cache set: "
                f"session_id={session.session_id!r}, "
                f"start_idx={start_idx}, "
                f"msg_count={msg_count}, "
                f"session_messages_len={len(session.messages)}, "
                f"canonical_len={len(canonical_now)}"
            )
        except Exception:
            self._undo_delete_cache = {}

        if self._is_streaming:
            self._on_stop_clicked()

        # 获取待回滚的文件操作（从该轮次到最后的全部）
        all_call_ids = self._get_all_tool_call_ids_from_round(round_index)

        # 如果有文件操作，显示预览对话框
        if all_call_ids and self.backend.tool_executor and self.backend.file_recorder:
            # 使用辅助函数收集操作
            operations = collect_operations_for_round(
                self.backend.file_recorder, self._current_session_id, all_call_ids
            )

            if operations:
                dialog = FileUndoCard(operations, self.backend.file_recorder, self)
                result = dialog.exec_()

                if result == FileUndoCard.CANCEL:
                    return  # 取消撤销，什么都不做

                # 执行回滚 - 只还原选中的操作
                selected_ops = dialog.get_selected_operations()
                if selected_ops:
                    # 在回滚前，缓存文件当前内容（AI 编辑后的版本），用于后续恢复
                    # （write_file 兼容分支已删：DB 实证 0 记录，工具名已为
                    #   write/edit/multi_edit；file_restore_ops 通用结构保留，
                    #   供后续按 registry 文件写入组分组的恢复实现复用）
                    file_restore_ops = []
                    self._undo_delete_cache["file_restore_ops"] = file_restore_ops

                    result = self.backend.file_recorder.rollback_operations(selected_ops)
                    self._show_undo_result(result)

        # 再次验证 round_index 是否仍有效（dialog.exec_() 期间 session 可能变化）
        session_final = self.session_manager.get_current_session()
        if session_final:
            canonical_final = consolidate_messages(session_final.messages)
            round_ranges_final = get_user_round_ranges(canonical_final)
            if round_index < 0 or round_index >= len(round_ranges_final):
                logger.warning(
                    "[UNDO] round_index became invalid after dialog, recalculating: "
                    f"round_index={round_index}, available={len(round_ranges_final)}"
                )
                # 尝试通过布局重新计算
                round_index = self._find_user_round_index_for_card(card)
                if round_index is None or round_index < 0 or round_index >= len(round_ranges_final):
                    logger.error("[UNDO] Cannot recover round_index after dialog, aborting undo")
                    return

        if not self._truncate_session_from_user_round(round_index=round_index, card=card):
            return

        # 显示撤销卡片（先隐藏再显示，绕过 CardManager 的"已可见"检查）
        if self._undo_delete_cache:
            self._undo_delete_card.set_count(self._undo_delete_cache.get("count", 0))
            if self._card_manager.is_card_visible("undo_delete", self._window_id):
                self._card_manager.hide_card("undo_delete", self._window_id)
            self._card_manager.show_card("undo_delete", self._window_id)

        # 恢复输入框内容
        restore_input_from_card(self.input_area, card)

        # 撤销后消息数变化，显式刷新历史问题徽章
        self._update_history_questions_badge()

    def _get_last_tool_call_id_after_round(self, round_index: int) -> Optional[str]:
        """获取指定 round_index 之后最后一个 tool_call_id"""
        session = self.session_manager.get_current_session()
        if not session:
            return None

        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)

        return find_last_tool_call_id_after_round(canonical_messages, round_ranges, round_index)

    def _get_all_tool_call_ids_from_round(self, round_index: int) -> List[str]:
        """获取从指定 round 到最后的所有 tool_call_id"""
        session = self.session_manager.get_current_session()
        if not session:
            return []

        start_idx, _ = get_round_message_indices(session, round_index)
        if start_idx is None:
            return []

        canonical_messages = consolidate_messages(session.messages)

        # 使用辅助函数收集剩余的 tool_call_id
        return collect_tool_call_ids(canonical_messages, start_idx, len(canonical_messages))

    def _get_tool_call_ids_in_round(self, round_index: int) -> List[str]:
        """获取指定 round 范围内的所有 tool_call_id"""
        session = self.session_manager.get_current_session()
        if not session:
            return []

        start_idx, end_idx = get_round_message_indices(session, round_index)
        if start_idx is None:
            logger.warning(f"[card-diff] get_round_message_indices returned None for round_index={round_index}")
            return []

        canonical_messages = consolidate_messages(session.messages)
        logger.debug(
            f"[card-diff] round_index={round_index}, start_idx={start_idx}, end_idx={end_idx}, total_msgs={len(canonical_messages)}"
        )

        # 使用辅助函数收集 tool_call_id
        return collect_tool_call_ids(canonical_messages, start_idx, end_idx)

    def _show_undo_result(self, result):
        """显示撤销结果"""
        if result.failed_count > 0:
            failed_list = format_file_list(result.failed_files, max_count=5)
            InfoBar.warning(
                "部分文件回滚失败",
                f"成功: {result.success_count}, 失败: {result.failed_count}\n{failed_list}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
        elif result.success_count > 0:
            InfoBar.success(
                "文件已回滚",
                f"已恢复 {result.success_count} 个文件",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )

    def _on_tool_diff_requested(self, tool_call_id: str):
        """
        处理工具差异对比请求

        优先通过 file_recorder 的备份文件生成差异；
        若备份文件缺失，则从会话消息中提取内嵌 diff 内容作为 fallback。

        Args:
            tool_call_id: 工具调用 ID
        """
        if not tool_call_id:
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        session_id = session.session_id

        # 检查是否有 file_recorder
        if not self.backend.tool_executor or not self.backend.file_recorder:
            logger.warning("[LLMChatter] file_recorder 未初始化，尝试从消息内容回退")
            self._show_diff_from_message_content(session, tool_call_id)
            return

        try:
            # 获取该 tool_call_id 对应的文件操作记录
            operations = self.backend.file_recorder.get_operations_for_preview(
                session_id=session_id, call_id=tool_call_id
            )

            # 使用辅助函数获取第一个文件操作
            success, backup_path, _ = get_first_file_operation(operations)
            if not success:
                # 🛡️ Fallback：备份文件缺失时，从会话消息中提取内嵌 diff
                logger.info(
                    f"[LLMChatter] file_recorder 无操作记录 (tool_call_id={tool_call_id[:12]})，回退到消息内嵌 diff"
                )
                self._show_diff_from_message_content(session, tool_call_id)
                return

            # 使用辅助函数读取备份文件和生成 diff
            old_content, new_content, backup_file = read_backup_files(backup_path)
            html = generate_diff_html(old_content, new_content, backup_file)

            # 显示差异
            show_diff_viewer(self, html)

        except FileNotFoundError:
            # 备份文件被清理或不存在，回退到消息内嵌 diff
            logger.info(f"[LLMChatter] 备份文件不存在 (tool_call_id={tool_call_id[:12]})，回退到消息内嵌 diff")
            self._show_diff_from_message_content(session, tool_call_id)
        except Exception as e:
            logger.error(f"[LLMChatter] 显示工具差异失败: {e}")
            InfoBar.error(
                "差异显示失败",
                str(e),
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )

    def _show_diff_from_message_content(self, session, tool_call_id: str):
        """从会话消息内容中提取内嵌 diff 并显示（file_recorder 无数据时的 fallback）

        遍历 session.messages 查找 role==tool 且 tool_call_id 匹配的消息，
        提取其 diff 字段，生成 HTML 报告并展示。
        """
        from app.utils.diff_viewer import DiffHtmlGenerator

        diff_content = None

        for msg in session.messages:
            if msg.get("role") != "tool":
                continue
            if msg.get("tool_call_id") != tool_call_id:
                # 兼容 content 字符串格式中的 tool_call_id
                content = msg.get("content", "")
                if isinstance(content, str) and f"tool_call_id: {tool_call_id}" in content:
                    pass  # 字符串格式匹配，继续提取
                else:
                    continue
            diff_content = msg.get("diff")
            break

        if not diff_content:
            InfoBar.warning(
                "无差异信息",
                "此工具没有修改任何文件，或差异数据已丢失",
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            return

        try:
            html = DiffHtmlGenerator.generate_html_report(diff_content)
            show_diff_viewer(self, html)
        except Exception as e:
            logger.error(f"[LLMChatter] 消息内嵌 diff 显示失败: {e}")
            InfoBar.error(
                "差异显示失败",
                str(e),
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )

    def _show_diff_from_messages_in_round(self, session, round_index: int, call_ids: List[str]) -> bool:
        """从 round 范围内 tool 消息提取内嵌 diff 并展示（file_recorder call_id 不匹配时的 fallback）

        团队/subagent 场景：主消息 tool_call_id = subagent_para 派发 id，
        子智能体执行工具（write/edit）时的 call_id 是子智能体自己的工具调用 id，
        两者不匹配 → file_recorder 按 (session_id, call_id) 精确查询匹配不到 →
        collect_operations_for_round 返回空 → 卡片差异误报"没有差异"。

        此方法遍历 round 范围内 tool 消息提取内嵌 `diff` 字段（多 call_id 匹配），
        生成合并 HTML 报告展示。直接工具调用（非 subagent）的 tool 消息也带
        diff 字段，故普通会话的备份缺失场景同样受益。

        Args:
            session: 当前会话
            round_index: 用户回合索引
            call_ids: 该 round 范围内的 tool_call_id 列表

        Returns:
            True 找到并展示了内嵌 diff；False 无内嵌 diff
        """
        from app.utils.diff_viewer import DiffHtmlGenerator

        start_idx, end_idx = get_round_message_indices(session, round_index)
        if start_idx is None:
            return False

        canonical = consolidate_messages(session.messages)
        call_ids_set = set(call_ids or [])
        diff_parts: List[str] = []

        for i in range(start_idx, min(end_idx, len(canonical))):
            msg = canonical[i]
            if msg.get("role") != "tool":
                continue
            # 匹配 round 内任一 call_id（含 content 字符串格式中的 tool_call_id）
            tid = msg.get("tool_call_id", "")
            content = msg.get("content", "")
            matched = False
            if tid and tid in call_ids_set:
                matched = True
            elif isinstance(content, str):
                for cid in call_ids_set:
                    if f"tool_call_id: {cid}" in content:
                        matched = True
                        break
            if not matched:
                continue
            diff_content = msg.get("diff")
            if not diff_content:
                continue
            # 🆕 review#30-#1：normalize_message（message_content.py L748）把任何
            # dict/list diff 序列化为 Python repr 字符串
            # （normalized["diff"] = str(message.get("diff"))），故 canonical 消息的
            # diff 恒为 str，直接 isinstance(dict/list) 分支不可达（此前实现陷阱）。
            # 为兼容未来工具/worker 产出 dict/list diff（避免 repr 垃圾串塞进
            # generate_html_report 产出空白 diff 视图、短路方案 C 会话级真实 diff），
            # 对 repr 字符串尝试 ast.literal_eval 恢复：解析出 dict 则取其 "diff" 键值、
            # 解析出 list 则逐项拼接；解析失败（纯字符串 diff，真实现状）保持原样。
            # literal_eval 只解析字面量（str/dict/list/tuple/数字/None），不执行代码，无注入风险。
            _candidate = str(diff_content)
            if _candidate.startswith(("{", "[")):
                try:
                    import ast as _ast

                    _parsed = _ast.literal_eval(_candidate)
                    if isinstance(_parsed, dict) and _parsed.get("diff"):
                        _candidate = str(_parsed.get("diff"))
                    elif isinstance(_parsed, list):
                        _candidate = "\n".join(str(x) for x in _parsed if x)
                except ValueError, SyntaxError:
                    pass  # 非字面量 repr，保持原样
            diff_parts.append(_candidate)

        if not diff_parts:
            return False

        try:
            combined = "\n".join(diff_parts)
            html = DiffHtmlGenerator.generate_html_report(combined, "")
            show_diff_viewer(self, html)
            return True
        except Exception as e:
            logger.error(f"[LLMChatter] 消息内嵌 diff（round 范围）显示失败: {e}")
            return False

    def _on_subagent_log_requested(self, task_ids_str: str):
        """
        处理子智能体日志查看请求 — 使用紧凑卡片显示任务信息

        Args:
            task_ids_str: 逗号分隔的任务ID列表
        """
        if not task_ids_str:
            return

        task_ids = [tid.strip() for tid in task_ids_str.split(",") if tid.strip()]
        if not task_ids:
            return

        sub_agent_mgr = self.backend.sub_agent_manager
        if not sub_agent_mgr:
            logger.warning("[LLMChatter] sub_agent_manager 未初始化")
            return

        compact = self._sub_agent_compact_widget
        found_any = False

        for task_id in task_ids:
            task_data = sub_agent_mgr.get_task_logs(task_id)
            if not task_data.get("found"):
                continue

            found_any = True
            summary = task_data.get("summary", {})
            agent_name = summary.get("agent_name", task_data.get("agent_name", "未知"))
            task_desc = summary.get("task_description", task_data.get("task_description", ""))
            tool_count = summary.get("tool_call_count", 0)
            elapsed = summary.get("elapsed_seconds", 0)
            model_name = summary.get("model_name", task_data.get("model_name", ""))
            ctx_usage = summary.get("context_usage", "")

            # 已在 compact 中则跳过
            if task_id in compact._task_rows:
                continue

            compact.show_completed_task(
                task_id,
                agent_name,
                task_desc,
                model_name=model_name,
                tool_call_count=tool_count,
                elapsed_seconds=elapsed,
                context_usage=ctx_usage,
            )

        if not found_any:
            return

        # 显示紧凑卡片
        self._card_manager.show_card("sub_agent_compact", self._window_id)

    def _on_card_diff_requested(self, round_index: int, message_index: int = -1):
        """
        处理卡片级差异对比请求，汇总一次对话中所有工具调用的文件修改。

        Args:
            round_index: 用户回合索引
            message_index: 消息在 _message_batch 中的索引（用于 fallback）
        """
        if round_index < 0 and message_index < 0:
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        # 验证 round_index 是否有效，如果无效则尝试从 message_index 重新计算
        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)
        if round_index < 0 or round_index >= len(round_ranges):
            logger.debug(f"[card-diff] round_index={round_index} out of range ({len(round_ranges)}), recomputing")
            round_index = -1

        if round_index < 0 and message_index >= 0:
            # 从 _message_batch 中的位置计算 round_index
            computed = 0
            for idx in range(message_index):
                if (
                    idx < len(self._message_batch)
                    and self._message_batch[idx]
                    and self._message_batch[idx][0].get("role") == "user"
                ):
                    computed += 1
            if computed < len(round_ranges):
                round_index = computed
                logger.debug(f"[card-diff] recomputed round_index={round_index} from message_index={message_index}")

        if round_index < 0 or round_index >= len(round_ranges):
            logger.warning("[card-diff] cannot determine valid round_index")
            return

        session_id = session.session_id
        logger.debug(
            f"[card-diff] requested round_index={round_index}, session_id={session_id}, msg_count={len(session.messages)}"
        )

        # 检查是否有 file_recorder
        if not self.backend.tool_executor or not self.backend.file_recorder:
            logger.warning("[LLMChatter] file_recorder 未初始化")
            return

        try:
            # 获取该 round 范围内的所有 tool_call_id
            all_call_ids = self._get_tool_call_ids_in_round(round_index)
            logger.debug(f"[card-diff] found call_ids: {all_call_ids}")

            if not all_call_ids:
                InfoBar.warning(
                    "无差异信息",
                    "此对话没有修改任何文件",
                    duration=3000,
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.BOTTOM,
                )
                return

            # 使用辅助函数收集所有工具的文件操作
            all_operations = collect_operations_for_round(self.backend.file_recorder, session_id, all_call_ids)

            if not all_operations:
                # 🆕 方案 A：file_recorder 按 (session_id, call_id) 精确查询匹配不到
                # （团队/subagent 场景 call_id 漂移）→ 回退到 round 范围内消息内嵌
                # diff 字段（subagent_worker 写回的 tool 消息 / 直接工具调用消息都带 diff）。
                if self._show_diff_from_messages_in_round(session, round_index, all_call_ids):
                    return
                # 🆕 方案 C：兜底引导——不再单纯误报"没有差异"。
                # 展示会话级 diff 汇总（get_all_operations_for_session 按 session 全量查
                # 不受 call_id 漂移影响），并提示用户可点工具运行框查看单工具差异。
                session_ops = []
                try:
                    session_ops = self.backend.file_recorder.get_all_operations_for_session(session_id)
                except Exception as _e:
                    logger.warning(f"[card-diff] 会话级查询失败: {_e}")
                    session_ops = []
                if session_ops:
                    try:
                        html = generate_multi_file_diff_html(session_ops)
                        show_diff_viewer(self, html)
                        InfoBar.info(
                            "会话级差异",
                            "卡片级未精确匹配到文件操作，已展示整个会话的差异汇总。\n"
                            "提示：团队/子智能体场景 call_id 可能不匹配，可点击工具运行框查看单工具差异。",
                            duration=5000,
                            parent=TabManagerWindow.get_instance() or self.window(),
                            position=InfoBarPosition.BOTTOM,
                        )
                        return
                    except Exception as _e2:
                        logger.error(f"[card-diff] 会话级 diff 展示失败: {_e2}")
                InfoBar.warning(
                    "无差异信息",
                    "此对话没有修改任何文件，或备份信息已丢失。\n提示：可点击工具运行框查看单工具差异。",
                    duration=4000,
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.BOTTOM,
                )
                return

            # 使用辅助函数生成合并的 diff HTML
            html = generate_multi_file_diff_html(all_operations)

            # 显示差异
            show_diff_viewer(self, html)

        except Exception as e:
            logger.error(f"[LLMChatter] 显示卡片差异失败: {e}")
            InfoBar.error(
                "差异显示失败",
                str(e),
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )

    def _on_review_requested(self, round_index: int, message_index: int = -1):
        """
        处理页脚 Review 按钮点击：收集该 round 内所有工具的文件修改，
        生成统一 diff 文本，作为任务描述触发 code-reviewer 子智能体快速审查。

        Args:
            round_index: 用户回合索引
            message_index: 消息在 _message_batch 中的索引（fallback）
        """
        if round_index < 0 and message_index < 0:
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        # 复用 _on_card_diff_requested 的 round_index 合法性校验 + fallback 推导
        # difflib 是标准库，移出 try 块以便失败时给出明确的诊断（不会被业务异常吞掉）
        import difflib

        from app.core.message_content import consolidate_messages, get_user_round_ranges

        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)
        if round_index < 0 or round_index >= len(round_ranges):
            logger.debug(f"[card-review] round_index={round_index} out of range ({len(round_ranges)}), recomputing")
            round_index = -1

        if round_index < 0 and message_index >= 0:
            computed = 0
            for idx in range(message_index):
                if (
                    idx < len(self._message_batch)
                    and self._message_batch[idx]
                    and self._message_batch[idx][0].get("role") == "user"
                ):
                    computed += 1
            if computed < len(round_ranges):
                round_index = computed

        if round_index < 0 or round_index >= len(round_ranges):
            logger.warning("[card-review] cannot determine valid round_index")
            return

        session_id = session.session_id

        if not self.backend.tool_executor or not self.backend.file_recorder:
            InfoBar.error(
                "审查失败",
                "file_recorder 未初始化",
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
            return

        try:
            from app.widgets.ui_helpers import (
                collect_operations_for_round,
                normalize_lines,
            )

            all_call_ids = self._get_tool_call_ids_in_round(round_index)
            logger.debug(f"[card-review] round_index={round_index}, call_ids={all_call_ids}")

            if not all_call_ids:
                InfoBar.warning(
                    "无差异信息",
                    "此对话没有可审查的文件修改",
                    duration=3000,
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.BOTTOM,
                )
                return

            all_operations = collect_operations_for_round(self.backend.file_recorder, session_id, all_call_ids)

            if not all_operations:
                InfoBar.warning(
                    "无差异信息",
                    "此对话没有可审查的文件修改，或备份信息已丢失",
                    duration=3000,
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.BOTTOM,
                )
                return

            # 收集统一 diff 文本（每个文件一段 unified diff），用于注入 review 任务描述

            # 写工具白名单：只审查产生文件内容改动的工具（registry group 派生，与
            # tool_executor._is_write_group 同源；str_replace_editor/apply_patch 为
            # Claude Code 生态名，file_recorder 只记录 registry 文件写入组工具，永不出现）

            def _write_group_tools() -> set:
                """文件写入组工具集合（registry 派生；异常时回退旧白名单保持过滤语义）"""
                try:
                    from app.tools.registry import ToolRegistry

                    return set(ToolRegistry.get_instance().tools_in_group("文件写入"))
                except Exception:
                    return {"write", "edit", "multi_edit", "str_replace_editor", "apply_patch"}

            WRITE_TOOLS = _write_group_tools()

            # 按文件路径分组：同一文件在当轮的多次连续编辑合并为一份累积 diff，
            # 避免文件名列重复、diff 碎片化，让 review 看清从初始到最终的完整变化。
            file_ops: dict = {}
            for op in all_operations:
                backup_path = op.get("backup_path", "")
                file_path = op.get("file_path", "")
                tool_name = op.get("tool_name", "")
                if not backup_path:
                    continue
                if tool_name not in WRITE_TOOLS:
                    continue
                key = file_path or Path(backup_path).name
                if key not in file_ops:
                    file_ops[key] = []
                file_ops[key].append(op)

            diff_text_parts: list[str] = []
            file_summaries: list[str] = []

            for file_key, ops in file_ops.items():
                # 首操作的备份 = 编辑前的原始内容；末态 = 编辑后的最终内容
                first_bp = ops[0].get("backup_path", "")
                last_bp = ops[-1].get("backup_path", "")
                last_file_path = ops[-1].get("file_path", "") or file_key

                # --- 编辑前内容：直接读首操作的前置备份，不再经 read_backup_files。
                # read_backup_files 会强制要求首操作的 .after.bak 存在，但此处只需要
                # old_content，强依赖 .after.bak 属于误伤，会导致本可审查的文件被跳过。---
                old_content = ""
                try:
                    if first_bp and Path(first_bp).exists():
                        with open(first_bp, "r", encoding="utf-8", errors="replace") as f:
                            old_content = f.read()
                except Exception as exc:
                    logger.warning(f"[card-review] 跳过文件 {file_key}: 读取编辑前备份失败 {exc}")
                    continue

                # --- 编辑后内容：优先读 .after.bak 快照；缺失时回退读磁盘实时文件。
                # 必须与页脚差异统计 compute_diff_stats 的口径保持一致——后者始终对比
                # 实时文件，因此 .after.bak 缺失（编辑后备份静默失败/被清理/竞态）时，
                # 若这里不回退，就会出现“有差异统计却无可审查内容”的不一致。---
                new_content = None
                last_after = str(Path(last_bp).with_suffix(".after.bak")) if last_bp else ""
                try:
                    if last_after and Path(last_after).exists():
                        with open(last_after, "r", encoding="utf-8", errors="replace") as f:
                            new_content = f.read()
                    elif last_file_path and Path(last_file_path).exists():
                        with open(last_file_path, "r", encoding="utf-8", errors="replace") as f:
                            new_content = f.read()
                        logger.debug(f"[card-review] {file_key}: .after.bak 缺失，回退读实时文件")
                except Exception as exc:
                    logger.warning(f"[card-review] 跳过文件 {file_key}: 读取编辑后内容失败 {exc}")
                    continue

                if new_content is None:
                    logger.warning(
                        f"[card-review] 跳过文件 {file_key}: 编辑后内容不可得（.after.bak 与实时文件均缺失）"
                    )
                    continue

                old_lines = normalize_lines(old_content)
                new_lines = normalize_lines(new_content)
                diff_iter = difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=f"a/{file_key}",
                    tofile=f"b/{file_key}",
                    lineterm="\n",
                )
                diff_text = "".join(diff_iter)
                if diff_text:
                    diff_text_parts.append(diff_text)
                    file_summaries.append(file_key)

            if not diff_text_parts:
                InfoBar.warning(
                    "无可审查内容",
                    "本轮所有文件修改均无可对比的差异",
                    duration=3000,
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.BOTTOM,
                )
                return

            # 拼接统一 diff（用 \n\n 分隔文件，避免相邻文件首尾粘连导致 diff 块解析歧义）
            combined_diff = "\n\n".join(diff_text_parts)
            MAX_DIFF_CHARS = 60_000
            truncated = len(combined_diff) > MAX_DIFF_CHARS
            truncation_note = ""
            if truncated:
                # 先记录原始长度，再截断，否则截断后 len(combined_diff) ≈ MAX_DIFF_CHARS 会输出废话
                original_len = len(combined_diff)
                combined_diff = combined_diff[:MAX_DIFF_CHARS] + "\n\n... (diff 已截断，仅展示前 60KB)"
                truncation_note = f"\n\n> ⚠️ 差异已截断（原始 {original_len:,} 字符，仅展示前 {MAX_DIFF_CHARS:,} 字符）"

            # 构造 review 子智能体任务描述
            files_bullet = "\n".join(f"- `{p}`" for p in file_summaries[:50])
            if len(file_summaries) > 50:
                files_bullet += f"\n... 及其他 {len(file_summaries) - 50} 个文件"

            task_description = (
                "请对以下 `code-reviewer` 任务进行快速代码审查：\n\n"
                "## 范围\n"
                "本轮（user round）涉及以下文件修改：\n"
                f"{files_bullet}\n\n"
                "## 审查重点\n"
                "1. 计划对齐：实现是否符合本轮用户意图？\n"
                "2. 代码质量：命名、错误处理、类型安全、可维护性\n"
                "3. 架构与设计：是否遵循现有架构与 SOLID 原则\n"
                "4. 安全/性能：潜在漏洞、明显的性能问题\n"
                "5. 文档与规范：注释、命名是否符合项目规范\n\n"
                "## 文件差异（unified diff）\n"
                "```diff\n"
                f"{combined_diff}\n"
                "```"
                f"{truncation_note}\n\n"
                "请按 Critical / Important / Suggestions 三档分类输出结论，"
                "并对每条问题给出具体文件:行号与可执行的修复建议。"
            )

            # 触发 review 子智能体
            self._execute_subagent_task(
                agent_name="code-reviewer",
                task_description=task_description,
                with_context=False,
            )

            # InfoBar 提示也带上截断信息，避免用户误以为审查了完整 diff
            success_msg = f"code-reviewer 子智能体正在审查 {len(file_summaries)} 个文件"
            if truncated:
                success_msg += f"（diff 已截断至 {MAX_DIFF_CHARS // 1000}KB）"
            InfoBar.success(
                "Review 已启动",
                success_msg,
                duration=2500,
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )

        except Exception as e:
            logger.error(f"[LLMChatter] 触发 review 子智能体失败: {e}")
            InfoBar.error(
                "Review 失败",
                str(e),
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )

    def _on_save_file_requested(self, code: str, lang: str):
        """
        处理保存文件请求

        Args:
            code: 代码内容
            lang: 代码语言
        """
        # 使用辅助函数获取默认文件名和扩展名
        ext = get_language_extension(lang)
        default_name = get_default_save_filename(lang, code)

        # 弹出文件保存对话框
        from PyQt5.QtWidgets import QFileDialog

        file_path, _ = QFileDialog.getSaveFileName(
            self, "保存代码文件", default_name, f"代码文件 (*{ext});;所有文件 (*.*)"
        )

        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)
            InfoBar.success(
                "文件已保存",
                file_path,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )
        except Exception as e:
            logger.error(f"[LLMChatter] 保存文件失败: {e}")
            InfoBar.error(
                "保存失败",
                str(e),
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )

    def _on_code_action(self, code: str, action: str = "copy"):
        from loguru import logger

        try:
            logger.info(f"[_on_code_action] action={action}, code_len={len(code)}")
            if action == "insert":
                self.insertResponse.emit(code)
            elif action == "create":
                self.createResponse.emit(code)
            elif action == "copy":
                from PyQt5.QtWidgets import QApplication

                clipboard = QApplication.clipboard()
                clipboard.setText(code)
                # 复制成功提示 - 使用 self 作为 parent
                logger.info("[_on_code_action] showing InfoBar")
                InfoBar.success(
                    "已复制",
                    "",
                    duration=1500,
                    parent=TabManagerWindow.get_instance() or self.window(),
                    position=InfoBarPosition.BOTTOM,
                )
        except Exception as e:
            logger.error(f"[_on_code_action] 异常: {e}")

    def _scroll_to_bottom(self, sticky_ms: int = 0):
        self._pending_scroll_to_bottom = True
        if sticky_ms > 0:
            self._bottom_anchor_deadline = max(
                self._bottom_anchor_deadline,
                time.monotonic() + sticky_ms / 1000.0,
            )
        self._scroll_bottom_timer.start()

    def _do_scroll_to_bottom(self):
        # 窗口已销毁时跳过：避免 QTimer 回调在 closeEvent 之后访问已释放的 chat_scroll_area
        if getattr(self, "_is_destroyed", False):
            return
        if not self._pending_scroll_to_bottom:
            return
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        max_val = scroll_bar.maximum()
        scroll_bar.setValue(max_val)
        # 再次设置确保卡片高度变化后仍在底部
        scroll_bar.setValue(max_val)
        self._pending_scroll_to_bottom = False
        self._user_intentionally_away_from_bottom = False
        if self._bottom_anchor_deadline > time.monotonic():
            self._bottom_anchor_timer.start()
        else:
            # 即使anchor到期，也再延迟多次检查，防止多批懒渲染卡片撑开高度导致没到底
            # 🆕 retries 3 → 8：延长兜底窗口（8×300ms ≈ 2.4s），覆盖 WebEngine
            # 全量重渲染异步完成后的最终高度（长消息渲染可能超时）。
            QTimer.singleShot(150, lambda: self._ensure_at_bottom(retries=8))
        # 加载完成后抑制滚动同步，避免节点跑到渲染的卡片数量位置
        self._suppress_scroll_sync_count = 0

    def _ensure_at_bottom(self, retries: int = 8):
        """确保滚动条在底部，用于懒渲染卡片高度变化后的二次修正

        Args:
            retries: 剩余重试次数，即使 bottom anchor 过期，也重试几次处理懒加载
                （默认 8 次 × 300ms 间隔 ≈ 2.4s 兜底窗口，覆盖长消息重渲染延迟）
        """
        # 窗口已销毁时跳过：避免 QTimer.singleShot 回调在 closeEvent 之后
        # 访问已释放的 chat_scroll_area 触发 RuntimeError
        if getattr(self, "_is_destroyed", False):
            return
        # 如果用户已经主动滚离底部，不再强制拉回。
        # 🆕 例外：流式刚结束的 2s 宽限期内仍强制滚底（防止用户流式中途上滚
        # 导致结束时的兜底滚底被跳过 —— 用户意图此时通常仍是"看最新回复"，
        # 2s 后恢复拦截，避免打断用户读取历史）。
        if self._user_intentionally_away_from_bottom and time.monotonic() >= self._stream_finished_grace_until:
            return
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        if scroll_bar.value() < scroll_bar.maximum() - 20:
            scroll_bar.setValue(scroll_bar.maximum())
            # 懒渲染可能需要更长时间，延迟再次检查
            # 如果还有重试次数，即使 bottom anchor 过期也继续重试
            if retries > 0:
                QTimer.singleShot(300, lambda: self._ensure_at_bottom(retries - 1))
            elif self._bottom_anchor_deadline > time.monotonic():
                QTimer.singleShot(300, self._ensure_at_bottom)

    def _maintain_bottom_anchor(self):
        # 窗口已销毁时跳过：避免 _bottom_anchor_timer 回调在 closeEvent 之后
        # 访问已释放的 chat_scroll_area 触发 RuntimeError
        if getattr(self, "_is_destroyed", False):
            return
        if self._bottom_anchor_deadline <= time.monotonic():
            self._bottom_anchor_deadline = 0.0
            self._suppress_scroll_sync_count = 0
            return
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        self._bottom_anchor_timer.start()

    def _on_message_card_height_changed(self, _height: int):
        """卡片高度变化时的滚动处理
        仅当卡片 _content_just_loaded 标记为 True 时触发滚动并清除标记。
        这样内容加载触发的高度变化会滚底，而用户折叠操作不会。

        修复规则：
        1. 如果正在往顶部加载历史批次（用户主动向上滚动） → 不滚动
        2. 如果这是整个会话最后一张卡片 → 强制滚动到底（初始加载完成保证到最底端）
        3. 如果正在流式输出 → 滚动到底
        4. 如果滚动条已经在底部附近 → 滚动到底
        """
        sender = self.sender()
        if not isinstance(sender, MessageCard):
            return
        if not sender._content_just_loaded:
            return

        # 如果正在往顶部加载历史批次（用户主动向上滚动触发），不滚动到底部
        if self._is_loading_history_batches:
            sender._content_just_loaded = False
            return

        # 检查是否是整个会话的最后一张卡片
        is_last_card = False
        if hasattr(self, "_batch_cards") and self._batch_cards:
            # 遍历最后一个非空批次
            for batch in reversed(self._batch_cards):
                if batch is not None and batch:
                    # 过滤掉已删除的卡片，防止 sender in batch 触发 RuntimeError
                    alive_batch = [w for w in batch if not sip.isdeleted(w)]
                    if not alive_batch:
                        continue
                    # 检查当前 sender 是否在最后批次中
                    if sender in alive_batch:
                        # 检查是否是最后批次的最后一张卡片
                        if sender is alive_batch[-1]:
                            is_last_card = True
                    break

        # 判断规则
        if is_last_card or self._is_streaming:
            # 如果是最后一张卡片，或者正在流式输出 → 强制滚底
            self._scroll_to_bottom()
        else:
            scroll_bar = self.chat_scroll_area.verticalScrollBar()
            max_val = scroll_bar.maximum()
            current_val = scroll_bar.value()
            # 如果滚动条已经在底部附近 → 滚底（严格阈值，避免误触发）
            if max_val - current_val < 20:
                self._scroll_to_bottom()

        sender._content_just_loaded = False

    def handle_recommended_question(self, content: str, action: str):
        if action == "ask":
            self.input_area.clear()
            self.send_preset_question(content)
        elif action == "session":
            # session_id 直接就是 content
            session_id = content.strip()
            self._switch_to_session_by_id(session_id)
        # 注：mode 切换由 MessageCard.welcomeModeChanged（PyQt 层）触发，不走 contextActionRequested

    def _switch_to_session_by_id(self, session_id: str):
        """根据 session_id 切换到对应会话（始终从最新源加载，保证跨窗口数据一致）"""
        if not session_id:
            return

        # 🐛 修复（保存顺序）：先停止流式并应用中断消息，再保存当前会话。
        # 原实现先 _save_current_session_to_history() 再 stop_streaming()——
        # 保存的是旧消息（缺最后 partial 回复），且 stop 后 _on_finalize_complete
        # 会被下方 _session_switched 哨兵拦截，中断消息永久丢失。
        if self._is_streaming and self.backend.chat_engine:
            # 与 _create_new_session / _on_auto_compact_requested 对齐：
            # 接收 stop_streaming 返回值应用回 session + 复位 AI 状态（否则
            # TabPanel 边框停留在 streaming 动画，stop 后 _on_worker_finished
            # 因 is_streaming=False 忽略 stream_finished，idle 永不触发）。
            self._set_ai_state("idle")
            try:
                interrupted = self.backend.stop_streaming()
                if interrupted:
                    self._apply_interrupted_messages_to_session(interrupted)
                    self._session_dirty = True
            except Exception as e:
                logger.warning(f"[MainWidget] 切换会话停止流式失败: {e}")
            self._is_streaming = False
            self._topic_summary_cancelled = True  # 🛡️ 取消标题生成重试
        elif self.backend.chat_engine:
            self.backend.cleanup_worker()

        # 🛡️ 切换前保存当前会话：防止流式刚结束时用户立即切换会话，
        # _do_post_stream_cleanup 的延迟保存尚未触发导致 AI 回复丢失。
        # （此时已应用中断消息，保存的是完整会话）
        if self.history_manager:
            self._save_current_session_to_history()
            self.history_manager.flush()

        # 🛡️ 标记会话切换：stop_streaming 后 old worker 的 finished_with_messages
        # 信号虽已断开再连接，但 Qt 事件队列中可能仍有已投递的旧回调。
        # 设置哨兵防止这些迟到回调将旧会话消息写入新加载的会话。
        # 哨兵在 _on_send_clicked 发起新 AI 请求时清零。
        self._session_switched = True

        # 清理旧会话的卡片
        self._cache_current_session_cards()
        # 💡 内存优化：会话切换时清理 LRU 缓存
        _cleanup_global_lru_caches()
        # 只重置会话状态，保留 tool_executor
        self.backend.reset_session_state()

        # 始终从 history_manager/SQLite 加载最新数据（保证跨窗口一致性）
        # 即便 session_id 在当前 SessionManager 中存在，其他窗口可能已更新该会话
        session_record = self.history_manager.get_session_by_session_id(session_id)
        if session_record:
            messages = self.history_manager.get_session_messages(session_id)
            title = session_record.get("title") or session_record.get("name") or "历史对话"
            from app.widgets.ui_helpers import (
                create_session_from_record,
                init_after_loading_session,
            )

            restored = create_session_from_record(session_record, messages, title)
            init_after_loading_session(self, restored, session_id, title, self.backend)
            self._release_inactive_session_messages()
            # 同步项目
            session_project = session_record.get("project", "默认项目") or "默认项目"
            self._current_project = session_project
            self.backend._current_project = session_project
            self._project_label.setText(session_project)
            self._refresh_project_branch_style()
            # 同步到 tool_executor，确保 stage_files 等工具写入正确的项目
            if self.backend and self.backend.tool_executor:
                self.backend.tool_executor.set_current_project(session_project)
            self._refresh_project_branch_style()
            # 🐛 修复（团队标记同步）：加载团队会话后必须恢复 _team_run_id /
            # _team_name / _team_agent_name（对齐 _load_session_from_record 的
            # F4 逻辑）。否则后续 _auto_save_current_session 的 team_kwargs 守卫
            # （仅 _team_run_id 非空才传团队字段）会把团队会话当成普通会话保存，
            # 团队元数据被清空 → 会话从团队分组消失 / 恢复时漏成员。
            self._sync_team_markers_from_record(session_record)
            # 自动切换到该会话关联的 worktree
            # 规则：会话有 worktree_path → 切到该 worktree
            #       会话没有 worktree_path → 切回主仓库（如果当前在 worktree 中）
            worktree_path = session_record.get("worktree_path", "") or ""
            current_wt = self._get_current_worktree_path()
            if worktree_path and os.path.isdir(worktree_path) and worktree_path != current_wt:
                self._switch_to_worktree(worktree_path)
            elif not worktree_path and current_wt:
                self._restore_main_repo()
            self._display_current_session()
            self._release_inactive_session_messages()
            self._hide_welcome_cards()
        else:
            # fallback: session_id 不在 history_manager 中，尝试 SessionManager
            for i, session in enumerate(self.session_manager.get_all_sessions()):
                if session.session_id == session_id:
                    self.backend.switch_session(i)
                    self._display_current_session()
                    self._release_inactive_session_messages()
                    self._hide_welcome_cards()
                    return
            logger.warning(f"未找到 session_id: {session_id}")

    def _load_input_history(self):
        """从数据库加载输入历史到输入框"""
        try:
            if hasattr(self, "session_store") and self.session_store:
                history = self.session_store.get_input_history()
                self.input_area.load_history(history)
        except Exception:
            pass

    def _record_input_history(self, text: str, attachments: Optional[list] = None):
        """记录用户输入（含附件路径）到历史数据库"""
        try:
            if hasattr(self, "session_store") and self.session_store:
                self.session_store.add_input_history(text, attachments)
                # 更新输入框的历史缓存
                history = self.session_store.get_input_history()
                self.input_area.load_history(history)
        except Exception:
            pass

    # ==================== 历史模式附件管理 ====================

    def _on_entering_history_mode(self):
        """历史浏览模式即将进入 — 保存当前附件，退出时恢复"""
        self._history_working_attachments = self._attachments.copy()

    def _on_history_attachments_restored(self, paths: list):
        """历史浏览模式切换条目 — 恢复对应的附件芯片

        在历史模式中切换条目时，清理当前所有附件芯片，
        然后根据历史条目保存的路径列表重建 AttachmentChip。
        """
        self._clear_attachments()
        for p in paths:
            if p not in self._attachments and os.path.exists(p):
                self._attachments.append(p)
                chip = AttachmentChip(p, self._attach_container)
                chip.removed.connect(lambda path=p: self._remove_attachment(path))
                self._attach_layout.insertWidget(self._attach_layout.count() - 1, chip)
        self._attach_container.setVisible(bool(self._attachments))

    def _on_history_mode_exited(self):
        """退出历史浏览模式 — 恢复进入时保存的附件"""
        self._on_history_attachments_restored(self._history_working_attachments)

    # ==================== 附件管理 ====================

    def _on_files_dropped(self, paths: list[str]):
        """文件拖入/粘贴 → 添加 AttachmentChip"""
        for p in paths:
            if p not in self._attachments:
                self._attachments.append(p)
                chip = AttachmentChip(p, self._attach_container)
                chip.removed.connect(lambda path=p: self._remove_attachment(path))
                # 插入到 stretch 之前
                self._attach_layout.insertWidget(self._attach_layout.count() - 1, chip)
        self._attach_container.setVisible(bool(self._attachments))

    def _remove_attachment(self, path: str):
        """移除指定附件"""
        if path in self._attachments:
            self._attachments.remove(path)
            # 清理对应的 chip widget
            for i in range(self._attach_layout.count()):
                item = self._attach_layout.itemAt(i)
                if item and item.widget() and isinstance(item.widget(), AttachmentChip):
                    if item.widget().filepath == path:
                        item.widget().deleteLater()
                        break
        self._attach_container.setVisible(bool(self._attachments))

        # 清理输入框中对应的 [[basename]] 占位符
        try:
            basename = os.path.basename(path)
            placeholder = f"[[{basename}]]"
            current = self.input_area.toPlainText()
            if placeholder in current:
                new_text = current.replace(placeholder, "")
                # 清理多余空格
                new_text = new_text.replace("  ", " ").strip()
                self.input_area.setPlainText(new_text)
        except Exception:
            pass

    def _clear_attachments(self):
        """清空所有附件"""
        self._attachments.clear()
        # 先收集 widgets 再统一 removeWidget（不能在迭代中修改 layout）
        chips = []
        while self._attach_layout.count() > 1:
            item = self._attach_layout.takeAt(0)
            if item and item.widget():
                chips.append(item.widget())
        for chip in chips:
            self._attach_layout.removeWidget(chip)
            try:
                chip.setParent(None)
            except Exception:
                pass
            chip.deleteLater()
        self._attach_container.hide()

    def _build_user_text_with_attachments(self, user_text: str) -> str:
        """将附件文件路径拼接到用户文本末尾，支持 [[basename]] 内联占位符替换

        文本中出现 [[filename.ext]] 占位符的 → 替换为完整路径（精确定位）
        未出现占位符的附件 → 照旧拼接到末尾（优雅降级）
        残留的 [[xxx]] 不匹配任何附件 → 自动清除
        """
        if not self._attachments:
            # 无附件时，清除文本中残留的 [[...]] 占位符
            return re.sub(r"\[\[[^\]]*\]\]", "", user_text).replace("  ", " ").strip()

        # 第一轮：替换文本中出现的 [[basename]] 占位符
        # 同名文件使用 count=1 左到右逐次替换，每个附件占一个 [[basename]]
        referenced = set()
        for p in self._attachments:
            basename = os.path.basename(p)
            placeholder = f"[[{basename}]]"
            if placeholder in user_text:
                user_text = user_text.replace(placeholder, p, 1)
                referenced.add(p)

        # 第二轮：未在文本中引用的附件拼到末尾
        remaining = [p for p in self._attachments if p not in referenced]
        if remaining:
            parts = [user_text]
            for p in remaining:
                parts.append(p)
            user_text = "\n".join(parts)

        # 第三轮：清除残留的 [[xxx]] 占位符（不匹配任何附件）
        user_text = re.sub(r"\[\[[^\]]*\]\]", "", user_text)
        user_text = user_text.replace("  ", " ").strip()

        return user_text

    # ==================== 发送消息 ====================

    def send_preset_question(self, question: str):
        if not isinstance(question, str) or not question.strip():
            return
        self._on_send_clicked(user_text=question.strip())

    def _apply_agent_command_permissions(self, agent_name: str):
        """智能体命令(非子智能体)执行时,自动注入其设定的工具权限

        Args:
            agent_name: 智能体名(对应 Agent 数据类的 name)
        """
        from app.core.agent import AgentManager

        agent_manager = AgentManager.get_instance()
        if agent_manager is None:
            logger.warning("[AgentCommand] AgentManager 未初始化,跳过权限注入")
            return

        agent = agent_manager.get_agent(agent_name)
        if agent is None:
            logger.warning(f"[AgentCommand] 未找到智能体 '{agent_name}',跳过权限注入")
            return

        # 注入 agent 的工具权限到当前窗口的 controller
        self._tool_permission_controller.apply_agent(
            agent_name=agent_name,
            agent_tools=dict(agent.tools or {}),
            agent_permission=dict(agent.permission or {}),
        )
        logger.info(f"[AgentCommand] 已注入智能体 '{agent_name}' 的工具权限")

        # 主动刷新工具控制卡片(确保立即显示 agent 权限,避免信号时序问题)
        if hasattr(self, "_tool_control_card") and self._tool_control_card is not None:
            self._tool_control_card.refresh()

    @staticmethod
    def _encode_image_attachments_to_multimodal(user_text: str, image_paths: list, model_name: str) -> list | None:
        """把图片附件编码为 OpenAI multimodal 格式的 user content

        同步执行，但只在 _on_send_clicked 的 QTimer 推迟闭包内调用，
        此时 UI 已渲染、用户看到"等待 AI"状态，不会感知主线程短暂阻塞。

        Returns:
            multimodal content 列表（[text, image_url, image_url, ...]），
            或 None（编码失败/无图片）。
        """
        import base64

        _MIME_MAP = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        image_blocks = []
        for img_path in image_paths:
            try:
                with open(img_path, "rb") as f:
                    img_bytes = f.read()
                img_b64 = base64.b64encode(img_bytes).decode()
                ext = os.path.splitext(img_path)[1].lower()
                mime = _MIME_MAP.get(ext, "image/png")
                data_uri = f"data:{mime};base64,{img_b64}"

                # 图片大小检查：超过 5MB 自动压缩，防止 API 400
                if len(img_b64) > 5 * 1024 * 1024:
                    from app.core.workers.chat_worker import compress_data_uri

                    compressed = compress_data_uri(data_uri)
                    if compressed != data_uri:
                        data_uri = compressed
                        logger.info(f"[ImageAttach] 附件图片已压缩: {img_path}")

                image_blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
            except Exception as e:
                logger.warning(f"处理图片附件失败 {img_path}: {e}")
        if not image_blocks:
            return None
        logger.info(
            f"[ImageAttach] 模型 {model_name} 支持视觉，已将 {len(image_blocks)} 张图片注入 multimodal user content"
        )
        return [{"type": "text", "text": user_text}] + image_blocks

    def _on_send_clicked(self, user_text: str = "", hook_event: Optional[str] = None, preserve_input: bool = False):
        """发送消息（用户主动发送 / 系统自动发送共用）。

        preserve_input=True：系统自动发送（如团队任务邮件 _process_team_task），
        不清空用户输入框/附件、不记录输入历史——用户正在编辑的内容必须原样保留。
        """
        if getattr(self, "_is_destroyed", False):
            return

        from PyQt5.QtCore import QTimer  # 推迟 send_message 用

        # 🛡️ 清零会话切换哨兵：用户即将发起新 AI 请求，worker 的旧回调通道已无意义，
        # 后续 _on_messages_updated / _do_post_stream_cleanup 应正常处理新会话。
        self._session_switched = False
        # 🛡️ 压缩守卫同步清零：新对话轮次开始，旧 worker 快照已无意义
        self._post_compact_guard = False

        if self._is_auto_loop_running:
            InfoBar.warning(
                "AutoLoop",
                "运行中无法发送消息，请先停止 AutoLoop",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            if not preserve_input:
                self.input_area.clear()
            return

        if not user_text:
            user_text = self.input_area.toPlainText().strip()

        if not user_text and not self._attachments:
            return

        # 纯附件无文字时给一个占位文本
        if not user_text:
            user_text = ""

        # ---- 记录输入到历史（含当前附件路径）----
        # 🛡️ 系统自动发送（preserve_input）不记录：邮件文本不是用户输入，
        # 记入历史会污染 ↑ 键回溯（用户会看到莫名其妙的邮件内容）。
        if not preserve_input:
            self._record_input_history(user_text, self._attachments.copy())

        # ---- 内置命令拦截（优先检查，不打断对话）----
        cmd_mgr = CommandManager.get_instance()
        # 从卡片选中项获取偏好类型（选提示词就按提示词执行，选智能体就按智能体执行）
        cmd_name = CommandManager.parse_command_name(user_text) or ""
        preferred_display_type = self.input_area.pop_card_selected_type(cmd_name)
        cmd_result = cmd_mgr.execute(user_text, preferred_display_type=preferred_display_type)
        if cmd_result is not None:
            match cmd_result.type:
                case CommandType.FUNCTION:
                    # 函数型命令：统一走 handler 执行（_execute_command）。
                    # handler 内部可抛 CommandNeedDegrade 降级到 prompt 注入
                    # （如 /team --create=xxx 无 handler、/team --load= 遇缺失成员、
                    # 插件 FUNCTION 命令无处理器注册）——_execute_command 捕获后
                    # 写 _pending_command 并置 _team_load_degraded=True。
                    if not preserve_input:
                        self.input_area.clear()
                    # ⚠️ review#15-#2：附件仅在 handler 实际执行成功后清除
                    # （_execute_command 返回 True）。降级到 prompt 注入时命令
                    # 未真正执行（如插件无 handler 命令 /team --create=），附件
                    # 保留走后续普通发送流程——_build_user_text_with_attachments
                    # 把附件文本拼入 user_text 发给 AI，避免附件静默丢失。
                    # ⚠️ _on_send_click 已把按钮切为 STOP，只在非流式时恢复为 SEND
                    if not self._is_streaming:
                        self.input_area.toggle_send_button(True)
                    if self._execute_command(cmd_result.command_name, cmd_result.remainder):
                        self._clear_attachments()
                    # 🆕 降级到 prompt 注入：_execute_command 捕获 CommandNeedDegrade 后
                    # 已写 _pending_command 并置 _team_load_degraded=True。清除标记并继续
                    # 走 engine.send_message，user_text 保留原始 /cmd ... 文本，
                    # 由 inject_command_prompt hook 注入提示词（与 PROMPT 命令一致）。
                    if getattr(self, "_team_load_degraded", False):
                        self._team_load_degraded = False
                        # 继续走后续流程
                    else:
                        return
                case CommandType.SUBAGENT:
                    # 子智能体命令：触发子智能体任务，不替换提示词
                    # ⚠️ _on_send_click 已把按钮切为 STOP，只在非流式时恢复为 SEND
                    if not self._is_streaming:
                        self.input_area.toggle_send_button(True)
                    self._execute_subagent_task(
                        cmd_result.command_name,
                        cmd_result.subagent_task,
                        with_context=cmd_result.subagent_with_context,
                        model_value=cmd_result.subagent_model_value,
                    )
                    return
                case CommandType.PROMPT | CommandType.AGENT:
                    # 智能体命令(非子智能体)：自动注入其设定的工具权限
                    # PROMPT 和 AGENT 类型均可触发（用户可能在卡片选了"提示词"而非"智能体"）
                    self._apply_agent_command_permissions(cmd_result.command_name)
                    # 提示词替换命令：prompt_sections 按参数过滤 body 段落
                    selected_text = cmd_mgr.select_prompt(cmd_result.command_name, cmd_result.remainder)
                    if not selected_text:
                        selected_text = cmd_result.replacement  # 无匹配/无 sections → 完整 body
                    # 🆕 改为 PreUserMessage hook 注入模式：
                    # 不再直接替换 user_text，而是将命令信息存入 session.metadata，
                    # 供 engine.py → PreUserMessage hook 读取并注入提示词。
                    session = self.session_manager.get_current_session()
                    if session:
                        session.metadata["_pending_command"] = {
                            "prompt_text": selected_text,
                            "command_name": cmd_result.command_name,
                            "remainder": cmd_result.remainder or "",
                        }
                    # 用户消息保持原始输入不变（含 /xxx 前缀）
                    # 命令提示词已通过 hook 注入，不再替换 user_text
        # ---- 内置命令拦截结束 ----

        # ---- 技能名称替换：/skillname → hook 注入技能内容 ----
        if cmd_result is None and user_text.startswith("/"):
            from app.utils.utils import get_skill_by_name

            parts = user_text[1:].split(maxsplit=1)
            if parts:
                # 确定实际技能名：优先用完整名 parts[0] 查找，失败才回退到 raw_name
                # 修复：parse_suffixed_name 会把技能真名"my-skill"错误截断为"my"，
                # 导致 get_skill_by_name("my") 查不到（技能实际叫"my-skill"）
                raw_name, _ = CommandManager.parse_suffixed_name(parts[0])
                skill = get_skill_by_name(parts[0])
                if skill:
                    skill_name = parts[0]
                elif raw_name:
                    skill = get_skill_by_name(raw_name)
                    skill_name = raw_name if skill else parts[0]
                else:
                    skill_name = parts[0]
                if skill:
                    remainder = parts[1] if len(parts) > 1 else ""
                    # ── --enable / --disable 作为 FUNCTION 命令执行 ──
                    if remainder == "--enable":
                        self._execute_skill_toggle(skill_name, enable=True)
                        return
                    elif remainder == "--disable":
                        self._execute_skill_toggle(skill_name, enable=False)
                        return
                    # 🆕 直接调用 load_skill 读取技能内容，通过 PreUserMessage hook 注入
                    from app.utils.utils import load_skill as load_skill_func

                    success, content, workspace = load_skill_func(skill_name)
                    if not success:
                        logger.warning(f"[Skill] load_skill failed for '{skill_name}': content={len(content)}")
                    if success:
                        session = self.session_manager.get_current_session()
                        if session:
                            # 🛡️ 清除可能残留的旧 pending_command（防止劫持技能注入）
                            session.metadata.pop("_pending_command", None)
                            session.metadata["_pending_skill"] = {
                                "name": skill_name,
                                "content": content,
                                "workspace": workspace,
                                "remainder": remainder or "",
                            }
                    # 用户消息保持原始输入不变（含 /xxx 前缀）
                    # 技能内容已通过 hook 注入，不再替换 user_text
        # ---- 技能替换结束 ----

        # ---- 检查模型配置（用于后续判断图片支持）----
        # 仅拦截"完全无模型配置"；有配置但无 API key 直接放行：
        # - 本地免认证端点（auth=none，如 Ollama）无需 key
        # - 云端端点无 key 时请求发出后服务端返回 401，走现有错误处理
        llm_config = self._get_current_model_config()
        if not llm_config:
            InfoBar.warning(
                "请先选择模型",
                "请在设置中选择一个可用的模型后再发送消息",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 检查当前模型是否支持视觉
        _model_name = str(llm_config.get("模型名称", "") or "")
        _model_caps = get_model_capabilities(_model_name)
        _supports_vision = bool(_model_caps.get("supports_vision"))

        # ---- 拼接附件路径到用户文本 + 图片附件处理 ----
        # 性能优化：图片 base64 编码是同步 IO + CPU 重活（1MB≈6ms, 5MB≈30ms），
        # 主线程做会让用户在发截图时感知到卡顿。改为只收集路径，编码推迟到
        # _do_deferred_send 闭包内执行（紧挨在 send_message 之前），那时
        # UI 已渲染完成，STOP 按钮已可见，AI 等待状态已显示给用户。
        # 🛡️ 系统自动发送（preserve_input）：跳过附件拼接，用户挂载的附件
        # 属于正在编辑的内容，不属于本次自动发送的邮件文本。
        _preserve_attachments = preserve_input and bool(self._attachments)
        _image_paths: list[str] = []  # 仅收集图片路径，编码推迟
        if self._attachments and not _preserve_attachments:
            # 先统一处理附件文本替换（含图片 [[basename]] → 路径）— UI 显示和
            # LLM 看到的文本必须一致，所以这一步仍在主线程做（轻量字符串操作）。
            user_text = self._build_user_text_with_attachments(user_text)

            if _supports_vision:
                # 视觉模型：收集图片路径到 _image_paths，编码推迟
                _image_paths = [p for p in self._attachments if os.path.splitext(p)[1].lower() in IMAGE_EXTENSIONS]
            else:
                # 非视觉模型 → 图片路径已作为文本拼入 user_text
                has_image = any(os.path.splitext(p)[1].lower() in IMAGE_EXTENSIONS for p in self._attachments)
                if has_image:
                    logger.warning(
                        f"[ImageAttach] 模型 {_model_name} 不支持视觉 (caps={_model_caps})，"
                        f"图片附件仅作为文件路径文本发送，模型将调用 read 读取"
                    )

        # 非函数命令：检查是否正在流式输出
        if self._is_streaming:
            self._on_stop_clicked()

        # 🛡️ 不清除截断哨兵！若此前发生过截断（撤销/删除），哨兵仍然有效，
        # 可在 event loop 后续处理中拦截旧 worker 的 finished_with_messages 回调（先于
        # 本函数返回后的 timer 事件到达），防止旧 worker 的过期数据覆盖已截断的会话。
        # 哨兵将在 _do_deferred_send 中新 worker 启动前被清除（见其内部逻辑）。
        if self._truncation_sentinel is not None:
            self._pending_send_after_truncation = True
            # 保存用户消息文本指纹，用于后续区分新旧 worker 的消息
            # （当新旧 worker 用户消息数相同时，通过检查消息是否包含此文本来识别）
            self._pending_send_user_text = user_text

        self._hide_welcome_cards()

        if not preserve_input:
            self.input_area.clear()
            self._clear_attachments()
        self._append_user_message(user_text)

        assistant_card = self._append_assistant_message(
            model_name=self._current_model_name,
            config_id=self._current_provider_name,
        )

        # 先设置当前卡片（必须在 send_message 之前，否则回调触发时 _current_assistant_card 为 None）
        self._current_assistant_card = assistant_card
        # 记录响应开始时间（供 _on_stream_finished 计算持续时间）
        self._response_start_time = time.time()
        assistant_card.start_elapsed_tracking()
        self._is_streaming = True
        self._toggle_send_stop(True)

        # 捕获 session 引用（推迟到下一 tick 时仍能取到当前会话）
        session = self.session_manager.get_current_session()

        # 推迟 send_message 到下一 event loop tick：
        # 让 Qt 先把刚刚创建的 user/assistant 卡片 + STOP 按钮绘制到屏幕，
        # 再开始执行 hook 注入 + LLM 消息构建（这两步会同步阻塞主线程）。
        # 用户立即看到"等待 AI"状态，AI 响应在下一 tick 启动 → 消除"卡一下"的感知。
        def _do_deferred_send():
            if getattr(self, "_is_destroyed", False):
                return

            # 🛡️ 不清除截断哨兵！哨兵继续守卫 `_on_messages_updated`，在 event loop
            # 中拦截旧 worker 延迟到达的 finished_with_messages 回调。新 worker 的消息
            # 通过 `_on_messages_updated` 中的 `_pending_send_after_truncation` 标志
            # + 用户消息数比对 / 文本指纹来识别放行（旧 worker 含截断前的用户消息，
            # 数量多于当前会话；数量相等时通过检查是否含新发文本来判断）。
            # 旧 worker 的 finalize 回调（_interrupt_complete）由 `_on_finalize_complete`
            # 中的同一标志直接丢弃。

            # 关键修复：确保 ToolExecutor 使用正确的 session_id
            if session and self.backend.tool_executor:
                self.backend.set_session_context(session.session_id)

            # 图片附件 base64 编码（在 UI 已渲染后执行，用户感知不到）
            _user_content = None
            if _image_paths:
                _user_content = self._encode_image_attachments_to_multimodal(
                    user_text=user_text,
                    image_paths=_image_paths,
                    model_name=_model_name,
                )
            # 如果 send_message 返回 False（通常是 LLM 配置无效），回滚 UI 状态
            engine_kwargs = {}
            if _user_content is not None:
                engine_kwargs["_user_content"] = _user_content
            # hook_event 透传给引擎：消息写入 session.messages 时带 _hook_event 标记
            # （None 时不写入，保持历史行为不变）
            if hook_event:
                engine_kwargs["_hook_event"] = hook_event
            # 🛡️ 标记会话脏：用户即将发送消息，引擎会在后台调用
            # add_user_message 修改 session.messages。即使后续被 / 命令拦截
            # 或引擎报错提前返回，脏标记也能确保关闭窗口/新建会话时不会漏存。
            self._session_dirty = True
            if not self.backend.send_message_to_engine(user_text, **engine_kwargs):
                self._is_streaming = False
                self._toggle_send_stop(False)
                assistant_card.deleteLater()
                # 🆕 补清引用：deleteLater 是延迟删除（下一轮事件循环 C++ 对象才销毁），
                # 若不置 None，期间 _refresh_context_usage_indicator 等仍会取到
                # _current_assistant_card 并访问已删除 QLabel → RuntimeError。
                # 与 L13885-13886 规范写法对齐。
                self._current_assistant_card = None
                return

            # 同步 batch 结构：_message_batch 已包含新 user batch
            self._sync_batch_structures()
            # 给新创建的用户卡片设置正确的 _message_index（_append_user_message 中未设置）
            self._fix_new_card_message_index(user_text=user_text)
            # 修复：扩展可见范围以覆盖新增的 batch，否则回收机制会误删新卡片
            self._visible_batch_end = len(self._message_batch)

            # 🛡️ 只在新会话的第一个问题时触发标题生成，避免每次对话都重复生成
            if session:
                # 🛡️ R6 修复：team 邮件（_hook_event="TeamMail"）视为真实用户问题
                # → mail-only 会话也能触发标题生成（之前被误伤跳过）
                user_msg_count = sum(
                    1
                    for m in session.messages
                    if m.get("role") == "user" and (not m.get("_hook_event") or m.get("_hook_event") == "TeamMail")
                )
                if user_msg_count == 1:
                    self._maybe_generate_topic_summary()
            # 用户问题已写入 session，立即刷新徽章（无需等流式结束）
            self._update_history_questions_badge()

        QTimer.singleShot(0, _do_deferred_send)

    def _on_stream_started(self):
        if getattr(self, "_is_destroyed", False):
            return
        self._is_streaming = True
        # 🛡️ 不覆盖首次设置的 _response_start_time（_on_send_clicked 已设），
        # 避免多个 worker 迭代（如工具执行后重开 API 调用）时耗时被重置。
        if self._response_start_time is None:
            self._response_start_time = time.time()
        self._set_ai_state("streaming")  # 桌宠：开始回复
        if self._current_assistant_card:
            # 🛡️ 只在尚未开始计时时启动，避免重复调用重置计数器。
            if self._current_assistant_card._elapsed_start_time is None:
                self._current_assistant_card.start_elapsed_tracking()
        # 内容已由 MessageCard._markdown_text 累加，无需主窗口冗余存储
        # 每个新的流式轮次清空工具结果去重集合
        self._processed_tool_result_ids: set = set()
        # tool_call_id -> 拥有其运行折叠框的卡片。
        # 保证工具结果（append_tool_result）与运行折叠框落在同一张卡片上，
        # 避免结果写入别的卡片导致运行框永远无法转换为完成框、并持续累积。
        self._tool_card_map: dict = {}
        # 当 LLM 实际开始流式响应时切换为停止按钮
        # 这样内建函数/子智能体执行后的回调阶段不会误切换按钮状态
        self._toggle_send_stop(True)
        if self._current_assistant_card:
            self._current_assistant_card.start_streaming_anim()

    def _on_content_received(self, content_piece: str):
        if getattr(self, "_is_destroyed", False):
            return
        # ★ 推理结束后首个内容到达时，确保桌宠从 thinking 切回 streaming
        # 背景：stream_started 在 worker 启动时即触发（早于任何内容），
        # 随后 thinking_started（推理内容到达）覆盖为 thinking，
        # 但内容到达时没有信号通知桌宠切回 streaming。
        if self._ai_state == "thinking":
            self._set_ai_state("streaming")

        # [PERF] 流式密集型渲染保护：单次事件循环中积累的多个 content_piece
        # 合并为一次 update_content 调用。重复调用仅保留最后一段，避免
        # MessageCard.append_chunk → _schedule_render → setHtml 高频链式触发。
        # 刷新时机：下一次 content_received 信号（由 chat_worker 批处理阈值 80ms 保障），
        # 或 MessageCard._SAFETY_RENDER_INTERVAL (2000ms) 安全兜底触发。
        if self._current_assistant_card:
            self._update_assistant_message(self._current_assistant_card, content_piece)
        # 内容已由 MessageCard.append_chunk 内部累加，无需主窗口冗余存储
        # ★ B4 强回收层（低频触发）：流式内容到达时顺带检查内存阈值
        self._maybe_strong_recycle()

    def _on_reasoning_content_received(self, reasoning_piece: str):
        """处理 DeepSeek 思考内容（流式接收）"""
        if getattr(self, "_is_destroyed", False):
            return
        card = self._current_assistant_card
        if card and getattr(card, "_content_data", None) is not None:
            card.start_streaming_anim()
            card.append_reasoning(reasoning_piece)

    def _on_thinking_started(self):
        """新轮次思考开始，为当前助手卡片创建新的独立思考块"""
        if getattr(self, "_is_destroyed", False):
            return
        self._set_ai_state("thinking")  # 桌宠：开始思考
        card = self._current_assistant_card
        if card and getattr(card, "_content_data", None) is not None:
            card.start_streaming_anim()
            card.start_new_thinking_block()

    def _on_tool_args_updated(self, tool_call_id: str, tool_name: str, partial_args: dict):
        """工具参数流式更新 — 流式接收过程中，参数逐块解析完成后触发"""
        if getattr(self, "_is_destroyed", False):
            return

        # 专属 UI 工具（metadata["ui_managed"]）：不创建流式块，由专属 UI 处理
        try:
            from app.tools.registry import ToolRegistry

            _ui_managed = ToolRegistry.get_instance().is_ui_managed(tool_name)
        except Exception:
            _ui_managed = False
        if _ui_managed:
            return

        # 注入到当前助手卡片的消息内容中（替代独立 ToolFloatingWidget）
        # 🐛 修复：统一使用 _current_assistant_card，避免与 _on_tool_result_received
        # 中的 fallback 路径分歧（_find_latest_assistant_card 在子智能体/多轮场景下
        # 可能解析到不同卡片，导致运行框与结果落在不同卡片，运行框永不转换）。
        card = self._current_assistant_card or self._find_latest_assistant_card()
        if card and getattr(card, "update_tool_streaming", None):
            # 记录 tool_call_id 归属的卡片，确保工具结果落在与运行折叠框同一张卡片上
            self._tool_card_map[tool_call_id] = card
            card.update_tool_streaming(tool_call_id, tool_name, partial_args)

    def _on_tool_call_started(self, tool_call_id: str, tool_name: str, arguments: dict, round_id: str = None):
        if getattr(self, "_is_destroyed", False):
            return
        self._current_tool_start_time = time.time()
        self._current_tool_call_id = tool_call_id
        self._current_tool_name = tool_name
        self._current_tool_args = arguments

        # 模型开始调用工具时激活彩虹边框（即使返回内容不含文本）
        if self._current_assistant_card:
            self._current_assistant_card.start_streaming_anim()
            # 🚀 [PERF] 工具调用触发时强制渲染待处理的正文
            # 工具调用前到达的 content_batch 已通过 append_chunk 写入
            # _markdown_text，但若未达自然边界（无句号/换行），安全定时器
            # 要等 300ms 才渲染。强制立即渲染让用户在工具执行前先看到正文，
            # 避免"正文等工具执行完才出现"的感知。
            # ⚠️ _schedule_render 是 CodeWebViewer 的方法，不是 MessageCard 的，
            # 需通过 .viewer 访问。viewer 可能为 None（懒渲染未就绪）。
            _vwr = getattr(self._current_assistant_card, "viewer", None)
            if _vwr is not None:
                _vwr._schedule_render(immediate=True)

        # 🐛 修复：在工具启动路径也触发 _maybe_finish_thinking_for_tool，
        # 覆盖 LLM 只输出 reasoning 然后直接调用工具（无 update_tool_streaming）的场景。
        # 原实现仅依赖 update_tool_streaming 触发，导致思考块永不 finalize。
        if self._current_assistant_card and getattr(
            self._current_assistant_card, "_maybe_finish_thinking_for_tool", None
        ):
            self._current_assistant_card._maybe_finish_thinking_for_tool(tool_call_id)

        # AutoLoop 运行期间不显示工具调用 UI
        if self._is_auto_loop_running:
            return

        # 交互式工具（metadata["interactive"]=True）：由 chat_worker 的 question_asked
        # 信号 → _on_question_asked 统一处理（含规范化后的数据）
        # 这里只需记录 ID，不做显示避免竞态
        try:
            from app.tools.registry import ToolRegistry

            _interactive = ToolRegistry.get_instance().is_interactive(tool_name)
            _ui_managed = ToolRegistry.get_instance().is_ui_managed(tool_name)
        except Exception:
            _interactive = False
            _ui_managed = False
        if _interactive:
            self._question_tool_call_id = tool_call_id
            self._hide_all_cards_for_question()
            return

        # 专属 UI 工具：更新数据，但只有在系统卡片未打开时才能显示
        if _ui_managed:
            if not self._is_system_card_visible:
                self._card_manager.show_card("todo", self._window_id)
            return

        # 工具参数接收完成，更新预览文本，保持"执行中"状态（金色转圈继续显示）
        # 转圈在 append_tool_result（工具结果返回）时由 DOM 原地替换自然消失，
        # 不可提前设为完成态 —— 此时工具尚未执行。
        # 🐛 修复：统一使用 _current_assistant_card，与 _on_tool_result_received
        # 的 fallback 保持一致，避免卡片定位分歧导致运行框卡死。
        card = self._current_assistant_card or self._find_latest_assistant_card()
        if card and getattr(card, "update_tool_streaming", None):
            # 记录 tool_call_id 归属的卡片，确保工具结果落在与运行折叠框同一张卡片上
            self._tool_card_map[tool_call_id] = card
            card.update_tool_streaming(tool_call_id, tool_name, arguments)

    def _on_sub_agent_compact_closed(self):
        """子智能体紧凑卡片关闭时清理状态"""
        if hasattr(self, "_sub_agent_compact_widget"):
            self._sub_agent_compact_widget._batch_started = False
        # 通知 CardManager 卡片已关闭，否则 show_card 以为它仍可见而跳过
        if hasattr(self, "_card_manager"):
            self._card_manager.hide_card("sub_agent_compact", self._window_id)

    def _on_todo_closed(self):
        """todo 卡片关闭时通知 CardManager

        与 sub_agent 对称：卡片关闭只 setVisible(False) 不会让 CardManager
        感知（visible_cards 仍为 todo，show_card 会被跳过、容器也不会折叠）。
        显式 hide_card 使状态同步 + 触发容器折叠释放 A3 min 锁，对话区恢复。
        """
        if hasattr(self, "_card_manager"):
            self._card_manager.hide_card("todo", self._window_id)

    def _on_sub_agent_stop_requested(self, task_id: str):
        """处理子智能体停止请求 - 中止当前运行中的子智能体"""
        if getattr(self, "_is_destroyed", False):
            return

        sub_agent_mgr = self.backend.sub_agent_manager
        executor = sub_agent_mgr._running_tasks.get(task_id)
        if not executor:
            logger.warning(f"[main_widget] 停止子智能体失败: task_id={task_id} 不在运行中")
            return

        agent_name = executor.agent_name
        task_description = getattr(executor, "task_description", "")

        # 1. 设置取消标志 + 执行错误（让后面的 _on_sub_agent_task_finished 读到正确状态）
        executor.cancel()
        executor._execution_error = "用户已手动中止子智能体"

        # 2. 通过 task_finished 信号走标准完成路径 → _on_sub_agent_task_finished
        #    会在那里更新 UI + 写入 _finished_tasks + 从 running_tasks 移除
        try:
            sub_agent_mgr.task_finished.emit(task_id, "")
        except Exception as e:
            logger.error(f"[main_widget] task_finished.emit 失败 (中止路径): {e}")

        # 3. 通知用户
        from qfluentwidgets import InfoBar, InfoBarPosition

        InfoBar.info(
            title="子智能体已中止",
            content=f"已手动中止子智能体「{agent_name}」: {task_description[:40]}{'...' if len(task_description) > 40 else ''}",
            parent=TabManagerWindow.get_instance() or self.window(),
            duration=3000,
            position=InfoBarPosition.BOTTOM,
        )

    def _on_sub_agent_enter_session(self, task_id: str, agent_name: str):
        """进入子智能体会话 - 内嵌卡片显示该子智能体的运行日志/消息"""
        if getattr(self, "_is_destroyed", False):
            return

        # 通过 SubAgentManager 获取任务日志（支持运行中/已完成/数据库三种来源）
        sub_agent_mgr = self.backend.sub_agent_manager
        task_log_data = sub_agent_mgr.get_task_logs(task_id)

        if not task_log_data.get("found"):
            return

        logs = task_log_data.get("logs", [])
        summary = task_log_data.get("summary", {})
        status = task_log_data.get("status", "unknown")
        summary["status"] = status
        summary["agent_name"] = agent_name

        # 显示子智能体会话卡片（内嵌于软件窗口内部，覆盖对话区域，与 DiffViewerCard 同模式）
        from app.widgets.cards.global_card_controller import get_global_card_controller

        controller = get_global_card_controller()
        if controller is None:
            return
        controller.show_sub_agent_session(
            task_id=task_id,
            agent_name=agent_name,
            logs=logs,
            summary=summary,
            logs_provider=lambda: sub_agent_mgr.get_task_logs(task_id),
        )

    def _handle_compact_command(self, args: str):
        """/compact 命令：触发上下文压缩

        支持参数：
        - --clear：压缩完成后清空当前会话的所有历史消息
        - 自由文本：附加给压缩智能体的补充说明（焦点 / 输出形态 / 分析诉求等）

        Args:
            args: 命令后的参数字符串
        """
        # 解析 --clear 标记（仅匹配独立 flag，不吞并后续内容）
        clear_after = bool(re.search(r"(?:^|\s)--clear(?=\s|$)", args or ""))
        # 剥离 --clear 标记后，剩余文本作为给子智能体的补充说明
        user_hint = re.sub(r"(?:^|\s)--clear(?=\s|$)", "", args or "").strip()
        self._trigger_context_compaction(clear_after=clear_after, user_hint=user_hint)

    def _handle_todos_command(self, args: str):
        """/todos 命令：手动显示/刷新待办事项卡片"""
        # 工具插件化：待办状态在工具插件内，主程序不读插件状态——
        # 通过执行 todoread 工具拿当前列表（主程序 → 工具执行，正常方向）
        todos = []
        try:
            if self.backend and getattr(self.backend, "_tool_executor", None):
                result = self.backend._tool_executor.execute("todoread", {})
                if result is not None:
                    todos = getattr(result, "todos", None) or []
        except Exception:
            todos = []
        if todos:
            self._todo_floating_widget.update_todos(todos)
            # 确保卡片可见（update_todos 内部已处理自动显示，但通过 CardManager 确保容器展开）
            from app.widgets.cards.card_manager import CardManager

            CardManager.get_instance().show_card("todo", self._window_id)
        else:
            self._todo_floating_widget.setVisible(False)
            InfoBar.info(
                "暂无待办事项",
                "",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=1000,
                position=InfoBarPosition.BOTTOM,
            )

    def _handle_subagents_command(self, args: str):
        """/subagents 命令：管理子智能体任务和默认模型

        参数：
          无参数     → 显示运行中的子智能体任务（紧凑卡片）
          --detail   → 显示子智能体详细日志面板
          --model=X  → 设置子智能体默认模型
          --reset    → 清空子智能体默认模型设置
          --create=X → 走 PROMPT 注入（prompt_sections 自动匹配，无需 Python handler）
        """
        import re

        from qfluentwidgets import InfoBar, InfoBarPosition

        from app.utils.config import Settings

        args = (args or "").strip()

        # ---- --reset：清空默认模型设置 ----
        if args == "--reset":
            cfg = Settings.get_instance()
            cfg.set(cfg.llm_subagent_default_model, "", save=True)
            InfoBar.success(
                title="已重置",
                content="子智能体默认模型已清空，将使用主智能体模型",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            # 更新命令卡参数描述
            self._update_subagents_param_description()
            return

        # ---- --model=xxx：设置默认模型 ----
        model_match = re.match(r"^--model=(.+)$", args)
        if model_match:
            model_value = model_match.group(1).strip().strip("\"'")
            if not model_value:
                InfoBar.warning(
                    title="参数错误",
                    content="--model= 后需要指定模型名或服务商:模型名",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    duration=3000,
                    position=InfoBarPosition.BOTTOM,
                )
                return

            # 复用现有模型解析逻辑
            llm_config = self._resolve_subagent_model_config(model_value)
            if llm_config is None:
                # _resolve_subagent_model_config 内部已弹出错误提示
                return

            # 保存显示名
            provider_display = llm_config.get("provider_name", "")
            model_display = llm_config.get("模型名称", model_value)
            display_value = f"{provider_display}:{model_display}" if provider_display else model_display

            cfg = Settings.get_instance()
            cfg.set(cfg.llm_subagent_default_model, display_value, save=True)

            if provider_display:
                info_text = f"{provider_display} - {model_display}"
            else:
                info_text = model_display

            InfoBar.success(
                title="子智能体模型设置",
                content=f"{info_text}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=2000,
                position=InfoBarPosition.BOTTOM,
            )
            # 更新命令卡参数描述
            self._update_subagents_param_description()
            return

        # ---- --create=<描述>：无 Python handler → 抛 CommandNeedDegrade 降级到
        # prompt 注入（_execute_command 捕获后 select_prompt 按 --create= 参数
        # 匹配 `<!-- section:create -->` 段，AI 自动生成子智能体 md 文件）。
        if args.startswith("--create"):
            from app.core.command_manager import CommandNeedDegrade

            raise CommandNeedDegrade("subagents", args)

        # ---- 无参数：显示紧凑卡片 ----
        sub_agent_mgr = self.backend.sub_agent_manager
        running_tasks = sub_agent_mgr._running_tasks

        if not running_tasks:
            InfoBar.warning(
                title="暂无运行中的子智能体",
                content="当前没有正在执行的子智能体任务",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        compact = self._sub_agent_compact_widget

        for task_id, executor in running_tasks.items():
            # 如果任务行已存在（之前关闭过），保留已有统计数据
            if task_id in compact._task_rows:
                continue
            llm_cfg = getattr(executor, "llm_config", {}) or {}
            model_name = str(llm_cfg.get("模型名称", "") or llm_cfg.get("model", "") or "")
            compact.add_task(task_id, executor.agent_name, executor.task_description, model_name=model_name)

        compact._batch_started = True
        self._card_manager.show_card("sub_agent_compact", self._window_id)

    def _handle_title_gen_command(self, args: str):
        """/title-gen 命令：切换标题生成使用的默认模型

        参数：
          --model=X  → 设置标题生成默认模型
          --reset    → 清空标题生成默认模型设置
        """
        import re

        from qfluentwidgets import InfoBar, InfoBarPosition

        from app.utils.config import Settings

        args = (args or "").strip()

        # ---- --reset：清空默认模型设置 ----
        if args == "--reset":
            cfg = Settings.get_instance()
            cfg.set(cfg.llm_title_gen_default_model, "", save=True)
            InfoBar.success(
                title="已重置",
                content="标题生成默认模型已清空，将使用主模型",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            self._update_title_gen_param_description()
            return

        # ---- --model=xxx：设置默认模型 ----
        model_match = re.match(r"^--model=(.+)$", args)
        if model_match:
            model_value = model_match.group(1).strip().strip("\"'")
            if not model_value:
                InfoBar.warning(
                    title="参数错误",
                    content="--model= 后需要指定模型名或服务商:模型名",
                    parent=TabManagerWindow.get_instance() or self.window(),
                    duration=3000,
                    position=InfoBarPosition.BOTTOM,
                )
                return

            llm_config = self._resolve_subagent_model_config(model_value)
            if llm_config is None:
                return

            provider_display = llm_config.get("provider_name", "")
            model_display = llm_config.get("模型名称", model_value)
            display_value = f"{provider_display}:{model_display}" if provider_display else model_display

            cfg = Settings.get_instance()
            cfg.set(cfg.llm_title_gen_default_model, display_value, save=True)

            if provider_display:
                info_text = f"{provider_display} - {model_display}"
            else:
                info_text = model_display

            InfoBar.success(
                title="标题生成模型设置",
                content=f"{info_text}",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=2000,
                position=InfoBarPosition.BOTTOM,
            )
            self._update_title_gen_param_description()
            return

        # ---- 无参数：显示当前设置 ----
        cfg = Settings.get_instance()
        saved = cfg.llm_title_gen_default_model.value or ""
        if saved:
            InfoBar.info(
                title="当前标题生成模型",
                content=saved,
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
        else:
            InfoBar.info(
                title="当前标题生成模型",
                content="未设置，将使用主模型",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )

    def _handle_toggle_window_command(self, args: str):
        """/toggle-window 命令：一键隐藏/显示所有窗口"""
        from app.tray_manager import TrayManager

        TrayManager.get_instance()._toggle_all_windows()

    def _handle_clear_command(self, args: str):
        """/clear 命令：清空当前会话的所有消息（重新显示欢迎页）"""
        self._on_clear_shortcut()

    def _update_subagents_param_description(self):
        """更新 /subagents 命令的 --model= 参数描述，反映当前默认值"""
        from app.core.command_manager import CommandManager
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = cfg.llm_subagent_default_model.value or ""

        cmd_mgr = CommandManager.get_instance()
        entries = cmd_mgr._commands.get("subagents", {})
        for cmd_type, cmd_def in entries.items():
            for param in cmd_def.parameters:
                if param.name == "--model=":
                    if saved:
                        param.description = f"当前子智能体默认模型: {saved}"
                    else:
                        param.description = "设置子智能体默认模型"
                    break

    def _update_title_gen_param_description(self):
        """更新 /title_gen 命令的 --model= 参数描述，反映当前默认值"""
        from app.core.command_manager import CommandManager
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = cfg.llm_title_gen_default_model.value or ""

        cmd_mgr = CommandManager.get_instance()
        entries = cmd_mgr._commands.get("title-gen", {})
        for cmd_type, cmd_def in entries.items():
            for param in cmd_def.parameters:
                if param.name == "--model=":
                    if saved:
                        param.description = f"当前标题生成默认模型: {saved}"
                    else:
                        param.description = "设置标题生成默认模型"
                    break

    def _on_subagent_model_config_changed(self):
        """子智能体默认模型配置变更时：更新命令描述 + 刷新命令卡 UI"""
        self._update_subagents_param_description()
        self._refresh_command_card_detail("subagents")

    def _on_title_gen_model_config_changed(self):
        """标题生成默认模型配置变更时：更新命令描述 + 刷新命令卡 UI"""
        self._update_title_gen_param_description()
        self._refresh_command_card_detail("title-gen")

    def _refresh_command_card_detail(self, cmd_name: str):
        """强制刷新命令卡的 detail 模式参数视图"""
        if not hasattr(self, "_command_card"):
            return
        card = self._command_card
        if card.is_detail_mode and card.detail_cmd_name == cmd_name:
            card._refresh_detail_view()

    def _on_sub_agent_task_started(self, task_id: str, agent_name: str, task_description: str):
        """子智能体任务启动（通过 SubAgentManager 信号触发）

        策略：
        - 紧凑卡片（sub_agent_compact_widget）：自动弹出，显示运行状态（旋转图标+agent名+任务描述）
        """
        if getattr(self, "_is_destroyed", False):
            return

        # 系统卡片打开时，阻止子智能体卡片显示
        if self._is_system_card_visible:
            return

        # ── 紧凑卡片：自动弹出 ──
        compact = self._sub_agent_compact_widget
        if not compact._batch_started:
            # 只清空已完成的任务，保留运行中的任务行（避免关闭再打开后统计数据重置）
            finished_ids = [tid for tid, row in compact._task_rows.items() if not row.is_running]
            for tid in finished_ids:
                compact.remove_task(tid)
        compact._batch_started = True

        # 从 executor 提取模型名称
        sub_agent_mgr = self.backend.sub_agent_manager
        executor_for_model = sub_agent_mgr._running_tasks.get(task_id)
        model_name = ""
        if executor_for_model:
            llm_cfg = getattr(executor_for_model, "llm_config", {}) or {}
            model_name = str(llm_cfg.get("模型名称", "") or llm_cfg.get("model", "") or "")

        compact.add_task(task_id, agent_name, task_description, model_name=model_name)
        if not compact.isVisible():
            compact.setVisible(True)

        # 连接 executor 信号（紧凑卡片实时更新）
        sub_agent_mgr = self.backend.sub_agent_manager
        executor = sub_agent_mgr._running_tasks.get(task_id)
        if executor:
            executor.tool_call_started.connect(
                lambda tid, name, args: (
                    self._sub_agent_compact_widget.add_tool_call(tid, name, args)
                    if not getattr(self, "_is_destroyed", False)
                    else None
                )
            )
            executor.token_usage_updated.connect(
                lambda tid, pt, ct, tt: (
                    self._on_sub_agent_token_usage(tid, pt, ct, tt)
                    if not getattr(self, "_is_destroyed", False)
                    else None
                )
            )
            executor.finished_with_result.connect(
                lambda tid, result: (
                    self._on_sub_agent_finished(tid, result) if not getattr(self, "_is_destroyed", False) else None
                )
            )

    def _on_subagent_permission_requested(self, window_id: str, task_id: str, tool_name: str, arguments: dict):
        """子智能体 ask 权限弹窗（主线程，T24）。

        多窗口隔离：仅本窗口的请求弹窗。用户允许 → respond_permission(True)
        继续执行；拒绝 → respond_permission(False) 回填失败；关闭弹窗默认拒绝。
        """
        if getattr(self, "_is_destroyed", False):
            return
        if window_id and window_id != self._window_id:
            return  # 其它窗口的请求，跳过
        try:
            args_preview = ""
            if arguments:
                try:
                    args_preview = json.dumps(arguments, ensure_ascii=False)[:200]
                except Exception:
                    args_preview = str(arguments)[:200]
            from PyQt5.QtWidgets import QMessageBox

            box = QMessageBox(self)
            box.setWindowTitle("子智能体工具权限请求")
            box.setIcon(QMessageBox.Question)
            box.setText(f"子智能体请求使用工具「{tool_name}」\n\n参数: {args_preview}\n\n是否允许？")
            allow_btn = box.addButton("允许", QMessageBox.AcceptRole)
            deny_btn = box.addButton("拒绝", QMessageBox.RejectRole)
            box.setDefaultButton(deny_btn)
            box.setEscapeButton(deny_btn)
            box.exec_()
            allow = box.clickedButton() is allow_btn
            self.backend.sub_agent_manager.respond_permission(task_id, allow)
        except Exception as e:
            logger.warning(f"[SubAgent] 权限弹窗失败: {e}")
            try:
                self.backend.sub_agent_manager.respond_permission(task_id, False)
            except Exception:
                pass

    def _on_sub_agent_token_usage(self, task_id: str, prompt_tokens: int, completion_tokens: int, total_tokens: int):
        """子智能体 token 用量更新"""
        if getattr(self, "_is_destroyed", False):
            return
        # 更新紧凑卡片的上下文用量行
        if hasattr(self, "_sub_agent_compact_widget"):
            # 显示单次 API 调用的 token 数（不累加，避免随多次工具调用无限膨胀）
            acc_total = total_tokens
            if acc_total >= 1000:
                display = f"{acc_total / 1000:.1f}K tokens"
            else:
                display = f"{acc_total} tokens"
            self._sub_agent_compact_widget.set_task_context(task_id, display)

    def _on_sub_agent_task_finished(self, task_id: str, result: str):
        """子智能体任务完成"""
        if getattr(self, "_is_destroyed", False):
            return
        # 从管理器获取执行器，检查是否有真实的错误状态
        sub_agent_mgr = self.backend.sub_agent_manager
        executor = sub_agent_mgr._running_tasks.get(task_id)

        # 优先使用 executor 中记录的 _execution_error 来判断成功/失败
        # 而不是依赖结果内容中的关键词（这会导致误判）
        execution_error = getattr(executor, "_execution_error", None) if executor else None
        success = execution_error is None or execution_error == ""

        # 更新紧凑卡片
        if hasattr(self, "_sub_agent_compact_widget"):
            self._sub_agent_compact_widget.finish_task(task_id, success)

        # 从管理器移除并记录结果
        if executor:
            agent_name = getattr(executor, "agent_name", "")
            task_description = getattr(executor, "task_description", "")
            task_session_id = getattr(executor, "_task_session_id", "")
            del sub_agent_mgr._running_tasks[task_id]
        else:
            # executor 可能已被 get_finished_tasks() 移除（DAG 节点由 _on_dag_node_finished 提前删除 running_tasks）
            # 此时从 _finished_tasks 恢复字段（此时 DAG 已写入 error 信息，不要覆盖）
            existing = sub_agent_mgr._finished_tasks.get(task_id, {})
            agent_name = existing.get("agent_name", "")
            task_description = existing.get("task_description", "")
            task_session_id = existing.get("session_id", "")

        # 如果 get_finished_tasks() 已经写入过完整数据，只更新 result/error 避免丢失 session_id/日志
        if task_id in sub_agent_mgr._finished_tasks:
            existing = sub_agent_mgr._finished_tasks[task_id]
            existing["result"] = result
            # 已有 error（如 DAG 写入的跳过信息）不要覆盖
            if "error" not in existing or not existing["error"]:
                existing["error"] = execution_error or ""
            existing.setdefault("agent_name", agent_name)
            existing.setdefault("task_description", task_description)
            existing.setdefault("session_id", task_session_id)
        else:
            sub_agent_mgr._finished_tasks[task_id] = {
                "result": result,
                "error": execution_error or "",
                "agent_name": agent_name,
                "task_description": task_description,
                "session_id": task_session_id,
            }

        # 批次完成检查：先检查当前计数，用于精确诊断
        _batch_before = sub_agent_mgr._batch_completed
        _batch_total = sub_agent_mgr._batch_total

        # ====== 流式注入：单个子智能体完成时，立即注入到流中（不中断） ======
        engine = self.backend.chat_engine
        is_currently_streaming = engine and engine.is_streaming
        if is_currently_streaming:
            logger.info(
                "[SubAgent-Callback] task_finished during streaming: "
                f"task_id={task_id[:8]}, result_len={len(result) if result else 0}, "
                f"batch_completed={_batch_before}/{_batch_total}"
            )
            self._inject_subagent_completion_into_stream(task_id, result, success, agent_name, task_description)
            # 流式场景下仍继续累加批次计数器，以便所有完成时通过 _do_trigger_callback 发送汇总

        # 批次完成检查：只有当所有任务都完成时才触发回调
        sub_agent_mgr._batch_completed += 1
        logger.info(
            "[SubAgent-Callback] task_finished counting: "
            f"task_id={task_id[:8]}, "
            f"before={_batch_before}, total={_batch_total}, "
            f"after={sub_agent_mgr._batch_completed}, "
            f"is_streaming={is_currently_streaming}"
        )
        if sub_agent_mgr._batch_completed >= sub_agent_mgr._batch_total and sub_agent_mgr._batch_total > 0:
            # 全部任务完成，发送回调通知
            self._do_trigger_callback(sub_agent_mgr)
            # 重置计数器和批次任务ID集合
            sub_agent_mgr._batch_total = 0
            sub_agent_mgr._batch_completed = 0
            sub_agent_mgr._batch_task_ids = set()

    def _inject_subagent_completion_into_stream(
        self, task_id: str, result: str, success: bool, agent_name: str, task_description: str
    ):
        """将子智能体完成信号注入到正在流式输出的消息流中（不中断流式）

        使用与 hook 相同的 _hook_message_queue 机制，worker 在下一轮 API 调用前
        自动消费队列并注入到上下文，LLM 在下一轮响应中即可感知完成信息。

        优化：流式场景下直接注入子智能体的执行结果，不再要求 LLM 调用
        subagent_status 去查询——LLM 在下一轮响应中直接看到结果。
        只有用户中断流式后，才需要使用 subagent_status 查询。

        Args:
            task_id: 任务 ID（汇总消息传 "__batch_summary__"）
            result: 执行结果
            success: 是否成功
            agent_name: 智能体名称
            task_description: 任务描述
        """
        # 构建消息内容
        if task_id == "__batch_summary__":
            # 最终汇总消息
            content = result
        else:
            # 单个任务完成消息：直接嵌入执行结果
            status_icon = "✅" if success else "❌"
            agent_label = f"[{agent_name}]" if agent_name else ""
            # 结果截断以防太长
            MAX_RESULT_CHARS = 4096
            if result and len(result) > MAX_RESULT_CHARS:
                result_preview = (
                    result[:MAX_RESULT_CHARS] + f"\n\n[... 已省略 {len(result) - MAX_RESULT_CHARS} 个字符 ...]"
                )
            else:
                result_preview = result or "(无输出)"
            content = (
                f"子智能体任务完成: {status_icon} {agent_label} {task_description} (id: {task_id})\n"
                f"执行结果：\n{result_preview}"
            )

        # 使用 hook 消息格式包裹
        from app.core.backend import _format_hook_output

        hook_content = _format_hook_output("SubAgentFinished", content, wrap_system_reminder=False)

        # 推送到 _hook_message_queue，worker 在下一轮 API 调用前自动消费
        self.backend._hook_message_queue.put(
            {
                "role": "user",
                "content": hook_content,
                "_hook_event": "SubAgentFinished",
            }
        )
        # 注意：不再 emit _hook_messages_updated，因为 backend.py:380 的 on_hook_finished
        # 回调已经在 put 后 emit 过了，重复 emit 会导致前端刷新两次
        logger.debug(f"[SubAgent] 流式注入完成信号: task_id={task_id[:12]}")

    def _do_trigger_callback(self, sub_agent_mgr):
        """执行回调触发 - 支持强制中断当前流式输出

        三种场景：
        1. 流式中回调（LLM 调用子智能体，持续流式）：注入汇总到流，不中断
        2. 非流式中回调（如 /compact 手动命令）：强制中断并创建新对话轮次

        优化：流式场景下直接注入每个任务的执行结果，不再要求 LLM 调用
        subagent_status 查询。只有非流式中断场景才需要后续主动查询。
        """
        # 只提取本次批次完成的任务信息（不是所有历史任务）
        batch_ids = sub_agent_mgr._batch_task_ids
        batch_tasks = []
        for tid in batch_ids:
            task_info = sub_agent_mgr._finished_tasks.get(tid, {})
            is_error = bool(task_info.get("error"))
            result_text = task_info.get("result", "") or "(无输出)"
            error_text = task_info.get("error", "") or ""
            batch_tasks.append(
                {
                    "task_id": tid,
                    "agent_name": task_info.get("agent_name", ""),
                    "task_description": task_info.get("task_description", ""),
                    "success": not is_error,
                    "result": result_text,
                    "error": error_text,
                }
            )

        total = len(batch_tasks)
        failed = sum(1 for t in batch_tasks if not t["success"])

        # 生成任务列表（包含任务名和ID，方便LLM用subagent_status查询）
        task_lines = []
        for t in batch_tasks:
            status_icon = "✅" if t["success"] else "❌"
            desc_preview = t["task_description"][:50] + ("..." if len(t["task_description"]) > 50 else "")
            agent = t["agent_name"]
            task_lines.append(f"- {status_icon} [{agent}] {desc_preview} (id: {t['task_id']})")

        task_list_text = "\n".join(task_lines) if task_lines else "- (无任务详情)"

        callback_content = (
            f"子智能体全部完成。\n"
            f"共 {total} 个，成功 {total - failed} 个，失败 {failed} 个。\n"
            f"本次完成的任务：\n{task_list_text}\n"
            f"可使用 subagent_status 查询详细结果。"
        )

        engine = self.backend.chat_engine
        is_currently_streaming = engine and engine.is_streaming
        logger.info(
            "[SubAgent-Callback] _do_trigger_callback: "
            f"engine_exists={engine is not None}, "
            f"is_streaming={is_currently_streaming}, "
            f"total={total}, failed={failed}"
        )

        # ====== 流式路径：注入汇总到流中，不中断 ======
        if is_currently_streaming:
            logger.info("[ChatEngine] Sub-agent all done during streaming: injecting summary without interrupt")
            self._inject_subagent_completion_into_stream(
                task_id="__batch_summary__",
                result=callback_content,
                success=(failed == 0),
                agent_name="",
                task_description="",
            )
            return

        # ====== 非流式路径：强制中断并创建新对话轮次 ======
        logger.info(
            f"[SubAgent-Callback] _do_trigger_callback: 非流式路径，创建新对话轮次 total={total}, failed={failed}"
        )

        # 统一处理：为回调消息准备 UI（用户卡片 + 新助手卡片）
        self._prepare_ui_for_callback_message(callback_content)

        # 发送回调消息到引擎（非流式场景下 UI 已在 _prepare_ui_for_callback_message 中准备好）
        if not self.backend.send_message_to_engine(callback_content):
            # 发送失败（通常是正在流式或配置无效），回滚 UI 状态
            logger.warning("[ChatEngine] Sub-agent callback: send_message_to_engine failed, rolling back UI")
            self._is_streaming = False
            self._toggle_send_stop(False)
            if self._current_assistant_card:
                self._current_assistant_card.deleteLater()
                self._current_assistant_card = None
        else:
            # send_message 成功后同步 batch 结构（send 会往 session 写入消息，因此同步必须在 send 之后）
            self._sync_batch_structures()
            self._fix_new_card_message_index(user_text=callback_content)
            self._visible_batch_end = len(self._message_batch)
            # ⚠️ 时间线节点在子智能体任务完成时不会更新 - 修复
            self._sync_node_preview_to_last()

    def _prepare_ui_for_callback_message(self, callback_text: str):
        """为子智能体回调消息准备 UI（用户卡片 + 助手卡片 + 流式状态）

        非流式场景（如 /compact 手动命令）下，回调消息发送后引擎会产生流式响应，
        但 UI 上没有对应的卡片来承载内容，导致消息写入 session 却不可见。
        此方法复用 _on_send_message 的 UI 准备逻辑，确保流式回调内容能正确渲染。

        注意：只做 UI 准备（创建卡片、设置状态），不做 batch 同步。
        batch 同步必须在 send_message_to_engine 之后执行（因为 send 会往 session 写消息）。
        """
        self._hide_welcome_cards()

        # 创建用户消息卡片（显示回调通知文本）
        self._append_user_message(callback_text)

        # 创建助手消息卡片（用于接收 LLM 的流式响应）
        assistant_card = self._append_assistant_message(
            model_name=self._current_model_name,
            config_id=self._current_provider_name,
        )

        # 设置当前卡片（必须在 send_message 之前，否则流式回调触发时 _current_assistant_card 为 None）
        self._current_assistant_card = assistant_card
        self._response_start_time = time.time()
        assistant_card.start_elapsed_tracking()
        self._is_streaming = True
        # ❌ 不在这里切换停止按钮！内建函数/子智能体执行后只是准备接收回调响应，
        # 按钮状态应在 LLM 实际开始流式响应时由 _on_stream_started 切换。
        # self._toggle_send_stop(True)

        # 确保 ToolExecutor 使用正确的 session_id
        session = self.session_manager.get_current_session()
        if session and self.backend.tool_executor:
            self.backend.set_session_context(session.session_id)

    def _on_sub_agent_finished(self, task_id: str, result: str):
        """单个子智能体执行完成"""
        if getattr(self, "_is_destroyed", False):
            return
        sub_agent_mgr = self.backend.sub_agent_manager
        if sub_agent_mgr:
            sub_agent_mgr.task_finished.emit(task_id, result)

    def _on_tool_result_received(self, tool_call_id: str, tool_name: str, arguments: dict, result: Any):
        if getattr(self, "_is_destroyed", False):
            return
        import time

        # 去重保护：_emit_with_callback 双路径（event_bus + signal.emit）
        # 会导致本方法被调用两次，产生重复工具块
        if not hasattr(self, "_processed_tool_result_ids"):
            self._processed_tool_result_ids: set = set()
        if tool_call_id in self._processed_tool_result_ids:
            return
        self._processed_tool_result_ids.add(tool_call_id)

        if self._is_auto_loop_running:
            # AutoLoop 模式：只记录日志，不操作 UI
            if self._auto_loop_running_card:
                self._auto_loop_running_card.append_log(f"工具完成: {tool_name}")

        elapsed = time.time() - self._current_tool_start_time if hasattr(self, "_current_tool_start_time") else 0

        # 支持 ToolResult 对象和 dict 格式的 result
        if isinstance(result, dict):
            success = result.get("success", True)
            error_msg = result.get("error", "") or ""
            content = (
                error_msg
                if not success
                else (str(result.get("content", "")) if result.get("content") is not None else "")
            )
        else:
            success = getattr(result, "success", True) if hasattr(result, "success") else True
            error_msg = str(getattr(result, "error", "") or "")
            content = str(result) if result else ""

        # 统一处理工具完成状态
        # 字段驱动：任何工具结果携带 todos 字段 → 联动待办 UI（插件声明，主程序不写死工具名）
        todos = result.get("todos") if isinstance(result, dict) else getattr(result, "todos", None)
        if todos:
            self._todo_floating_widget.update_todos(todos)
        # （T11-3c：原 if/elif 空分支已删——todo 更新由上方完成；
        #   工具结果块由 append_tool_result 原地转换处理，无需额外动作）

        # 提取 diff 字段（ToolResult 对象或 dict 格式）
        diff_val = None
        if isinstance(result, dict):
            diff_val = result.get("diff", None)
        else:
            diff_val = getattr(result, "diff", None) if result else None

        # 提取 echarts 字段（ToolResult 对象或 dict 格式）
        echarts_val = None
        if isinstance(result, dict):
            echarts_val = result.get("echarts", None)
        else:
            echarts_val = getattr(result, "echarts", None) if result else None

        if self._current_assistant_card:
            # 工具结果必须写入“拥有该运行折叠框”的卡片（与流式块同一张），
            # 否则运行框无法被原地转换为完成框，并会不断累积。
            target_card = self._tool_card_map.get(tool_call_id) or self._current_assistant_card
            target_card.append_tool_result(
                tool_name=tool_name,
                arguments=arguments or {},
                result=content,
                success=success,
                tool_call_id=tool_call_id,
                diff=diff_val,
                echarts=echarts_val,
            )

        self._scroll_to_bottom()

        # 字段驱动：任何工具结果携带 diff 时实时更新差异统计（插件声明，主程序不写死工具名）
        if diff_val:
            self._update_card_diff_stats_for_call(tool_call_id)

    def _find_latest_assistant_card(self) -> Optional[MessageCard]:
        for i in range(self.chat_layout.count() - 1, -1, -1):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if isinstance(widget, MessageCard) and widget.role == "assistant":
                    return widget
        return None

    def _notify_if_inactive(self, title: str, message: str):
        if not self.cfg.llm_notify_enabled.value:
            return

        if not self._should_show_inactive_notification():
            return

        sound_type = self.cfg.llm_notify_sound.value
        if sound_type != "none":
            QApplication.beep()

        # 使用全局 TrayManager 发送通知（避免多窗口多个托盘图标的问题）
        # 传递顶层窗口引用，确保点击通知时能正确恢复最小化的对话框
        from app.tray_manager import TrayManager

        # Tab 模式：计算当前窗口对应的标签页索引，点击通知时自动跳转
        tab_index = -1
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is not None:
                tab_index = tm._window_to_index.get(id(self), -1)
        except Exception:
            pass

        top_window = self.window() if callable(self.window) else self
        TrayManager.get_instance().notify(title, message, window=top_window, tab_index=tab_index)

    def _should_show_inactive_notification(self) -> bool:
        """Only notify when the app window is not effectively visible to the user."""
        window = None
        if self.homepage and self.homepage.window():
            window = self.homepage.window()
        else:
            window = self.window()

        if window is None:
            return True

        if not window.isVisible() or window.isMinimized():
            return True

        native_result = self._is_window_in_foreground_native(window)
        if native_result is not None:
            return not native_result

        app = QApplication.instance()
        active_window = app.activeWindow() if app is not None else None

        # When the current app is not foreground, the chat reply should notify.
        if active_window is None:
            return True

        if active_window is window:
            return False

        return not window.isActiveWindow()

    def _is_window_in_foreground_native(self, window) -> Optional[bool]:
        """Use the OS foreground window when available to avoid Qt focus misreads."""
        if os.name != "nt":
            return None

        try:
            user32 = ctypes.windll.user32
            foreground_hwnd = user32.GetForegroundWindow()
            if not foreground_hwnd:
                return None

            foreground_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(foreground_hwnd, ctypes.byref(foreground_pid))
            return foreground_pid.value == os.getpid()
        except Exception:
            return None

    def _on_notification_clicked(self):
        window = self.window()
        if window:
            window.show()
            if window.isMinimized():
                window.showNormal()
            window.activateWindow()

    def _on_stream_finished(self, response: str):
        if getattr(self, "_is_destroyed", False):
            return
        self._is_streaming = False
        self._toggle_send_stop(False)
        self._set_ai_state("idle")  # 桌宠：任务完成

        # 写入模型名称/服务商到卡片和 session 消息
        if self._current_assistant_card:
            current_model_name = getattr(self, "_current_model_name", "") or ""
            if current_model_name:
                provider_display = self._valid_configs.get(self._current_provider_name, {}).get(
                    "display_name", self._current_provider_name
                )
                self._current_assistant_card.set_model_name(
                    current_model_name,
                    provider_name=provider_display,
                    config_id=self._current_provider_name,
                )
                session = self.session_manager.get_current_session()
                if session and session.messages:
                    # 🐛 修复：只更新最近一条 assistant 消息，避免每次新对话把历史消息的
                    # model_name/provider_name/config_id 都覆盖成当前轮的，导致之前卡片
                    # 的页脚模型显示全部变成最后那次对话的模型。
                    for msg in reversed(session.messages):
                        if msg.get("role") == "assistant":
                            msg["model_name"] = current_model_name
                            if not msg.get("provider_name"):
                                msg["provider_name"] = provider_display
                            if not msg.get("config_id") and self._current_provider_name:
                                msg["config_id"] = self._current_provider_name
                            break
        # 计算流式耗时并设置到卡片底部栏
        elapsed = None
        if self._response_start_time is not None:
            elapsed = time.time() - self._response_start_time
        self._response_start_time = None

        if self._current_assistant_card and not _is_sip_deleted(self._current_assistant_card):
            if elapsed is not None:
                self._current_assistant_card.set_meta_info(elapsed=elapsed)
            self._current_assistant_card.finish_streaming()

        # 🛡️ 流式完成后显式滚底：finish_streaming 触发的最后一次全量渲染
        # 替换 DOM 后，contentHeightChanged 可能因高度不变而不触发，或
        # 触发时 _is_streaming 已为 False 导致 _on_message_card_height_changed
        # 不滚底。此处显式调用（内部有 24ms 定时器等待 WebEngine 布局完成）。
        self._scroll_to_bottom()
        # 🆕 流式结束滚底宽限 2s：此窗口内 _ensure_at_bottom 忽略用户滚离拦截
        # （防止用户流式中途上滚导致兜底滚底被跳过）。
        self._stream_finished_grace_until = time.monotonic() + 2.0
        # 🆕 延迟兜底滚底：finish_streaming 的 WebEngine 全量重渲染是异步的，
        # 立即滚底时 scrollbar.maximum() 可能仍是旧值；500ms/1000ms 两次
        # 延迟兜底覆盖重渲染完成后的最终高度（长消息渲染可能更久）。
        QTimer.singleShot(500, self._scroll_to_bottom)
        QTimer.singleShot(1000, self._scroll_to_bottom)

        # 🚀 [PERF] 拆分持久化：save 立即执行（快，仅序列化），flush 延迟执行
        # 原同步执行 save + flush 与 finish_streaming 的 WebEngine 重渲染连续阻塞主线程。
        # save 是内存操作+SQLite INSERT（~1-3ms），flush 是 fsync 写盘（~10-50ms）。
        # 立即 save 保留会话数据以防用户切换，延迟 flush 让 WebEngine 先完成布局/绘制。
        # _do_post_stream_cleanup 中的 flush 会补上磁盘同步。
        if self.history_manager and not getattr(self, "_session_switched", False):
            self._save_current_session_to_history()
            # flush 延迟到 _do_post_stream_cleanup，避免阻塞主线程渲染

        # 🛡️ 延迟非UI关键操作到下一轮事件循环，让上一次 _perform_update 的
        # WebEngine layout/paint 事件有机会先被处理，避免主线程连续阻塞导致
        # UI 卡顿后「刷的更新一片」
        QTimer.singleShot(0, self._do_post_stream_cleanup)

        # ★ B4 强回收层（低频触发）：流式结束后顺带检查内存阈值
        self._maybe_strong_recycle()

        # 团队任务：流式完成后清理邮件状态 + 注入 Stop 提示
        if getattr(self, "_current_team_mail", None):
            self._on_task_stream_finished()
        else:
            # 🆕 流式结束（非团队任务场景），检查流式过程中是否有新到达的团队邮件
            # 被 _is_streaming 守卫拦住，现在重新触发处理
            self._check_and_process_pending()

        # 🆕 流式结束：标记流式中 hook 注入的团队邮件为已完成
        self._finalize_injected_team_mails()

        self._focus_input_if_active()

    def _do_post_stream_cleanup(self):
        """流式完成后延迟执行的清理和同步操作（不阻塞 UI 渲染流程）"""
        if getattr(self, "_is_destroyed", False):
            return

        # 🛡️ 会话切换哨兵：流式回调链最末端的兜底。若 stop_streaming 后 worker
        # 仍投递了 _on_messages_updated 但被丢弃，但 _on_stream_finished 仍可能
        # 触发 _do_post_stream_cleanup 把"当前"会话（其实是新会话）保存到新项目。
        # 哨兵在 _on_send_clicked 发起新 AI 请求时清零。
        if getattr(self, "_session_switched", False):
            logger.warning("[PostStreamCleanup] 检测到会话切换哨兵，跳过本次保存防止重复落盘")
            return

        if self.history_manager:
            self._save_current_session_to_history()
            # 🛡️ 立即落盘，确保数据写入 SQLite
            self.history_manager.flush()
            # 流式完成后同步 batch 结构，确保 _message_batch 包含完整的 assistant batch
            self._sync_batch_structures()
            # 修复：同步可见范围，避免回收机制误删当前轮次的卡片
            self._visible_batch_end = len(self._message_batch)

        session = self.session_manager.get_current_session()
        if session and session.messages:
            last_msg = session.messages[-1] if session.messages else None
            if last_msg and last_msg.get("role") == "assistant":
                content = last_msg.get("content", "")
                if isinstance(content, list):
                    from app.core import content_to_text

                    content = content_to_text(content)
                # 如果最后一条消息是hook消息，不触发通知（例如新建会话时的SessionStart）
                if last_msg.get("_hook_event"):
                    logger.debug("[Notify] Skip notification for hook message")
                else:
                    preview = content[:50] + "..." if len(content) > 50 else content
                    current_title = self.title_edit.text() if self.title_edit else "对话完成"
                    self._notify_if_inactive(current_title, preview)

        # 对话完成后更新缓存统计显示
        self._refresh_cache_stats()

        # ⚠️ 时间线节点在流式完成时不会更新 - 修复
        self._update_node_preview()
        self._sync_node_preview_to_last()

        # 对话完成后刷新余额显示
        self._refresh_balance()

        # 更新当前助手卡片差异统计
        self._update_card_diff_stats()

    def _update_card_diff_stats_for_call(self, tool_call_id: str, card=None):
        """单个工具完成后增量更新卡片差异统计（直接用 call_id 查 file_recorder，不依赖 session.messages）

        Args:
            tool_call_id: 刚刚完成的工具调用 ID
            card: 可选，目标卡片；不传则用 _current_assistant_card
        """
        if card is None:
            card = getattr(self, "_current_assistant_card", None)
        if not card or card.role != "assistant":
            return
        if not self.backend.file_recorder:
            return

        from app.widgets.ui_helpers import compute_diff_stats

        try:
            session = self.session_manager.get_current_session()
            if not session:
                return
            session_id = session.session_id

            ops = self.backend.file_recorder.get_operations_for_preview(session_id, tool_call_id)
            if not ops:
                return

            stats = compute_diff_stats(ops)
            if stats["files"] > 0 or stats["additions"] > 0 or stats["deletions"] > 0:
                # 提取本次涉及的文件路径用于去重
                seen = {op.get("file_path", "") for op in ops if op.get("file_path")}
                card.add_diff_stats(
                    files_count=stats["files"],
                    additions=stats["additions"],
                    deletions=stats["deletions"],
                    seen_files=seen,
                )
        except Exception as e:
            logger.warning(f"[DiffStats] 增量更新统计失败: {e}")

    def _update_card_diff_stats(self, card=None):
        """计算助手卡片的文件修改统计并更新到页脚

        Args:
            card: 可选，指定要更新的卡片；不传则使用 _current_assistant_card
        """
        if card is None:
            card = getattr(self, "_current_assistant_card", None)
        if not card or card.role != "assistant":
            return
        session = self.session_manager.get_current_session()
        if not session:
            return

        round_index = getattr(card, "_round_index", None)
        if round_index is None or round_index < 0:
            return

        # 检查 file_recorder 是否可用
        if not self.backend.tool_executor or not self.backend.file_recorder:
            return

        try:
            from app.widgets.ui_helpers import (
                collect_operations_for_round,
                compute_diff_stats,
            )

            call_ids = self._get_tool_call_ids_in_round(round_index)
            if not call_ids:
                return

            operations = collect_operations_for_round(self.backend.file_recorder, session.session_id, call_ids)
            if not operations:
                return

            stats = compute_diff_stats(operations)
            if stats["files"] > 0 or stats["additions"] > 0 or stats["deletions"] > 0:
                card.set_diff_stats(
                    files_count=stats["files"],
                    additions=stats["additions"],
                    deletions=stats["deletions"],
                )
        except Exception as e:
            logger.warning(f"[DiffStats] 计算差异统计失败: {e}")

    def _refresh_balance(self):
        """刷新余额显示（对话完成后调用）— 用量聚合 T6：委托 UsageService 单例"""
        logger.debug(f"[Balance] _refresh_balance called, config_id={getattr(self, '_current_provider_name', 'None')}")
        balance_display = getattr(self, "balance_display", None)
        if balance_display:
            # _current_provider_name 是 config_id；要先取出真实的 provider_name
            # 才能匹配余额查询白名单。
            config_id = getattr(self, "_current_provider_name", "")
            config = self._valid_configs.get(config_id, {})
            provider_name = config.get("provider_name", "")
            logger.debug(f"[Balance] provider_name={provider_name}")
            if provider_name in ("DeepSeek", "SiliconFlow (硅基流动)"):
                api_key = config.get("API_KEY", "")
                logger.debug(f"[Balance] api_key exists: {bool(api_key)}")
                if api_key:
                    balance_display.set_provider(provider_name, config_id)
                    # 委托全局单例：缓存命中直接广播，未命中单例后台抓取
                    from app.core.usage_service import UsageService

                    UsageService.get_instance().request_balance(provider_name, config_id, config)
                    return
            # 如果不支持余额查询，隐藏
            balance_display.setVisible(False)

    def _resolve_session_project_fallback(
        self,
        session_id: str,
        current_project: str,
        session: Optional["ChatSession"] = None,
    ) -> str:
        """解析会话的项目归属兜底值：优先沿用已有归属，避免被默认项目覆盖。

        策略（优先级从高到低）：
        1. session.originating_project：会话级「首发项目」快照。
           用户首次发消息时锁定（即使用户切换项目也不变），
           避免"对话完成后立即切换项目导致会话错存到切换后项目"bug。
        2. 通过 history_manager 查询该 session_id 在内存/SQLite 中的现有 project
           （保护"项目切换不影响老会话归属"）
        3. 查不到则使用 current_project（仅适用于真正的新会话）

        Args:
            session_id: 会话 ID
            current_project: 当前项目（兜底值）
            session: 可选 ChatSession 对象，若提供则优先用其 originating_project 字段

        Returns:
            解析后的项目名
        """
        # 1. 优先：会话级首发项目快照（用户在哪个项目下首发的对话）
        if session is not None:
            op = getattr(session, "originating_project", "") or ""
            if op:
                return op
        # 2. 次之：内存/SQLite 中该 session_id 的已有 project
        if not session_id or not self.history_manager:
            return current_project
        try:
            existing = self.history_manager.get_session_by_session_id(session_id)
            if existing:
                existing_project = existing.get("project")
                if existing_project:
                    return existing_project
        except Exception as e:
            logger.warning(f"[ProjectResolve] 查询已有项目归属失败: {e}, fallback={current_project}")
        # 3. 兜底：current_project
        return current_project

    def _save_current_session_to_history(self):
        session = self.session_manager.get_current_session()
        saved_messages = list(session.messages or []) if session else []
        if not saved_messages:
            return

        # 🛡️ 团队解散空白会话守卫：跳过没有用户消息的会话。
        # 团队批量创建窗口时 create_session 触发 SessionStart hook 注入团队指引
        # （role=user 但带 _hook_event）。若团队在首次真实对话前被解散/离开
        # （_team_run_id 已清空），后台 finalize 链（_launch_background_finalize）
        # 强制置脏后走本方法保存——旧实现仅检查 saved_messages 非空 + _session_dirty，
        # 会把仅含 hook 消息的空白会话落库成普通类型记录（历史面板出现大量空白
        # 普通会话）。与 _auto_save_current_session 同口径：team 邮件
        # （_hook_event="TeamMail"）视为真实用户问题（邮件触发的对话应保存），
        # 其余 hook 消息不算。
        has_user_message = any(
            msg.get("role") == "user" and (not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail")
            for msg in saved_messages
        )
        if not has_user_message:
            return

        # 🛡️ 跳过无变更保存：自上次保存后会话没有新消息/修改，
        # 避免 _do_post_stream_cleanup 等延迟清理路径重复持久化。
        # 脏标记由 _on_send_clicked / _on_messages_updated / _on_finalize_complete
        # / AutoLoop 等消息修改点置 True，由本 save 函数置 False。
        if not self._session_dirty:
            return

        # 🛡️ 团队元数据：高频保存路径（每轮消息）也需携带团队字段，
        # 否则团队窗口的会话记录缺 team_run_id/agent_name，恢复时按
        # agent 分组会漏成员（成员 4→1 根因之一）。
        # 与 _auto_save_current_session 语义一致：仅当本窗口处于团队模式
        # （_team_run_id 非空）才传团队字段；非团队窗口不传（None →
        # update 保留现值 / INSERT 落空值），避免普通编辑篡改团队元数据。
        # 🛡️ M1-r：team_members 快照也随高频保存落库（与 _auto_save
        # _current_session 同口径）——否则仅靠关闭窗口时的 _auto_save 落库，
        # 没对话过的成员（无自身会话、仅靠快照找回）在历史合并条目中不显示
        # （_merge_team_lightweight 快照成员并入依赖会话记录里的 team_members）。
        team_kwargs = {}
        if getattr(self, "_team_run_id", None):
            team_kwargs = {
                "team_run_id": self._team_run_id,
                "team_name": getattr(self, "_team_name", "") or "",
                "agent_name": getattr(self, "_team_agent_name", "") or "",
                "team_members": self._get_team_members_snapshot_json(),
            }

        system_prompt = getattr(session, "system_prompt", "") or ""
        # 优先使用已有的 topic_summary，避免被用户消息前30字覆盖
        session_title = getattr(session, "topic_summary", "") or ""
        worktree_path = self._get_current_worktree_path()
        # 🛡️ 始终记录当前 worktree_path（空字符串表示主仓库，清除旧关联）
        worktree_kwargs = {"worktree_path": worktree_path or ""}

        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(self._current_session_id)
            if idx is not None:
                # 🛡️ 更新已有会话时不传 project，保留该会话原有的项目归属
                self.history_manager.update_session(
                    idx,
                    saved_messages,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                    **worktree_kwargs,
                    **team_kwargs,
                )
            else:
                # 🛡️ 内存中找不到（如老会话被 _history_limit=100 截断），
                # 优先查询 SQLite 原有 project，避免被当前 _current_project 错误覆盖
                resolved_project = self._resolve_session_project_fallback(
                    self._current_session_id, self._current_project, session=session
                )
                self.history_manager.save_session(
                    saved_messages,
                    title=session_title,  # 使用已有的 topic_summary
                    session_id=session.session_id if session else None,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                    project=resolved_project,
                    **worktree_kwargs,
                    **team_kwargs,
                )
                self._current_session_id = session.session_id if session else None
        else:
            # 🛡️ 新会话首次入库：优先用 session.originating_project（用户首次发消息时锁定），
            # 若未锁定则查 SQLite，最后兜底 _current_project。
            # 这条路径专门防止"对话进行中切换项目导致会话错存"bug。
            resolved_project = self._resolve_session_project_fallback(
                session.session_id if session else None, self._current_project, session=session
            )
            self.history_manager.save_session(
                saved_messages,
                title=session_title,  # 使用已有的 topic_summary
                session_id=session.session_id if session else None,
                compaction_state=getattr(session, "compaction_state", {}),
                compaction_cache=getattr(session, "compaction_cache", {}),
                system_prompt=system_prompt,
                project=resolved_project,
                **worktree_kwargs,
                **team_kwargs,
            )
            self._current_session_id = session.session_id if session else None

        # 🛡️ 成功保存后清除脏标记，后续无变更的重复 save 将被跳过
        self._session_dirty = False
        self._update_node_preview()
        # 🆕 历史面板刷新：保存后同步内存缓存到历史面板 UI，
        # 避免「历史面板已展开但列表停在保存前快照」bug。
        # 仅历史卡片可见时执行（不可见时 0 开销），下次打开面板仍走
        # _toggle_history_card → _refresh_history_toggle_panel 拉取最新数据。
        refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

    @staticmethod
    def _count_user_messages(messages: List[Dict]) -> int:
        """统计用户消息数量（用于截断后新旧 worker 识别）

        🛡️ R7 修复：team 邮件（_hook_event="TeamMail"）视为真实用户消息
        → worker 截断后新旧消息识别不再错位（mail 计入比对基数）
        """
        return sum(
            1
            for m in messages
            if m.get("role") == "user" and (not m.get("_hook_event") or m.get("_hook_event") == "TeamMail")
        )

    @staticmethod
    def _contains_user_text(messages: List[Dict], text: str) -> bool:
        """检查消息列表中是否包含指定文本的用户消息（用于文本指纹比对）

        🛡️ R7 修复：team 邮件（_hook_event="TeamMail"）参与文本指纹比对
        """
        for msg in messages:
            if msg.get("role") != "user":
                continue
            if msg.get("_hook_event") and msg.get("_hook_event") != "TeamMail":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and content == text:
                return True
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text") == text:
                        return True
        return False

    def _on_messages_updated(self, messages: List[Dict[str, Any]]):
        # 🛡️ 关闭窗口路径守护：强制停止后 closeEvent 同步清理 backend，
        # 跨线程 queued 的 finished_with_messages 可能在 widget 已 _is_destroyed 后才被
        # 主线程事件循环处理。closeEvent 路径已主动同步应用 + 持久化（见 _apply_interrupted_messages_to_session），
        # 这里无需再 set_messages，避免 UI 副作用（set_meta_info/ring 刷新）访问已销毁的 widget。
        if getattr(self, "_is_destroyed", False):
            return
        # 🛡️ 压缩守卫（P-Compact）：自动压缩清空会话后，被 stop_streaming 取消的旧 worker
        # 其 finished_with_messages（旧消息快照）可能延迟到达，全量覆写会把已清空的会话
        # 恢复成旧消息（压缩失效 + 反复触发）。判定：消息数 > 当前 session → 旧 worker
        # 快照，丢弃。新 worker 长度与 session 长度一致时不误拦（+0 容差）。
        # 修复：原阈值 `+10` 在短对话（≤10 条）下失效，会被覆盖回旧消息。
        if getattr(self, "_post_compact_guard", False):
            _cur_session = self.session_manager.get_current_session() if self.session_manager else None
            if _cur_session and messages and len(messages) > len(_cur_session.messages):
                self._post_compact_guard = False
                from loguru import logger

                logger.info(
                    f"[MessagesUpdated] 压缩守卫拦截旧 worker 延迟消息: "
                    f"msg_count={len(messages)}, current_len={len(_cur_session.messages)}"
                )
                return
        # 🛡️ 会话切换哨兵：_create_new_session 会创建新会话并 stop_streaming，
        # 但 worker 跨线程的 _on_messages_updated 可能延迟到达，此时
        # get_current_session() 返回的是新会话（旧会话已被替换），
        # 继续 set_messages 会把旧消息写到新会话，再被后续 save 错误保存到新项目。
        # 哨兵在 _on_send_clicked 发起新 AI 请求时清零。
        #
        # ⚠️ 例外：_pending_session_hook 标记表示当前正处在 _create_new_session /
        # _clear_session / _on_compact_finished 中的 SessionStart hook 注入阶段，
        # 这批消息是合法的新会话 hook 输出，放行不被丢弃。
        if getattr(self, "_session_switched", False):
            if not getattr(self, "_pending_session_hook", False):
                from loguru import logger

                logger.warning(
                    f"[MessagesUpdated] 检测到会话切换哨兵，丢弃 worker 的过期回调："
                    f"msg_count={len(messages)}, current_session_id="
                    f"{self.session_manager.get_current_session().session_id[:8] if self.session_manager.get_current_session() else 'None'}"
                )
                return
        session = self.session_manager.get_current_session()
        if not session:
            return

        # 🛡️ 截断保护：old worker 过期消息识别与丢弃
        #
        # 截断哨兵（_truncation_sentinel）：由 _persist_session_after_mutation 在截断时设置，
        # 拦截 old worker 延迟到达的 finished_with_messages 回调。哨兵由 _on_finalize_complete
        # 负责清除（单次使用），此处只读取不修改。
        #
        # pending_send 标志（_pending_send_after_truncation）：截断后用户在 old worker 结束前
        # 发送了新消息时置 True。此时 new worker（W2）的消息也会被哨兵拦截，通过以下两层区分：
        #   第1层：用户消息数比对 — old worker 含截断前消息，数量通常多于当前会话
        #   第2层：文本指纹比对 — 数量相等时（截断1条+新发1条），检查消息列表是否含新发文本
        sentinel = self._truncation_sentinel

        if sentinel and sentinel.get("session_id") == session.session_id:
            if self._pending_send_after_truncation:
                # === 哨兵拦截 + pending_send：检查是 old worker 还是 new worker ===
                incoming_users = self._count_user_messages(messages)
                current_users = self._count_user_messages(session.messages)

                # 第1层：用户数更多 → 含截断前消息 → old worker
                if incoming_users > current_users:
                    from loguru import logger

                    logger.warning(
                        "[MessagesUpdated] 哨兵拦截 + pending_send：旧 worker（用户数更多），丢弃："
                        f"incoming_users={incoming_users}, current_users={current_users}"
                    )
                    return

                # 第2层：用户数相等 → 可能截断1条后新发1条，新旧用户数相同
                # 通过文本指纹判断消息列表是否包含新发的用户文本
                if incoming_users == current_users and self._pending_send_user_text is not None:
                    if not self._contains_user_text(messages, self._pending_send_user_text):
                        from loguru import logger

                        logger.warning(
                            "[MessagesUpdated] 哨兵拦截 + pending_send：旧 worker（等用户数+缺新文本），丢弃："
                            f"incoming_users={incoming_users}"
                        )
                        return

                # 新 worker：清除哨兵 + 标志，继续处理
                from loguru import logger

                logger.info(
                    "[MessagesUpdated] 哨兵拦截 + pending_send：新 worker 消息到达，放行并清除哨兵："
                    f"incoming_users={incoming_users}, current_users={current_users}"
                )
                self._truncation_sentinel = None
                self._pending_send_after_truncation = False
                self._pending_send_user_text = None
            else:
                from loguru import logger

                logger.warning(
                    "[MessagesUpdated] 检测到截断哨兵，丢弃 worker 返回的过期消息："
                    f"worker_len={len(messages)}, current_len={len(session.messages)}, "
                    f"session_id={session.session_id[:8]}"
                )
                return

        elif self._pending_send_after_truncation:
            # === 哨兵已被 old worker 的 _on_finalize_complete 消耗（罕见路径）===
            # old worker 的 finished_with_messages 在长阻塞后延迟到达。
            incoming_users = self._count_user_messages(messages)
            current_users = self._count_user_messages(session.messages)

            # 第1层：用户数更多 → old worker
            if incoming_users > current_users:
                from loguru import logger

                logger.warning(
                    "[MessagesUpdated] 哨兵已消耗 + pending_send：旧 worker（用户数更多），丢弃："
                    f"incoming_users={incoming_users}, current_users={current_users}"
                )
                return

            # 第2层：用户数相等 + 缺新文本 → old worker
            if incoming_users == current_users and self._pending_send_user_text is not None:
                if not self._contains_user_text(messages, self._pending_send_user_text):
                    from loguru import logger

                    logger.warning(
                        "[MessagesUpdated] 哨兵已消耗 + pending_send：旧 worker（等用户数+缺新文本），丢弃："
                        f"incoming_users={incoming_users}"
                    )
                    return

            # 新 worker：清除标志，继续处理
            self._pending_send_after_truncation = False
            self._pending_send_user_text = None

        # 哨兵+user_count 已在上方完成过期消息拦截，正常流到此处的
        # messages 为合法的新 worker 消息，直接更新 session。
        self._history_preview_messages = None
        # 注意：preserve_compaction=False
        # worker 送回来的 current_session_messages 是原始未压缩消息，
        # 保留旧的压缩缓存会导致 state 不一致（缓存说"已压缩"但消息已膨胀）。
        # 清空缓存让下一次 ContextBudgetAllocator 从原始消息正确重新压缩。

        # ⚠️ 写入 elapsed 和 config_id 必须在 set_messages 之前：set_messages 内部 consolidate
        # 会创建新 dict，之后修改参数 messages 不会反映到 session.messages
        if self._response_start_time is not None:
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    if "elapsed" not in msg:
                        msg["elapsed"] = round(time.time() - self._response_start_time, 1)
                    if not msg.get("config_id") and self._current_provider_name:
                        msg["config_id"] = self._current_provider_name

        session.set_messages(messages or [], preserve_compaction=False)
        # 🛡️ Worker 回传了完整消息列表，标记会话脏以确保后续持久化。
        self._session_dirty = True

        # 注：卡片底部 token 显示不再从这里驱动——统一由上下文圆环的快照
        # （_refresh_context_usage_indicator）驱动，保证两者数字完全一致。
        # 这里仍保留 msg["token_usage"] 落库，供历史卡片复现使用。

        # 刷新上下文指示器
        # 工具迭代（最后消息为 tool role）走本地估算以反映含 tool result 的累计上下文；
        # 最终响应（最后消息为 assistant role）优先使用 API 返回的精确 prompt_tokens，
        # 避免对话结束时本地估算与 API 结果不一致。
        is_tool_iter = bool(messages and messages[-1].get("role") == "tool")
        if not is_tool_iter:
            last_tc = getattr(self, "_last_ctx_token_count", 0)
            last_lim = getattr(self, "_last_ctx_limit", 0)
            if last_tc > 0 and last_lim > 0:
                ring = getattr(self, "context_usage_ring", None)
                if ring:
                    percent = min(100, int((last_tc / last_lim) * 100))
                    # 获取压缩状态，避免缓存路径丢失压缩信息
                    session = self.session_manager.get_current_session()
                    compaction_state = dict(getattr(session, "compaction_state", {}) or {})
                    normal_tokens = last_tc
                    compacted_tokens = 0
                    if compaction_state.get("active"):
                        compaction_cache = dict(getattr(session, "compaction_cache", {}) or {})
                        summary_msg = compaction_cache.get("summary_message")
                        if isinstance(summary_msg, dict):
                            content = str(summary_msg.get("content", "") or "")
                            compacted_tokens = max(10, len(content) // 4)
                            normal_tokens = max(0, last_tc - compacted_tokens)
                    ring.set_usage(
                        percent,
                        last_tc,
                        last_lim,
                        compaction_state,
                        normal_tokens,
                        compacted_tokens,
                        breakdown=getattr(ring, "_breakdown", None) or [],
                        # 透传上一次的 pruned_tokens，避免该补充路径把节省量闪回 0
                        pruned_tokens=getattr(ring, "_pruned_tokens", 0),
                    )
                    # 卡片底部 token 显示与上下文圆环同步（同一 last_tc）
                    card = getattr(self, "_current_assistant_card", None)
                    if card and not _is_sip_deleted(card):
                        card.set_meta_info(token_usage={"total": last_tc})
                    # 继续调度节流刷新，补全各类型上下文占比条（breakdown）
                    from PyQt5.QtCore import QTimer

                    QTimer.singleShot(0, self._refresh_context_usage_indicator)
                    return

        # 工具迭代中或没有 API 数据时：本地估算 + compaction 信息
        from PyQt5.QtCore import QTimer

        QTimer.singleShot(0, self._refresh_context_usage_indicator)

    def _on_engine_error(self, error: str):
        if self._current_assistant_card:
            self._current_assistant_card.stop_streaming_anim()
            self._current_assistant_card.set_error_state(True, error_message=error)
            self._current_assistant_card.update_content(error)

        self._is_streaming = False
        self._set_ai_state("error")  # 桌宠：发生错误

        self._toggle_send_stop(False)

        # 停止计时器并写入最终耗时到 session 消息（引擎错误路径不走 _on_messages_updated）
        if (
            self._current_assistant_card
            and not _is_sip_deleted(self._current_assistant_card)
            and self._response_start_time is not None
        ):
            elapsed = time.time() - self._response_start_time
            self._current_assistant_card.set_meta_info(elapsed=elapsed)
            # 写入 session 最后一条 assistant 消息以便持久化
            session = self.session_manager.get_current_session()
            if session and session.messages:
                for msg in reversed(session.messages):
                    if msg.get("role") == "assistant":
                        if "elapsed" not in msg:
                            msg["elapsed"] = round(elapsed, 1)
                        if not msg.get("config_id") and self._current_provider_name:
                            msg["config_id"] = self._current_provider_name
            self._response_start_time = None

        # 🔧 异常时保存已生成的部分消息到历史记录
        # finished_with_messages 信号已先于 error_occurred 被处理，
        # 会话已包含部分消息，这里持久化到历史
        if self.history_manager:
            self._save_current_session_to_history()

        current_title = self.title_edit.text() if self.title_edit else "对话"
        self._notify_if_inactive(f"{current_title} - 错误", error[:100])

        # ⚠️ 时间线节点在引擎错误时不会更新 - 修复
        self._update_node_preview()
        self._sync_node_preview_to_last()

    def _on_retry_status(self, error_type: str, attempt: int, max_retries: int, wait_time: float):
        """API 重试状态通知 - 更新卡片边框和状态栏，同时通知桌宠报错"""
        if getattr(self, "_is_destroyed", False):
            return
        # 🛡️ 不在流式状态时忽略重试信号（说明已停止或不在对话中）
        if not self._is_streaming:
            return
        if self._current_assistant_card:
            if not self._current_assistant_card._retrying:
                self._current_assistant_card.start_retry_anim(error_type, attempt, max_retries, wait_time)
            else:
                self._current_assistant_card.update_retry_status(error_type, attempt, max_retries, wait_time)
        # 通知桌宠：重试 = 出错了（等待恢复）
        self._set_ai_state("error")

    def _on_retry_resolved(self):
        """API 重试成功 - 恢复卡片彩虹边框，通知桌宠继续回复"""
        if getattr(self, "_is_destroyed", False):
            return
        if self._current_assistant_card:
            self._current_assistant_card.stop_retry_anim()
        # 通知桌宠：重试成功，回到 streaming
        self._set_ai_state("streaming")

    def _on_context_updated(self, token_count: int, limit: int, from_api: bool = False):
        """实时上下文占用更新回调"""
        self._last_ctx_token_count = token_count
        self._last_ctx_limit = limit
        # 记录最近一次 API 返回的 prompt_tokens，供快照作为权威占用值
        session = self.session_manager.get_current_session()
        if session is not None:
            try:
                session.last_api_prompt_tokens = token_count
                session.last_api_message_count = len(session.messages)
                session.last_api_prompt_from_usage = from_api
            except Exception:
                pass
        ring = getattr(self, "context_usage_ring", None)
        if not ring or limit <= 0:
            return
        percent = min(100, int((token_count / limit) * 100))

        # 获取压缩状态，以便圆环 tooltip 显示压缩信息
        compaction_state = dict(getattr(session, "compaction_state", {}) or {}) if session else {}
        normal_tokens = token_count
        compacted_tokens = 0
        if compaction_state.get("active"):
            compaction_cache = dict(getattr(session, "compaction_cache", {}) or {})
            summary_msg = compaction_cache.get("summary_message")
            if isinstance(summary_msg, dict):
                # 用字符长度估算摘要 token 数（仅显示用，不需要精确）
                content = str(summary_msg.get("content", "") or "")
                compacted_tokens = max(10, len(content) // 4)
                normal_tokens = max(0, token_count - compacted_tokens)

        ring.set_usage(
            percent,
            token_count,
            limit,
            compaction_state,
            normal_tokens,
            compacted_tokens,
            breakdown=getattr(ring, "_breakdown", None) or [],
        )
        # 卡片底部 token 显示与上下文圆环同步（同一 token_count）
        card = getattr(self, "_current_assistant_card", None)
        if card and not _is_sip_deleted(card):
            card.set_meta_info(token_usage={"total": token_count})
        # 流式期间也调度一次补全各类型占比 breakdown（_refresh_context_usage_indicator
        # 已不再在 _is_streaming 时拦截，0.5s 节流保护）
        from PyQt5.QtCore import QTimer

        QTimer.singleShot(0, self._refresh_context_usage_indicator)

    def _on_user_message_added(self, user_text: str):
        """TODO: 实现用户消息添加时的回调处理"""
        pass

    def _on_skill_requested(self, method: str, params: dict):
        if getattr(self, "_is_destroyed", False):
            return
        result = self.backend.execute_skill(method, params)
        content = f"[Skill Result] {result}" if "error" not in result else f"[Skill Error] {result.get('error')}"
        # 确保 round_index 正确
        self._current_assistant_round_index = self._get_current_user_round_index()
        new_card = self._append_assistant_message(
            model_name=self._current_model_name,
            config_id=self._current_provider_name,
        )
        new_card.update_content(str(content))
        new_card.finish_streaming()
        self._scroll_to_bottom()

    def _hide_all_cards_for_question(self):
        """Question 卡片显示时，隐藏所有其他卡片（最高优先级）"""
        # 保存 todo 可见状态（用于 question 关闭后恢复）
        self._todo_was_visible_before_system = self._todo_floating_widget.isVisible()
        # 通过 CardManager 隐藏所有卡片
        for card_id in [
            "todo",
            "tool",
            "sub_agent",
            "model_config",
            "history",
            "settings",
            "memory",
            "provider_edit",
            "auto_loop_config",
            "hook_edit",
            "undo_delete",
        ]:
            self._card_manager.hide_card(card_id, self._window_id)
        if not self._is_auto_loop_running:
            self._card_manager.hide_card("auto_loop_running", self._window_id)

    def _restore_after_question_close(self):
        """Question 卡片关闭后，恢复非系统卡片的显示状态"""
        # 恢复 todo（如果之前是显示的且还有内容）
        if self._todo_was_visible_before_system and self._todo_floating_widget._todo_list:
            self._todo_floating_widget.setVisible(True)
        # tool 和 sub_agent 有自我生命周期管理，不需要强制恢复

    def _on_question_asked(self, tool_call_id: str, questions: list, extra: dict = None):
        if getattr(self, "_is_destroyed", False):
            return
        self._set_ai_state("question")  # 桌宠：等待用户回答
        # 隐藏输入框 + 工具栏 + 胶囊发光层，让用户专注看问题
        # （工具栏是 self 的直接子控件，不在 _bottom_input_container 里，
        #  必须单独隐藏，否则会与提问卡片重叠）
        if hasattr(self, "_bottom_input_container"):
            self._bottom_input_container.setVisible(False)
        if hasattr(self, "_bottom_toolbar_strip"):
            self._bottom_toolbar_strip.setVisible(False)
        if hasattr(self, "_input_glow_underlay"):
            self._input_glow_underlay.setVisible(False)
        self._question_tool_call_id = tool_call_id
        if not isinstance(questions, list):
            questions = []
        # 先填充内容再展开容器：否则 CardContainer._do_expand 在空内容上读 sizeHint
        # 算出错误高度并锁住，后续 updateGeometry + QTimer.singleShot 排在同一帧事件循环
        # 里竞争，sizeHint 可能仍是过期的，导致卡片偶尔不显示/被裁切（需手动 resize 兜底）
        #
        # 🛠️ 第二次调用（同轮多工具迭代）：_render_current → _recycle_options 会
        #   复用旧 option widgets + 创建新的 custom input widget，内部布局被大范围
        #   修改但 layout cache 没有 invalidate。_do_expand 读 sizeHint 时拿到过期
        #   值（0 或极小），容器跳过展开卡在 0 高度，直到 resize 触发完整 layout pass。
        #   显式 invalidate + updateGeometry 强制清除缓存，确保 _do_expand 读到
        #   正确高度。
        self._question_floating_widget.setUpdatesEnabled(False)
        self._question_floating_widget.show_question(questions)
        self._question_floating_widget.setUpdatesEnabled(True)
        # 🛠️ show_card 前强制容器 layout 同步刷新，确保 _do_expand 不读过期 sizeHint。
        # 使用 local variable 避免 layout() 返回 None 时崩溃。
        _ql = self._question_floating_widget.layout()
        if _ql is not None:
            _ql.invalidate()
        self._question_floating_widget.updateGeometry()
        self._card_manager.show_card("question", self._window_id)

        # 🛡️ 安全网：延迟 200ms 后重试展开容器（绕过拖拽检查，直接调 _do_expand）
        # 同时强制父级布局激活，确保容器被正确分配高度。
        QTimer.singleShot(200, self._bottom_card_container._do_expand)

        question_text = questions[0].get("question", "") if questions else ""
        self._notify_if_inactive("需要回答问题", question_text[:100])

    def _on_question_answered(self, answer: str):
        if getattr(self, "_is_destroyed", False):
            return
        self._set_ai_state("streaming")  # 桌宠：已回答，准备继续生成
        self._card_manager.hide_card("question", self._window_id)
        # 恢复输入框 + 工具栏 + 胶囊发光层
        if hasattr(self, "_bottom_input_container"):
            self._bottom_input_container.setVisible(True)
        if hasattr(self, "_bottom_toolbar_strip"):
            self._bottom_toolbar_strip.setVisible(True)
        if hasattr(self, "_input_glow_underlay"):
            self._input_glow_underlay.setVisible(True)
        self._restore_after_question_close()
        if self.pixel_pet:
            self.pixel_pet.set_state("streaming")  # 回答后继续回复
        if self._pending_permission_tool_call_id:
            tool_call_id = self._pending_permission_tool_call_id
            self._pending_permission_tool_call_id = None
            # answer 格式为 "问题「...」的回答：\n【允许】"，用 in 匹配标签
            if "【允许】" in answer:
                self.backend.approve_tool_permission(tool_call_id, False, False)
            elif "【允许且该轮对话自动允许】" in answer:
                self.backend.approve_tool_permission(tool_call_id, True, False)
            elif "【本次会话允许】" in answer:
                self.backend.approve_tool_permission(tool_call_id, False, True)
            else:
                self.backend.deny_tool_permission(tool_call_id)
            self._focus_input_if_active()
            return

        if not self._question_tool_call_id:
            return

        tool_call_id = self._question_tool_call_id
        self._question_tool_call_id = None

        if self.backend.chat_engine:
            self.backend.provide_question_answer(answer)

        self._focus_input_if_active()

    def _on_question_cancelled(self):
        """用户关闭问题窗口时，返回空答案让大模型继续"""
        if getattr(self, "_is_destroyed", False):
            return
        self._set_ai_state("idle")  # 桌宠：取消提问，恢复空闲
        self._card_manager.hide_card("question", self._window_id)
        # 恢复输入框 + 工具栏 + 胶囊发光层
        if hasattr(self, "_bottom_input_container"):
            self._bottom_input_container.setVisible(True)
        if hasattr(self, "_bottom_toolbar_strip"):
            self._bottom_toolbar_strip.setVisible(True)
        if hasattr(self, "_input_glow_underlay"):
            self._input_glow_underlay.setVisible(True)
        self._restore_after_question_close()
        if self.pixel_pet:
            self.pixel_pet.set_state("idle")  # 取消则回 idle

        if self._pending_permission_tool_call_id:
            tool_call_id = self._pending_permission_tool_call_id
            self._pending_permission_tool_call_id = None
            self.backend.deny_tool_permission(tool_call_id)
            self._focus_input_if_active()
            return

        if not self._question_tool_call_id:
            return

        self._question_tool_call_id = None

        if self.backend.chat_engine:
            self.backend.provide_question_answer("")

        self._focus_input_if_active()

    def _on_question_preview_requested(self, payload: object):
        """显示权限请求的完整工具参数预览。"""
        if getattr(self, "_is_destroyed", False):
            return
        if not isinstance(payload, dict):
            return

        try:
            from app.utils.diff_viewer import ToolPayloadHtmlGenerator

            html = ToolPayloadHtmlGenerator.generate_html_report(
                tool_name=payload.get("tool_name", ""),
                tool_call_id=payload.get("tool_call_id", ""),
                arguments=payload.get("arguments") or {},
            )
            show_diff_viewer(self, html, title="工具调用参数预览")
        except Exception as e:
            logger.error(f"[Permission] Preview error: {e}")
            InfoBar.error(
                "预览失败",
                str(e),
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
                position=InfoBarPosition.BOTTOM,
            )

    def _on_agent_switched(self, agent_name: str):
        """TODO: 实现智能体切换时的状态同步"""
        if getattr(self, "_is_destroyed", False):
            return
        pass

    def _on_permission_approval_requested(self, tool_call_id: str, tool_name: str, arguments: dict):
        if getattr(self, "_is_destroyed", False):
            return
        # 🛠️ 与 _on_question_asked 对齐：必须先切到 question 状态，
        # 否则 TabPanel 听不到 ai_state_changed 变化、不会在 Tab 边框
        # 渲染问题动画，用户在多 Tab 场景下分不清"哪个窗口在等权限"。
        self._set_ai_state("question")
        self._pending_permission_tool_call_id = tool_call_id
        self._pending_permission_auto_allow = False
        # 隐藏输入框 + 工具栏 + 胶囊发光层，让用户专注看问题
        if hasattr(self, "_bottom_input_container"):
            self._bottom_input_container.setVisible(False)
        if hasattr(self, "_bottom_toolbar_strip"):
            self._bottom_toolbar_strip.setVisible(False)
        if hasattr(self, "_input_glow_underlay"):
            self._input_glow_underlay.setVisible(False)
        # 先填充内容再展开容器：见 _on_question_asked 同源 bug 注释
        self._question_floating_widget.setUpdatesEnabled(False)
        try:
            arg_str = str(arguments)[:160] if arguments else ""
            if arguments and len(str(arguments)) > 160:
                arg_str += "..."
            question_text = f"工具 `{tool_name}` 需要权限执行。\n\n参数摘要: {arg_str}\n\n点击“预览”可查看完整参数。"
            options = [
                {"label": "允许", "description": ""},
                {"label": "允许且该轮对话自动允许", "description": ""},
                {"label": "本次会话允许", "description": ""},
                {"label": "不允许", "description": ""},
            ]
            self._question_floating_widget.show_question(
                [{"question": question_text, "options": options, "multiple": False}],
                show_custom_input=False,
                preview_payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "arguments": arguments or {},
                },
            )
        except Exception as e:
            self._question_floating_widget.setUpdatesEnabled(True)
            logger.error(f"[Permission] Approval error: {e}")
            self.backend.deny_tool_permission(tool_call_id)
            self._pending_permission_tool_call_id = None
            self._restore_after_question_close()
            return
        self._question_floating_widget.setUpdatesEnabled(True)
        # 🛠️ 同 _on_question_asked 的 layout cache invalidate，防止第二次权限请求
        # 时 _do_expand 读到过期 sizeHint 导致卡片不显示
        _ql = self._question_floating_widget.layout()
        if _ql is not None:
            _ql.invalidate()
        self._question_floating_widget.updateGeometry()
        self._card_manager.show_card("question", self._window_id)

        # 🛡️ 同 _on_question_asked 的安全网：延迟重试展开容器（绕过拖拽检查）
        QTimer.singleShot(200, self._bottom_card_container._do_expand)

    def _maybe_generate_topic_summary(self):
        # 🛡️ 每次启动新的标题生成任务时重置取消标记
        self._topic_summary_cancelled = False

        # 检查是否有标题生成专用模型配置
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = cfg.llm_title_gen_default_model.value
        if saved:
            # 标题生成是后台自动流程：解析失败静默回退主模型，不弹 InfoBar 警告
            resolved = self._resolve_subagent_model_config(saved, show_error=False)
            if resolved:
                llm_config = resolved
            else:
                # 解析失败，回退到主模型
                selected_name = self._current_provider_name if self._current_provider_name else "系统默认配置"
                llm_config = self._valid_configs.get(selected_name)
        else:
            selected_name = self._current_provider_name if self._current_provider_name else "系统默认配置"
            llm_config = self._valid_configs.get(selected_name)

        if not llm_config:
            logger.warning("[Topic Summary] No LLM config found, skipping")
            return
        session = self.session_manager.get_current_session()
        if not session:
            logger.warning("[Topic Summary] No session found, skipping")
            return

        # 🛡️ 如果用户已手动编辑过标题，跳过自动生成
        if getattr(session, "user_edited_title", False):
            logger.info("[Topic Summary] User edited title, skipping auto generation")
            return

        # 🛡️ R6 修复：team 邮件（_hook_event="TeamMail"）视为真实用户问题
        # → mail-only 会话也能完成标题生成（之前 _hook_event 全过滤误伤）
        user_messages = [
            m
            for m in session.messages
            if m.get("role") == "user" and (not m.get("_hook_event") or m.get("_hook_event") == "TeamMail")
        ]
        if not user_messages:
            logger.warning("[Topic Summary] No user messages found, skipping")
            return
        previous_summary = ""
        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(self._current_session_id)
            if idx is not None:
                previous_summary = self.history_manager.get_topic_summary(idx)

        # 线程安全包装：QRunnable 在后台线程直接调用 callback，
        # 通过 pyqtSignal emit 桥接到主线程，避免 GUI 操作崩溃
        def _thread_safe_callback(result, error=None):
            self._topic_summary_ready.emit(result, error)

        task = TopicSummaryTask(
            messages=session.messages,
            llm_config=llm_config,
            callback=_thread_safe_callback,
            previous_summary=previous_summary if previous_summary else None,
            cancel_check=lambda: self._topic_summary_cancelled,
        )
        self._gen_thread_pool.start(task)

    def _on_topic_summary_generated(self, result, error: str = None):
        if error:
            logger.error(f"[Topic Summary] Failed to generate: {error}")
            return
        if not result:
            return

        # 🛡️ 如果用户已手动编辑过标题，跳过自动生成结果的更新
        session = self.session_manager.get_current_session()
        if session and getattr(session, "user_edited_title", False):
            logger.info("[Topic Summary] User edited title, skipping result update")
            return

        if isinstance(result, dict):
            summary = result.get("topic_summary", "")
        else:
            summary = result

        if not summary:
            return

        clean_summary = summary.strip()

        # 校验标题长度，超长说明解析异常或LLM输出异常，跳过更新
        MAX_TITLE_LENGTH = 50
        if len(clean_summary) > MAX_TITLE_LENGTH:
            logger.warning(f"[Topic Summary] 标题过长({len(clean_summary)}字)，跳过更新")
            return

        session = self.session_manager.get_current_session()

        # 先设置 session 的 topic_summary，避免 save_session 时 title 为空
        if session:
            session.set_topic_summary(clean_summary)

        if self._current_session_id is None and session and session.messages:
            worktree_path = self._get_current_worktree_path()
            worktree_kwargs = {"worktree_path": worktree_path or ""}
            # 🛡️ 团队元数据：标题生成时的首次落库同样携带团队字段，
            # 防御"标题生成先于首条消息保存"时团队会话元数据丢失。
            team_kwargs = {}
            if getattr(self, "_team_run_id", None):
                team_kwargs = {
                    "team_run_id": self._team_run_id,
                    "team_name": getattr(self, "_team_name", "") or "",
                    "agent_name": getattr(self, "_team_agent_name", "") or "",
                }
            # 🛡️ 新会话首存：使用 originating_project 优先的 fallback 链，
            # 避免"标题生成完成时用户已切到其他项目"导致会话错存。
            resolved_project = self._resolve_session_project_fallback(
                session.session_id, self._current_project, session=session
            )
            self.history_manager.save_session(
                session.messages if session else [],
                title=clean_summary,  # 使用生成的摘要作为标题
                session_id=session.session_id if session else None,
                compaction_state=getattr(session, "compaction_state", {}),
                compaction_cache=getattr(session, "compaction_cache", {}),
                project=resolved_project,
                **worktree_kwargs,
                **team_kwargs,
            )
            self._current_session_id = session.session_id if session else None

        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(self._current_session_id)
            if idx is not None:
                self.history_manager.update_topic_summary(idx, clean_summary)

        self.title_edit.setText(clean_summary)
        # 同步对话框窗口标题（便于 Windows 任务栏区分各窗口）
        self._sync_dialog_title()

    def _update_project_display(self, project: str):
        """更新项目名称显示"""
        self._current_project = project
        self.backend._current_project = project
        self._project_label.setText(project)
        self._refresh_project_branch_style()

    def _on_project_label_clicked(self, event):
        """项目标签点击 - 切换项目选择卡片"""
        event.accept()
        self._toggle_project_selector_card()

    def _refresh_branch_widget_style(self):
        """刷新分支按钮的文字样式"""
        Colors.refresh()
        self._branch_widget.setStyleSheet(f"""
            #_branchWidget {{
                background: transparent;
                border: none;
                color: {Colors.TEXT_SECONDARY};
                {get_font_family_css()}
                {font_size_css(12)};
                padding: 2px 6px 2px 2px;
            }}
            #_branchWidget:hover {{
                background: {Colors.HOVER_BG};
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
            }}
        """)

    def _refresh_project_branch_style(self):
        """刷新项目+分支组合控件的整体样式（面包屑风格）"""
        Colors.refresh()
        # 容器 — 面包屑整体底框
        self._project_branch_container.setStyleSheet(f"""
            QFrame#projectBranchContainer {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
            }}
            QFrame#projectBranchContainer:hover {{
                background: {Colors.HOVER_BG};
            }}
        """)
        # 项目标签 — 面包屑第一级（粗体 + 项目专属色）
        project_color = get_project_color(self._current_project)
        # 同步更新方形 avatar
        if hasattr(self, "_project_avatar"):
            self._project_avatar.set_project(self._current_project, project_color)
        self._project_label.setStyleSheet(f"""
            QLabel {{
                color: {project_color};
                {get_font_family_css()}
                {font_size_css(13)}
                font-weight: bold;
                padding: 0px 2px 0px 2px;
                background: transparent;
                border: none;
                border-radius: 4px;
            }}
        """)
        # 分隔符 — 三角箭头（小号 + 次级色）
        self._pb_separator.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_MUTED};
                {get_font_family_css()}
                {font_size_css(16)}
                background: transparent;
                border: none;
                padding: 2px;
            }}
        """)
        # 同步刷新分支按钮样式
        self._refresh_branch_widget_style()

    def _copy_branch_from(self, source):
        """性能优化：从源窗口复制 git 分支标签状态，跳过同步 git 子进程调用。

        复制/分支窗口与源窗口共享完全相同的项目与工作目录，git 分支必然一致，
        无需再次执行 `git branch --show-current`（最坏可达 3s 阻塞主线程）。
        直接复制源窗口已渲染的分支标签 UI 状态（文本/可见性/提示/项目 avatar 提示）即可。
        """
        from PyQt5 import sip

        try:
            if sip.isdeleted(source) or not hasattr(source, "_branch_widget"):
                self._update_branch()
                return
            if sip.isdeleted(source._branch_widget):
                self._update_branch()
                return
            branch_visible = source._branch_widget.isVisible()
            self._branch_widget.setText(source._branch_widget.text())
            self._branch_widget.setVisible(branch_visible)
            self._branch_widget.setToolTip(source._branch_widget.toolTip())
            if hasattr(self, "_pb_separator"):
                self._pb_separator.setVisible(branch_visible)
            # 同步项目 avatar tooltip（含完整项目名、路径、分支）
            if hasattr(self, "_project_avatar") and hasattr(source, "_project_avatar"):
                self._project_avatar.setToolTip(source._project_avatar.toolTip())
        except Exception:
            # 兜底：复制失败则回退到正常的 git 检测
            self._update_branch()

    def _resolve_project_workdir(self) -> Optional[str]:
        """解析当前项目的工作目录（多窗口隔离：实例缓存 → DB → tool_executor）"""
        workdir = self._current_workdir.get(self._current_project)
        if not workdir and self.backend and self.backend.memory_manager:
            workdir = self.backend.memory_manager.get_working_directory(self._current_project)
        if not workdir and self.backend and self.backend.tool_executor:
            workdir = getattr(self.backend.tool_executor, "_workdir", None)
        return str(workdir) if workdir else None

    def _update_branch(self):
        """更新项目 avatar tooltip 和分支标签（性能优化：异步 git 检测）

        旧实现：在主线程同步执行 `git branch --show-current`（timeout=3），
        tooltip 在子进程后才更新，导致项目切换/工作目录变更时 tooltip 延迟 0~3s。
        改为：tooltip 立即显示「项目名+路径」，git 分支后台线程异步检测，
        完成后通过 _on_branch_detected 回调追加分支信息。
        """
        workdir = self._resolve_project_workdir()

        # Phase A（同步、即时）：tooltip 先显示项目名 + 工作目录
        tooltip = self._current_project
        if workdir:
            tooltip += f"\n{workdir}"
        self._project_avatar.setToolTip(tooltip)

        # 先隐藏分支标签，等后台检测完成再决定显示
        self._branch_widget.setVisible(False)
        self._pb_separator.setVisible(False)

        # Phase B（异步）：git 分支检测
        if not workdir or not os.path.isdir(workdir):
            return

        # 缓存命中：直接应用，避免重复 git 调用
        if workdir in self._branch_cache:
            self._apply_branch_to_ui(workdir, self._branch_cache[workdir])
            return

        # 启动后台检测（自增 request_id 用于丢弃过期结果）
        self._branch_detect_request_id += 1
        task = _BranchDetectTask(workdir, self._branch_detect_request_id, self._branch_detect_signals)
        QThreadPool.globalInstance().start(task)

    def _on_branch_detected(self, request_id: int, branch: str):
        """后台 git 分支检测完成的主线程回调。"""
        # 过期结果：用户已切换到其他项目，丢弃
        if request_id != self._branch_detect_request_id:
            return

        workdir = self._resolve_project_workdir()
        if not workdir:
            return

        # 缓存结果（同一路径下次直接命中）
        self._branch_cache[workdir] = branch
        # 再次校验：缓存写完后若 request_id 又变了，说明并发切换，仍跳过
        if request_id != self._branch_detect_request_id:
            return
        self._apply_branch_to_ui(workdir, branch)

    def _apply_branch_to_ui(self, workdir: str, branch: str):
        """应用分支结果到 tooltip 和分支标签。"""
        # 完整 tooltip（项目名 + 路径 + 分支）
        tooltip = self._current_project
        if workdir:
            tooltip += f"\n{workdir}"
        if branch:
            tooltip += f"\n🌿 {branch}"
        self._project_avatar.setToolTip(tooltip)

        # 分支标签
        if branch:
            display = branch if len(branch) <= 20 else branch[:8] + "…" + branch[-8:]
            self._branch_widget.setText(display)
            self._branch_widget.setToolTip(f"分支: {branch}\n点击打开关键文档")
            self._branch_widget.setVisible(True)
            self._pb_separator.setVisible(True)
        else:
            self._branch_widget.setVisible(False)
            self._pb_separator.setVisible(False)

    def _on_branch_label_clicked(self, event):
        """分支标签点击 — 打开关键文档卡片"""
        self._toggle_memory_card()
        # 确保切换到关键文档 Tab
        if hasattr(self, "_memory_card_popup") and self._memory_card_popup:
            from app.widgets.cards.settings.memory_card import TAB_KEY_DOCUMENTS

            self._memory_card_popup.switch_tab(TAB_KEY_DOCUMENTS)
            # 同步卡片 Tab 按钮
            if hasattr(self, "_memory_card") and self._memory_card:
                self._memory_card.set_current_tab(TAB_KEY_DOCUMENTS)

    def _toggle_project_selector_card(self):
        """切换项目选择卡片的显示"""
        self._card_manager.toggle_card("project_selector", self._window_id)
        if self._card_manager.is_card_visible("project_selector", self._window_id):
            # 加载项目数据
            projects = self.history_manager.get_projects() if self.history_manager else ["默认项目"]
            # 确保当前项目在列表中（新建项目可能还没有会话/文档记录）
            if self._current_project not in projects:
                projects.insert(0, self._current_project)

            # 获取每个项目的会话数和 worktree 数
            meta_map = self._build_project_meta_map(projects)
            # 获取每个项目的根目录（用于卡片显示）
            root_dir_map = self._build_project_root_dir_map(projects)

            self._project_selector_card_content.set_projects_data(
                projects, self._current_project, meta_map, root_dir_map
            )
            # 更新卡片标题 — 固定显示"项目切换"，不显示当前项目名
            self._project_selector_card.set_title_text("📁 项目切换")
            # 清空过滤输入框
            self._project_new_edit.clear()

    def _build_project_meta_map(self, projects: List[str]) -> Dict[str, Dict[str, int]]:
        """构建项目元数据映射 {项目名: {"sessions": N, "worktrees": N}}

        会话数使用 session_store.get_session_counts()（COUNT DISTINCT session_id 去重），
        工作目录数使用 memory_manager.get_worktree_counts()。
        """
        meta_map: Dict[str, Dict[str, int]] = {p: {"sessions": 0, "worktrees": 0} for p in projects}
        try:
            # 会话数（SQL COUNT DISTINCT session_id 去重统计）
            if self.backend and self.backend.session_store:
                session_counts = self.backend.session_store.get_session_counts()
                for p, c in session_counts.items():
                    if p in meta_map:
                        meta_map[p]["sessions"] = c
            # 工作目录数
            if self.backend and self.backend.memory_manager:
                worktree_counts = self.backend.memory_manager.get_worktree_counts()
                for p, c in worktree_counts.items():
                    if p in meta_map:
                        meta_map[p]["worktrees"] = c
        except Exception as e:
            logger.warning(f"[MainWidget] 获取项目元数据异常: {e}")
        return meta_map

    def _build_project_root_dir_map(self, projects: List[str]) -> Dict[str, str]:
        """构建项目根目录映射 {项目名: 根目录路径}（未设置的项目不会出现）

        根目录来源于关键文档中标记为"工作目录"的文件夹（每个项目最多 1 个）。
        若 DB 中无根目录但实例缓存有临时工作目录，也一并返回。
        """
        root_dir_map: Dict[str, str] = {}
        try:
            if self.backend and self.backend.memory_manager:
                for p in projects:
                    wd = self.backend.memory_manager.get_working_directory(p)
                    if wd:
                        root_dir_map[p] = wd
            # 补充实例缓存中的临时工作目录（未持久化到 DB）
            if hasattr(self, "_current_workdir") and self._current_workdir:
                for p, wd in self._current_workdir.items():
                    if wd and p not in root_dir_map:
                        root_dir_map[p] = wd
        except Exception as e:
            logger.warning(f"[MainWidget] 获取项目根目录异常: {e}")
        return root_dir_map

    def _apply_team_project(self, project: str):
        """团队级项目同步接收方：应用同团队其他成员广播的项目切换

        复用 _on_project_selected 的公共段（_current_project / backend /
        tool_executor / _project_label / 分支样式 / 记忆卡片 / 历史面板 /
        Tab 图标），但跳过：
        - _create_new_session()（避免连环新建会话）
        - cfg.current_project 全局写入（全局默认项目仅由发送方写）
        - hide_card("project_selector")（关闭项目卡片仅针对发送方）
        """
        if getattr(self, "_is_destroyed", False):
            return
        if self._current_project == project:
            return
        self._current_project = project
        if self.backend:
            self.backend._current_project = project
            if self.backend.tool_executor:
                self.backend.tool_executor.set_current_project(project)
        if getattr(self, "_project_label", None):
            self._project_label.setText(project)
        self._refresh_project_branch_style()
        self._update_branch()
        # 失效欢迎卡片缓存（recent_sessions/top_by_count 按 _current_project 过滤）
        self._invalidate_welcome_card()
        # 刷新记忆卡片（与 _on_project_selected 一致的 workdir 预计算）
        workdir = self._current_workdir.get(project)
        if workdir is None and self.backend and self.backend.memory_manager:
            workdir = self.backend.memory_manager.get_working_directory(project)
            if workdir:
                self._current_workdir[project] = workdir
        if getattr(self, "_memory_card_popup", None):
            self._memory_card_popup.set_project(project, workdir=workdir)
        # 🐛 修复：tool_executor 工作目录同步不能挂在记忆卡片 UI 的惰性构建状态上。
        # PreUserMessage hook（format_memory_context 的 githook 项目根目录/Git 状态）
        # 数据源是 tool_executor.get_workdir()，卡片未构建时跳过同步会导致切换项目后
        # hook 仍注入旧项目的根目录与 Git 状态（残留）。
        self._sync_working_directory()
        # 刷新历史面板（切换项目过滤）
        self._current_history_project = project
        if getattr(self, "_history_popup_card", None):
            self._history_popup_card.set_current_project(project)
            self._refresh_history_toggle_panel()
        # Tab 模式下同步更新 Tab 图标
        if self.cfg.enable_tab_manager.value:
            try:
                from app.widgets.tab_manager_window import TabManagerWindow, _update_tab_icon

                tm = TabManagerWindow.get_instance()
                if tm and self in tm._windows:
                    idx = tm._windows.index(self)
                    _update_tab_icon(idx, project)
            except Exception:
                pass

    def _broadcast_team_project(self, project: str, prev_project: str = None):
        """团队内项目切换广播：写团队级 project + 同团队其他成员同步应用

        防循环（风险点 1）：仅遍历其他窗口（不含发送方），且接收方
        _apply_team_project 内部有「值相等直接跳过」防御。
        按团队过滤（风险点 3）：仅 _team_agent_name 非空、is_team_member、
        且同 _team_run_id/_team_name（同一次团队运行）的窗口才应用，
        不同团队互不影响，非团队成员不受影响。
        P2-B（Bug A）：接收方当前项目必须与发送方切换前项目一致才应用广播，
        防止 A 项目团队切项目误广播到 B 项目窗口。
        """
        if not getattr(self, "_team_agent_name", ""):
            return
        from app.core.team_manager import TeamManager

        tm_mgr = TeamManager.get_instance()
        # 发送方切换前项目兜底：未显式传入时用当前 _current_project
        if prev_project is None:
            prev_project = getattr(self, "_current_project", "")
        # 写团队级统一项目（team.json 顶层，与 run_id 平级）
        tm_mgr.set_team_project(project)
        # 本窗口团队 key：run_id 优先（同一次 /team --load 共享），回退团队名
        my_key = getattr(self, "_team_run_id", "") or getattr(self, "_team_name", "") or TeamManager.DEFAULT_TEAM
        for win in type(self)._instances:
            if win is self or getattr(win, "_is_destroyed", False):
                continue
            if not getattr(win, "_team_agent_name", ""):
                continue
            try:
                if not tm_mgr.is_team_member(win._window_id):
                    continue
            except Exception:
                continue
            win_key = getattr(win, "_team_run_id", "") or getattr(win, "_team_name", "") or TeamManager.DEFAULT_TEAM
            if win_key != my_key:
                continue
            # P2-B：仅当接收方当前项目与发送方切换前项目一致时才应用广播，
            # 防止 A 项目团队切项目误广播到 B 项目窗口（Bug A）
            if getattr(win, "_current_project", "") != prev_project:
                continue
            try:
                win._apply_team_project(project)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[TeamProject] 同步项目到窗口失败: {e}")

    def _apply_team_workdir(self, workdir: str):
        """团队级统一工作目录/工作树接收方：应用同团队其他成员广播的 workdir 切换

        与 _apply_team_project 同语义：只同步「实例缓存 + tool_executor +
        记忆卡片实例缓存 + 分支标签」，不写 DB（DB 由发送方负责）、不触发
        新广播（防循环）、值相等直接跳过（幂等）。
        workdir 为空 = 发送方清除了工作目录，本地回退临时工作目录兜底。
        """
        if getattr(self, "_is_destroyed", False):
            return
        project = self._current_project
        if workdir:
            if self._current_workdir.get(project) == workdir:
                return
            self._current_workdir[project] = workdir
            resolved = workdir
        else:
            self._current_workdir.pop(project, None)
            resolved = self._ensure_temp_workdir(project) or None
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_workdir(resolved or None)
        if hasattr(self, "_memory_card_popup") and self._memory_card_popup:
            self._memory_card_popup._instance_workdir[project] = resolved or ""
        self._update_branch()
        logger.info(f"[TeamWorkdir] 窗口 {self._window_id} 已应用团队工作目录: {resolved or 'cleared'}")

    def _broadcast_team_workdir(self, workdir: str):
        """团队内工作目录/工作树切换广播：写团队级 workdir + 同团队其他成员同步应用

        触发点：_on_working_dir_changed / _switch_to_worktree / _restore_main_repo，
        任一成员切换工作目录或 git worktree 时全员同步（统一工作树）。
        防循环：仅遍历其他窗口（不含发送方），接收方 _apply_team_workdir 不广播。
        按团队过滤：仅 _team_agent_name 非空、is_team_member、且同
        _team_run_id/_team_name（同一次团队运行）的窗口才应用。
        """
        if not getattr(self, "_team_agent_name", ""):
            return
        from app.core.team_manager import TeamManager

        tm_mgr = TeamManager.get_instance()
        # 写团队级统一工作目录/工作树（team.json 顶层，与 project/run_id 平级）
        tm_mgr.set_team_workdir(workdir or "")
        # 本窗口团队 key：run_id 优先（同一次 /team --load 共享），回退团队名
        my_key = getattr(self, "_team_run_id", "") or getattr(self, "_team_name", "") or TeamManager.DEFAULT_TEAM
        for win in type(self)._instances:
            if win is self or getattr(win, "_is_destroyed", False):
                continue
            if not getattr(win, "_team_agent_name", ""):
                continue
            try:
                if not tm_mgr.is_team_member(win._window_id):
                    continue
            except Exception:
                continue
            win_key = getattr(win, "_team_run_id", "") or getattr(win, "_team_name", "") or TeamManager.DEFAULT_TEAM
            if win_key != my_key:
                continue
            try:
                win._apply_team_workdir(workdir)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[TeamWorkdir] 同步工作目录到窗口失败: {e}")

    def _on_project_selected(self, project: str):
        """切换到选中的项目"""
        # P2-B：捕获切换前项目，供团队广播校验接收方一致性
        prev_project = self._current_project
        self._current_project = project
        self.backend._current_project = project
        self._project_label.setText(project)
        self._refresh_project_branch_style()
        self._update_branch()
        self.cfg.current_project.value = project
        self.cfg.save()
        # 🛡️ 失效欢迎卡片缓存：recent_sessions/top_by_count 按 _current_project 过滤，
        # 项目切换后必须重建。虽然下方 _create_new_session 内部也会失效一次，
        # 这里提前失效可在 _create_new_session 早期失败/跳过时仍有兜底。
        self._invalidate_welcome_card()
        # 更新 tool_executor 的当前项目
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_current_project(project)
        # 性能优化：先算 workdir（实例缓存 / DB 单次读取），再下发给 memory_card_popup，
        # 避免 set_project 内部又走一遍 _get_effective_workdir。
        workdir = self._current_workdir.get(project)
        if workdir is None and self.backend and self.backend.memory_manager:
            workdir = self.backend.memory_manager.get_working_directory(project)
            if workdir:
                self._current_workdir[project] = workdir
        # 刷新记忆卡片的项目（项目笔记、关键文档会跟着刷新）
        if hasattr(self, "_memory_card_popup") and self._memory_card_popup:
            from loguru import logger

            logger.info(f"[MainWidget] Calling set_project({project}) on memory_card_popup")
            self._memory_card_popup.set_project(project, workdir=workdir)
        # 🐛 修复：workdir 同步移出 _memory_card_popup 惰性判断——卡片未构建时
        # 也必须更新 tool_executor，否则 PreUserMessage hook 的 project_root
        # 残留旧项目（githook 显示旧项目根目录 + Git 状态）。
        # _sync_working_directory 内部已对 _memory_card_popup 判空，可安全无条件调用。
        self._sync_working_directory()
        # 刷新历史面板（切换项目过滤）
        self._current_history_project = project
        self._history_popup_card.set_current_project(project)
        self._refresh_history_toggle_panel()
        # 自动触发新建会话，避免原会话与切换后的项目不匹配
        self._create_new_session()
        # 隐藏项目选择卡片
        self._card_manager.hide_card("project_selector", self._window_id)

        # 团队模式：一人改项目全员同步（写团队 project + 广播同团队其他窗口）。
        # ★ 必须在 Tab 图标更新之前执行：广播先写入团队级 project，发送方自身的
        # _update_tab_icon 团队分支才能读到新值（否则 header 显示旧团队项目——
        # review #11 问题 1：发送方 header 图标滞后）
        self._broadcast_team_project(project, prev_project)

        # Tab 模式下同步更新 Tab 图标
        if self.cfg.enable_tab_manager.value:
            try:
                from app.widgets.tab_manager_window import TabManagerWindow, _update_tab_icon

                tm = TabManagerWindow.get_instance()
                if tm and self in tm._windows:
                    idx = tm._windows.index(self)
                    _update_tab_icon(idx, project)
            except Exception:
                pass

    def _on_project_filter_changed(self, text: str):
        """输入过滤文本变化时同步过滤项目列表"""
        if hasattr(self, "_project_selector_card_content"):
            self._project_selector_card_content.set_filter(text)

    def _on_header_new_project(self):
        """从标题栏新建项目按钮/回车触发

        行为：
        1. 如果输入内容完全匹配某个已有项目 → 切换到该项目
        2. 如果输入内容为空 → 不做任何操作
        3. 否则 → 创建新项目
        """
        name = self._project_new_edit.text().strip()
        if not name:
            return

        # 检查是否完全匹配某个已有项目
        if hasattr(self, "_project_selector_card_content"):
            matching = [p for p in self._project_selector_card_content._projects if p.lower() == name.lower()]
            if matching:
                # 匹配到已有项目 → 直接切换
                self._project_new_edit.clear()
                self._project_selector_card_content.set_filter("")
                self._on_project_selected(matching[0])
                return

        # 无匹配 → 创建新项目
        self._project_new_edit.clear()
        self._project_selector_card_content.set_filter("")
        self._on_new_project_created(name)

    def _on_new_project_created(self, project: str, suppress_memory_card: bool = False, root_dir: str = ""):
        """新建项目后

        Args:
            suppress_memory_card: 为 True 时不自动弹出关键文档卡片
                                  （拖拽/选择文件夹设了根目录时使用）
            root_dir: 指定的项目根目录（拖拽/选择文件夹建项目时传入）。
                      传入时直接绑定该目录为工作目录，不再创建默认项目文件夹
                      （~/.drifox/workspaces/<project>/），AGENTS.md 等文件
                      写入指定路径而非默认路径。
        """
        # P2-B：捕获切换前项目，供团队广播校验接收方一致性
        prev_project = self._current_project
        self._current_project = project
        self.backend._current_project = project
        self._project_label.setText(project)
        self._refresh_project_branch_style()
        self._update_branch()
        # 同步到 tool_executor，确保 stage_files 等工具写入正确的项目
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_current_project(project)
        # 保存到配置
        self.cfg.current_project.value = project
        self.cfg.save()
        # 刷新记忆卡片的项目
        if hasattr(self, "_memory_card_popup") and self._memory_card_popup:
            self._memory_card_popup.set_project(project)
        # 🐛 修复：切换项目时无条件同步工作目录（不依赖记忆卡片惰性构建状态），
        # 否则 tool_executor.get_workdir() 残留旧项目 → PreUserMessage hook 的
        # githook 显示旧项目根目录。
        # 指定了项目根目录：先预置实例缓存 + 写 DB，_sync_working_directory 直接
        # 使用该路径，不再走 _ensure_temp_workdir 创建默认项目文件夹；
        # 同时确保团队广播时其他窗口从 DB 也能读到正确路径（不会回退默认目录）。
        if root_dir:
            self._current_workdir[project] = root_dir
            if self.backend and self.backend.memory_manager:
                self.backend.memory_manager.add_key_document(project, root_dir, added_by="manual")
                self.backend.memory_manager.set_working_directory(project, root_dir)
        self._sync_working_directory()
        # 刷新历史面板
        self._history_popup_card.refreshRequested.emit()
        # 自动弹出长期记忆卡片（已设根目录时跳过，避免干扰已绑定的文件夹）
        if not suppress_memory_card:
            # P0-1：卡片懒创建，未创建视为不可见 → toggle 内部会 ensure
            if not getattr(self, "_memory_card", None) or not self._memory_card.isVisible():
                self._toggle_memory_card()
            # 卡片弹出后，再切换到关键文档标签
            if hasattr(self, "_memory_card") and self._memory_card:
                self._memory_card.set_current_tab(TAB_KEY_DOCUMENTS)
        # 自动触发新建会话
        self._create_new_session()
        # 隐藏项目选择卡片
        self._card_manager.hide_card("project_selector", self._window_id)

        # 团队模式：一人改项目全员同步（新建项目也是团队级项目切换）
        self._broadcast_team_project(project, prev_project)

        # Tab 模式下同步更新 Tab 图标（与 _on_project_selected 对齐：
        # 新建项目后必须显式刷新，否则依赖 windowTitleChanged 间接触发，
        # 标题未变化时图标停留在旧项目）
        if self.cfg.enable_tab_manager.value:
            try:
                from app.widgets.tab_manager_window import TabManagerWindow, _update_tab_icon

                tm = TabManagerWindow.get_instance()
                if tm and self in tm._windows:
                    idx = tm._windows.index(self)
                    _update_tab_icon(idx, project)
            except Exception:
                pass

    def _on_archive_project(self, project_name: str):
        """归档项目处理"""
        ## 触发警示动画
        if self.pixel_pet:
            self.pixel_pet.set_state("warning")
        if not self.history_manager:
            if self.pixel_pet:
                self.pixel_pet.set_state("idle")
            return

        # 后端执行归档（可能返回0个会话，但项目本身仍应被清理）
        count = self.history_manager.archive_project(project_name)

        # 强制清理该项目在 SQLite 三张表中的所有数据（绕过 repo 层，直接 SQL 删除）
        # 避免 key_documents/project_notes 残留数据导致 UNION 查询让项目"复活"
        if self.backend and self.backend.session_store:
            self.backend.session_store.force_cleanup_project(project_name)
        else:
            logger.warning(f"[Archive] session_store 不可用，无法强制清理 {project_name} 的关联数据")

        # 如果归档的是当前项目，切换到默认项目
        if project_name == self._current_project:
            default_project = "默认项目"
            self._current_project = default_project
            self.backend._current_project = default_project
            self._project_label.setText(default_project)
            self._refresh_project_branch_style()
            self._update_branch()
            self.cfg.current_project.value = default_project
            self.cfg.save()
            # 同步到 tool_executor，确保 stage_files 等工具写入正确的项目
            if self.backend and self.backend.tool_executor:
                self.backend.tool_executor.set_current_project(default_project)
            self._create_new_session()
            # 团队模式：归档当前项目切回默认项目，同样触发团队级同步
            # P2-B：prev_project = project_name（归档前项目），此时
            # _current_project 已切到默认项目，不能靠函数内兜底取值。
            self._broadcast_team_project(default_project, project_name)
        else:
            self._current_history_project = self._current_project
            self._refresh_history_toggle_panel()

        # 刷新历史面板
        self._history_popup_card.refreshRequested.emit()

        if count > 0:
            InfoBar.success(
                "归档成功",
                f"已归档项目「{project_name}」的 {count} 个会话",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
        else:
            InfoBar.success(
                "归档成功",
                f"已移除项目「{project_name}」（无会话）",
                parent=TabManagerWindow.get_instance() or self.window(),
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )

        # 刷新项目选择卡片的列表
        if hasattr(self, "_project_selector_card_content"):
            projects = self.history_manager.get_projects() if self.history_manager else ["默认项目"]
            # 确保刚归档的项目不在列表中（兜底，防止残留数据导致复活）
            if project_name in projects:
                projects.remove(project_name)
            if self._current_project not in projects:
                projects.insert(0, self._current_project)
            meta_map = self._build_project_meta_map(projects)
            root_dir_map = self._build_project_root_dir_map(projects)
            self._project_selector_card_content.set_projects_data(
                projects, self._current_project, meta_map, root_dir_map
            )

        # 操作完成，恢复正常状态
        if self.pixel_pet:
            self.pixel_pet.set_state("idle")

    def _on_export_project(self, project_name: str):
        """导出项目为 .drifox_project 压缩包 — 先选方式再导出（后台线程避免 UI 冻结）"""
        if not self.history_manager:
            InfoBar.warning(
                title="",
                content="历史管理器不可用",
                duration=2000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            return

        # 防止重复导出 — 注意 C++ 对象可能已被 deleteLater 销毁
        if hasattr(self, "_export_thread"):
            try:
                if self._export_thread is not None and self._export_thread.isRunning():
                    InfoBar.warning(
                        title="",
                        content="导出进行中，请稍候…",
                        duration=2000,
                        parent=TabManagerWindow.get_instance() or self.window(),
                    )
                    return
            except RuntimeError:
                # C++ 对象已被 deleteLater 销毁，清理 Python 引用后继续
                self._export_thread = None

        # 先弹出选择对话框，再根据选择执行导出
        dialog = _ProjectExportChoiceDialog(project_name, parent=self.window())

        def _on_choice(mode: int):
            # 获取项目根目录
            root_dir = ""
            if self.backend and self.backend.session_store:
                root_dir_map = self._build_project_root_dir_map([project_name])
                root_dir = root_dir_map.get(project_name, "")

            if self.pixel_pet:
                self.pixel_pet.set_state("warning")

            # 后台线程导出 ZIP，避免大项目阻塞 UI
            self._export_thread = _ProjectExportThread(self.history_manager, project_name, root_dir)
            self._export_thread.exportDone.connect(
                lambda zp, err, m=mode, pn=project_name: self._on_project_export_done(zp, err, pn, m)
            )
            self._export_thread.finished.connect(self._export_thread.deleteLater)
            self._export_thread.finished.connect(lambda: setattr(self, "_export_thread", None))
            self._export_thread.start()

        dialog.exportChosen.connect(_on_choice)
        dialog.exec_()

    def _on_project_export_done(self, zip_path: str, error: str, project_name: str, mode: int):
        """后台导出完成回调（主线程）"""
        if self.pixel_pet:
            self.pixel_pet.set_state("idle")

        if error:
            InfoBar.error(
                title="导出失败",
                content=error,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            return

        if mode == _ProjectExportChoiceDialog.EXPORT_LOCAL:
            self._on_export_local(zip_path, project_name)
        elif mode == _ProjectExportChoiceDialog.EXPORT_UPLOAD:
            self._on_export_upload(zip_path, project_name)

    def _insert_project_share_record(self, project_name: str, zip_path: str, upload_url: str = ""):
        """插入项目导出分享记录"""
        session_count = 0
        if self.history_manager:
            try:
                sessions = self.history_manager.get_history_list(project_name, with_messages=False)
                session_count = len(sessions) if sessions else 0
            except Exception:
                pass
        from app.utils.share_records import insert_record

        insert_record(
            type_="project",
            title=project_name,
            format_="drifox_project",
            file_path=str(Path(zip_path)) if zip_path else "",
            upload_url=upload_url,
            ref_id=project_name,
            extra_info={"session_count": session_count},
        )

    def _on_export_local(self, zip_path: str, project_name: str):
        """导出到本地：保存到默认路径，自动打开文件夹"""
        try:
            path = Path(zip_path)
            InfoBar.success(
                title="",
                content=f"项目「{project_name}」已导出到: {path}",
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            # ── 写入分享记录 ──
            self._insert_project_share_record(project_name, zip_path)
            # 自动打开文件夹并选中文件
            try:
                if os.name == "nt":
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(str(path))])
                else:
                    folder = str(path.parent)
                    subprocess.Popen(["xdg-open", folder])
            except Exception as e:
                logger.warning(f"[MainWidget] 打开导出路径失败: {e}")
        except Exception as e:
            InfoBar.error(
                title="",
                content=f"导出本地失败: {e}",
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )

    def _on_export_upload(self, zip_path: str, project_name: str):
        """导出并上传到 Gitee，复制分享链接（异步，不卡 UI）"""
        # 先检查 Gitee 是否配置（快速检查，不涉及网络请求）
        from app.gateway.utils.gitee_uploader import GiteeUploader

        uploader = GiteeUploader.get_instance()
        if not uploader.is_configured():
            InfoBar.warning(
                title="",
                content="Gitee 未配置（缺少 token/owner/repo），文件已保存到本地",
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            self._on_export_local(zip_path, project_name)
            return

        # 启动后台上传线程
        self._upload_thread = _ProjectUploadThread(zip_path)
        self._upload_thread.finished.connect(
            lambda url, err: self._on_upload_finished(url, err, zip_path, project_name)
        )
        self._upload_thread.finished.connect(self._upload_thread.deleteLater)
        self._upload_thread.start()

        InfoBar.info(
            title="",
            content="正在上传项目压缩包...（异步上传，不阻塞界面）",
            duration=8000,
            parent=TabManagerWindow.get_instance() or self.window(),
        )

    def _on_upload_finished(self, url: str, err: str, zip_path: str, project_name: str):
        """上传线程完成后的回调（主线程执行）"""
        if err:
            InfoBar.warning(
                title="",
                content=f"上传失败: {err}（文件已保存到本地）",
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            self._on_export_local(zip_path, project_name)
            return

        # 复制链接到剪贴板
        from PyQt5.QtWidgets import QApplication

        QApplication.clipboard().setText(url)
        InfoBar.success(
            title="",
            content=f"✅ 上传成功！链接已复制到剪贴板\n{url}",
            duration=5000,
            parent=TabManagerWindow.get_instance() or self.window(),
        )
        # ── 写入分享记录（含上传链接） ──
        self._insert_project_share_record(project_name, zip_path, url)

    def _on_import_project(self):
        """从 .drifox_project 压缩包导入项目 — 与 ImportOptionDialog 一致风格"""
        if not self.history_manager:
            InfoBar.warning(
                title="",
                content="历史管理器不可用",
                duration=2000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            return

        dialog = _ProjectImportOptionDialog(parent=self.window())
        dialog.fileImportRequested.connect(self._on_import_project_from_file)
        dialog.urlImportRequested.connect(self._on_import_project_from_url)
        dialog.exec_()

    def _on_import_project_from_file(self):
        """从文件导入项目压缩包"""
        from PyQt5.QtWidgets import QFileDialog

        files, _ = QFileDialog.getOpenFileNames(
            self,
            "导入项目",
            "",
            "DriFox 项目包 (*.drifox_project);;ZIP 文件 (*.zip);;所有文件 (*)",
        )
        if not files:
            return

        success_count = 0
        for file_path in files:
            if self._do_import_project_archive(file_path):
                success_count += 1

        if success_count > 0:
            # 刷新项目列表和历史面板
            self._refresh_project_selector()
            self._refresh_history_toggle_panel()
            InfoBar.success(
                title="",
                content=f"成功导入 {success_count} 个项目",
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )

    def _on_import_project_from_url(self):
        """从URL导入项目压缩包"""
        from app.widgets.cards.settings.memory_card import SingleInputDialog

        dialog = SingleInputDialog(
            title="🔗 从URL导入项目",
            hint="请输入 .drifox_project 项目压缩包的分享链接",
            placeholder="https://gitee.com/.../xxx.drifox_project",
            default_text="https://",
            confirm_text="导入",
            cancel_text="取消",
            # parent 取顶层主窗口（Tab 模式下为 TabManagerWindow），而非 self 这个
            # 嵌入 QStackedWidget 的子 widget——MaskDialogBase 用 parent 尺寸铺遮罩，
            # 传子 widget 会导致遮罩只覆盖聊天区、弹窗层级/定位异常。
            parent=self.window(),
        )
        dialog.confirmed.connect(self._on_url_project_import_confirmed)
        dialog.exec_()

    def _on_url_project_import_confirmed(self, url: str):
        """URL确认后的项目导入处理（后台线程下载）"""
        url = url.strip()
        if not url:
            return

        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url

        # 启动后台下载线程
        self._url_import_thread = _ProjectUrlImportThread(url)
        self._url_import_thread.finished.connect(self._on_project_url_import_result)
        self._url_import_thread.finished.connect(self._url_import_thread.deleteLater)
        self._url_import_thread.start()

        InfoBar.info(
            title="",
            content="正在下载项目压缩包...",
            duration=5000,
            parent=TabManagerWindow.get_instance() or self.window(),
        )

    @pyqtSlot(str, str)
    def _on_project_url_import_result(self, file_path: str, error: str):
        """后台下载完成后的回调（主线程执行）"""
        if error:
            InfoBar.error(
                title="",
                content=f"下载失败: {error}",
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            return

        if self._do_import_project_archive(file_path):
            self._refresh_project_selector()
            self._refresh_history_toggle_panel()

    def _do_import_project_archive(self, zip_path: str) -> bool:
        """执行项目压缩包导入，含项目文件的自动恢复"""
        try:
            result = self.history_manager.import_project_archive(zip_path)
            if result and result.get("session_count", 0) > 0:
                project_name = (result.get("project_name") or "导入项目")[:40]
                InfoBar.success(
                    title="",
                    content=f"项目「{project_name}」导入成功（{result['session_count']} 个会话）",
                    duration=3000,
                    parent=TabManagerWindow.get_instance() or self.window(),
                )

                # ── 项目文件恢复 ──
                extract_dir = result.get("extract_dir")
                original_root = result.get("original_root")

                if extract_dir:
                    try:
                        extract_path = Path(extract_dir)
                        if not extract_path.is_dir():
                            logger.warning(f"[MainWidget] extract_dir 不存在或不是目录: {extract_dir}")
                        else:
                            # 确定恢复目标路径
                            restore_path = self._resolve_restore_path(project_name, original_root)
                            if restore_path:
                                os.makedirs(restore_path, exist_ok=True)
                                # 提取到的文件列表
                                items = list(extract_path.iterdir())
                                if items:
                                    logger.info(f"[MainWidget] 开始恢复 {len(items)} 个项目文件到 {restore_path}")
                                    for item in items:
                                        dst = Path(restore_path) / item.name
                                        if item.is_dir():
                                            if dst.exists():
                                                logger.warning(f"[MainWidget] 目标目录已存在将被覆盖: {dst}")
                                                shutil.rmtree(str(dst))
                                            shutil.copytree(str(item), str(dst))
                                        elif item.is_file():
                                            shutil.copy2(str(item), str(dst))
                                    # ── 更新项目的根目录 ──
                                    # 仅当恢复路径就是原始 git 仓库路径时才更新：
                                    # 这样下次导出时 git 检测仍能正常工作。
                                    # 如果恢复到了默认路径（非 git 仓库），不更新，
                                    # 保持项目原有根目录不变，避免下次导出丢失 git 信息。
                                    if original_root and os.path.abspath(restore_path) == os.path.abspath(
                                        original_root
                                    ):
                                        self._update_project_root_dir(project_name, restore_path)
                                        logger.info("[MainWidget] 恢复路径为原始 git 仓库，已更新项目根目录")
                                    else:
                                        logger.info(
                                            "[MainWidget] 恢复路径为默认路径（非 git 仓库），跳过更新项目根目录"
                                        )

                                    InfoBar.success(
                                        title="",
                                        content=f"项目文件已恢复到: {restore_path}",
                                        duration=5000,
                                        parent=TabManagerWindow.get_instance() or self.window(),
                                    )
                                else:
                                    logger.info(f"[MainWidget] extract_dir 为空，无文件可恢复: {extract_dir}")
                    except Exception as e:
                        logger.error(f"[MainWidget] 恢复项目文件失败: {e}", exc_info=True)
                        InfoBar.warning(
                            title="",
                            content=f"文件已暂存到: {extract_dir}，可手动复制到项目目录",
                            duration=5000,
                            parent=TabManagerWindow.get_instance() or self.window(),
                        )

                # 清理临时文件
                try:
                    if zip_path.startswith(tempfile.gettempdir()):
                        os.unlink(zip_path)
                except Exception:
                    pass

                return True
            else:
                InfoBar.warning(
                    title="",
                    content="导入失败：压缩包中无有效会话数据",
                    duration=3000,
                    parent=TabManagerWindow.get_instance() or self.window(),
                )
                return False
        except Exception as e:
            logger.error(f"[MainWidget] 导入项目异常: {e}", exc_info=True)
            InfoBar.error(
                title="",
                content=f"导入失败: {e}",
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            return False

    def _resolve_restore_path(self, project_name: str, original_root: Optional[str]) -> Optional[str]:
        """确定项目文件的恢复路径（仅返回路径，不创建目录）

        优先级：
        1. 原路径存在且合法（不含 .. 遍历） → 恢复回原路径，尊重用户已有配置
        2. 否则 → 默认恢复到 ~/.drifox/workspaces/<project_name>/
        """
        if original_root:
            resolved = os.path.abspath(original_root)
            # 防止路径遍历攻击：拒绝包含 .. 的非法路径
            # 但不限制必须在 home 下，用户的项目可能在任何位置
            if ".." in resolved.split(os.sep) or ".." in resolved.split("/"):
                logger.warning(f"[MainWidget] 原始路径包含 .. ，已跳过: {resolved}")
            elif os.path.isdir(resolved):
                logger.info(f"[MainWidget] 使用原始项目路径恢复: {resolved}")
                return resolved
            else:
                logger.info(f"[MainWidget] 原始路径不存在，将使用默认路径: {original_root}")

        # 默认恢复路径：与 _ensure_temp_workdir 一致，使用 ~/.drifox/workspaces/<project_name>/
        safe_name = re.sub(r'[<>:"/\\|?*]', "_", (project_name or "imported_project")[:40]).strip()
        if not safe_name:
            safe_name = "imported_project"
        default_dir = Path.home() / ".drifox" / "workspaces" / safe_name
        logger.info(f"[MainWidget] 使用默认路径恢复: {default_dir}")
        return str(default_dir)

    def _update_project_root_dir(self, project_name: str, restore_path: str):
        """导入后更新项目的根目录指向恢复路径"""
        try:
            if self.backend and self.backend.memory_manager:
                self.backend.memory_manager.set_working_directory(project_name, restore_path)
                # 同时更新实例缓存
                if not hasattr(self, "_current_workdir"):
                    self._current_workdir = {}
                self._current_workdir[project_name] = restore_path
                logger.info(f"[MainWidget] 已更新项目「{project_name}」根目录为: {restore_path}")
        except Exception as e:
            logger.warning(f"[MainWidget] 更新项目根目录失败: {e}")

    def _on_project_file_dropped(self, file_path: str):
        """拖拽 .drifox_project 文件到项目选择卡片的处理"""
        if not self.history_manager:
            InfoBar.warning(
                title="",
                content="历史管理器不可用",
                duration=2000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            return
        if self._do_import_project_archive(file_path):
            self._refresh_project_selector()
            self._refresh_history_toggle_panel()

    def _refresh_project_selector(self):
        """刷新项目选择器列表"""
        if not hasattr(self, "_project_selector_card_content"):
            return
        projects = self.history_manager.get_projects() if self.history_manager else ["默认项目"]
        if self._current_project not in projects:
            projects.insert(0, self._current_project)
        meta_map = self._build_project_meta_map(projects)
        root_dir_map = self._build_project_root_dir_map(projects)
        self._project_selector_card_content.set_projects_data(projects, self._current_project, meta_map, root_dir_map)

    def _on_open_project_folder(self, project_name: str, root_dir: str):
        """打开项目根目录（在文件管理器中打开）"""
        if not root_dir:
            return
        path = Path(root_dir)
        if not path.exists():
            logger.warning(f"[MainWidget] 项目根目录不存在: {root_dir}")
            InfoBar.warning(
                title="路径不存在",
                content=f"项目「{project_name}」的根目录已被移动或删除",
                orient=Qt.Horizontal,
                isClosable=True,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_project_folder_dropped(self, folder_path: str):
        """拖拽文件夹到项目选择卡片时的处理

        1. 弹出对话框让用户确认/修改项目名（默认使用文件夹名）
        2. 创建项目
        3. 将拖入文件夹加入关键文档并设为工作目录（根目录）
        4. 刷新项目列表
        """
        from app.widgets.cards.settings.memory_card import SingleInputDialog

        # ── 提取文件夹名作为默认项目名 ──
        folder_name = os.path.basename(folder_path.rstrip("/\\"))

        # ── 弹出输入对话框（统一样式） ──
        _project_name: list[str] = [""]

        def _on_project_created(name: str):
            _project_name[0] = name

        _dialog = SingleInputDialog(
            title="📁 新建项目",
            hint="将自动绑定此文件夹为项目根目录",
            placeholder="项目名称",
            default_text=folder_name,
            confirm_text="创建",
            cancel_text="取消",
            # parent 取顶层主窗口（Tab 模式下为 TabManagerWindow），而非 self 这个
            # 嵌入 QStackedWidget 的子 widget——MaskDialogBase 用 parent 尺寸铺遮罩，
            # 传子 widget 会导致遮罩只覆盖聊天区、弹窗层级/定位异常。
            parent=self.window(),
        )
        _dialog.confirmed.connect(_on_project_created)
        _dialog.exec_()
        project_name = _project_name[0]

        if not project_name:
            return

        # ── 检查项目名是否已存在 ──
        projects = self.history_manager.get_projects() if self.history_manager else ["默认项目"]
        if project_name in projects:
            InfoBar.warning(
                title="项目已存在",
                content=f"项目「{project_name}」已存在，请使用其他名称",
                orient=Qt.Horizontal,
                isClosable=True,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            return

        # ── 创建项目（已设根目录，跳过关键文档卡片弹出） ──
        # 传入 root_dir=folder_path：直接绑定指定文件夹为工作目录，
        # 不再创建默认项目文件夹（~/.drifox/workspaces/<project>/），
        # AGENTS.md 等文件写入指定路径而非默认路径。
        self._on_new_project_created(project_name, suppress_memory_card=True, root_dir=folder_path)

        # ── 将拖入文件夹加入关键文档并设为工作目录（根目录） ──
        try:
            if self.backend and self.backend.memory_manager:
                # 添加为关键文档
                self.backend.memory_manager.add_key_document(project_name, folder_path, added_by="manual")
                # 设置为工作目录（根目录）
                self.backend.memory_manager.set_working_directory(project_name, folder_path)
                logger.info(f"[MainWidget] 已绑定项目「{project_name}」根目录: {folder_path}")

            # 同步实例缓存（与手动添加并标记根目录路径保持一致）
            # 修复 bug：_on_new_project_created 内部调用的 _sync_working_directory 在
            # add_key_document 之前执行，导致 _instance_workdir 被错误缓存为空字符串，
            # 后续 _get_effective_workdir 返回 None，has_active_wd=False，
            # 关键文档 Tab 中工作目录条目下方的 worktree section 永远不显示。
            self._current_workdir[project_name] = folder_path
            if hasattr(self, "_memory_card_popup") and self._memory_card_popup:
                self._memory_card_popup._instance_workdir[project_name] = folder_path
                # 主动刷新关键文档 UI，确保 worktree section 立即显示
                # （与 _switch_to_worktree 行为保持一致）
                self._memory_card_popup._load_key_documents()
            # 同步到 tool_executor
            if self.backend and self.backend.tool_executor:
                self.backend.tool_executor.set_workdir(folder_path)
            # 刷新顶部项目 icon 右侧的分支标签
            # 修复：_on_new_project_created 中的 _update_branch 在 add_key_document 之前调用，
            # 此时 DB 没有 workdir，分支标签停留在旧状态（隐藏或显示旧分支）。
            self._update_branch()

            # ── 刷新项目选择卡片 ──
            projects = self.history_manager.get_projects() if self.history_manager else [project_name]
            if self._current_project not in projects:
                projects.insert(0, self._current_project)
            meta_map = self._build_project_meta_map(projects)
            root_dir_map = self._build_project_root_dir_map(projects)
            self._project_selector_card_content.set_projects_data(
                projects, self._current_project, meta_map, root_dir_map
            )

            InfoBar.success(
                title="项目已创建",
                content=f"已创建项目「{project_name}」并绑定根目录",
                orient=Qt.Horizontal,
                isClosable=True,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
        except Exception as e:
            logger.error(f"[MainWidget] 绑定项目根目录失败: {e}")
            InfoBar.error(
                title="绑定失败",
                content=f"项目「{project_name}」创建成功，但绑定根目录时出错: {e}",
                orient=Qt.Horizontal,
                isClosable=True,
                duration=5000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )

    def _on_project_open_folder_btn(self):
        """选择文件夹按钮：弹出文件夹选择器，选取文件夹后走拖拽建项目流程"""
        from PyQt5.QtWidgets import QFileDialog

        folder_path = QFileDialog.getExistingDirectory(
            self,
            "选择项目根目录",
            "",
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if not folder_path:
            return
        # 复用手动选择文件夹 → 弹出建项目对话框
        self._on_project_folder_dropped(folder_path)

    def _on_memory_tab_changed(self, tab_id: str):
        """处理记忆管理标签切换"""
        # 清空搜索
        search_input = getattr(self._memory_card, "_search_input", None)
        if search_input:
            search_input.clear()
        # 更新占位文本
        placeholders = {
            "entries": "🔍 搜索条目记忆...",
            "notes": "🔍 搜索项目笔记...",
            "docs": "🔍 搜索关键文档...",
        }
        if search_input:
            search_input.setPlaceholderText(placeholders.get(tab_id, "🔍 搜索..."))
        # 切换内容（_deferred_build_cards 可能尚未运行）
        if self._memory_card_popup:
            self._memory_card_popup.switch_tab(tab_id)

    def _on_working_dir_changed(self, file_path: str):
        """工作目录变更 → 更新实例缓存 + 同步到工具执行器 + 刷新分支标签

        多窗口隔离：更新实例缓存（关键！），DB 写入在 memory_card 层已完成。
        """
        # 更新实例缓存（多窗口隔离：每个窗口独立持有自己的 workdir）
        if file_path:
            self._current_workdir[self._current_project] = file_path
            resolved_path = file_path
        else:
            self._current_workdir.pop(self._current_project, None)
            # 清除根目录时自动创建临时工作目录兜底
            resolved_path = self._ensure_temp_workdir(self._current_project) or None
        # 同步到工具执行器
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_workdir(resolved_path or None)
            logger.info(f"[MainWidget] Working directory synced to tool executor: {resolved_path or 'cleared'}")
        # 同步到记忆卡片的实例缓存（使内部 get_effective_workdir 保持一致）
        if hasattr(self, "_memory_card_popup") and self._memory_card_popup:
            self._memory_card_popup._instance_workdir[self._current_project] = resolved_path or ""
        # 工作目录变更后刷新分支标签
        self._update_branch()
        # 工作目录变更后重扫文件列表（预缓存，下次 @ 即时显示）
        if hasattr(self, "_file_mention_card") and resolved_path:
            self._file_mention_card.ensure_cache(resolved_path)
        # 团队模式：一人改工作目录全员同步（统一工作树）
        if resolved_path:
            self._broadcast_team_workdir(resolved_path)

    def _sync_working_directory(self):
        """切换项目时自动加载并同步工作目录

        多窗口隔离：实例缓存优先；首次启动时从 DB 读取（新窗口默认值回退）。
        无根目录时自动在当前路径创建临时工作目录。
        """
        if getattr(self, "_is_destroyed", False):
            return
        if not self.backend or not self.backend.tool_executor:
            return
        project = self._current_project
        # 实例缓存优先（多窗口隔离的关键：保持自身选择，不受其他窗口 DB 写入影响）
        workdir = self._current_workdir.get(project)
        if workdir is None:
            # 首次启动或项目首次切换，从 DB 读取默认值（新窗口恢复用）
            if self.backend.memory_manager:
                workdir = self.backend.memory_manager.get_working_directory(project)
            if workdir:
                self._current_workdir[project] = workdir

        # 无根目录时自动创建临时工作目录
        if not workdir:
            workdir = self._ensure_temp_workdir(project)

        # workdir 变化时强制重渲染欢迎卡片（project-dashboard 看板等依赖 project_root）：
        # 启动时 workdir 延迟 2s 才同步，同步前渲染会拿到空/兜底路径（显示
        # "未检测到 git 项目"或旧项目信息）；同步完成后重渲染自动恢复为正确项目。
        # 仅重渲染 body（markdown），不重建 QWebEngineView（省 100-500ms 主线程）。
        prev_wd = self.backend.tool_executor.get_workdir()
        self.backend.tool_executor.set_workdir(workdir or None)
        new_wd = self.backend.tool_executor.get_workdir()
        if new_wd != prev_wd:
            self._rerender_welcome_card()

        # 同步到记忆卡片的实例缓存（确保关键文档能感知到当前工作目录）
        if hasattr(self, "_memory_card_popup") and self._memory_card_popup:
            self._memory_card_popup._instance_workdir[project] = workdir or ""

        # 工作目录变化后刷新分支标签
        self._update_branch()

        from loguru import logger

        logger.info(f"[MainWidget] Synced working directory for project '{project}': {workdir or 'default'}")

    def _ensure_temp_workdir(self, project: str) -> str:
        """确保项目有临时工作目录

        当项目未设置根目录时，在 ~/.drifox/workspaces/{project}/ 下创建
        临时工作目录，确保文件操作总有安全的基础目录。

        使用 Path.home() / '.drifox'（用户家目录）而非 resource_path("")，原因：
        - 用户数据归属：工作区数据应归属用户数据目录而非项目目录
        - 持久性：~/.drifox 在开发和打包环境下始终可写、路径固定
        - 多窗口隔离：所有窗口统一使用同一基准，避免进程 cwd 飘移导致混乱
        - 稳定性：os.getcwd() 可能被外部工具/IDE 改变，家目录是固定路径
        """
        try:
            from pathlib import Path

            base_dir = str(Path.home() / ".drifox")
            temp_dir = os.path.join(base_dir, "workspaces", project)
            os.makedirs(temp_dir, exist_ok=True)
            self._current_workdir[project] = temp_dir

            # 将临时目录加入关键文档并标记为工作目录（set_working_directory 会自动插入路径）
            try:
                if self.backend and self.backend.memory_manager:
                    self.backend.memory_manager.set_working_directory(project, temp_dir)
            except Exception as e:
                logger.warning(f"[MainWidget] Failed to sync temp workdir to key docs: {e}")

            # 刷新关键文档卡片 UI，使新增的根目录立即可见
            try:
                if hasattr(self, "_memory_card_popup") and self._memory_card_popup:
                    self._memory_card_popup._instance_workdir[project] = temp_dir
                    self._memory_card_popup._load_key_documents()
            except Exception as e:
                logger.warning(f"[MainWidget] Failed to refresh key docs UI: {e}")

            logger.info(f"[MainWidget] Created temp workdir for '{project}': {temp_dir}")
            return temp_dir
        except Exception as e:
            logger.warning(f"[MainWidget] Failed to create temp workdir: {e}")
            return ""

    def _show_soul_memory(self):
        """切换记忆管理卡片的显示"""
        self._toggle_memory_card()

    def _toggle_memory_card(self):
        """切换记忆管理卡片的显示"""
        self._ensure_memory_card()  # P0-1：框架懒创建，确保注册/入容器
        self._card_manager.toggle_card("memory", self._window_id)
        # 显示时刷新记忆数据
        if self._card_manager.is_card_visible("memory", self._window_id) and hasattr(self, "_memory_card_popup"):
            # 默认切换到条目记忆标签
            default_tab = "entries"
            # 强制切换 tab
            if hasattr(self._memory_card_popup, "_current_tab"):
                self._memory_card_popup._current_tab = None  # 临时改变，确保 switch_tab 执行
                self._memory_card_popup.switch_tab(default_tab)
            # 同时更新 BaseSettingsCard 的 tab 状态
            if hasattr(self._memory_card, "set_current_tab"):
                self._memory_card.set_current_tab(default_tab)

    def _on_memory_card_saved(self, memories: list):
        """记忆卡片保存后的回调"""
        # 数据已经在 MemoryCardContent 中通过 backend 保存
        # 这里只显示提示信息
        InfoBar.success(
            "已保存",
            "长期记忆已更新",
            parent=TabManagerWindow.get_instance() or self.window(),
            duration=1500,
            position=InfoBarPosition.BOTTOM,
        )

    def _on_memory_updated(self, memories: list):
        self.backend.update_user_memories(memories)
        InfoBar.success(
            "已保存",
            "长期记忆已更新",
            parent=TabManagerWindow.get_instance() or self.window(),
            duration=1500,
            position=InfoBarPosition.BOTTOM,
        )

    def _on_title_edit_finished(self):
        """标题编辑完成 - 保存用户编辑的标题"""
        new_title = self.title_edit.text().strip()
        if not new_title:
            return
        self._save_edited_title(new_title)

    def _save_edited_title(self, new_title: str):
        """保存用户编辑的标题"""
        self.title_edit.setText(new_title)

        # 标记 session 的 user_edited_title = True
        session = self.session_manager.get_current_session()
        if session:
            session.set_user_edited_title(True)
            session.set_topic_summary(new_title)

        # 更新 history_manager 中的标题
        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(self._current_session_id)
            if idx is not None:
                self.history_manager.update_session_title(idx, new_title)
                # 同步 user_edited_title 标记
                self.history_manager.set_user_edited_title(idx, True)

        # 持久化到 DB
        if self._current_session_id and self.session_store:
            self.session_store.update_session_title(self._current_session_id, new_title)

        # 同步对话框窗口标题（便于 Windows 任务栏区分各窗口）
        self._sync_dialog_title()

    def _restore_title_display(self):
        """恢复标题显示（编辑取消时）"""
        session = self.session_manager.get_current_session()
        if session:
            current_title = session.topic_summary or session.name or "新对话"
            self.title_edit.setText(current_title)
            # 同步对话框窗口标题（便于 Windows 任务栏区分各窗口）
            self._sync_dialog_title()

    def _get_current_worktree_path(self) -> str:
        """检测当前工作目录是否在 git worktree 中，返回 worktree 路径（空字符串表示不在）"""
        workdir = self._current_workdir.get(self._current_project)
        if not workdir or not os.path.isdir(str(workdir)):
            return ""
        from app.utils.git_worktree import GitWorktreeDetector

        git_root = GitWorktreeDetector.detect_git(str(workdir))
        if not git_root:
            return ""
        # worktree 的 .git 是文件，主仓库的 .git 是目录
        if GitWorktreeDetector.is_worktree(git_root):
            return git_root
        # 工作目录可能是 worktree 内的子目录，向上探测
        if GitWorktreeDetector.is_worktree(str(workdir)):
            return str(workdir)
        return ""

    def _switch_to_worktree(self, worktree_path: str):
        """切换到指定 worktree，幂等——已在目标 worktree 中则跳过。

        加载会话时自动调用：会话关联了哪个 worktree，就切到哪个 worktree。
        """
        if not worktree_path or not os.path.isdir(worktree_path):
            return
        project = self._current_project

        # 幂等：已在目标 worktree 中则跳过
        if self._current_workdir.get(project) == worktree_path:
            return

        # 1. 通过 memory_manager 切换工作目录
        if self.backend and self.backend.memory_manager:
            mm = self.backend.memory_manager
            db_wd = mm.get_working_directory(project)
            mm.add_key_document(project, worktree_path, "git_worktree")
            mm.set_working_directory(project, worktree_path)
            # 恢复非 worktree 根目录的 is_working_dir 标记，确保记忆卡片
            # 能正确识别用户设定的根目录。get_working_directory 可能返回
            # worktree 路径（ORDER BY 优先），此时跳过 restore 以避免
            # 错误地为 worktree 恢复标记。
            if db_wd and db_wd != worktree_path and db_wd != "clear":
                # 如果 db_wd 指向的是 worktree（added_by 为 git_worktree），
                # 不恢复它 — 我们需要恢复的是主仓库/根目录的标记
                all_docs = mm.get_key_documents(project)
                is_db_wd_worktree = any(
                    d.get("file_path") == db_wd and d.get("added_by") == "git_worktree" for d in all_docs
                )
                if not is_db_wd_worktree:
                    mm.restore_working_directory_mark(project, db_wd)

        # 2. 更新实例缓存 + 同步工具执行器 + 刷新分支标签
        self._current_workdir[project] = worktree_path
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_workdir(worktree_path)
        self._update_branch()

        # 3. 同步记忆卡片 UI
        try:
            if hasattr(self, "_memory_card_popup") and self._memory_card_popup:
                mc = self._memory_card_popup
                mc._instance_workdir[project] = worktree_path
                mc._load_key_documents()
        except Exception:
            pass

        logger.info(f"[MainWidget] 已自动切换到 worktree: {worktree_path}（项目: {project}）")
        # 团队模式：worktree 切换全员同步（统一工作树）
        self._broadcast_team_workdir(worktree_path)

    def _restore_main_repo(self):
        """从 worktree 切换回主仓库，幂等——已不在 worktree 中则跳过。

        加载主仓库会话时自动调用：会话没有关联 worktree，说明属于主仓库。
        """
        project = self._current_project
        current_wt = self._get_current_worktree_path()
        if not current_wt:
            # 已不在 worktree 中，无需切换
            return

        from app.utils.git_worktree import GitWorktreeDetector

        main_repo = GitWorktreeDetector.get_main_repo_path(current_wt)
        if not main_repo or not os.path.isdir(main_repo):
            logger.warning(f"[MainWidget] 无法找到主仓库路径，跳过切换（当前 worktree: {current_wt}）")
            self._update_branch()
            return

        # 幂等：已回到主仓库则跳过
        if self._current_workdir.get(project) == main_repo:
            return

        if self.backend and self.backend.memory_manager:
            mm = self.backend.memory_manager
            mm.set_working_directory(project, main_repo)

        self._current_workdir[project] = main_repo
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_workdir(main_repo)
        self._update_branch()

        try:
            if hasattr(self, "_memory_card_popup") and self._memory_card_popup:
                mc = self._memory_card_popup
                mc._instance_workdir[project] = main_repo
                mc._load_key_documents()
        except Exception:
            pass

        logger.info(f"[MainWidget] 已自动切换回主仓库: {main_repo}（项目: {project}）")
        # 团队模式：切回主仓库全员同步（统一工作树）
        self._broadcast_team_workdir(main_repo)

    @classmethod
    def _on_app_about_to_quit(cls):
        """应用退出时保存所有窗口的脏会话（单次注册，批量执行）"""
        for win in getattr(cls, "_instances", []):
            if getattr(win, "_is_destroyed", False):
                continue
            try:
                win._auto_save_current_session()
            except Exception:
                pass

    def _get_team_members_snapshot_json(self) -> str:
        """获取团队成员快照 JSON 字符串（供会话落库，F3 第 2 层）。

        仅团队模式有值；非团队窗口返回空串（避免普通会话写入团队字段）。

        🛡️ M1-r：快照按当前 run_id 过滤——M1 多团队并存时 default team.json
        的 team_members 含所有 run 的成员，不过滤会把其他团队的成员写进
        本团队会话快照（会话保存"成员污染"）；传入 run_id 后仅保存
        当前团队的成员，且含没对话过的成员（join 即入快照）。
        """
        if not getattr(self, "_team_run_id", ""):
            return ""
        try:
            tm = self._get_team_manager()
            snapshot = tm.get_team_member_snapshot(run_id=self._team_run_id or "")
            import json as _json

            return _json.dumps(snapshot, ensure_ascii=False)
        except Exception:
            return ""

    def _auto_save_current_session(self):
        session = self.session_manager.get_current_session()
        if not session or not session.messages:
            return

        # 跳过没有用户消息的会话（SessionStart hook 产生的空会话不应保存到历史）
        # 🛡️ R7 修复：team 邮件（_hook_event="TeamMail"）视为真实用户问题
        # → mail-only 会话也能自动保存到历史（之前被全过滤误伤，永不落库）
        has_user_message = any(
            msg.get("role") == "user" and (not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail")
            for msg in session.messages
        )
        if not has_user_message:
            return

        # 🛡️ 跳过无变更保存：自上次保存后会话没有被修改，
        # 避免新建会话/加载历史/关闭窗口时对已持久化的会话做无意义重复保存。
        # 脏标记由各个消息修改点置 True，由本 save 函数置 False。
        if not self._session_dirty:
            return

        # 🛡️ 落库前回填：给有 model_name 但缺 provider_name 的 assistant 消息补上
        # 策略：优先精确匹配当前选中的服务商，其次模糊匹配
        current_provider_display = ""
        if self._current_provider_name:
            cfg = self._valid_configs.get(self._current_provider_name, {})
            current_provider_display = cfg.get("display_name", self._current_provider_name)
        for msg in session.messages:
            if msg.get("role") != "assistant":
                continue
            if msg.get("model_name") and not msg.get("provider_name") and self._valid_configs:
                model = msg["model_name"]
                # 先尝试精确匹配当前选中的服务商
                if current_provider_display:
                    cfg = self._valid_configs.get(self._current_provider_name, {})
                    if cfg.get("模型名称") == model or model in (cfg.get("模型列表") or []):
                        msg["provider_name"] = current_provider_display
                        continue
                matched = None
                for cid, info in self._valid_configs.items():
                    if info.get("模型名称") == model or model in (info.get("模型列表") or []):
                        if matched is None:
                            matched = info.get("display_name", info.get("provider_name", cid))
                        else:
                            matched = None  # 多个匹配 → 不明确，跳过
                            break
                if matched:
                    msg["provider_name"] = matched

        system_prompt = getattr(session, "system_prompt", "") or ""
        worktree_path = self._get_current_worktree_path()
        # 🛡️ 始终记录当前 worktree_path（空字符串表示主仓库，清除旧关联）
        worktree_kwargs = {"worktree_path": worktree_path or ""}

        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(self._current_session_id)
            if idx is not None:
                # 🛡️ 更新已有会话时不传 project，保留该会话原有的项目归属
                # 避免项目切换后 _current_project 已改变，导致旧会话被错误地划归新项目
                # 团队元数据：update_session 语义为 None 保留现值，故仅当本窗口
                # 处于团队模式（_team_run_id 非空）才显式传参覆盖；非团队窗口
                # 不传（None）→ 保留历史值，避免普通编辑把团队会话元数据清空。
                team_kwargs = {}
                if self._team_run_id:
                    team_kwargs = {
                        "team_run_id": self._team_run_id,
                        "team_name": self._team_name or "",
                        "agent_name": self._team_agent_name or "",
                        # 🛡️ F3（T2-P3 第 2 层）：团队成员快照随会话落库——
                        # 恢复不依赖当前 team.json（历史 run 的 team.json 会被新
                        # run 覆盖），手动成员在历史 run 中也能找回。
                        "team_members": self._get_team_members_snapshot_json(),
                    }
                self.history_manager.update_session(
                    idx,
                    session.messages,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                    **worktree_kwargs,
                    **team_kwargs,
                )
            else:
                # 🛡️ 内存找不到（老会话被截断出 _history_limit）：
                # 先查 SQLite 原 project，避免被当前 _current_project 覆盖
                resolved_project = self._resolve_session_project_fallback(
                    self._current_session_id, self._current_project, session=session
                )
                # 团队元数据：与 update 分支语义一致——仅当本窗口处于团队模式
                # （_team_run_id 非空）才传团队字段；非团队窗口传默认空串会经
                # INSERT OR REPLACE 清空该团队会话的元数据（截断出内存的
                # 老会话无法走 update 保留现值路径），故此处也需守卫。
                team_save_kwargs = {}
                if self._team_run_id:
                    team_save_kwargs = {
                        "team_run_id": self._team_run_id,
                        "team_name": self._team_name or "",
                        "agent_name": self._team_agent_name or "",
                        # 🛡️ F3：团队成员快照随会话落库（见上方 update 分支注释）
                        "team_members": self._get_team_members_snapshot_json(),
                    }
                self.history_manager.save_session(
                    session.messages,
                    session_id=session.session_id,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                    project=resolved_project,
                    **team_save_kwargs,
                    **worktree_kwargs,
                )
                self._current_session_id = session.session_id
        else:
            # 🛡️ 新会话：用 _current_project；若 session_id 在 SQLite 已存在则沿用原 project
            resolved_project = self._resolve_session_project_fallback(
                session.session_id, self._current_project, session=session
            )
            self.history_manager.save_session(
                session.messages,
                session_id=session.session_id,
                compaction_state=getattr(session, "compaction_state", {}),
                compaction_cache=getattr(session, "compaction_cache", {}),
                system_prompt=system_prompt,
                project=resolved_project,
                team_run_id=self._team_run_id,
                team_name=self._team_name or "",
                agent_name=self._team_agent_name or "",
                # 🛡️ F3：团队成员快照随会话落库（见上方 update 分支注释）
                team_members=self._get_team_members_snapshot_json(),
                **worktree_kwargs,
            )
            self._current_session_id = session.session_id

        # 🛡️ 成功保存后清除脏标记
        self._session_dirty = False

        # 立即落盘，确保退出前数据写入 SQLite
        if self.history_manager:
            self.history_manager.flush()

        # 🆕 刷新历史面板：自动保存路径同样需要触发 UI 同步
        # （关闭窗口/项目切换等触发此函数时，历史面板可能已展开）。
        # 仅历史卡片可见时执行（不可见时 0 开销）。
        refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

        if self._current_session_id:
            idx = self.history_manager.find_index_by_session_id(self._current_session_id)
            if idx is not None:
                return self.history_manager.get_current_title(idx)
        return None

    def closeEvent(self, event):
        # 🔧 F2: 主动断开 destroyed 清理连接 + 立即清理快捷键，避免窗口 C++ 对象
        # 销毁触发 destroyed 时 lambda 访问已删除的 self 抛 RuntimeError
        # （wrapped C/C++ object has been deleted）。
        self._disconnect_command_shortcut_cleanup()

        # ★ 泄漏修复（A1-3）：断开引用环——复制/分支窗口持有源窗口引用，
        # 不清空则已关闭窗口的对象树被源窗口持续持有（wrapper 不回收）。
        # 必须在 _disconnect_command_shortcut_cleanup 之后（该处可能访问
        # _source_window），置 None 后窗口对象树即刻可回收。
        try:
            self._source_window = None
        except Exception:
            pass

        # ★ 泄漏修复（A1-3）：补断 Tab 模式下 add_window 挂接的信号闭包
        # （windowTitleChanged → _tab_title_changed_slot、ai_state_changed →
        # _tab_ai_state_slot）。tab_manager_window._close_window_at 已断开
        # 一次（覆盖 Tab 关闭路径），此处兜底覆盖非 Tab 路径/窗口直接 close
        # （如 _on_team_close_requested 的批量关闭），防止闭包
        # __defaults__ 持有窗口引用导致整树泄漏。须在 C++ 对象存活时
        # disconnect 安全执行。
        for _slot_attr, _signal in (
            ("_tab_title_changed_slot", "windowTitleChanged"),
            ("_tab_ai_state_slot", "ai_state_changed"),
        ):
            try:
                _slot = getattr(self, _slot_attr, None)
                if _slot is not None:
                    getattr(self, _signal).disconnect(_slot)
                    setattr(self, _slot_attr, None)
            except Exception:
                pass

        # 标记窗口正在关闭，防止所有异步回调访问已销毁的 UI
        self._is_destroyed = True

        # 关闭锁屏远程：恢复系统正常休眠策略，避免电脑一直无法休眠
        try:
            from app.core.system.lock_screen_remote import get_lock_screen_remote_manager

            get_lock_screen_remote_manager().disable()
        except Exception:
            pass

        # 显式停止所有窗口级 timer，避免在 closeEvent 之后还触发回调
        # 访问已删除的 widget（_is_destroyed 守卫是第二道防线）
        for timer_attr in (
            "_scroll_bottom_timer",
            "_bottom_anchor_timer",
            "_virtual_scroll_timer",
            "_resize_debounce_timer",
            "_resize_complete_timer",
            "_scroll_sync_timer",
        ):
            timer = getattr(self, timer_attr, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass

        # ★ 用量聚合（T6）：注销本窗口 config 的用量轮询注册（active key + 快照）。
        # 套餐用量轮询已由 UsageService 单例统一驱动（窗口不再自建 60s timer），
        # 此处仅清理注册，避免已关闭窗口的配置持续触发后台请求。
        try:
            _cid = getattr(self, "_current_provider_name", "")
            if _cid:
                from app.core.usage_service import UsageService

                UsageService.get_instance().unregister(_cid)
        except Exception:
            pass

        # ★ 清理像素小狐桌宠（停止所有定时器）
        if getattr(self, "pixel_pet", None) is not None:
            try:
                self.pixel_pet.cleanup()
            except Exception:
                pass

        # 🔧 清理团队邮箱监听器
        if getattr(self, "_team_fs_watcher", None):
            try:
                # 🛡️ Bug5：先收尾团队邮件状态再停监听。流式中窗口关闭时，
                # 正在处理/hook 注入的邮件若不同步，会永久卡 running（done/pending
                # 收尾只在流式结束/手动停止路径触发，closeEvent 不触发则死锁）。
                # _sync_team_mail_on_stop 按 _mail_was_responded 收尾：有响应→done，
                # 无响应→回滚 pending（下次该窗口重建后可重新处理，不丢失）。
                self._sync_team_mail_on_stop()
            except Exception:
                pass
            try:
                self._stop_team_watcher()
            except Exception:
                pass

        # 🔧 内存泄漏修复：停止窗口级 ThreadPool
        if hasattr(self, "_gen_thread_pool") and self._gen_thread_pool:
            try:
                self._gen_thread_pool.waitForDone(1000)
            except Exception:
                pass

        # 🔧 内存泄漏修复：清理 AutoLoop Worker
        if getattr(self, "_is_auto_loop_running", False) or getattr(self, "_auto_loop_worker", None):
            try:
                self._cleanup_auto_loop_state("窗口关闭")
            except Exception:
                pass

        # 从全局实例列表中移除
        try:
            OpenAIChatToolWindow._instances.remove(self)
        except ValueError, Exception:
            pass

        # 离开团队并同步活跃窗口
        try:
            # 🛡️ 批量解散场景（_handle_team_leave(batch_disband=True) 置位标志）：
            # 跳过本窗口的全局同步——TabManager 在解散循环结束后统一同步 1 次，
            # 避免 n 个窗口各同步 1 次（O(n×m)）。单窗关闭路径不受影响。
            if not getattr(self, "_batch_disband_in_progress", False):
                self._sync_active_windows_to_team_manager()
        except Exception:
            pass

        # 多窗口隔离：注销窗口及其卡片数据
        try:
            from app.widgets.cards.card_manager import CardManager

            CardManager.get_instance().unregister_window(self._window_id)
            logger.debug(f"[OpenAIChatToolWindow] 注销窗口卡片: {self._window_id}")
        except Exception:
            pass

        # ★ 泄漏修复（P0）：注销窗口的 UI 插件状态，释放注册表对窗口的强引用。
        # 窗口 __init__ 调用 ui_registry.set_main_widget(self) +
        # set_context_provider(self._build_ui_context, self._window_id)——
        # provider 闭包、_window_main_widgets、_card_widget_instances 均按
        # window_id 持有窗口引用，不清理则窗口对象树被全局单例持续持有。
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            UIPluginRegistry.get_instance().unregister_window(self._window_id)
        except Exception:
            pass

        # 停止所有正在进行的流式输出 + 清理窗口独有资源（不影响其他窗口）
        if hasattr(self, "backend") and self.backend:
            # 🔧 内存泄漏修复：先断开信号连接，防止闭包持有窗口引用
            for signal_pair in (("plugin_changed", "_on_plugin_hot_reload"),):
                try:
                    sig = getattr(self.backend, signal_pair[0], None)
                    slot = getattr(self, signal_pair[1], None)
                    if sig is not None and slot is not None:
                        sig.disconnect(slot)
                except TypeError, RuntimeError:
                    pass

            # 🛡️ B5 异步化：closeEvent 不再同步调用 backend.stop_streaming()
            # （其内部 finalize 等待 worker 退出，最多阻塞 ~4s）。
            # 改为 cancel_streaming()（仅置标志，非阻塞）+ 后台 daemon 线程
            # finalize（见 _launch_background_finalize）：
            # - 取消 worker（非阻塞，不等待）
            try:
                self.backend.cancel_streaming()
                self._topic_summary_cancelled = True  # 🛡️ 取消标题生成重试
            except Exception:
                pass

            # 🛡️ 强制关窗路径独立计算并持久化累计运行时长（elapsed）
            #
            # 关键：_on_stop_clicked 第一阶段才会计算 self._stop_elapsed（从
            # _response_start_time），但强制关闭窗口不走 _on_stop_clicked，
            # 所以 _stop_elapsed 永远是 None。原来 _persist_stop_elapsed 早退，
            # 最新一条 assistant 消息的 elapsed 字段永远不存 → 重启/重载后
            # 看不到运行时长 = 用户报告的"累计运行时长无法保存"。
            #
            # 修复：手动从 _response_start_time 计算 elapsed → 由后台 finalize
            # 线程 _persist_stop_elapsed 写入 → save + flush（见 _launch_background_finalize）。
            # ⚠️ 必须在 _launch_background_finalize 之前算好 _stop_elapsed，
            # 否则后台线程 _persist_stop_elapsed 读到 None 早退 → elapsed 丢失。
            if self._response_start_time is not None:
                self._stop_elapsed = time.time() - self._response_start_time
                self._response_start_time = None

            # - 后台 finalize：收集 interrupted_messages + 应用 + 持久化
            #   （_is_destroyed=True 时后台线程直接处理，主线程回调全 return
            #   无并发写；停止按钮路径走 emit 回主线程原语义）
            try:
                self._launch_background_finalize()
            except Exception as e:
                logger.warning(f"[ChatWindow] closeEvent 启动后台 finalize 失败: {e}")
            # - backend.cleanup() 保留主线程（B6 已让超时转后台，不阻塞）
            try:
                self.backend.cleanup()
            except Exception:
                pass

        # 标记初始化已完成（防止窗口在初始化期间关闭导致竞态条件）
        self._initialization_in_progress = False

        try:
            self._auto_save_current_session()
        except Exception:
            pass

        # ★ 泄漏修复（P0-1）：关闭窗口时释放 HistoryManager 内存缓存中的
        # 会话消息驻留（删会话/关标签不清理 → 全局单例 _history_sessions
        # 持全量消息不回落）。保留记录元数据，重开会话时 SQLite 懒加载回填。
        try:
            if hasattr(self, "session_manager") and self.session_manager:
                for _s in self.session_manager.get_all_sessions():
                    _sid = getattr(_s, "session_id", None)
                    if _sid and getattr(self, "history_manager", None):
                        self.history_manager.remove_session(_sid, release_messages_only=True)
        except Exception:
            pass

        # ★ 泄漏修复（T6-B）：closeEvent 清理窗口 UI 缓存强引用
        # （_welcome_card_cache 持卡片 / _session_card_cache / batch 结构），
        # 避免已关闭窗口的卡片对象被缓存持续持有。
        try:
            self._message_batch = []
            self._batch_cards = []
            self._pending_lazy_cards.clear()
            wc = self._welcome_card_cache.pop(self._window_id, None)
            if wc is not None:
                from PyQt5 import sip

                if not sip.isdeleted(wc):
                    if hasattr(wc, "cleanup"):
                        wc.cleanup()
                    wc.deleteLater()
            self._session_card_cache.clear()
        except Exception:
            pass

        # ★ 泄漏修复（A1-2）：显式清理窗口树内全部 MessageCard。
        # findChildren 覆盖未被缓存引用的消息卡片（如批量加载渲染中/虚拟
        # 滚动窗口外挂起的卡片），stop timer + viewer.cleanup + 断信号后
        # deleteLater，避免卡片持有的 QWebEngineView/定时器/信号闭包把
        # 窗口对象树拖住不回收。整体 try/except：清理失败不影响关闭流程。
        try:
            from app.widgets.message_card import MessageCard

            for card in self.findChildren(MessageCard):
                try:
                    from PyQt5 import sip

                    if sip.isdeleted(card):
                        continue
                    if hasattr(card, "cleanup"):
                        card.cleanup()
                    card.deleteLater()
                except Exception:
                    continue
        except Exception:
            pass

        # ★ B4 温和层：窗口关闭时全局观测计数递减（本窗口已渲染卡片数）
        global _global_rendered_pages
        _global_rendered_pages = max(0, _global_rendered_pages - self._rendered_card_count)
        # ★ B4 强回收层：进程退出整体回收，不 kill（窗口销毁时 renderer
        # 子进程随 WebEngine profile 自动退出，显式 kill 反而可能误伤共享进程）
        self._unloaded_pids.clear()
        # ★ B4 强回收层：进程退出整体回收（不 kill——窗口销毁时 renderer
        # 子进程随 WebEngine 自动退出，kill 反而可能误伤其他窗口的复用 PID）
        self._unloaded_pids.clear()

        # 最后一个窗口关闭 → 应用退出，保存工作目录到 DB，下次启动时自动恢复
        if not OpenAIChatToolWindow._instances:
            try:
                workdir = self._current_workdir.get(self._current_project)
                if workdir and self.backend and self.backend.memory_manager:
                    self.backend.memory_manager.set_working_directory(self._current_project, workdir)
                    logger.info(f"[MainWidget] 应用退出，保存工作目录: {workdir}")
            except Exception:
                pass

        # 断开 aboutToQuit 信号，防止退出时访问已销毁的 widget
        try:
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.disconnect(self._auto_save_current_session)
        except Exception:
            pass

        # ★ 泄漏修复（P0）：兜底冲刷已排队的 DeferredDelete 事件。
        # Tab 模式下 _close_window_at 在 close() 之后调用 setParent(None)+
        # deleteLater()，若此时主线程事件循环未及时进入下一轮（或应用即将退出），
        # 排队的删除事件可能滞留。此处主动处理本窗口及其子树已排队的延迟删除，
        # 确保 C++ 对象树即刻回收。仅主线程安全；由 closeEvent 在主线程执行，
        # 且异常被吞不影响关闭流程。
        try:
            from PyQt5.QtCore import QCoreApplication, QEvent

            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        except Exception:
            pass

        # GC 钩子：closeEvent 末尾防抖触发全局缓存清理 + 堆回收
        self._schedule_gc_hook()

        super().closeEvent(event)

    def _toggle_send_stop(self, is_sending: bool):
        if is_sending:
            self.input_area.toggle_send_button(False)
        else:
            self.input_area.toggle_send_button(True)

    def _on_stop_clicked(self):
        if getattr(self, "_is_destroyed", False):
            return
        # 🛡️ 取消正在进行的标题生成任务，防止停止后仍继续重试
        self._topic_summary_cancelled = True

        # 🛡️ 防止重复点击停止按钮
        if getattr(self, "_stop_deferred_pending", False):
            return
        self._stop_deferred_pending = True

        # 🛡️ 记录手动停止时刻（F1 P1-3）：停止后 1s 冷却期内不自动拉起 pending 邮件
        self._last_stop_time = time.monotonic()

        # ===== 第一阶段：非阻塞取消 + 立即更新 UI =====
        # 先取消 worker（仅设置标志 + 断开信号，不阻塞）
        if self.backend.chat_engine:
            self.backend.cancel_streaming()

        self._is_streaming = False
        self._set_ai_state("idle")  # 桌宠：用户手动停止

        self._toggle_send_stop(False)
        if self._current_assistant_card and not _is_sip_deleted(self._current_assistant_card):
            # 强制停止计时器（不依赖 set_meta_info）
            self._current_assistant_card._elapsed_timer.stop()
            self._current_assistant_card._elapsed_start_time = None
            # 设置并暂存最终耗时（_on_finalize_complete 可能收不到消息，需独立持久化）
            if self._response_start_time is not None:
                self._stop_elapsed = time.time() - self._response_start_time
                self._current_assistant_card.set_meta_info(elapsed=self._stop_elapsed)
            else:
                self._stop_elapsed = None
            self._current_assistant_card.stop_streaming_anim()
            self._current_assistant_card.finish_streaming()

        # 优先显示中止提示，让用户立即感知到操作已生效
        InfoBar.warning(
            title="已中止",
            content="",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.BOTTOM,
            duration=2000,
            parent=TabManagerWindow.get_instance() or self.window(),
        )
        self._focus_input_if_active()

        # ⚠️ 立即更新时间线节点（即使后台 finalize 还未完成）
        self._update_node_preview()

        # 🆕 手动停止时同步团队邮件状态
        self._sync_team_mail_on_stop()

        # ===== 第二阶段：延迟执行阻塞操作（等待 worker + 收集中断消息）=====
        # 使用 QTimer.singleShot 延迟到 UI 事件处理完成后执行，
        # 避免 worker.wait() 等阻塞操作影响界面响应
        QTimer.singleShot(0, self._deferred_stop_handler)

    def _deferred_stop_handler(self):
        """延迟停止处理：等待 worker 结束 + 收集中断消息 + 保存会话

        此方法在 _on_stop_clicked() 的 UI 更新完成后执行，
        包含可能会阻塞的 worker.wait() 和 I/O 操作。

        B5 加固：统一委托 _launch_background_finalize（幂等锁 + engine_ref 判空），
        closeEvent 并发时 backend.chat_engine 可能被置 None。
        """
        if getattr(self, "_is_destroyed", False):
            return

        self._stop_deferred_pending = False

        # 🛡️ B5：后台 finalize（幂等；daemon 闭包对 engine_ref 判空）
        self._launch_background_finalize()

    def _launch_background_finalize(self):
        """B5：停止/关窗路径后台 finalize（幂等，不阻塞主线程）。

        由两处调用：
        - closeEvent（关窗路径，_is_destroyed=True）：后台线程直接应用中断消息
          + 持久化（窗口已销毁，主线程回调全 return，无并发写）。
        - _deferred_stop_handler（停止按钮路径，_is_destroyed=False）：
          emit _interrupt_complete 回主线程（原 _on_finalize_complete 语义不变）。

        幂等锁：_bg_finalize_started 防止「停止按钮 → 关窗」双触发时启动两个
        finalize 线程（双 finalize / 重复保存）。
        """
        with self._bg_finalize_lock:
            if self._bg_finalize_started:
                return
            self._bg_finalize_started = True

        # 🛡️ engine_ref 提前捕获并判空（closeEvent 并发时 backend.chat_engine 可能被置 None）
        engine_ref = self.backend.chat_engine if self.backend else None
        if engine_ref is None:
            # 无 chat_engine：关窗路径由 _auto_save_current_session 兜底；停止路径更新时间线
            if not getattr(self, "_is_destroyed", False):
                self._update_node_preview()
            return

        import threading
        import traceback

        def _do_finalize():
            try:
                interrupted_messages = engine_ref.finalize_stop() or []
            except Exception as e:
                logger.error(f"[ChatWindow] _launch_background_finalize error: {e}\n{traceback.format_exc()}")
                interrupted_messages = []
            try:
                if getattr(self, "_is_destroyed", False):
                    # 关窗路径：窗口已销毁，后台线程直接应用中断消息 + 持久化。
                    # 主线程回调（_on_finalize_complete 等）检测 _is_destroyed 全 return，
                    # 无并发写；SessionStore 每线程独立连接（threading.local），
                    # flush() 直接 _do_save() 同步落盘不依赖 QTimer。
                    if interrupted_messages:
                        self._apply_interrupted_messages_to_session(interrupted_messages)
                    self._persist_stop_elapsed()
                    # 🛡️ 主线程 _auto_save_current_session 已清零脏标记，
                    # 后台链必须重新置脏，否则 _save_current_session_to_history
                    # 因 not _session_dirty 直接 return → partial/elapsed 丢失。
                    self._session_dirty = True
                    if self.history_manager:
                        self._save_current_session_to_history()
                        self.history_manager.flush()
                else:
                    # 停止按钮路径：跨线程信号回主线程（daemon 线程无事件循环）
                    self._interrupt_complete.emit(interrupted_messages)
            except Exception as e:
                logger.error(f"[ChatWindow] _launch_background_finalize 应用/保存失败: {e}\n{traceback.format_exc()}")

        t = threading.Thread(target=_do_finalize, daemon=True)
        t.start()

    def _persist_stop_elapsed(self):
        """将手动停止时暂存的耗时写入 session.messages（不保存，不 flush）。

        仅写入 elapsed 字段，不触发 save/flush。
        由调用者统一 save+flush，避免多次保存同一会话。
        """
        if self._stop_elapsed is None:
            return
        session = self.session_manager.get_current_session()
        if not session or not session.messages:
            return
        for msg in reversed(session.messages):
            if msg.get("role") == "assistant":
                if "elapsed" not in msg:
                    msg["elapsed"] = round(self._stop_elapsed, 1)
                if not msg.get("config_id") and self._current_provider_name:
                    msg["config_id"] = self._current_provider_name

    def _apply_interrupted_messages_to_session(self, interrupted_messages: List[Dict[str, Any]]) -> bool:
        """将 worker 中断时的快照应用到当前 session（同步）。

        这是强制停止后保证 partial 消息不丢失的核心路径。
        与 _on_messages_updated 的差异：
        1. 不读取/刷新 UI 控件（ring/卡片）—— 可以在 widget 已销毁的 closeEvent 中调用。
        2. 不写入 elapsed 字段（避免重复覆盖 _on_stop_clicked 第一阶段写入的值）。
        3. 不重置 _history_preview_messages / 不触发 GC 等额外副作用。
        4. 仍尊重 truncation_sentinel：若用户/系统在 finalize 期间发生截断，
           则丢弃 worker 的过期快照，避免覆盖新状态。

        Args:
            interrupted_messages: ChatWorker.get_interrupted_messages() 返回的快照。

        Returns:
            True = 已应用到 session；False = 未应用（原因通常是：session 不存在、
            sentinel 命中、或 interrupted_messages 为空）。
        """
        if not interrupted_messages:
            return False
        session = self.session_manager.get_current_session()
        if not session:
            return False

        sentinel = self._truncation_sentinel
        if sentinel and sentinel.get("session_id") == session.session_id:
            from loguru import logger

            logger.warning(
                "[ApplyInterrupted] 检测到截断哨兵，跳过 worker 快照以保护会话状态："
                f"worker_len={len(interrupted_messages)}, current_len={len(session.messages)}, "
                f"session_id={session.session_id[:8]}"
            )
            return False

        # 直接写入 session，不走 _on_messages_updated（避免触发 ring/卡片刷新副作用）
        # preserve_compaction=False：worker 送回的是未压缩消息，保留旧缓存会导致 state 不一致
        session.set_messages(interrupted_messages, preserve_compaction=False)
        return True

    def _on_finalize_complete(self, interrupted_messages: List[Dict[str, Any]]):
        """主线程回调：处理 finalize_stop 异步完成后的消息保存

        由 _interrupt_complete 信号触发。
        """
        if getattr(self, "_is_destroyed", False):
            return

        # 🛡️ 会话切换哨兵：用户在 AI 流式期间切换了项目或新建了会话，
        # finalize_stop 此时才到达，apply_interrupted_messages 会把旧消息写到新会话，
        # save 会落到新项目。直接丢弃整条回调（closeEvent 路径已处理数据持久化）。
        if getattr(self, "_session_switched", False):
            logger.warning(
                f"[FinalizeStop] 检测到会话切换哨兵，丢弃 finalize 回调："
                f"interrupted_count={len(interrupted_messages) if interrupted_messages else 0}"
            )
            return

        current_session = self.session_manager.get_current_session()
        sentinel = self._truncation_sentinel

        # 截断命中：用户在 finalize 期间发生了删除/截断。
        # Worker 送回的快照可能污染已截断的正确状态，必须丢弃。
        # 🛡️ 关键修复：不清空哨兵！让其继续守卫，防止旧 worker 的
        # _on_messages_updated 回调晚到并覆盖已截断的 session。
        # 哨兵将由 _on_messages_updated 识别出新 worker 时清空，
        # 或由下一个 _on_send_clicked 清零。
        if sentinel and current_session and sentinel.get("session_id") == current_session.session_id:
            logger.warning(
                "[FinalizeStop] 检测到截断哨兵，丢弃覆盖以保护会话状态："
                f"worker_len={len(interrupted_messages) if interrupted_messages else 0}, "
                f"current_len={len(current_session.messages)}, "
                f"session_id={current_session.session_id[:8]}"
            )
            self._persist_stop_elapsed()  # 只写 elapsed，不 save
            return

        # 未命中截断：安全清空哨兵，允许后续逻辑正常执行
        self._truncation_sentinel = None

        # 🛡️ 截断后发送标志：用户撤销消息后快速发送了新消息，
        # 旧 worker 的 finalize 回调是过期数据，必须丢弃。
        # 哨兵已在上面被安全清空（未命中截断），或由先到的
        # _on_messages_updated 在识别出新 worker 时已清除。
        #
        # 守护场景：_on_messages_updated 中 sentinel 被其他旧 worker 消耗
        # （非本 finalize 对应的 worker）但 _pending_send_after_truncation 仍为 True，
        # 此时需丢弃本 finalize 的过期数据。
        if self._pending_send_after_truncation:
            self._pending_send_after_truncation = False
            self._pending_send_user_text = None
            if current_session and current_session.messages:
                logger.warning(
                    "[FinalizeStop] 检测到截断后发送标志，丢弃旧 worker 的 finalize 回调："
                    f"worker_len={len(interrupted_messages) if interrupted_messages else 0}, "
                    f"current_len={len(current_session.messages)}"
                )
                self._persist_stop_elapsed()  # 只写 elapsed，不 save
            if self.history_manager:
                self._save_current_session_to_history()
                self.history_manager.flush()
            return

        if interrupted_messages:
            self._apply_interrupted_messages_to_session(interrupted_messages)
            # 🛡️ 中断消息已应用到 session，标记脏以便后续 save 不跳过。
            self._session_dirty = True
            # _apply_interrupted_messages_to_session 不写入 elapsed
            self._persist_stop_elapsed()  # 只写 elapsed，不 save
        else:
            # interrupted_messages 为空（如：closeEvent 路径已同步获取、
            # 或 daemon finalize 过程中并发被另一线程释放）。
            # 这种情况下，worker 已 emit 的 finished_with_messages 在主线程事件队列中，
            # _on_messages_updated 已经被 dispatch 处理并把 partial 写入 session.messages，
            # 但 _on_messages_updated 不触发 _save_current_session_to_history。
            # 所以这里补一次持久化，避免 partial 丢失。
            if current_session and current_session.messages:
                self._persist_stop_elapsed()  # 只写 elapsed，不 save

        # 🔧 统一一次 save+flush：不拆分到 _persist_stop_elapsed 内部 + 外部各一次。
        # 修复前：_persist_stop_elapsed 内部 save，外面又 save → 同 pending_id 跳 3 次。
        if self.history_manager:
            self._save_current_session_to_history()
            self.history_manager.flush()

        # 清理计时相关状态
        self._response_start_time = None
        self._stop_elapsed = None

        # ⚠️ 时间线节点在停止流式后不会更新 - 修复
        self._update_node_preview()
        self._sync_node_preview_to_last()

    def _create_context_menu(self):
        self._context_menu_actions = {}

    # ================================================================
    #  AutoLoop 相关方法
    # ================================================================

    def _show_auto_loop_config(self):
        """显示/隐藏 AutoLoop 配置卡（类似记忆卡片，点击切换）"""
        if self._is_auto_loop_running:
            return
        # 延迟构建（T7）兜底：确保配置卡已创建并注册（800ms 链未跑完时入口先行）
        self._ensure_auto_loop_config_card()
        self._card_manager.toggle_card("auto_loop_config", self._window_id)

    def _on_auto_loop_start(self, config: "AutoLoopConfig"):
        """开始 AutoLoop"""
        if self._is_auto_loop_running:
            return

        # 设置项目路径（工作目录）
        import os

        project_path = config.project_path.strip() if config.project_path else ""
        if not project_path:
            # 优先级：实例缓存（worktree 切换后的目录）→ tool_executor → os.getcwd() 兜底
            project_path = self._current_workdir.get(self._current_project)
            if not project_path and self.backend and self.backend.tool_executor:
                project_path = self.backend.tool_executor.get_workdir()
            if not project_path:
                project_path = os.getcwd()

        abs_path = os.path.abspath(project_path)
        if os.path.isdir(abs_path):
            if self.backend.tool_executor:
                # 通过 ToolExecutor.set_workdir 统一设置，同时更新 builtin_tools
                self.backend.tool_executor.set_workdir(abs_path)
            config.project_path = abs_path
            logger.info(f"[AutoLoop] Workdir set to: {abs_path}")
        else:
            logger.warning(f"[AutoLoop] Project path does not exist: {abs_path}")

        # 隐藏配置卡（通过 CardManager 确保状态同步），显示运行卡
        self._card_manager.hide_card("auto_loop_config", self._window_id)
        # 延迟构建（T7）兜底：确保运行卡已创建（防止 800ms 链未跑完即 start）
        self._ensure_auto_loop_running_card()
        self._auto_loop_running_card.show()
        # 确保停止按钮可见（彻底修复完成后重新运行时停止按钮消失的问题）
        self._auto_loop_running_card.show_stop_button()
        self._auto_loop_running_card.start_animation()
        self._auto_loop_running_card.set_max_tokens(config.max_tokens)
        self._auto_loop_running_card.set_task(config.task_prompt)

        # 锁定 UI
        self._lock_ui_for_autoloop()

        # 获取 auto_loop agent 的工具权限，过滤掉 deny 的工具
        agent_manager = self.backend.agent_manager
        agent = agent_manager.get_agent("auto_loop") if agent_manager else None
        agent_perms = agent.permission if agent else {}
        denied_tools = {name for name, val in agent_perms.items() if val in ("deny", False)}

        from app.tools import get_builtin_tools_schema

        all_tools = get_builtin_tools_schema(
            agent_manager=agent_manager,
            builtin_tools=self.backend.tool_executor._builtin_tools if hasattr(self.backend, "tool_executor") else None,
        )
        tools_schema = [t for t in all_tools if t.get("function", {}).get("name", "") not in denied_tools]

        # 获取 compactor
        compactor = None
        if self.backend.chat_engine and hasattr(self.backend.chat_engine, "_compactor"):
            compactor = self.backend.chat_engine._compactor

        # 创建并启动 worker
        from app.core.workers.auto_loop_worker import AutoLoopWorker

        self._auto_loop_worker = AutoLoopWorker()
        self._auto_loop_worker.configure(
            config=config,
            model_config_getter=self._get_current_model_config,
            tool_executor=self.backend.tool_executor,
            tools_schema=tools_schema,
            agent_system_prompt_getter=lambda name: (
                self.backend.agent_manager.get_agent(name).prompt
                if self.backend.agent_manager and self.backend.agent_manager.get_agent(name)
                else ""
            ),
            permission_check_callback=(
                self.backend.chat_engine._check_tool_permission if self.backend.chat_engine else None
            ),
            permission_cache=(self.backend.chat_engine._permission_cache if self.backend.chat_engine else None),
            compactor=compactor,
        )

        # 连接信号 - 注意：tokens_updated 必须用 DirectConnection 确保实时更新
        self._auto_loop_worker.iteration_started.connect(self._on_auto_loop_iteration_started, Qt.QueuedConnection)
        self._auto_loop_worker.iteration_completed.connect(self._on_auto_loop_iteration_completed, Qt.QueuedConnection)
        self._auto_loop_worker.progress_updated.connect(self._on_auto_loop_progress, Qt.QueuedConnection)
        self._auto_loop_worker.loop_completed.connect(self._on_auto_loop_completed, Qt.QueuedConnection)
        self._auto_loop_worker.loop_error.connect(self._on_auto_loop_error, Qt.QueuedConnection)
        self._auto_loop_worker.loop_stopped.connect(self._on_auto_loop_stopped, Qt.QueuedConnection)
        self._auto_loop_worker.log_signal.connect(self._on_auto_loop_log, Qt.QueuedConnection)
        self._auto_loop_worker.log_update.connect(self._on_auto_loop_log_update, Qt.QueuedConnection)
        self._auto_loop_worker.phase_changed.connect(self._on_auto_loop_phase_changed, Qt.QueuedConnection)
        # tokens_updated 使用 QueuedConnection 确保 UI 更新在主线程执行（避免 DirectConnection 在 worker 线程执行导致 UI 无法更新）
        self._auto_loop_worker.tokens_updated.connect(self._on_auto_loop_tokens_updated, Qt.QueuedConnection)

        self._is_auto_loop_running = True
        self._auto_loop_worker.start()

    def _on_auto_loop_archive(self):
        """用户点击归档按钮 — 直接跳转到归档阶段

        通知 Worker 进入归档阶段，Worker 会在下一轮循环中执行自动归档。
        """
        if not self._auto_loop_worker or not self._auto_loop_worker.isRunning():
            return
        if self._auto_loop_running_card:
            self._auto_loop_running_card.set_phase("archiving")
            self._auto_loop_running_card.set_status("📦 正在归档...")
            self._auto_loop_running_card.hide_archive_button()
            self._auto_loop_running_card.hide_stop_button()
        self._auto_loop_worker.request_archive()

    def _on_auto_loop_stop(self):
        """停止 AutoLoop（用户主动停止）

        不再阻塞 UI 线程！通过 loop_stopped 信号异步处理清理。
        只在 looper 线程退出后(通过信号)才执行 _finish_auto_loop，
        避免 UI 卡死和二次清理导致的闪退。
        """
        if self._auto_loop_worker and self._auto_loop_worker.isRunning():
            # 1. 立即发送取消信号给 worker 线程
            self._auto_loop_worker.cancel()
            # 2. UI 立即反馈，不阻塞
            if self._auto_loop_running_card:
                self._auto_loop_running_card.set_status("⏹ 正在停止...")
            # 3. 安全兜底：5 秒后如果还没停，强制清理（避免永久卡住）
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(5000, self._force_cleanup_autoloop)

    def _force_cleanup_autoloop(self):
        """兜底清理：如果 worker 线程未正常结束，强制清理"""
        if not self._is_auto_loop_running:
            return  # 已经通过信号正常清理了
        logger.warning("[AutoLoop] Force cleanup after timeout")
        if self._auto_loop_worker and self._auto_loop_worker.isRunning():
            self._auto_loop_worker.wait(2000)
        self._finish_auto_loop("⏹ 强制停止（超时）")

    def _on_auto_loop_phase_changed(self, phase: str):
        """AutoLoop 阶段变更"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.set_phase(phase)

    def _on_auto_loop_iteration_started(self, current: int, total: int):
        """迭代开始"""
        if self._auto_loop_running_card:
            progress = self._auto_loop_worker.get_current_progress()
            phase = progress.get("phase", "planning")
            current_step = progress.get("current_step", 0)
            total_steps = progress.get("total_steps", 0)

            if phase == "planning":
                # 规划阶段
                self._auto_loop_running_card.set_phase("planning")
                self._auto_loop_running_card.set_status(f"📋 第 {current} 轮: 规划中...")
            else:
                # 执行阶段
                self._auto_loop_running_card.set_phase("executing")
                # 显示当前步骤在状态文本中
                if total_steps > 0:
                    self._auto_loop_running_card.set_status(
                        f"▶ 第 {current} 轮 / 共 {total} 轮 | 步骤 {current_step}/{total_steps}"
                    )
                else:
                    self._auto_loop_running_card.set_status(f"▶ 第 {current} 轮 / 共 {total} 轮")

    def _on_auto_loop_iteration_completed(self, iteration: int, summary: str):
        """迭代完成"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.append_log(f"第 {iteration} 轮完成: {summary[:40]}")

    def _on_auto_loop_log(self, text: str):
        """离散日志更新（带时间戳的事件）"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.append_log(text)

    def _on_auto_loop_log_update(self, text: str):
        """流式日志更新（实时覆盖，无时间戳）"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.update_log(text)

    def _on_auto_loop_tokens_updated(self, total_tokens: int):
        """Token 实时更新 — 直接使用信号携带的值"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.update_tokens(total_tokens)

    def _on_auto_loop_progress(self, progress: dict):
        """更新运行卡进度（不更新 token，因为 update_tokens() 会专门处理）

        注意：token 更新由 update_tokens() 专门处理，避免与 progress_updated 信号的竞争条件
        导致 token 显示被覆盖的问题。
        """
        if self._auto_loop_running_card:
            self._auto_loop_running_card.update_progress_no_token(progress)

    def _on_auto_loop_completed(self, message: str):
        """AutoLoop 完成"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.set_phase("completed")
            self._auto_loop_running_card.show_completed(message)
        self._finish_auto_loop(message)

    def _on_auto_loop_error(self, message: str):
        """AutoLoop 出错"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.show_error(message)
            self._auto_loop_running_card.append_log(f"❌ {message[:50]}")
        self._finish_auto_loop(f"❌ {message}")

    def _on_auto_loop_stopped(self):
        """AutoLoop 已停止"""
        self._finish_auto_loop("⏹ 已停止")

    def _finish_auto_loop(self, message: str):
        """清理 AutoLoop 状态

        注意：可能被多个路径调用（loop_completed/loop_error/loop_stopped 信号 + 兜底定时器），
        必须在顶部做防重入保护。
        """
        if not self._is_auto_loop_running:
            # 防止二次清理导致 worker.deleteLater 冲突和闪退
            return
        self._is_auto_loop_running = False

        # 恢复为当前项目配置的工作目录
        self._sync_working_directory()

        # 停止动画（只调用一次，移除重复调用）
        if self._auto_loop_running_card:
            self._auto_loop_running_card.stop_animation()

        # 隐藏运行卡
        self._auto_loop_running_card.hide()
        self._restore_after_system_close()

        # 保存 AutoLoop 消息到会话历史
        if self._auto_loop_worker:
            try:
                messages = self._auto_loop_worker.get_all_messages()
                if messages:
                    self._save_auto_loop_messages_to_session(messages)
            except Exception as e:
                logger.warning(f"[AutoLoop] Failed to save messages to session: {e}")

        # 清理 worker
        if self._auto_loop_worker:
            try:
                self._auto_loop_worker.quit()
                self._auto_loop_worker.wait(1000)
            except Exception:
                pass
            self._auto_loop_worker.deleteLater()
            self._auto_loop_worker = None

        # 解锁 UI
        self._unlock_ui_after_autoloop()

        # 通知用户
        InfoBar.success(
            "AutoLoop",
            message,
            parent=TabManagerWindow.get_instance() or self.window(),
            duration=5000,
            position=InfoBarPosition.BOTTOM,
        )

    def _lock_ui_for_autoloop(self):
        """锁定 UI — 隐藏消息列表和输入框，禁止新建会话"""
        # 清空输入框内容
        self.input_area.clear()
        # 隐藏消息列表（保持滚动位置不变）
        self.chat_scroll_area.setVisible(False)
        # 隐藏输入容器 + 工具栏
        self._bottom_input_container.setVisible(False)
        if hasattr(self, "_bottom_toolbar_strip"):
            self._bottom_toolbar_strip.setVisible(False)
        # 隐藏输入框的发光控件
        if hasattr(self, "_input_glow_underlay"):
            self._input_glow_underlay.setVisible(False)
        # 禁用新建按钮
        self.new_session_btn.setDisabled(True)

        # 窗口自适应缩小（聊天区和输入框隐藏后只保留运行卡片）
        self.adjustSize()

        # 记录原有状态，用于解锁
        logger.info("[AutoLoop] UI locked")

    def _unlock_ui_after_autoloop(self):
        """解锁 UI — 恢复消息列表和输入框"""
        # 恢复消息列表
        self.chat_scroll_area.setVisible(True)
        # 恢复输入容器 + 工具栏
        self._bottom_input_container.setVisible(True)
        if hasattr(self, "_bottom_toolbar_strip"):
            self._bottom_toolbar_strip.setVisible(True)
        # 恢复输入框的发光控件
        if hasattr(self, "_input_glow_underlay"):
            self._input_glow_underlay.setVisible(True)
        # 启用新建按钮
        self.new_session_btn.setDisabled(False)

        # 重新聚焦输入框（仅当此窗口为活动 Tab 时）
        self._focus_input_if_active()
        logger.info("[AutoLoop] UI unlocked")
        logger.info("[AutoLoop] UI unlocked")

    def _save_auto_loop_messages_to_session(self, messages: List[Dict]):
        """将 AutoLoop 执行的消息保存到当前会话"""
        session = self.session_manager.get_current_session()
        if not session:
            return

        # messages 来自 AutoLoopWorker.get_all_messages()，
        # 包含 on_messages_updated 收集的完整消息（含 user + assistant + tool_calls）
        auto_loop_messages = list(messages or [])

        # 确保 user 消息存在（第一条 user 消息，排除 hook 消息）
        # 🛡️ R7 修复：team 邮件（_hook_event="TeamMail"）视为真实用户问题
        # → mail-only AutoLoop 会话避免 task_prompt 重复插入
        has_user = any(
            msg.get("role") == "user" and (not msg.get("_hook_event") or msg.get("_hook_event") == "TeamMail")
            for msg in auto_loop_messages
        )
        if not has_user and self._auto_loop_worker:
            task_prompt = self._auto_loop_worker.get_task_prompt()
            if task_prompt:
                from datetime import datetime

                user_msg = {
                    "role": "user",
                    "content": task_prompt,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                auto_loop_messages.insert(0, user_msg)

        # 更新会话（preserve_compaction=True 避免压缩状态被破坏）
        session.set_messages(auto_loop_messages, preserve_compaction=True)
        self._session_dirty = True

        logger.info(f"[AutoLoop] 保存 {len(auto_loop_messages)} 条消息到会话: {self._current_project}")

        # 触发 topic_summary 生成标题（如果还没有标题）
        session = self.session_manager.get_current_session()
        if session and not session.topic_summary:
            self._maybe_generate_topic_summary()

        # 同步保存到历史记录
        self._save_current_session_to_history()


def _is_sip_deleted(obj) -> bool:
    """判断 PyQt 对象是否已被 C++ 侧销毁（防御 wrapped C/C++ object has been deleted）。

    destroyed 信号在 C++ 对象真正销毁时触发，此时 Python 侧的 self 仍存在但
    C++ 包装已失效，访问 self 的任何 Qt 属性都会抛 RuntimeError。
    用于 destroyed 回调 / 清理路径的入口守卫，静默返回 False 兜底。
    """
    try:
        return sip.isdeleted(obj)
    except Exception:
        return False


# GC 钩子（T9）：模块级防抖标志，150ms 内多次触发只执行一次。
_gc_hook_pending = False


# ── B4 温和层：WebEngine 并发页上限（T12 蓝图） ──
# 每渲染卡 ~64MB renderer 进程，长对话必须锁峰值：
# 温和层：并发页 ≤ _MAX_RENDERED_CARDS（18 = 可视 12 批 + 上下 6 批缓冲）。
# 强回收层（kill 离屏 renderer）依赖 message_card 的 renderer_pid 记录，暂缓。
_MAX_RENDERED_CARDS = 18
_global_rendered_pages: int = 0  # 跨窗口观测计数（日志用，非硬约束）

# ── B4 强回收层：内存超阈值时 kill 离屏 renderer 进程（T13 蓝图 / T30 双判据） ──
# 双判据：主进程 RSS 超总阈值，且 WebEngine 子进程 RSS 超子阈值才触发强回收——
# 避免仅主进程内存高（如 Python 堆）时误杀 renderer。
_MEM_THRESHOLD_TOTAL_MB = 900  # 总 RSS 阈值触发强回收（1800→900：修复 renderer 永不回收）
_WEB_MEM_THRESHOLD_MB = 300  # WebEngine 子进程 RSS 阈值（待窗口期回填校准）
_LRU_RENDERER_KEEP = 8  # 强回收后保留最近活跃 renderer 数
_KILL_COOLDOWN_S = 60  # kill 冷却（防抖动）
_KILL_BATCH_MAX = 12  # 每轮最多 kill
_OFFSCREEN_BATCHES_FOR_KILL = 8  # 距可视区 ≥8 批才可 kill（严格离屏护栏）


def _run_gc_hook():
    """GC 钩子执行体：清理全局渲染缓存 + 回收进程堆。

    由 _schedule_gc_hook 防抖合并后调用（150ms singleShot），
    全 try/except 吞异常，不影响主流程。
    """
    global _gc_hook_pending
    _gc_hook_pending = False
    try:
        from app.widgets.message_card import clear_global_render_cache

        clear_global_render_cache()
    except Exception:
        pass
    try:
        _compact_process_heap_after_cleanup()
    except Exception:
        pass


def _cleanup_global_lru_caches():
    """清理全局 LRU 缓存，释放旧会话渲染/估算占用的内存。

    在新建会话、切换会话时调用，避免缓存的 HTML 渲染结果和 token 估算值累积。
    """
    try:
        from app.widgets.message_card import clear_global_render_cache

        clear_global_render_cache()
    except Exception:
        pass
    try:
        from app.core.token_estimator import estimate_tokens

        estimate_tokens.cache_clear()
    except Exception:
        pass
    try:
        from app.widgets.message_card import _render_tool_block_content

        _render_tool_block_content.cache_clear()
    except Exception:
        pass


def _compact_process_heap_after_cleanup():
    """卡片清理后触发 gc，回收 Python 对象图（T11：移除失效的 HeapCompact/malloc_trim）。

    T10 实测：Python 3.14 下 ctypes.WinDLL("kernel32").HeapCompact 100% 抛
    access violation（被 except 吞掉），主线程高频路径（新建/切换/恢复会话、
    切换项目、undo）上制造无效异常开销；Linux malloc_trim 收益同样有限。
    故移除两者，保留 gc.collect() —— pymalloc arena 的归还由 CPython
    内存管理自行处理，gc 收集足够。
    """
    gc.collect()
