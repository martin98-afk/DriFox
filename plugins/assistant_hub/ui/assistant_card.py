# -*- coding: utf-8 -*-
"""assistant_card.py — 助手中心主卡片（full 容器浮动卡）

布局（参考 OpenHanako AgentCardStack / DriFox 设置卡）：

    ┌──────────────────────────────────────────────────────────────┐
    │  🤖 助手中心   [新建助手]   N 个 · 主助手: xxx                │ 头部
    ├───────────────┬──────────────────────────────────────────────┤
    │ 助手列表       │  头像 + 名称 + 副标题 + 激活按钮              │
    │ [avatar] 名称 │  ┌ 身份 │ 提示词 │ 对外 │ 头像 │ 记忆 │ 技能 ┐ │
    │ [avatar] 名称 │  └───────────────────────────────────────────┘ │
    │ [avatar] 名称 │  编辑器内容                                     │
    └───────────────┴──────────────────────────────────────────────┘

- 左侧列表：每个助手一个头像 + 名称行；主助手带 ★；激活项高亮。
- 右侧编辑器：Tab 切换（身份/提示词/对外人格/头像/记忆/技能）。
- 记忆 Tab 内置 Dream 一键整理 + 自动开关。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import InfoBar, InfoBarPosition

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css

from ..assistant_manager import Assistant, AssistantManager
from .assistant_avatar import RoundAvatar
from .avatar_picker import AvatarPicker
from .editor_tabs import (
    AvatarTab,
    IdentityTab,
    MemoryTab,
    PromptTab,
    PublicTab,
    SkillsTab,
)


class _AssistantListWidget(QFrame):
    """左侧助手列表：头像 + 名称 + ★ 主助手标记"""

    assistantSelected = pyqtSignal(str)  # assistant_id
    assistantCreated = pyqtSignal()
    assistantDeleted = pyqtSignal(str)  # assistant_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mgr = AssistantManager.get_instance()
        self._active_aid: str = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("助手", self)
        title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(13)}; font-weight: 600;"
        )
        header.addWidget(title)
        header.addStretch()
        new_btn = QPushButton("＋", self)
        new_btn.setFixedSize(26, 26)
        new_btn.setToolTip("新建助手")
        new_btn.clicked.connect(self._on_create)
        header.addWidget(new_btn)
        v.addLayout(header)

        self._list = QListWidget(self)
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setStyleSheet(
            f"""
            QListWidget {{
                background: transparent;
                border: none;
                {get_font_family_css()} {font_size_css(12)}
            }}
            QListWidget::item {{
                padding: 2px 4px;
                border-radius: 6px;
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget::item:hover {{ background: {Colors.HOVER_BG}; }}
            QListWidget::item:selected {{ background: {Colors.SELECTED_BG}; }}
        """
        )
        self._list.currentItemChanged.connect(self._on_current_changed)
        v.addWidget(self._list, 1)

    def refresh(self, select_aid: str = "") -> None:
        self._list.clear()
        assistants = self._mgr.list_assistants()
        for a in assistants:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, a.id)
            self._list.addItem(item)
        if select_aid or self._active_aid:
            target = select_aid or self._active_aid
            for i in range(self._list.count()):
                it = self._list.item(i)
                if it.data(Qt.UserRole) == target:
                    self._list.setCurrentRow(i)
                    break

    def set_active(self, aid: str) -> None:
        self._active_aid = aid
        self.refresh(select_aid=aid)

    def _on_current_changed(self, current: Optional[QListWidgetItem], _prev) -> None:
        if current is None:
            return
        aid = current.data(Qt.UserRole)
        if aid:
            self._active_aid = aid
            self.assistantSelected.emit(aid)

    def _on_create(self) -> None:
        self.assistantCreated.emit()


class AssistantCardWidget(QWidget):
    """助手中心主卡片（full 容器）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mgr = AssistantManager.get_instance()
        self._active_aid: str = ""
        self._build_ui()
        self._reload_all()

    # ── UI 构建 ──────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # 头部
        head = QHBoxLayout()
        self._head_icon = QLabel("🤖", self)
        self._head_title = QLabel("助手中心", self)
        self._head_title.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; {get_font_family_css()} {font_size_css(14)}; font-weight: 700;"
        )
        self._head_count = QLabel("", self)
        self._head_count.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}"
        )
        head.addWidget(self._head_icon)
        head.addWidget(self._head_title)
        head.addWidget(self._head_count)
        head.addStretch()
        self._activate_btn = QPushButton("设为当前助手", self)
        self._activate_btn.setFixedHeight(30)
        self._activate_btn.clicked.connect(self._on_activate)
        head.addWidget(self._activate_btn)
        self._create_btn = QPushButton("新建助手", self)
        self._create_btn.setFixedHeight(30)
        self._create_btn.clicked.connect(self._on_create)
        head.addWidget(self._create_btn)
        outer.addLayout(head)

        # 主体：左列表 + 右编辑器
        splitter = QSplitter(Qt.Horizontal, self)
        self._list_widget = _AssistantListWidget(splitter)
        self._list_widget.assistantSelected.connect(self._on_select)
        self._list_widget.assistantCreated.connect(self._on_create)
        splitter.addWidget(self._list_widget)

        self._editor = self._build_editor(splitter)
        splitter.addWidget(self._editor)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 760])
        outer.addWidget(splitter, 1)

    def _build_editor(self, parent) -> QWidget:
        wrap = QFrame(parent)
        wrap.setStyleSheet(
            f"""
            QFrame {{
                background: {Colors.CARD_BG.format(alpha=180)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """
        )
        v = QVBoxLayout(wrap)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        # 当前助手标题行
        self._editor_header = QHBoxLayout()
        self._editor_avatar = RoundAvatar(size=40, text="?", color="#7C3AED", parent=wrap)
        self._editor_header.addWidget(self._editor_avatar)
        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        self._editor_name = QLabel("(未选择)", wrap)
        self._editor_name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(13)}; font-weight: 600;"
        )
        self._editor_desc = QLabel("", wrap)
        self._editor_desc.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}"
        )
        name_col.addWidget(self._editor_name)
        name_col.addWidget(self._editor_desc)
        self._editor_header.addLayout(name_col, 1)
        # 主助手标记
        self._primary_badge = QLabel("★ 主助手", wrap)
        self._primary_badge.setStyleSheet(
            f"color: {Colors.BORDER_ACCENT}; {get_font_family_css()} {font_size_css(11)};"
        )
        self._primary_badge.hide()
        self._editor_header.addWidget(self._primary_badge)
        v.addLayout(self._editor_header)

        # Tab
        self._tabs = QTabWidget(wrap)
        self._tabs.setDocumentMode(True)
        self._tab_identity = IdentityTab(wrap)
        self._tab_prompt = PromptTab(wrap)
        self._tab_public = PublicTab(wrap)
        self._tab_avatar = AvatarTab(wrap)
        self._tab_memory = MemoryTab(wrap)
        self._tab_skills = SkillsTab(wrap)
        self._tabs.addTab(self._tab_identity, "身份")
        self._tabs.addTab(self._tab_prompt, "提示词")
        self._tabs.addTab(self._tab_public, "对外")
        self._tabs.addTab(self._tab_avatar, "头像")
        self._tabs.addTab(self._tab_memory, "记忆")
        self._tabs.addTab(self._tab_skills, "技能")
        v.addWidget(self._tabs, 1)

        # 底部删除行
        bottom = QHBoxLayout()
        bottom.addStretch()
        self._delete_btn = QPushButton("删除此助手", wrap)
        self._delete_btn.setFixedHeight(28)
        self._delete_btn.setStyleSheet(
            f"""
            QPushButton {{
                color: {Colors.ERROR};
                border: 1px solid {Colors.ERROR};
                border-radius: 6px;
                padding: 3px 14px;
                background: transparent;
                {get_font_family_css()} {font_size_css(12)}
            }}
            QPushButton:hover {{ background: rgba(250, 81, 81, 0.1); }}
        """
        )
        self._delete_btn.clicked.connect(self._on_delete)
        bottom.addWidget(self._delete_btn)
        v.addLayout(bottom)
        return wrap

    # ── 数据刷新 ─────────────────────────────────────────

    def _reload_all(self, select_aid: str = "") -> None:
        assistants = self._mgr.list_assistants()
        self._head_count.setText(f"{len(assistants)} 个")
        if not assistants:
            self._show_empty()
            self._list_widget.refresh()
            return
        self._list_widget.refresh(select_aid=select_aid)
        if select_aid:
            self._bind_editor(select_aid)
        elif self._active_aid and self._mgr.has(self._active_aid):
            self._bind_editor(self._active_aid)
        else:
            first = assistants[0].id
            self._bind_editor(first)

    def _show_empty(self) -> None:
        self._active_aid = ""
        self._editor_avatar.set_text("?")
        self._editor_name.setText("(无助手)")
        self._editor_desc.setText("点击「新建助手」创建第一个")
        self._primary_badge.hide()
        self._activate_btn.setEnabled(False)
        self._delete_btn.setEnabled(False)

    def _bind_editor(self, aid: str) -> None:
        self._active_aid = aid
        a = self._mgr.get(aid)
        if not a:
            return
        ap = self._mgr.avatar_path(aid)
        self._editor_avatar.set_text(a.name or a.id)
        self._editor_avatar.set_color(a.color)
        self._editor_avatar.set_image(str(ap) if ap else None)
        self._editor_name.setText(a.name or a.id)
        self._editor_desc.setText(
            f"id: {a.id} · yuan: {a.yuan}" + (" · 记忆开" if a.memory_enabled else " · 记忆关")
        )
        self._primary_badge.setVisible(a.primary)
        self._activate_btn.setEnabled(True)
        self._delete_btn.setEnabled(True)

        # Tab 绑定（节流到下一帧，避免编辑器 setText 触发保存回调误写新助手）
        aid_capture = aid

        def _do_bind() -> None:
            self._tab_identity.bind(self._mgr, aid_capture)
            self._tab_prompt.bind(self._mgr, aid_capture)
            self._tab_public.bind(self._mgr, aid_capture)
            self._tab_avatar.bind(self._mgr, aid_capture)
            self._tab_memory.bind(aid_capture)
            self._tab_skills.bind(aid_capture)

        QTimer.singleShot(0, _do_bind)

    # ── 信号 ─────────────────────────────────────────────

    def _on_select(self, aid: str) -> None:
        if aid and self._mgr.has(aid):
            self._bind_editor(aid)

    def _on_create(self) -> None:
        from .rename_dialog import RenameDialog

        dlg = RenameDialog(
            title="新建助手",
            hint="输入助手名称（例如：小助手 / 翻译官 / 代码审查员）：",
            default="",
            parent=self.window(),
        )
        dlg.confirmed.connect(self._do_create)
        dlg.exec_()

    def _do_create(self, name: str) -> None:
        a = self._mgr.create(name=name)
        self._reload_all(select_aid=a.id)
        self._notify(f"助手「{a.name}」已创建")

    def _on_activate(self) -> None:
        if not self._active_aid:
            return
        if self._mgr.set_active(self._active_aid):
            self._notify(f"已将「{self._active_aid}」设为当前助手")
            self._list_widget.set_active(self._active_aid)

    def _on_delete(self) -> None:
        if not self._active_aid:
            return
        a = self._mgr.get(self._active_aid)
        if not a:
            return
        ret = QMessageBox.question(
            self,
            "删除助手",
            f"确定删除助手「{a.name}」？\n其身份、提示词、记忆、头像将一并删除。\n（该操作不可撤销）",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        if self._mgr.delete(a.id):
            self._active_aid = ""
            self._reload_all()
            self._notify(f"助手「{a.name}」已删除")
        else:
            self._notify_error("至少保留一个助手，无法删除")

    # ── 通知 ─────────────────────────────────────────────

    def _notify(self, text: str) -> None:
        try:
            InfoBar.success(
                title="助手中心",
                content=text,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2500,
                parent=self.window(),
            )
        except Exception:
            pass

    def _notify_error(self, text: str) -> None:
        try:
            InfoBar.error(
                title="助手中心",
                content=text,
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=3000,
                parent=self.window(),
            )
        except Exception:
            pass

    # ── 外部入口 ─────────────────────────────────────────

    def refresh_style(self) -> None:
        """主题切换后刷新（宿主契约）"""
        Colors.refresh()
        self._reload_all(select_aid=self._active_aid)
