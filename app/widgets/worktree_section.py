# -*- coding: utf-8 -*-
"""
Worktree 树状展示组件

紧贴文件夹条目下方，左侧竖线 + 圆点标识分支
主分支也有切换按钮，当前分支高亮
分支名自动省略、内联确认删除
"""

import os
import subprocess
import sys

from loguru import logger
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.git_worktree import GitWorktreeDetector
from app.utils.utils import get_font_family_css
from app.widgets.pixel_pet import PixelPetWidget  # ★ 用于重要操作时触发警示动画

# Windows 下隐藏 cmd 窗口
_CREATION_FLAGS = 0
if sys.platform == "win32":
    _CREATION_FLAGS = subprocess.CREATE_NO_WINDOW


class _WorktreeRow(QWidget):
    """单行 worktree 分支"""

    switched = pyqtSignal(str)   # worktree_path
    deleted = pyqtSignal(str)    # worktree_path

    def __init__(self, branch: str, wt_path: str, is_main: bool,
                 is_current: bool, is_prunable: bool, parent=None,
                 behind_main: int = 0, ahead_main: int = 0):
        super().__init__(parent)
        self._branch = branch
        self._wt_path = wt_path
        self._is_main = is_main
        self._is_current = is_current
        self._is_prunable = is_prunable
        self._behind_main = behind_main
        self._ahead_main = ahead_main
        self._confirming_delete = False
        self._delete_timer: QTimer = None
        self._del_btn: QLabel = None
        self.setFixedHeight(24)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 16, 0)
        layout.setSpacing(4)

        # 左侧竖线（加粗到 3px，配合更大圆点）
        bar = QFrame(self)
        bar.setFixedWidth(3)
        bar.setStyleSheet("background-color: rgba(255,255,255,0.15);")
        layout.addWidget(bar)

        # 圆点（加大到 8x8，配合加粗的线）
        dot = QLabel("", self)
        dot.setFixedSize(8, 8)
        if self._is_current:
            dot.setStyleSheet(
                "background-color: #58a6ff; border-radius: 4px;"
            )
        else:
            dot.setStyleSheet(
                "background: transparent; border: 1.5px solid #484f58; border-radius: 4px;"
            )
        layout.addWidget(dot)

        # 分支名（自动省略超长名称）
        if self._is_current:
            branch_ss = f"color: #e6edf3; font-weight: 600; {get_font_family_css()} {font_size_css(12)}"
        else:
            branch_ss = f"color: #8b949e; {get_font_family_css()} {font_size_css(12)}"

        self._branch_label = QLabel(self._branch, self)
        self._branch_label.setStyleSheet(branch_ss)
        # 允许压缩：窗口缩小时分支名自动省略右侧
        self._branch_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._branch_label.setMinimumWidth(30)
        layout.addWidget(self._branch_label, 1)

        # 标签（小圆角 badge，适配系统字体大小）
        if self._is_main:
            tag = QLabel("main", self)
            tag.setStyleSheet(
                f"color: rgba(255,255,255,0.35); {font_size_css(10)}"
                f"background: rgba(255,255,255,0.06);"
                f"padding: 0 4px; border-radius: 2px;"
            )
            layout.addWidget(tag)

        # 落后/超前提交数显示（仅非主 worktree）
        if not self._is_main and (self._behind_main > 0 or self._ahead_main > 0):
            parts = []
            color = "#8b949e"  # 默认灰
            if self._behind_main > 0:
                parts.append(f"-{self._behind_main}")
                color = "#d29922"  # 黄色-落后警告
            if self._ahead_main > 0:
                parts.append(f"+{self._ahead_main}")
                color = "#7ee787"  # 绿色-超前
            diff_label = QLabel(" ".join(parts), self)
            diff_label.setStyleSheet(
                f"color: {color}; {get_font_family_css()} {font_size_css(10)}"
                f"padding: 0 2px;"
            )
            layout.addWidget(diff_label)

        layout.addStretch()

        # 切换按钮（纯白色加粗，适配系统字体）
        if not self._is_current:
            btn = QLabel("切换", self)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"color: #ffffff; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
                f"padding: 0 4px;"
            )
            btn.mousePressEvent = lambda e: self.switched.emit(self._wt_path)
            layout.addWidget(btn)

        # 删除按钮（纯白色加粗，适配系统字体，支持内联确认）
        if not self._is_main:
            self._del_btn = QLabel("✕", self)
            self._del_btn.setMinimumWidth(14)
            self._del_btn.setAlignment(Qt.AlignCenter)
            self._del_btn.setCursor(Qt.PointingHandCursor)
            self._del_btn.setToolTip("删除 worktree")
            self._del_btn.setStyleSheet(
                f"color: #ffffff; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
            )
            self._del_btn.mousePressEvent = lambda e: self._on_delete_clicked()
            layout.addWidget(self._del_btn)

    def resizeEvent(self, event):
        """窗口缩小时自动省略分支名右侧"""
        super().resizeEvent(event)
        self._update_branch_elision()

    def _update_branch_elision(self):
        """根据可用宽度自动省略分支名（右侧截断）"""
        if not hasattr(self, '_branch_label') or self._branch_label is None:
            return
        available = self._branch_label.width()
        if available <= 0:
            return
        fm = self._branch_label.fontMetrics()
        elided = fm.elidedText(self._branch, Qt.ElideRight, available)
        self._branch_label.setText(elided)

    def _on_delete_clicked(self):
        """删除按钮点击：内联确认，第二次点击才真正删除"""
        if not self._confirming_delete:
            # 第一次点击：进入确认状态
            self._confirming_delete = True
            self._del_btn.setText("确认删除")
            self._del_btn.setStyleSheet(
                f"color: #f85149; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
                f"padding: 0 4px;"
            )
            self._del_btn.setToolTip("再次点击确认删除，3秒后自动取消")
            # 3秒后自动恢复
            self._delete_timer = QTimer(self)
            self._delete_timer.setSingleShot(True)
            self._delete_timer.setInterval(3000)
            self._delete_timer.timeout.connect(self._cancel_delete_confirm)
            self._delete_timer.start()
        else:
            # 第二次点击：确认删除
            if self._delete_timer:
                self._delete_timer.stop()
                self._delete_timer = None
            self._confirming_delete = False
            self._do_delete()

    def _cancel_delete_confirm(self):
        """取消确认状态，恢复删除按钮样式"""
        self._confirming_delete = False
        if self._del_btn:
            self._del_btn.setText("✕")
            self._del_btn.setMinimumWidth(14)
            self._del_btn.setStyleSheet(
                f"color: #ffffff; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
            )
            self._del_btn.setToolTip("删除 worktree")

    def _do_delete(self):
        """执行删除 worktree + 自动删除分支"""
        ## 触发警示动画
        pet = self.window().findChild(PixelPetWidget)
        if pet:
            pet.set_state("warning")
        cwd = self._find_git_root()
        # 1. 删除 worktree 目录
        try:
            r = subprocess.run(
                ["git", "worktree", "remove", self._wt_path],
                capture_output=True, text=True, cwd=cwd,
                timeout=10, encoding="utf-8", errors="replace",
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
        layout.setContentsMargins(0, 0, 16, 0)
        layout.setSpacing(4)

        # 竖线（加粗到 3px，配合圆点尺寸）
        bar = QFrame(self)
        bar.setFixedWidth(3)
        bar.setStyleSheet("background-color: rgba(255,255,255,0.15);")
        layout.addWidget(bar)

        add_label = QLabel("＋ 新建 worktree", self)
        add_label.setStyleSheet(
            f"color: #ffffff; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
        )
        add_label.setCursor(Qt.PointingHandCursor)
        add_label.mousePressEvent = lambda e: self._on_add()
        layout.addWidget(add_label)
        layout.addStretch()

    def _on_add(self):
        from app.widgets.cards.settings.memory_card import SingleInputDialog

        # 注意：parent 必须传顶层窗口（self.window()）。
        # 如果传 self（_AddWorktreeRow，24px 高），MaskDialogBase 会把
        # dialog 强制设成 100×24，widget（420×220）溢出导致看不见。
        dialog = SingleInputDialog(
            title="新建 Worktree",
            hint=f"从 <b>{self._current_branch}</b> 创建新分支：",
            default_text=f"{self._repo_name}/",
            confirm_text="创建",
            cancel_text="取消",
            parent=self.window(),
        )
        if not dialog.exec_():
            return

        branch = dialog.input.text().strip()
        if branch:
            self.createRequested.emit(branch, self._current_branch)


class WorktreeSectionWidget(QWidget):
    """
    紧贴文件夹条目下方的 worktree 分支列表

    │ ● dev         主·当前
    │ ○ feature     [切换] [确认删除?]
    │ ＋ 新建
    """

    worktreeSwitched = pyqtSignal(str, str)  # (original_folder, worktree_path)
    worktreeDeleted = pyqtSignal(str)         # 被删除的 worktree 路径
    sizeChanged = pyqtSignal(int)             # 高度变化通知

    def refresh_style(self):
        """刷新样式（用于系统字体大小切换时重绘）"""
        # 重建所有行以应用新的 font_size_css()
        self._populate_rows()

    def __init__(self, repo_info, original_folder: str, parent=None, current_workdir: str = None):
        super().__init__(parent)
        self._repo_info = repo_info
        self._original_folder = original_folder
        self._current_workdir = current_workdir  # 当前实际工作目录（用于判断哪个分支激活）
        self._setup_ui()

    def _setup_ui(self):
        self.setStyleSheet("WorktreeSectionWidget { background: transparent; border: none; }")

        layout = QVBoxLayout(self)
        # 左边距从硬编码 84px 改为 24px，窗口缩小时不会溢出
        layout.setContentsMargins(24, 2, 0, 4)
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
                behind_main=wt.behind_main,
                ahead_main=wt.ahead_main,
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
        # 1. 先刷新本地列表（移除已删除的分支）
        self._refresh()
        # 2. 再通知父级（worktree_path 只作为标识，worktree 已不存在）
        self.worktreeDeleted.emit(worktree_path)

    def _refresh(self):
        """刷新 worktree 列表"""
        # 清除缓存，确保获取最新的 worktree 列表
        GitWorktreeDetector._info_cache.pop(self._original_folder, None)
        self._repo_info = GitWorktreeDetector.get_repo_info(self._original_folder)
        if self._repo_info:
            self._populate_rows()

    def _on_create(self, branch_name: str, base_branch: str):
        ## 触发警示动画
        pet = self.window().findChild(PixelPetWidget)
        if pet:
            pet.set_state("warning")
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

            # 清除缓存，确保获取最新的 worktree 列表
            GitWorktreeDetector._info_cache.pop(self._original_folder, None)
            self._repo_info = GitWorktreeDetector.get_repo_info(self._original_folder)
            if self._repo_info:
                # 归一化路径再比较（Windows 下 git 用 /，os.path.join 用 \）
                norm_new_path = os.path.normpath(worktree_dir)
                for wt in self._repo_info.worktrees:
                    if os.path.normpath(wt.path) == norm_new_path:
                        # 先刷新列表，再切换
                        self._refresh()
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
