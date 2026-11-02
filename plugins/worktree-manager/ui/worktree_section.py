# -*- coding: utf-8 -*-
"""Worktree 树状展示组件

主仓库卡片 + 子工作树列表：
- 主仓库卡片：分支名、路径、本地分支切换器、领先/落后
- 子工作树行：分支、路径、说明（AI 创建时显示）、领先落后、切换/删除
- 底部「＋ 新建工作树」：分支名 + 基分支 + 一句话说明

约定（与 git 语义一致）：
- 主仓库可以切分支（git switch）；子工作树不提供切分支入口，
  因为一个工作树同时只能检出其中一个分支，切分支会让它的「用途」漂移。
- 说明存 git config worktree.<名>.description，git 原生支持，跨工具可见。
"""

import os
import subprocess
import sys

from loguru import logger
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QPaintEvent
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.plugins.sdk import Colors, font_size_css, readable_text_on
from app.plugins.sdk import GitWorktreeDetector
from app.plugins.sdk import get_font_family_css

# Windows 下隐藏 cmd 窗口
_CREATION_FLAGS = 0
if sys.platform == "win32":
    _CREATION_FLAGS = subprocess.CREATE_NO_WINDOW

# 说明字段最大长度（与 manage_worktree 工具 schema 对齐）
DESCRIPTION_MAX_LEN = 60


def _caps(cap_id):
    """取某能力（无 ctx 场景统一走 sdk 的 caps_for，host 由宿主解析）。"""
    from app.plugins.sdk import caps_for

    return caps_for(cap_id)


def _git(args, cwd, timeout=10):
    """同步 git 调用（仅在后台线程或短操作里用）"""
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATION_FLAGS,
    )


def read_worktree_description(wt_path: str) -> str:
    """读取工作树说明（git config worktree.<name>.description）

    git 以工作树目录名作为 config key 的 <name> 段。读取失败一律返回空串，
    说明只是辅助信息，不能因为它让整个列表渲染失败。
    """
    if not wt_path or not os.path.isdir(wt_path):
        return ""
    try:
        r = _git(["config", f"worktree.{os.path.basename(wt_path)}.description"], wt_path, timeout=3)
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except Exception:
        pass
    return ""


