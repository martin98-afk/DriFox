# -*- coding: utf-8 -*-
"""工作树页 —— git 工作树管理（主仓库卡片 + 子工作树列表）

page_id="worktree-manager"（与插件同名）：注册它即填充右侧工作台的「工作树」页。
插件卸载后显示占位页。

历史：本页原先还承载「关键文档列表」（拖拽文件/文件夹/URL，设为工作目录）。
该功能已下线，工作目录改由 project_workdir 表记录，页面只做工作树管理。

宿主契约：
- set_project(project, workdir=None)：接收当前项目与工作目录并刷新
- refresh_data()：数据自拉入口（宿主 refresh_current_page_data 调用）
- refresh_style()：主题刷新
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from loguru import logger

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel

from app.plugins.sdk import Colors, font_size_css
from app.plugins.sdk import GitWorktreeDetector
from app.plugins.sdk import get_font_family_css
from .worktree_section import WorktreeSectionWidget


def _caps(cap_id):
    """取某能力（无 ctx 场景统一走 sdk 的 caps_for，host 由宿主解析）。"""
    from app.plugins.sdk import caps_for

    return caps_for(cap_id)


class SystemWorktreePage(QWidget):
    """工作树页：主仓库卡片 + 子工作树列表"""

    workingDirChanged = pyqtSignal(str)  # 工作目录路径，空字符串=清除

    def __init__(self, parent=None, context: Optional[Dict[str, object]] = None):
        super().__init__(parent)
        self._context = context or {}
        self._memory_manager = None
        self._current_project = "默认项目"
        # 多窗口隔离：实例级工作目录缓存（{project: workdir_path}）
        self._instance_workdir: Dict[str, str] = {}
        self._section: WorktreeSectionWidget = None
        self._body: QWidget = None
        self._empty_hint: QLabel = None
        self._repo_info = None
        self._connect_change_bridge()
        self._init_ui()

    # ── 宿主/插件的连接 ──

    def _connect_change_bridge(self) -> None:
        """连接工具变更信号桥（emit 在后台线程，本槽在主线程执行）"""
        try:
            from .change_bridge import get_bridge

            get_bridge().changed.connect(self._on_tool_worktree_changed)
        except Exception as e:
            logger.warning(f"[worktree-manager] 工具变更信号桥连接失败（工具操作后不自动刷新）: {e}")

    def _on_tool_worktree_changed(self, summary: str) -> None:
        logger.info(f"[worktree-manager] 工具操作工作树，刷新页面: {summary}")
        self.refresh_data()

    def _get_memory_manager(self):
        """从 context 的 backend 获取 memory_manager（宿主未就绪时返回 None）"""
        if self._memory_manager:
            return self._memory_manager
        backend = self._context.get("backend")
        mgr = getattr(backend, "memory_manager", None)
        if mgr is not None:
            self._memory_manager = mgr
        return mgr

    def _emit_working_dir(self, path: str) -> None:
        """工作目录变更：信号 + context 回调双通道（宿主转发给活跃窗口）"""
        self.workingDirChanged.emit(path)
        cb = self._context.get("working_dir_changed_callback")
        if callable(cb):
            try:
                cb(path)
            except Exception:
                pass

    # ── 数据 ──

    def set_project(self, project: str, workdir: Optional[str] = None):
        """设置当前项目与工作目录并刷新"""
        if workdir is not None:
            self._instance_workdir[project] = workdir or ""
        self._current_project = project or "默认项目"
        self._rebuild()

    def refresh_data(self) -> None:
        """数据自拉入口（工作台通用页协议）"""
        project = ""
        workdir: Optional[str] = None
        try:
            snap = _caps("window.current")
            if snap is not None:
                snap = snap()
            if snap:
                project = snap.get("project") or ""
                workdir = snap.get("workdir")
        except Exception:
            pass
        self.set_project(project, workdir)

    def _get_effective_workdir(self, project: str) -> Optional[str]:
        """有效工作目录（实例缓存优先，回退 DB）"""
        workdir = self._instance_workdir.get(project)
        if workdir is not None:
            return workdir or None
        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            db_workdir = memory_mgr.get_working_directory(project)
            if db_workdir:
                self._instance_workdir[project] = db_workdir
            return db_workdir
        return None

    # ── UI ──

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 页头：标题 + 仓库路径
        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 10, 12, 6)
        header_layout.setSpacing(8)

        self._title_label = BodyLabel("工作树", header)
        Colors.refresh()
        self._title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(14)}"
        )
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()
        root.addWidget(header)

        # 内容容器
        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(12, 0, 12, 12)
        self._body_layout.setSpacing(8)
        root.addWidget(self._body, 1)

        # 空态提示（非 git 仓库 / 无工作目录）
        self._empty_hint = BodyLabel("", self)
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setStyleSheet(
            f"background: transparent; color: {Colors.TEXT_MUTED};"
            f" {get_font_family_css()} {font_size_css(13)} padding: 24px;"
        )
        self._empty_hint.setVisible(False)
        self._body_layout.addWidget(self._empty_hint, 1)

        self._section = None

    def _clear_body(self):
        if self._section is not None:
            self._section.hide()
            self._section.deleteLater()
            self._section = None
        self._empty_hint.setVisible(False)

    def _rebuild(self):
        """按当前项目/工作目录重建内容"""
        self._clear_body()

        project = self._current_project
        workdir = self._get_effective_workdir(project)

        if not workdir:
            self._show_empty(
                "还没有设置工作目录。\n"
                "在左侧项目面板选择一个文件夹作为项目根，或直接新建项目。"
            )
            return

        if not os.path.isdir(workdir):
            self._show_empty(f"工作目录不存在：\n{workdir}\n\n请重新指定项目根目录。")
            return

        git_root = GitWorktreeDetector.detect_git(workdir)
        if not git_root:
            self._show_empty(f"当前工作目录不是 git 仓库：\n{workdir}\n\n工作树管理仅支持 git 项目。")
            return

        repo_info = GitWorktreeDetector.get_repo_info(git_root)
        if repo_info is None or not repo_info.worktrees:
            self._show_empty(f"读取仓库信息失败：\n{git_root}")
            return

        self._repo_info = repo_info
        section = WorktreeSectionWidget(
            repo_info,
            original_folder=git_root,
            parent=self._body,
            current_workdir=workdir,
            project=project,
        )
        section.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        section.worktreeSwitched.connect(self._on_worktree_switched)
        section.workingDirRestored.connect(self._on_workdir_restored)
        section.worktreeDeleted.connect(lambda _p: self.refresh_data())
        self._body_layout.insertWidget(0, section, 1)
        self._section = section

    def _show_empty(self, text: str):
        self._empty_hint.setText(text)
        self._empty_hint.setVisible(True)

    # ── 交互 ──

    def _on_worktree_switched(self, original_folder: str, worktree_path: str):
        """切到指定工作树/主仓库：持久化 + 上抛宿主同步"""
        if not worktree_path or not os.path.isdir(worktree_path):
            return
        project = self._current_project
        memory_mgr = self._get_memory_manager()
        if memory_mgr is not None:
            try:
                memory_mgr.set_working_directory(project, worktree_path)
            except Exception as e:
                logger.warning(f"[worktree-manager] 持久化工作目录失败: {e}")
        self._instance_workdir[project] = worktree_path
        self._emit_working_dir(worktree_path)
        self.refresh_data()

    def _on_workdir_restored(self, path: str):
        """工作目录被外部删除后回退（不重建工作树）"""
        self._instance_workdir[self._current_project] = path
        self._emit_working_dir(path)

    def refresh_style(self):
        """主题刷新：重建整页（内联样式在构造时写死）"""
        Colors.refresh()
        self._title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(14)}"
        )
        self._empty_hint.setStyleSheet(
            f"background: transparent; color: {Colors.TEXT_MUTED};"
            f" {get_font_family_css()} {font_size_css(13)} padding: 24px;"
        )
        self._rebuild()

    def refresh(self):
        """外部刷新入口（与 refresh_data 同义）"""
        self.refresh_data()

    def refresh_from_db(self):
        """强制从 DB 重读工作目录后刷新"""
        self._instance_workdir.pop(self._current_project, None)
        self.refresh_data()
