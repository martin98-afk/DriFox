# -*- coding: utf-8 -*-
"""
Worktree 树状展示组件

紧贴文件夹条目下方，左侧竖线 + 圆点标识分支
主分支也有切换按钮，当前分支高亮
"""

import os
import subprocess
import sys

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QSizePolicy, QDialog,
    QLineEdit,
)
from loguru import logger

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css
from app.utils.git_worktree import GitWorktreeDetector

# Windows 下隐藏 cmd 窗口
_CREATION_FLAGS = 0
if sys.platform == "win32":
    _CREATION_FLAGS = subprocess.CREATE_NO_WINDOW


class _WorktreeRow(QWidget):
    """单行 worktree 分支"""

    switched = pyqtSignal(str)   # worktree_path
    deleted = pyqtSignal(str)    # worktree_path

    def __init__(self, branch: str, wt_path: str, is_main: bool,
                 is_current: bool, is_prunable: bool, parent=None):
        super().__init__(parent)
        self._branch = branch
        self._wt_path = wt_path
        self._is_main = is_main
        self._is_current = is_current
        self._is_prunable = is_prunable
        self.setFixedHeight(24)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(4)

        # 左侧竖线
        bar = QFrame(self)
        bar.setFixedWidth(1)
        bar.setStyleSheet(f"background-color: rgba(255,255,255,0.06);")
        layout.addWidget(bar)

        # 圆点
        dot = QLabel("", self)
        dot.setFixedSize(6, 6)
        if self._is_current:
            dot.setStyleSheet(
                "background-color: #58a6ff; border-radius: 3px;"
            )
        else:
            dot.setStyleSheet(
                "background: transparent; border: 1.5px solid #484f58; border-radius: 3px;"
            )
        layout.addWidget(dot)

        # 分支名
        if self._is_current:
            branch_ss = f"color: #e6edf3; font-weight: 600; {get_font_family_css()} {font_size_css(12)}"
        else:
            branch_ss = f"color: #8b949e; {get_font_family_css()} {font_size_css(12)}"

        branch_label = QLabel(self._branch, self)
        branch_label.setStyleSheet(branch_ss)
        layout.addWidget(branch_label)

        # 标签（小圆角 badge）
        if self._is_main:
            tag = QLabel("main", self)
            tag.setStyleSheet(
                f"color: rgba(255,255,255,0.35); {font_size_css(8)};"
                f"background: rgba(255,255,255,0.06);"
                f"padding: 0 4px; border-radius: 2px;"
            )
            layout.addWidget(tag)

        layout.addStretch()

        # 切换（非当前分支显示）
        if not self._is_current:
            btn = QLabel("切换", self)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"color: #8b949e; {get_font_family_css()} {font_size_css(10)};"
                f"padding: 0 4px;"
            )
            # 用 QLabel 模拟 hover 需要重写 enterEvent
            btn.mousePressEvent = lambda e: self.switched.emit(self._wt_path)
            layout.addWidget(btn)

        # 删除（仅非主仓库可删）
        if not self._is_main:
            del_btn = QLabel("✕", self)
            del_btn.setFixedWidth(14)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setToolTip("删除 worktree")
            del_btn.setStyleSheet(
                f"color: #8b949e; {get_font_family_css()} {font_size_css(10)};"
            )
            del_btn.mousePressEvent = lambda e: self._confirm_delete()
            layout.addWidget(del_btn)

    def _confirm_delete(self):
        """确认删除 worktree + 自动删除分支"""
        dlg = QDialog(self)
        dlg.setWindowTitle("删除 Worktree")
        dlg.setFixedSize(380, 170)
        dlg.setStyleSheet(f"""
            QDialog {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(12)}
            }}
            QPushButton {{
                border: none; border-radius: 4px; padding: 6px 20px;
                {get_font_family_css()} {font_size_css(12)} min-width: 60px;
            }}
            QPushButton#okBtn {{
                background-color: {Colors.ERROR};
                color: white;
            }}
            QPushButton#okBtn:hover {{ opacity: 0.8; }}
            QPushButton#cancelBtn {{
                background: transparent; color: {Colors.TEXT_SECONDARY};
                border: 1px solid {Colors.BORDER};
            }}
            QPushButton#cancelBtn:hover {{ background-color: {Colors.HOVER_BG}; }}
        """)
        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(16, 16, 16, 16)
        vl.setSpacing(8)

        vl.addWidget(QLabel(f"删除 worktree「{self._branch}」？"))
        vl.addWidget(QLabel(f"路径: {self._wt_path}"))
        vl.addWidget(QLabel(f"将同时删除分支「{self._branch}」"))

        bl = QHBoxLayout()
        bl.addStretch()
        cancel = QPushButton("取消", dlg)
        cancel.setObjectName("cancelBtn")
        cancel.clicked.connect(dlg.reject)
        bl.addWidget(cancel)
        ok = QPushButton("删除", dlg)
        ok.setObjectName("okBtn")
        ok.clicked.connect(dlg.accept)
        bl.addWidget(ok)
        vl.addLayout(bl)

        if dlg.exec_() != QDialog.Accepted:
            return

        cwd = self._find_git_root()
        # 1. 删除 worktree 目录
        try:
            r = subprocess.run(
                ["git", "worktree", "remove", self._wt_path],
                capture_output=True, text=True, cwd=cwd,
                timeout=10, encoding="utf-8", errors="replace",
                creationflags=_CREATION_FLAGS,
            )
            if r.returncode != 0:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", self._wt_path],
                    capture_output=True, text=True, cwd=cwd,
                    timeout=10, encoding="utf-8", errors="replace",
                    creationflags=_CREATION_FLAGS,
                )
            subprocess.run(
                ["git", "worktree", "prune"],
                capture_output=True, text=True, cwd=cwd,
                timeout=5, encoding="utf-8", errors="replace",
                creationflags=_CREATION_FLAGS,
            )
        except Exception as e:
            logger.error(f"[Worktree] delete failed: {e}")

        # 2. 自动删除分支
        try:
            subprocess.run(
                ["git", "branch", "-D", self._branch],
                capture_output=True, text=True, cwd=cwd,
                timeout=10, encoding="utf-8", errors="replace",
                creationflags=_CREATION_FLAGS,
            )
        except Exception as e:
            logger.error(f"[Worktree] delete branch failed: {e}")

        self.deleted.emit(self._wt_path)

    def _find_git_root(self) -> str:
        if os.path.isdir(self._wt_path):
            try:
                r = subprocess.run(
                    ["git", "rev-parse", "--git-common-dir"],
                    capture_output=True, text=True, cwd=self._wt_path,
                    timeout=5, encoding="utf-8", errors="replace",
                    creationflags=_CREATION_FLAGS,
                )
                if r.returncode == 0:
                    common = r.stdout.strip()
                    if "worktrees" in common:
                        return os.path.dirname(os.path.dirname(common))
                    if os.path.isdir(common):
                        return os.path.dirname(common)
            except Exception:
                pass
        return os.path.dirname(self._wt_path)