def write_worktree_description(wt_path: str, text: str) -> bool:
    """写入工作树说明；空文本表示删除该配置项"""
    if not wt_path or not os.path.isdir(wt_path):
        return False
    key = f"worktree.{os.path.basename(wt_path)}.description"
    try:
        if text:
            r = _git(["config", key, text], wt_path, timeout=5)
        else:
            r = _git(["config", "--unset", key], wt_path, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def list_local_branches(repo_root: str) -> list:
    """列出本地分支（当前分支排最前）"""
    try:
        r = _git(["branch", "--format=%(refname:short)"], repo_root, timeout=8)
        if r.returncode != 0:
            return []
        branches = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        current = ""
        try:
            cur = _git(["branch", "--show-current"], repo_root, timeout=5)
            current = (cur.stdout or "").strip()
        except Exception:
            pass
        if current and current in branches:
            branches.remove(current)
            branches.insert(0, current)
        return branches
    except Exception:
        return []


class ThemeIconLabel(QLabel):
    """主题感知 svg 图标标签。

    不用 QLabel.setPixmap(icon.pixmap())：那会把当前主题的像素快照固化下来，
    主题切换后图标不刷新。本类持有 QIcon 名，每次 paintEvent 重新取色。
    """

    def __init__(self, icon_name: str, size: int = 14, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._size = size
        self._confirm_style = False
        self.setFixedSize(size + 4, size + 4)
        self.setStyleSheet("background: transparent;")

    def set_icon(self, icon_name: str):
        """更换图标名（触发重绘）"""
        self._icon_name = icon_name
        self.update()

    def show_confirm_style(self, on: bool):
        """确认态：危险色。svg 是单色掩码，用 QIcon 的 paint 无法直接改色，
        这里改用主题危险色重新生成一个临时图标（通过 colorize 保底）。"""
        self._confirm_style = bool(on)
        self.update()

    def paintEvent(self, event):
        from PyQt5.QtGui import QColor, QPainter, QPixmap

        p = QPainter(self)
        icon = get_icon(self._icon_name)
        if self._confirm_style:
            # 危险态：把图标着色为错误色（QIcon 本身不暴露颜色，改用 pixmap + 合成）
            pm = icon.pixmap(self._size, self._size)
            tinted = QPixmap(pm.size())
            tinted.fill(Qt.transparent)
            tp = QPainter(tinted)
            tp.drawPixmap(0, 0, pm)
            tp.setCompositionMode(QPainter.CompositionMode_SourceIn)
            tp.fillRect(tinted.rect(), QColor("#f85149"))
            tp.end()
            p.drawPixmap(2, 2, tinted)
        else:
            icon.paint(p, self.rect(), Qt.AlignCenter)
        p.end()


class _WorktreeTaskSignals(QObject):
    """后台 git 任务的完成信号（跨线程 emit → 主线程槽）"""

    finished = pyqtSignal(object)  # 任务成功：携带结果
    failed = pyqtSignal(str)  # 任务失败：携带错误消息


class _WorktreeTaskWorker(QRunnable):
    """后台执行 git 操作的 worker

    主线程只发起任务 + 显示进度，git 子进程在后台线程串行执行，
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


def _invalidate_repo_cache(folder: str) -> None:
    """删除 worktree 后失效 GitWorktreeDetector 的相关缓存

    `_info_cache` 只是最外层；`_worktrees_cache` / `_branch_cache` 各有
    30s TTL，不清会拿回删除前的列表（表现为已删的 worktree 仍在行里）。
    """
    GitWorktreeDetector._info_cache.pop(folder, None)
    GitWorktreeDetector._worktrees_cache.clear()
    GitWorktreeDetector._branch_cache.clear()


def _find_git_root_for(wt_path: str) -> str:
    """后台线程：查找 worktree 所在 git 仓库根目录（rev-parse 移出主线程）"""
    if os.path.isdir(wt_path):
        try:
            r = _git(["rev-parse", "--git-common-dir"], wt_path, timeout=5)
            if r.returncode == 0:
                common = (r.stdout or "").strip()
                if "worktrees" in common:
                    return os.path.dirname(os.path.dirname(common))
                if os.path.isdir(common):
                    return os.path.dirname(common)
        except Exception:
            pass
    return os.path.dirname(wt_path)


def _delete_worktree_job(wt_path: str, branch: str) -> bool:
    """后台线程：串行执行 rev-parse → remove → prune → branch -D（顺序依赖硬约束）"""
    cwd = _find_git_root_for(wt_path)
    try:
        r = _git(["worktree", "remove", wt_path], cwd, timeout=10)
        if r.returncode != 0:
            _git(["worktree", "remove", "--force", wt_path], cwd, timeout=10)
    except Exception as e:
        logger.warning(f"[Worktree] remove 失败: {e}")
    try:
        _git(["worktree", "prune"], cwd, timeout=10)
    except Exception:
        pass
    if branch and branch != "(detached)":
        try:
            _git(["branch", "-D", branch], cwd, timeout=10)
        except Exception as e:
            logger.info(f"[Worktree] 分支 {branch} 未删除（可能含未合并提交）: {e}")
    return True


def _create_worktree_job(repo_root: str, branch: str, worktree_dir: str, base: str) -> str:
    """后台线程：git worktree add [-b branch] dir base"""
    # 分支已存在 → 直接挂载；否则新建
    exists = _git(["rev-parse", "--verify", f"refs/heads/{branch}"], repo_root, timeout=8)
    if exists.returncode == 0:
        r = _git(["worktree", "add", worktree_dir, branch], repo_root, timeout=30)
    else:
        r = _git(["worktree", "add", "-b", branch, worktree_dir, base or "HEAD"], repo_root, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "git worktree add 失败").strip())
    return worktree_dir


def _switch_branch_job(repo_root: str, branch: str) -> tuple:
    """后台线程：主仓库切分支（git switch）"""
    r = _git(["switch", branch], repo_root, timeout=30)
    if r.returncode != 0:
        # switch 不被老 git 支持时回退 checkout
        r = _git(["checkout", branch], repo_root, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "git switch 失败").strip())
    return repo_root, branch


def _fetch_repo_info_job(git_root: str):
    """后台线程：拉取仓库信息 + 缺失工作树清理

    Returns:
        (GitRepoInfo | None, missing_paths: list[str])
    """
    info = GitWorktreeDetector.get_repo_info(git_root)
    if info is None:
        return None, []
    missing = [wt.path for wt in info.worktrees if not wt.is_main and not os.path.isdir(wt.path)]
    if missing:
        try:
            _git(["worktree", "prune"], info.root, timeout=10)
        except Exception:
            pass
    # 说明不随 get_repo_info 返回，这里补一次（每个工作树一次 git config，通常 <5ms）
    for wt in info.worktrees:
        if not wt.is_main:
            setattr(wt, "description", read_worktree_description(wt.path))
    return info, missing


class MainRepoCard(QWidget):
    """主仓库卡片：分支切换器 + 路径 + 领先落后

    只有主仓库允许切分支：子工作树的存在意义就是固定检出某个分支，
    允许它切分支等于让它与别的会话抢同一个分支。
    """

    switched = pyqtSignal(str)  # worktree_path（切换到该工作目录）
    branchSwitched = pyqtSignal(str)  # branch_name

    def __init__(self, repo_info, is_current: bool, parent=None):
        super().__init__(parent)
        self._repo_info = repo_info
        self._is_current = is_current
        self._switching = False
        self._setup_ui()

    def _setup_ui(self):
        root = self._repo_info.root
        self.setObjectName("mainRepoCard")
        Colors.refresh()
        self.setStyleSheet(
            f"""
            QWidget#mainRepoCard {{
                background-color: {Colors.CARD_BG.format(alpha=200)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # ── 第一行：图标 + 仓库名 + 当前标记 + 切工作目录 ──
        head = QHBoxLayout()
        head.setSpacing(6)

        icon = ThemeIconLabel("仓库", 16, self)
        head.addWidget(icon)

        name = QLabel(os.path.basename(root.rstrip("/\\")) or root, self)
        name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(13)}"
        )
        head.addWidget(name)

        if self._is_current:
            badge = QLabel("当前", self)
            badge.setStyleSheet(
                f"color: {Colors.BUTTON_TEXT_ON_ACCENT}; background: {Colors.TEXT_ACCENT};"
                f" {font_size_css(10)} padding: 1px 6px; border-radius: 3px;"
            )
            head.addWidget(badge)

        head.addStretch()

        if not self._is_current:
            btn = QLabel("切到此处", self)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"color: {Colors.TEXT_ACCENT}; font-weight: 600;"
                f" {get_font_family_css()} {font_size_css(11)}"
            )
            btn.mousePressEvent = lambda e: self.switched.emit(root)
            head.addWidget(btn)

        layout.addLayout(head)

        # ── 第二行：分支切换器 ──
        branch_row = QHBoxLayout()
        branch_row.setSpacing(6)

        branch_label = QLabel("分支", self)
        branch_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}"
        )
        branch_row.addWidget(branch_label)

        self._branch_combo = QComboBox(self)
        self._branch_combo.setMinimumWidth(180)
        self._branch_combo.setMaxVisibleItems(15)
        self._branch_combo.setStyleSheet(
            f"QComboBox {{ {get_font_family_css()} {font_size_css(12)} }}"
        )
        branches = list_local_branches(root)
        current = self._repo_info.current_branch
        if current and current not in branches:
            branches.insert(0, current)
        self._branch_combo.addItems(branches or [current or "(detached)"])
        if current:
            idx = self._branch_combo.findText(current)
            if idx >= 0:
                self._branch_combo.setCurrentIndex(idx)
        self._branch_combo.currentTextChanged.connect(self._on_branch_changed)
        branch_row.addWidget(self._branch_combo, 1)

        layout.addLayout(branch_row)

        # ── 第三行：路径 ──
        path_label = QLabel(root, self)
        path_label.setWordWrap(False)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)}"
        )
        layout.addWidget(path_label)

    def _on_branch_changed(self, branch: str):
        """下拉框切换 → 通知外层执行 git switch（外层负责后台线程与错误提示）"""
        if self._switching or not branch or branch == self._repo_info.current_branch:
            return
        self.branchSwitched.emit(branch)

    def revert_branch(self, branch: str):
        """切分支失败时把下拉框拨回实际分支（不再触发信号）"""
        self._switching = True
        try:
            idx = self._branch_combo.findText(branch)
            if idx >= 0:
                self._branch_combo.setCurrentIndex(idx)
        finally:
            self._switching = False


