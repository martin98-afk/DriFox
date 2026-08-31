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
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QPaintEvent
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


class _WorktreeTaskSignals(QObject):
    """后台 git 任务的完成信号（跨线程 emit → 主线程槽）"""

    finished = pyqtSignal(object)  # 任务成功：携带结果
    failed = pyqtSignal(str)  # 任务失败：携带错误消息


class _WorktreeTaskWorker(QRunnable):
    """后台执行 git 操作的 worker

    [P3] 主线程只发起任务 + 显示进度，git 子进程在后台线程串行执行，
    完成后经 finished/failed 信号回主线程刷新列表，UI 不冻结。
    顺序依赖（remove→prune→branch -D）由任务内的串行步骤保证。
    """

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = _WorktreeTaskSignals()

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as e:  # noqa: BLE001 - 后台任务兜底，失败信息回主线程
            self.signals.failed.emit(str(e))
            return
        self.signals.finished.emit(result)


# worktree 面板专用线程池（单线程串行：保证 git 写操作不并发交叉）
_WORKTREE_POOL = QThreadPool()
_WORKTREE_POOL.setMaxThreadCount(1)


def _find_git_root_for(wt_path: str) -> str:
    """后台线程：查找 worktree 所在 git 仓库根目录（rev-parse 移出主线程）"""
    if os.path.isdir(wt_path):
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                cwd=wt_path,
                timeout=5,
                encoding="utf-8",
                errors="replace",
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
    return os.path.dirname(wt_path)


def _delete_worktree_job(wt_path: str, branch: str) -> bool:
    """后台线程：串行执行 rev-parse → remove → prune → branch -D（顺序依赖硬约束）

    失败时记录日志但不中断后续步骤（与改造前主线程版本行为一致）。
    """
    cwd = _find_git_root_for(wt_path)
    # 1. 删除 worktree 目录
    try:
        r = subprocess.run(
            ["git", "worktree", "remove", wt_path],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode != 0:
            subprocess.run(
                ["git", "worktree", "remove", "--force", wt_path],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=10,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATION_FLAGS,
            )
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATION_FLAGS,
        )
    except Exception as e:
        logger.error(f"[Worktree] delete failed: {e}")

    # 2. 自动删除分支
    try:
        subprocess.run(
            ["git", "branch", "-D", branch],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATION_FLAGS,
        )
    except Exception as e:
        logger.error(f"[Worktree] delete branch failed: {e}")

    return True


def _fetch_repo_info_job(folder: str):
    """后台线程：获取仓库信息；若存在外部删除目录则先 prune 再重新获取

    返回 (GitRepoInfo | None, missing_paths)：missing_paths 供主线程做
    DB 清理 / 工作目录恢复收尾（prune 本身已在后台完成）。
    """
    info = GitWorktreeDetector.get_repo_info(folder)
    if info is None:
        return None, []

    missing_paths = [wt.path for wt in info.worktrees if not os.path.isdir(wt.path)]
    if missing_paths:
        logger.info(f"[Worktree] 缺失 {len(missing_paths)} 个外部删除目录，内部清理中...")
        # 1) prune（后台线程执行，主线程不冻结）
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                capture_output=True,
                text=True,
                cwd=info.root,
                timeout=5,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATION_FLAGS,
            )
        except Exception:
            pass
        # 2) 重新获取（prune 后 git 记录已清理）
        GitWorktreeDetector._info_cache.pop(folder, None)
        info = GitWorktreeDetector.get_repo_info(folder)
    return info, missing_paths


def _create_worktree_job(repo_root: str, branch_name: str, worktree_dir: str, base_branch: str) -> str:
    """后台线程：执行 `git worktree add -b`（创建 worktree）

    返回创建的 worktree 目录；失败抛异常（错误消息回主线程弹窗提示）。
    """
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, worktree_dir, base_branch or "HEAD"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=30,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATION_FLAGS,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git worktree add 失败 (rc={result.returncode})")
    return worktree_dir