class _AddWorktreeRow(QWidget):
    """新建 worktree 行"""

    createRequested = pyqtSignal(str, str)  # (branch_name, base_branch)

    def __init__(self, repo_root: str, repo_name: str, current_branch: str, parent=None):
        super().__init__(parent)
        self._repo_root = repo_root
        self._repo_name = repo_name
        self._current_branch = current_branch
        self.setFixedHeight(24)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(4)

        # 竖线
        bar = QFrame(self)
        bar.setFixedWidth(1)
        bar.setStyleSheet(f"background-color: rgba(255,255,255,0.06);")
        layout.addWidget(bar)

        add_label = QLabel("＋ 新建 worktree", self)
        add_label.setStyleSheet(
            f"color: #8b949e; {get_font_family_css()} {font_size_css(10)};"
        )
        add_label.setCursor(Qt.PointingHandCursor)
        add_label.mousePressEvent = lambda e: self._on_add()
        layout.addWidget(add_label)
        layout.addStretch()

    def _on_add(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("新建 Worktree")
        dialog.setFixedSize(360, 140)
        dialog.setStyleSheet(f"""
            QDialog {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(12)}
            }}
            QLineEdit {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 6px 10px;
                {get_font_family_css()} {font_size_css(12)}
            }}
            QLineEdit:focus {{ border-color: {Colors.BORDER_ACCENT}; }}
            QPushButton {{
                background-color: {Colors.BORDER_ACCENT};
                color: white; border: none;
                border-radius: 4px; padding: 6px 18px;
                {get_font_family_css()} {font_size_css(12)}
                min-width: 60px;
            }}
            QPushButton#cancelBtn {{
                background: transparent; color: {Colors.TEXT_SECONDARY};
                border: 1px solid {Colors.BORDER};
            }}
            QPushButton#cancelBtn:hover {{ background-color: {Colors.HOVER_BG}; }}
        """)

        vl = QVBoxLayout(dialog)
        vl.setContentsMargins(16, 16, 16, 16)
        vl.setSpacing(8)

        vl.addWidget(QLabel(f"从 <b>{self._current_branch}</b> 创建新分支："))

        edit = QLineEdit(dialog)
        edit.setText(f"{self._repo_name}/")
        edit.setFocus()
        vl.addWidget(edit)

        bl = QHBoxLayout()
        bl.addStretch()
        c = QPushButton("取消", dialog)
        c.setObjectName("cancelBtn")
        c.clicked.connect(dialog.reject)
        bl.addWidget(c)
        ok = QPushButton("创建", dialog)
        ok.clicked.connect(dialog.accept)
        bl.addWidget(ok)
        vl.addLayout(bl)

        edit.returnPressed.connect(dialog.accept)

        if dialog.exec_() != QDialog.Accepted:
            return

        branch = edit.text().strip()
        if branch:
            self.createRequested.emit(branch, self._current_branch)


class WorktreeSectionWidget(QWidget):
    """
    紧贴文件夹条目下方的 worktree 分支列表
    
    │ ● dev         主·当前
    │ ○ feature     [切换] [✕]
    │ ＋ 新建
    """

    worktreeSwitched = pyqtSignal(str, str)  # (original_folder, worktree_path)
    worktreeDeleted = pyqtSignal(str)         # 被删除的 worktree 路径
    sizeChanged = pyqtSignal(int)             # 高度变化通知

    def __init__(self, repo_info, original_folder: str, parent=None, current_workdir: str = None):
        super().__init__(parent)
        self._repo_info = repo_info
        self._original_folder = original_folder
        self._current_workdir = current_workdir  # 当前实际工作目录（用于判断哪个分支激活）
        self._setup_ui()

    def _setup_ui(self):
        self.setStyleSheet("WorktreeSectionWidget { background: transparent; border: none; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 2, 0, 4)
        layout.setSpacing(0)

        self._rows = QWidget(self)
        self._rows_layout = QVBoxLayout(self._rows)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        layout.addWidget(self._rows)

        self._populate_rows()

    def _populate_rows(self):
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 用 _current_workdir 来判断哪个分支是当前激活的
        current_wd = self._current_workdir or self._original_folder
        normalized_wd = os.path.normpath(current_wd)

        for wt in self._repo_info.worktrees:
            # 比较 worktree 路径与当前工作目录
            is_current = os.path.normpath(wt.path) == normalized_wd
            row = _WorktreeRow(
                branch=wt.branch,
                wt_path=wt.path,
                is_main=wt.is_main,
                is_current=is_current,
                is_prunable=wt.is_bare,
                parent=self,
            )
            row.switched.connect(self._on_switch)
            row.deleted.connect(self._on_deleted)
            self._rows_layout.addWidget(row)

        add = _AddWorktreeRow(
            self._repo_info.root,
            os.path.basename(self._repo_info.root).lower(),
            self._repo_info.current_branch,
            parent=self,
        )
        add.createRequested.connect(self._on_create)
        self._rows_layout.addWidget(add)

        # 通知父级高度变化
        wt_count = len(self._repo_info.worktrees) or 1
        height = wt_count * 24 + 24 + 4
        self.sizeChanged.emit(height)

    def _on_switch(self, worktree_path: str):
        """切换 worktree"""
        if os.path.isdir(worktree_path):
            self.worktreeSwitched.emit(self._original_folder, worktree_path)

    def _on_deleted(self, worktree_path: str):
        """删除 worktree"""
        self.worktreeDeleted.emit(worktree_path)

    def _refresh(self):
        """刷新 worktree 列表"""
        self._repo_info = GitWorktreeDetector.get_repo_info(self._original_folder)
        if self._repo_info:
            self._populate_rows()

    def _on_create(self, branch_name: str, base_branch: str):
        repo_root = self._repo_info.root
        worktree_dir = os.path.join(
            os.path.dirname(repo_root),
            f"{os.path.basename(repo_root)}-{branch_name.replace('/', '-')}"
        )

        try:
            result = subprocess.run(
                ["git", "worktree", "add", "-b", branch_name,
                 worktree_dir, base_branch or "HEAD"],
                capture_output=True, text=True, cwd=repo_root, timeout=30,
                encoding="utf-8", errors="replace",
                creationflags=_CREATION_FLAGS,
            )
            if result.returncode != 0:
                self._show_error_dialog(
                    "创建失败",
                    f"无法创建 worktree「{branch_name}」：\n\n{result.stderr.strip()}\n\n"
                    f"💡 如果分支已存在，可先删除旧 worktree 再创建"
                )
                return

            # 获取最新 repo_info 后直接切换
            self._repo_info = GitWorktreeDetector.get_repo_info(self._original_folder)
            if self._repo_info:
                # 归一化路径再比较（Windows 下 git 用 /，os.path.join 用 \）
                norm_new_path = os.path.normpath(worktree_dir)
                for wt in self._repo_info.worktrees:
                    if os.path.normpath(wt.path) == norm_new_path:
                        self._on_switch(wt.path)
                        break

        except subprocess.TimeoutExpired:
            self._show_error_dialog("超时", "创建 worktree 超时")
        except Exception as e:
            self._show_error_dialog("错误", f"创建失败：{e}")

    def _show_error_dialog(self, title: str, message: str):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setFixedSize(380, 200)
        dlg.setStyleSheet(f"""
            QDialog {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(12)}
            }}
            QPushButton {{
                background-color: {Colors.BORDER_ACCENT};
                color: white; border: none;
                border-radius: 4px; padding: 6px 20px;
                {get_font_family_css()} {font_size_css(12)}
                min-width: 60px;
            }}
        """)
        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(16, 16, 16, 16)
        vl.setSpacing(10)
        lbl = QLabel(message, dlg)
        lbl.setWordWrap(True)
        vl.addWidget(lbl)
        vl.addStretch()
        bl = QHBoxLayout()
        bl.addStretch()
        ok = QPushButton("确定", dlg)
        ok.clicked.connect(dlg.accept)
        bl.addWidget(ok)
        vl.addLayout(bl)
        dlg.exec_()