class _WorktreeRow(QWidget):
    """单行 worktree（子工作树用：无切分支入口）"""

    switched = pyqtSignal(str)  # worktree_path
    deleted = pyqtSignal(str)  # worktree_path

    def __init__(self, wt, is_current: bool, parent=None):
        super().__init__(parent)
        self._wt = wt
        self._is_current = is_current
        self._confirming_delete = False
        self._delete_timer: QTimer = None
        self._del_btn: QLabel = None
        self._delete_worker = None
        self.setMinimumHeight(30)
        self._setup_ui()

    def _setup_ui(self):
        wt = self._wt
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        dot = QLabel("", self)
        dot.setFixedSize(8, 8)
        if self._is_current:
            dot.setStyleSheet(f"background-color: {Colors.TEXT_ACCENT}; border-radius: 4px;")
        else:
            dot.setStyleSheet(f"background: transparent; border: 1.5px solid {Colors.BORDER}; border-radius: 4px;")
        layout.addWidget(dot)

        # 左侧主区：分支名 + 说明 + 路径
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(1)

        top = QHBoxLayout()
        top.setSpacing(4)
        branch_ss = (
            f"color: {Colors.TEXT_PRIMARY}; font-weight: 600;"
            if self._is_current
            else f"color: {Colors.TEXT_PRIMARY};"
        )
        branch_label = QLabel(wt.branch, self)
        branch_label.setStyleSheet(f"{branch_ss} {get_font_family_css()} {font_size_css(12)}")
        branch_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        branch_label.setMinimumWidth(40)
        branch_label.setToolTip(wt.branch)
        top.addWidget(branch_label)

        desc = str(getattr(wt, "description", "") or "")
        if desc:
            ai_tag = QLabel("AI", self)
            ai_tag.setStyleSheet(
                f"color: {Colors.BUTTON_TEXT_ON_ACCENT}; background: {Colors.TEXT_ACCENT};"
                f" {font_size_css(9)} padding: 0 4px; border-radius: 2px;"
            )
            ai_tag.setToolTip("该工作树由 AI 创建")
            top.addWidget(ai_tag)

        if wt.behind_main > 0:
            b = QLabel(f"-{wt.behind_main}", self)
            b.setToolTip(f"落后主仓库 {wt.behind_main} 个提交")
            b.setStyleSheet(f"color: #f85149; {get_font_family_css()} {font_size_css(10)}")
            top.addWidget(b)
        if wt.ahead_main > 0:
            a = QLabel(f"+{wt.ahead_main}", self)
            a.setToolTip(f"领先主仓库 {wt.ahead_main} 个提交")
            a.setStyleSheet(f"color: #3fb950; {get_font_family_css()} {font_size_css(10)}")
            top.addWidget(a)

        top.addStretch()
        text_box.addLayout(top)

        # 说明行（AI 创建的工作树必带，人工创建的可选）
        if desc:
            desc_label = QLabel(desc, self)
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet(
                f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(11)}"
            )
            text_box.addWidget(desc_label)

        path_label = QLabel(wt.path, self)
        path_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)}"
        )
        path_label.setToolTip(wt.path)
        text_box.addWidget(path_label)

        layout.addLayout(text_box, 1)

        # 右侧操作
        if not self._is_current:
            switch_btn = QLabel("切换", self)
            switch_btn.setCursor(Qt.PointingHandCursor)
            switch_btn.setStyleSheet(
                f"color: {Colors.TEXT_ACCENT}; font-weight: 600;"
                f" {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
            )
            switch_btn.setToolTip("把当前会话工作目录切到该工作树")
            switch_btn.mousePressEvent = lambda e: self.switched.emit(self._wt.path)
            layout.addWidget(switch_btn)

        self._del_btn = ThemeIconLabel("删除", 13, self)
        self._del_btn.setCursor(Qt.PointingHandCursor)
        self._del_btn.setToolTip("删除工作树")
        self._del_btn.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
        )
        self._del_btn.mousePressEvent = lambda e: self._on_delete_click()
        layout.addWidget(self._del_btn)

    def _on_delete_click(self):
        """内联二次确认：首次点击变「确认删除?」，3 秒后自动还原"""
        if self._confirming_delete:
            self._do_delete()
            return
        self._confirming_delete = True
        self._del_btn.set_icon("删除")
        self._del_btn.show_confirm_style(True)
        self._del_btn.setStyleSheet(
            f"color: #f85149; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
        )
        self._delete_timer = QTimer(self)
        self._delete_timer.setSingleShot(True)
        self._delete_timer.timeout.connect(self._cancel_delete_confirm)
        self._delete_timer.start(3000)

    def _cancel_delete_confirm(self):
        self._confirming_delete = False
        if self._del_btn is not None:
            self._del_btn.set_icon("删除")
            self._del_btn.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
            )

    def _do_delete(self):
        if self._delete_timer:
            self._delete_timer.stop()
            self._delete_timer = None
        self._del_btn.setEnabled(False)
        self._del_btn.show_confirm_style(True)
        worker = _WorktreeTaskWorker(_delete_worktree_job, self._wt.path, self._wt.branch)
        worker.signals.finished.connect(lambda _r: self.deleted.emit(self._wt.path))
        worker.signals.failed.connect(self._on_delete_failed)
        self._delete_worker = worker
        _WORKTREE_POOL.start(worker)

    def _on_delete_failed(self, msg: str):
        logger.warning(f"[Worktree] 删除失败: {msg}")
        self._cancel_delete_confirm()
        self._del_btn.setEnabled(True)