class _WorktreeRow(QWidget):
    """单行 worktree 分支"""

    switched = pyqtSignal(str)  # worktree_path
    deleted = pyqtSignal(str)  # worktree_path

    def __init__(
        self,
        branch: str,
        wt_path: str,
        is_main: bool,
        is_current: bool,
        is_prunable: bool,
        parent=None,
        behind_main: int = 0,
        ahead_main: int = 0,
    ):
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
        self._delete_worker = None  # [P3] 后台删除任务引用（防 GC）
        self.setFixedHeight(24)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 16, 0)
        layout.setSpacing(4)

        # 左侧竖线（加粗到 3px，配合更大圆点）
        bar = QFrame(self)
        bar.setFixedWidth(3)
        bar.setStyleSheet(f"background-color: {Colors.DIVIDER_COLOR};")
        layout.addWidget(bar)

        # 圆点（加大到 8x8，配合加粗的线）
        dot = QLabel("", self)
        dot.setFixedSize(8, 8)
        if self._is_current:
            dot.setStyleSheet("background-color: #58a6ff; border-radius: 4px;")
        else:
            dot.setStyleSheet("background: transparent; border: 1.5px solid #484f58; border-radius: 4px;")
        layout.addWidget(dot)

        # 分支名（自动省略超长名称）
        if self._is_current:
            branch_ss = f"color: {Colors.TEXT_PRIMARY}; font-weight: 600; {get_font_family_css()} {font_size_css(12)}"
        else:
            branch_ss = f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(12)}"

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
                f"color: {Colors.TEXT_MUTED}; {font_size_css(10)}"
                f"background: {Colors.HOVER_BG};"
                f"padding: 0 4px; border-radius: 2px;"
            )
            layout.addWidget(tag)

        # 落后/超前提交数显示（仅非主 worktree）：落后红、领先深绿
        if not self._is_main and (self._behind_main > 0 or self._ahead_main > 0):
            base_ss = f"{get_font_family_css()} {font_size_css(10)}padding: 0 2px;"
            if self._behind_main > 0:
                behind_label = QLabel(f"-{self._behind_main}", self)
                behind_label.setStyleSheet(f"color: #f85149; {base_ss}")  # 红色-落后
                layout.addWidget(behind_label)
            if self._ahead_main > 0:
                ahead_label = QLabel(f"+{self._ahead_main}", self)
                ahead_label.setStyleSheet(f"color: #3fb950; {base_ss}")  # 深绿-领先
                layout.addWidget(ahead_label)

        layout.addStretch()

        # 切换按钮（纯白色加粗，适配系统字体）
        if not self._is_current:
            btn = QLabel("切换", self)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"color: {Colors.TEXT_ACCENT}; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
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
                f"color: {Colors.TEXT_ACCENT}; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
            )
            self._del_btn.mousePressEvent = lambda e: self._on_delete_clicked()
            layout.addWidget(self._del_btn)

    def resizeEvent(self, event):
        """窗口缩小时自动省略分支名右侧"""
        super().resizeEvent(event)
        self._update_branch_elision()

    def _update_branch_elision(self):
        """根据可用宽度自动省略分支名（右侧截断）"""
        if not hasattr(self, "_branch_label") or self._branch_label is None:
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
                f"color: #f85149; font-weight: 600; {get_font_family_css()} {font_size_css(11)}padding: 0 4px;"
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
                f"color: {Colors.TEXT_ACCENT}; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
            )
            self._del_btn.setToolTip("删除 worktree")

    def _do_delete(self):
        """执行删除 worktree + 自动删除分支（后台线程串行执行，UI 不冻结）"""
        ## 触发警示动画
        pet = self.window().findChild(PixelPetWidget)
        if pet:
            pet.set_state("warning")
        # 防重入：删除期间禁用删除按钮并显示执行中，避免用户连点重复触发
        if self._del_btn:
            self._del_btn.setEnabled(False)
            self._del_btn.setText("删除中...")
            self._del_btn.setToolTip("正在删除，请稍候")
        worker = _WorktreeTaskWorker(_delete_worktree_job, self._wt_path, self._branch)
        worker.signals.finished.connect(self._on_delete_finished)
        worker.signals.failed.connect(self._on_delete_failed)
        self._delete_worker = worker  # 持有引用，防止任务期间被 GC
        _WORKTREE_POOL.start(worker)

    def _on_delete_finished(self, _result):
        """删除完成（主线程）：恢复 UI 并通知父级刷新"""
        # 恢复删除按钮
        self._confirming_delete = False
        if self._delete_timer:
            self._delete_timer.stop()
            self._delete_timer = None
        if self._del_btn:
            self._del_btn.setEnabled(True)
            self._del_btn.setText("✕")
            self._del_btn.setMinimumWidth(14)
            self._del_btn.setStyleSheet(
                f"color: {Colors.TEXT_ACCENT}; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
            )
            self._del_btn.setToolTip("删除 worktree")
        self.deleted.emit(self._wt_path)
        # 操作完成，恢复正常状态
        pet = self.window().findChild(PixelPetWidget)
        if pet:
            pet.set_state("idle")

    def _on_delete_failed(self, msg: str):
        """删除失败（主线程）：恢复 UI，失败信息已由后台记录日志"""
        logger.error(f"[Worktree] delete failed: {msg}")
        self._confirming_delete = False
        if self._delete_timer:
            self._delete_timer.stop()
            self._delete_timer = None
        if self._del_btn:
            self._del_btn.setEnabled(True)
            self._del_btn.setText("✕")
            self._del_btn.setMinimumWidth(14)
            self._del_btn.setStyleSheet(
                f"color: {Colors.TEXT_ACCENT}; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
            )
            self._del_btn.setToolTip("删除 worktree")
        pet = self.window().findChild(PixelPetWidget)
        if pet:
            pet.set_state("idle")

    def _find_git_root(self) -> str:
        return _find_git_root_for(self._wt_path)


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
        bar.setStyleSheet(f"background-color: {Colors.DIVIDER_COLOR};")
        layout.addWidget(bar)

        add_label = QLabel("＋ 新建工作树", self)
        add_label.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; font-weight: 600; {get_font_family_css()} {font_size_css(11)}"
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
    worktreeDeleted = pyqtSignal(str)  # 仅 UI 操作删除时发射
    workingDirRestored = pyqtSignal(str)  # 外部删除导致工作目录恢复时发射（无重建）
    sizeChanged = pyqtSignal(int)  # 高度变化通知

    def refresh_style(self):
        """刷新样式（用于系统字体大小切换时重绘）"""
        self._repopulate()

    def __init__(self, repo_info, original_folder: str, parent=None, current_workdir: str = None, project: str = None):
        super().__init__(parent)
        self._repo_info = repo_info
        self._original_folder = original_folder
        self._current_workdir = current_workdir
        self._project = project or ""  # 用于 DB 直接清理
        self._last_check_time = 0.0  # paintEvent 防抖时间戳
        # [P3] 后台任务引用（防止任务执行期间被 GC）+ 防重入标志
        self._fetch_worker = None
        self._create_worker = None
        self._create_busy = False
        self._pending_switch_path = None  # 创建完成后待切换的 worktree 路径
        self._setup_ui()

    def paintEvent(self, event: QPaintEvent):
        """在每次绘制时检查 worktree 路径是否仍然存在

         为什么用 paintEvent 而非 QTimer：
         QTimer 在嵌入 QListWidget.setItemWidget 的 widget 中可能无法可靠触发
        （widget 不是标准层级结构的一部分）。
         paintEvent 由 Qt 绘制系统保证调用，是检测文件系统变化的最可靠方式。
         防抖 5s 避免频繁 I/O。
        """
        self._check_paths()
        super().paintEvent(event)

    def _check_paths(self):
        import time

        now = time.monotonic()
        if now - self._last_check_time < 5.0:
            return
        self._last_check_time = now

        if not self._repo_info or not self._repo_info.worktrees:
            return
        has_missing = any(not os.path.isdir(wt.path) for wt in self._repo_info.worktrees)
        if has_missing:
            logger.info("[Worktree] paintEvent 检测到外部删除，内部清理...")
            self._repopulate()

    def _setup_ui(self):
        self.setStyleSheet("WorktreeSectionWidget { background: transparent; border: none; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 2, 0, 4)
        layout.setSpacing(0)

        self._rows = QWidget(self)
        self._rows_layout = QVBoxLayout(self._rows)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        layout.addWidget(self._rows)

        self._populate_rows()

    def _repopulate(self):
        """清除缓存后重新 populate（git 查询移入后台线程，UI 不冻结）

        如果重新获取失败（_repo_info 为 None），仍要清空旧行避免残留。
        """
        GitWorktreeDetector._info_cache.pop(self._original_folder, None)
        worker = _WorktreeTaskWorker(_fetch_repo_info_job, self._original_folder)
        worker.signals.finished.connect(self._on_repo_info_loaded)
        worker.signals.failed.connect(self._on_repo_info_load_failed)
        self._fetch_worker = worker
        _WORKTREE_POOL.start(worker)

    def _on_repo_info_loaded(self, payload):
        """后台获取完成（主线程）：应用结果并重建行列表"""
        info, missing_paths = payload
        self._repo_info = info
        if not info:
            # 获取失败（仓库已删除等），清空所有行
            self._fetch_worker = None
            self._clear_rows()
            return
        # 外部删除处理（prune 已在后台完成，这里只做 DB/工作目录收尾）
        if missing_paths:
            logger.info(f"[Worktree] 缺失 {len(missing_paths)} 个外部删除目录，内部清理中...")
            # DB 清理（直接，不走 signal → 不触发 _load_key_documents）
            for p in missing_paths:
                self._cleanup_db(p)
            # 如果当前工作目录就是被删的 path，恢复为原始仓库
            if self._current_workdir and os.path.normpath(self._current_workdir) in (
                os.path.normpath(mp) for mp in missing_paths
            ):
                logger.info(f"[Worktree] 当前 workdir 已被删除，恢复为原始仓库: {self._original_folder}")
                self._restore_workdir(self._original_folder)
        self._fetch_worker = None
        self._populate_rows()
        # 创建 worktree 成功后，刷新完成 → 切换到新 worktree
        if self._pending_switch_path:
            target = self._pending_switch_path
            self._pending_switch_path = None
            norm_target = os.path.normpath(target)
            for wt in info.worktrees:
                if os.path.normpath(wt.path) == norm_target:
                    self._on_switch(wt.path)
                    break

    def _on_repo_info_load_failed(self, msg: str):
        """后台获取失败（主线程）：记录日志并清空行避免残留"""
        logger.error(f"[Worktree] repo info load failed: {msg}")
        self._fetch_worker = None
        self._clear_rows()

    def _cleanup_db(self, path: str):
        """直接清理 DB 中该路径的记录（不触发父级重建）"""
        if not self._project:
            return
        try:
            from app.core.memory_manager import MemoryManagerCore

            mm = MemoryManagerCore.get_instance()
            if mm and mm._key_documents_repo:
                mm._key_documents_repo.remove_by_path(self._project, path)
        except Exception:
            pass

    def _restore_workdir(self, original_path: str):
        """将工作目录恢复为原始 git 仓库根目录（当被删 path 恰是当前 workdir 时）"""
        try:
            from app.core.memory_manager import MemoryManagerCore

            mm = MemoryManagerCore.get_instance()
            if mm and self._project:
                mm.set_working_directory(self._project, original_path)
                self._current_workdir = original_path
                # 通知父级更新缓存（工具执行器等），不触发全量重建
                self.workingDirRestored.emit(original_path)
        except Exception:
            pass

    def _clear_rows(self):
        """清空所有 worktree 行 + 新建行（供 _populate_rows 和 _repopulate 共用）"""
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _populate_rows(self):
        """重新构建 worktree 行列表（纯渲染，git 查询在后台完成）

        处理外部删除的策略（全内部闭环，不触发父级重建）：
        1. 缺失路径检测 + `git worktree prune` 由后台任务 `_fetch_repo_info_job` 完成
        2. 主线程这里只做 DB 清理、工作目录恢复收尾（_on_repo_info_loaded）
        3. 跳过缺失行，渲染其余行
        """
        self._clear_rows()

        current_wd = self._current_workdir or self._original_folder
        normalized_wd = os.path.normpath(current_wd)

        # ====== 渲染 ======
        visible_count = 0
        for wt in self._repo_info.worktrees:
            if not os.path.isdir(wt.path):
                continue  # prune 失败等边缘情况
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
            visible_count += 1

        add = _AddWorktreeRow(
            self._repo_info.root,
            os.path.basename(self._repo_info.root).lower(),
            self._repo_info.current_branch,
            parent=self,
        )
        add.createRequested.connect(self._on_create)
        self._rows_layout.addWidget(add)

        wt_count = visible_count or 1
        height = wt_count * 24 + 24 + 4
        self.sizeChanged.emit(height)

    def _on_switch(self, worktree_path: str):
        """切换 worktree"""
        if os.path.isdir(worktree_path):
            self.worktreeSwitched.emit(self._original_folder, worktree_path)

    def _on_deleted(self, worktree_path: str):
        """UI 删除 worktree（内联确认后）"""
        self._repopulate()
        self.worktreeDeleted.emit(worktree_path)

    def _refresh(self):
        """刷新 worktree 列表（外部调用，如创建新 worktree 后；后台执行）"""
        # 清除缓存，确保获取最新的 worktree 列表
        self._repopulate()

    def _on_create(self, branch_name: str, base_branch: str):
        ## 触发警示动画
        pet = self.window().findChild(PixelPetWidget)
        if pet:
            pet.set_state("warning")
        # 防重入：创建进行中时忽略重复点击
        if self._create_busy:
            logger.info("[Worktree] 创建进行中，忽略重复请求")
            return
        self._create_busy = True
        repo_root = self._repo_info.root
        worktree_dir = os.path.join(
            os.path.dirname(repo_root), f"{os.path.basename(repo_root)}-{branch_name.replace('/', '-')}"
        )

        worker = _WorktreeTaskWorker(_create_worktree_job, repo_root, branch_name, worktree_dir, base_branch or "HEAD")
        worker.signals.finished.connect(lambda _r: self._on_create_finished(branch_name, worktree_dir, pet))
        worker.signals.failed.connect(lambda msg: self._on_create_failed(branch_name, msg, pet))
        self._create_worker = worker
        _WORKTREE_POOL.start(worker)

    def _on_create_finished(self, branch_name: str, worktree_dir: str, pet):
        """创建成功（主线程）：置位待切换路径并刷新列表"""
        self._create_busy = False
        self._create_worker = None
        self._pending_switch_path = worktree_dir
        # 清除缓存后刷新（后台），刷新完成时自动切换到新 worktree
        self._repopulate()
        # 操作完成，恢复正常状态
        if pet:
            pet.set_state("idle")

    def _on_create_failed(self, branch_name: str, msg: str, pet):
        """创建失败（主线程）：恢复状态并提示"""
        self._create_busy = False
        self._create_worker = None
        if pet:
            pet.set_state("idle")
        self._show_error_dialog(
            "创建失败",
            f"无法创建 worktree「{branch_name}」：\n\n{msg}\n\n💡 如果分支已存在，可先删除旧 worktree 再创建",
        )

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