class _AddWorktreeRow(QWidget):
    """新建工作树入口"""

    createRequested = pyqtSignal(str, str, str)  # (branch_name, base_branch, description)

    def __init__(self, repo_root: str, current_branch: str, parent=None):
        super().__init__(parent)
        self._repo_root = repo_root
        self._current_branch = current_branch
        self.setFixedHeight(26)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)

        add_label = QLabel("新建工作树", self)
        add_label.setCursor(Qt.PointingHandCursor)
        add_label.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(11)}"
        )
        add_label.mousePressEvent = lambda e: self._on_add()
        layout.addWidget(add_label)
        layout.addStretch()

    def _on_add(self):
        from app.plugins.sdk import SingleInputDialog

        dialog = SingleInputDialog(
            title="新建 Worktree",
            hint=f"从 <b>{self._current_branch}</b> 创建新分支：",
            default_text=f"{os.path.basename(self._repo_root)}/",
            confirm_text="下一步",
            cancel_text="取消",
            parent=self.window(),
        )
        if not dialog.exec_():
            return
        branch = dialog.input.text().strip()
        if not branch:
            return

        # 第二步：说明（人工创建可选填，留空即无说明）
        desc_dialog = SingleInputDialog(
            title="工作树说明",
            hint="一句话说明这个工作树要做什么（可留空）：",
            default_text="",
            confirm_text="创建",
            cancel_text="取消",
            parent=self.window(),
        )
        description = ""
        if desc_dialog.exec_():
            description = desc_dialog.input.text().strip()[:DESCRIPTION_MAX_LEN]
        self.createRequested.emit(branch, self._current_branch or "HEAD", description)


class WorktreeSectionWidget(QWidget):
    """工作树面板：主仓库卡片 + 子工作树列表 + 新建入口"""

    worktreeSwitched = pyqtSignal(str, str)  # (original_folder, worktree_path)
    worktreeDeleted = pyqtSignal(str)  # 仅 UI 操作删除时发射
    workingDirRestored = pyqtSignal(str)  # 外部删除导致工作目录恢复时发射
    sizeChanged = pyqtSignal(int)  # 高度变化通知

    def __init__(self, repo_info, original_folder: str, parent=None, current_workdir: str = None, project: str = None):
        super().__init__(parent)
        self._repo_info = repo_info
        self._original_folder = original_folder
        self._current_workdir = current_workdir
        self._project = project or ""
        self._last_check_time = 0.0
        self._fetch_worker = None
        self._create_worker = None
        self._create_busy = False
        self._pending_switch_path = None
        self._main_card: MainRepoCard = None
        self._setup_ui()
        # 主题自刷
        try:
            ev = _caps("ui.events")
            if ev is not None:
                ev()["subscribe"]("theme_changed", self.refresh_style)
        except Exception:
            pass

    def paintEvent(self, event: QPaintEvent):
        """绘制时检查 worktree 路径是否仍存在（防抖 5s）"""
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
        if any(not os.path.isdir(wt.path) for wt in self._repo_info.worktrees if not wt.is_main):
            logger.info("[Worktree] 检测到外部删除，内部清理...")
            self._repopulate()

    def _setup_ui(self):
        self.setStyleSheet("WorktreeSectionWidget { background: transparent; border: none; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(6)

        self._rows = QWidget(self)
        self._rows_layout = QVBoxLayout(self._rows)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(6)
        layout.addWidget(self._rows)

        self._populate_rows()

    def refresh_style(self, _payload: dict = None):
        """主题刷新：重建（内联样式在构造时写死，重建最省心）"""
        self._repopulate()

    def _repopulate(self):
        """清缓存后重新拉取（git 查询在后台线程）"""
        _invalidate_repo_cache(self._original_folder)
        worker = _WorktreeTaskWorker(_fetch_repo_info_job, self._original_folder)
        worker.signals.finished.connect(self._on_repo_info_loaded)
        worker.signals.failed.connect(self._on_repo_info_load_failed)
        self._fetch_worker = worker
        _WORKTREE_POOL.start(worker)

    def _on_repo_info_loaded(self, payload):
        """后台拉取完成（主线程）：应用结果并重建"""
        info, missing_paths = payload
        self._fetch_worker = None
        if not info:
            self._clear_rows()
            return
        self._repo_info = info

        if missing_paths:
            logger.info(f"[Worktree] 缺失 {len(missing_paths)} 个外部删除目录，内部清理中...")
            # 当前工作目录落在被删的路径里 → 回主仓库
            if self._current_workdir and any(
                os.path.normpath(self._current_workdir) == os.path.normpath(mp) for mp in missing_paths
            ):
                self._restore_workdir(info.root)

        self._populate_rows()

        # 创建完成后自动切到新工作树
        if self._pending_switch_path:
            target = self._pending_switch_path
            self._pending_switch_path = None
            if os.path.isdir(target):
                self._on_switch(target)

    def _on_repo_info_load_failed(self, msg: str):
        self._fetch_worker = None
        logger.warning(f"[Worktree] 仓库信息拉取失败: {msg}")
        self._clear_rows()

    def _restore_workdir(self, original_path: str):
        """工作目录回退到主仓库根（被删路径恰是当前 workdir 时）"""
        try:
            from app.core.conversation.memory_manager import MemoryManagerCore

            mm = MemoryManagerCore.get_instance()
            if mm and self._project:
                mm.set_working_directory(self._project, original_path)
                self._current_workdir = original_path
                self.workingDirRestored.emit(original_path)
        except Exception:
            pass

    def _clear_rows(self):
        """清空所有子控件（先 hide 再 deleteLater，避免一帧叠影）"""
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            w.hide()
            w.deleteLater()
        self._main_card = None

    def _populate_rows(self):
        """重建：主仓库卡片置顶 + 子工作树列表 + 新建入口"""
        self._clear_rows()
        if not self._repo_info:
            return

        current_wd = self._current_workdir or self._original_folder
        normalized_wd = os.path.normpath(current_wd)

        main_wt = next((w for w in self._repo_info.worktrees if w.is_main), None)
        if main_wt is not None and os.path.isdir(main_wt.path):
            is_current = os.path.normpath(main_wt.path) == normalized_wd
            self._main_card = MainRepoCard(self._repo_info, is_current, parent=self)
            self._main_card.switched.connect(self._on_switch)
            self._main_card.branchSwitched.connect(self._on_main_branch_switch)
            self._rows_layout.addWidget(self._main_card)

        for wt in self._repo_info.worktrees:
            if wt.is_main or not os.path.isdir(wt.path):
                continue
            is_current = os.path.normpath(wt.path) == normalized_wd
            row = _WorktreeRow(wt, is_current, parent=self)
            row.switched.connect(self._on_switch)
            row.deleted.connect(self._on_deleted)
            self._rows_layout.addWidget(row)

        add = _AddWorktreeRow(self._repo_info.root, self._repo_info.current_branch, parent=self)
        add.createRequested.connect(self._on_create)
        self._rows_layout.addWidget(add)

        self.sizeChanged.emit(self.content_height())

    def content_height(self) -> int:
        """本组件需要的高度（各子控件高度之和 + 间距 + margins）"""
        m = self.layout().contentsMargins()
        spacing = self._rows_layout.spacing()
        total = 0
        count = 0
        for i in range(self._rows_layout.count()):
            w = self._rows_layout.itemAt(i).widget()
            if w is None:
                continue
            total += max(w.minimumHeight(), w.sizeHint().height())
            count += 1
        if count > 1:
            total += spacing * (count - 1)
        return total + m.top() + m.bottom()

    def _on_switch(self, worktree_path: str):
        if os.path.isdir(worktree_path):
            self.worktreeSwitched.emit(self._original_folder, worktree_path)

    def _on_main_branch_switch(self, branch: str):
        """主仓库切分支（后台执行，失败回滚下拉框并提示）"""
        repo_root = self._repo_info.root
        current = self._repo_info.current_branch

        def _on_ok(_result):
            _invalidate_repo_cache(self._original_folder)
            self._repopulate()
            self._show_info_dialog("已切换分支", f"主仓库当前分支：{branch}")

        def _on_fail(msg: str):
            if self._main_card is not None:
                self._main_card.revert_branch(current)
            self._show_error_dialog(
                "切换分支失败",
                f"无法切换到「{branch}」：\n\n{msg}\n\n"
                "常见原因：工作区有未提交改动与目标分支冲突，先提交或 stash 再切。",
            )

        worker = _WorktreeTaskWorker(_switch_branch_job, repo_root, branch)
        worker.signals.finished.connect(_on_ok)
        worker.signals.failed.connect(_on_fail)
        self._create_worker = worker
        _WORKTREE_POOL.start(worker)

    def _on_deleted(self, worktree_path: str):
        """UI 删除完成（主线程）：刷新并通知外层"""
        _invalidate_repo_cache(self._original_folder)
        self._repopulate()
        self.worktreeDeleted.emit(worktree_path)

    def _refresh(self):
        self._repopulate()

    def _on_create(self, branch_name: str, base_branch: str, description: str = ""):
        if self._create_busy:
            logger.info("[Worktree] 创建进行中，忽略重复请求")
            return
        self._create_busy = True
        repo_root = self._repo_info.root
        worktree_dir = os.path.join(repo_root, ".worktrees", branch_name.replace("/", "-"))

        def _on_ok(_r):
            self._on_create_finished(branch_name, worktree_dir, description)

        def _on_fail(msg: str):
            self._on_create_failed(branch_name, msg)

        worker = _WorktreeTaskWorker(
            _create_worktree_job, repo_root, branch_name, worktree_dir, base_branch or "HEAD"
        )
        worker.signals.finished.connect(_on_ok)
        worker.signals.failed.connect(_on_fail)
        self._create_worker = worker
        _WORKTREE_POOL.start(worker)

    def _on_create_finished(self, branch_name: str, worktree_dir: str, description: str = ""):
        self._create_busy = False
        self._create_worker = None
        if description:
            write_worktree_description(worktree_dir, description)
        self._pending_switch_path = worktree_dir
        _invalidate_repo_cache(self._original_folder)
        self._repopulate()

    def _on_create_failed(self, branch_name: str, msg: str):
        self._create_busy = False
        self._create_worker = None
        self._show_error_dialog(
            "创建失败",
            f"无法创建 worktree「{branch_name}」：\n\n{msg}\n\n"
            "如果分支已存在，可先删除旧 worktree 再创建",
        )

    def _show_error_dialog(self, title: str, message: str):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(420, 220)
        dlg.setStyleSheet(
            f"""
            QDialog {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(12)}
            }}
            """
        )
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)
        label = QLabel(message, dlg)
        label.setWordWrap(True)
        lay.addWidget(label, 1)
        btn = QPushButton("知道了", dlg)
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, 0, Qt.AlignRight)
        dlg.exec_()

    def _show_info_dialog(self, title: str, message: str):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(360, 150)
        dlg.setStyleSheet(
            f"""
            QDialog {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(12)}
            }}
            """
        )
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 16, 16, 16)
        label = QLabel(message, dlg)
        label.setWordWrap(True)
        lay.addWidget(label, 1)
        btn = QPushButton("好", dlg)
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, 0, Qt.AlignRight)
        dlg.exec_()
