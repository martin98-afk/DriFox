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
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, Qt, pyqtSignal
from PyQt5.QtGui import QPaintEvent
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import ComboBox

from app.plugins.sdk import Colors, font_size_css, readable_text_on
from app.plugins.sdk import GitWorktreeDetector
from app.plugins.sdk import get_font_family_css, get_icon
from app.plugins.sdk import ElidedLabel

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


def write_worktree_creator(wt_path: str, creator: str) -> bool:
    """写工作树创建者标记（human/ai），用于区分列表里的 AI 标签；写失败不影响主流程"""
    if not wt_path or not os.path.isdir(wt_path) or creator not in ("human", "ai"):
        return False
    key = f"worktree.{os.path.basename(wt_path)}.creator"
    try:
        return _git(["config", key, creator], wt_path, timeout=5).returncode == 0
    except Exception:
        return False


def read_all_worktree_meta(repo_root: str) -> tuple:
    """批量读取所有 worktree 说明与创建者标记（一条 git config 读全部，替代逐 worktree 读）

    元数据写在各仓库共享的 common config 里，`git config --get-regexp` 一轮即
    可取回；返回 ({worktree目录名: 说明}, {worktree目录名: creator})。
    解析失败/无配置返回空 dict，说明只是辅助信息，不能因为它让整个列表渲染失败。
    """
    if not repo_root or not os.path.isdir(repo_root):
        return {}, {}
    try:
        r = _git(["config", "--get-regexp", r"^worktree\..*\.(description|creator)$"], repo_root, timeout=5)
        if r.returncode != 0:
            return {}, {}
        descriptions, creators = {}, {}
        prefix = "worktree."
        for line in (r.stdout or "").splitlines():
            if not line.strip():
                continue
            key, _, value = line.partition(" ")
            if not key.startswith(prefix):
                continue
            name, _, field = key[len(prefix):].rpartition(".")
            if not name:
                continue
            if field == "description":
                descriptions[name] = value
            elif field == "creator":
                creators[name] = value
        return descriptions, creators
    except Exception:
        return {}, {}

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
        self.setFixedSize(size + 4, size + 4)
        self.setStyleSheet("background: transparent;")

    def set_icon(self, icon_name: str):
        """更换图标名（触发重绘）"""
        self._icon_name = icon_name
        self.update()

    def paintEvent(self, event):
        from PyQt5.QtGui import QPainter

        p = QPainter(self)
        get_icon(self._icon_name).paint(p, self.rect(), Qt.AlignCenter)
        p.end()


class _DeleteDoneBroker(QObject):
    """删除任务完成的模块级中转信号

    删除进行中目录已消失，页面任意重绘会触发 _check_paths 重建、切换会
    触发 _mount_section 替换，都可能把正在删除的行（含所在 section）提前
    销毁；worker 完成回调不能再依赖行存活。正常路径仍走行信号，本信号
    仅在行已析构时由页面直连兜底刷新。
    """

    done = pyqtSignal(str)

_DELETE_DONE = _DeleteDoneBroker()

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
    """后台线程：拉取仓库信息 + 缺失工作树清理 + 分支列表

    Returns:
        (GitRepoInfo | None, missing_paths: list[str], branches: list[str])
    """
    info = GitWorktreeDetector.get_repo_info(git_root)
    if info is None:
        return None, [], []
    missing = [wt.path for wt in info.worktrees if not wt.is_main and not os.path.isdir(wt.path)]
    if missing:
        try:
            _git(["worktree", "prune"], info.root, timeout=10)
        except Exception:
            pass
    # 说明与创建者标记不随 get_repo_info 返回，批量补一次（单条 git config 取回全部；
    # 逐 worktree 读在 Windows 下每条子进程 300ms+）
    descriptions, creators = read_all_worktree_meta(info.root)
    for wt in info.worktrees:
        if not wt.is_main:
            name = os.path.basename(wt.path)
            setattr(wt, "description", descriptions.get(name, ""))
            setattr(wt, "creator", creators.get(name, ""))
    branches = list_local_branches(info.root)
    return info, missing, branches


class MainRepoCard(QWidget):
    """主仓库卡片：分支切换器 + 路径 + 领先落后

    只有主仓库允许切分支：子工作树的存在意义就是固定检出某个分支，
    允许它切分支等于让它与别的会话抢同一个分支。
    """

    switched = pyqtSignal(str)  # worktree_path（切换到该工作目录）
    branchSwitched = pyqtSignal(str)  # branch_name

    def __init__(self, repo_info, is_current: bool, parent=None, branches=None):
        super().__init__(parent)
        self._repo_info = repo_info
        self._is_current = is_current
        self._switching = False
        # 分支列表由后台线程预先取好（主线程调 git 会阻塞 100~300ms/次）
        self._branches = list(branches or [])
        self._setup_ui()

    def _setup_ui(self):
        root = self._repo_info.root
        self.setObjectName("mainRepoCard")
        # WA_StyledBackground：不设它时 QWidget 子类的 background QSS 根本不绘制
        self.setAttribute(Qt.WA_StyledBackground, True)
        Colors.refresh()
        # 左侧竖条：仅当前工作目录落在主仓库时用强调色（与子工作树行同语义），
        # 否则用边框灰；当前在子工作树时主树不再高亮
        bar = Colors.TEXT_ACCENT if self._is_current else Colors.BORDER
        self.setStyleSheet(
            f"""
            QWidget#mainRepoCard {{
                background-color: {Colors.CARD_BG.format(alpha=160)};
                border: none;
                border-left: 3px solid {bar};
                border-radius: 6px;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(5)
        # 内容三行：head 20 + 分支 26 + 路径 17 + 两次 spacing 5 + 上下 padding 18
        self.setFixedHeight(91)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        # ── 第一行：图标 + 仓库名 + 当前标记 + 切工作目录 ──
        head = QHBoxLayout()
        head.setSpacing(6)

        icon = ThemeIconLabel("根目录", 16, self)
        head.addWidget(icon)

        name = ElidedLabel(os.path.basename(root.rstrip("/\\")) or root, self)
        name.setMinimumWidth(40)
        name.setFixedHeight(20)
        # ElidedLabel 默认水平 Ignored 策略会把本控件按 0 宽排位，同排的
        # 「当前」tag 直接叠上来盖住项目名；改 Preferred 让 sizeHint 参与分配，
        # 空间不足时仍走省略逻辑（resizeEvent 驱动，行为不变）
        name.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        name.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(13)}"
        )
        head.addWidget(name)

        if self._is_current:
            badge = QLabel("当前", self)
            badge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            badge.setFixedHeight(19)
            # 真黑等主题 accent 与 BUTTON_TEXT_ON_ACCENT 可能同为纯白（白底白字不可见），
            # 前景色按底色 WCAG 对比度自适应
            on_accent = readable_text_on(Colors.TEXT_ACCENT, Colors.BUTTON_TEXT_ON_ACCENT)
            badge.setStyleSheet(
                f"color: {on_accent}; background: {Colors.TEXT_ACCENT};"
                f" {get_font_family_css()} {font_size_css(10)} padding: 1px 6px; border-radius: 3px;"
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
            btn.setFixedHeight(20)
            btn.mousePressEvent = lambda e: self.switched.emit(root)
            head.addWidget(btn)

        layout.addLayout(head)

        # ── 第二行：分支切换器 ──
        branch_row = QHBoxLayout()
        branch_row.setSpacing(6)

        branch_label = QLabel("分支", self)
        branch_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)}"
        )
        branch_label.setFixedHeight(26)
        branch_row.addWidget(branch_label)

        self._branch_combo = ComboBox(self)
        self._branch_combo.setMinimumWidth(140)
        self._branch_combo.setFixedHeight(26)
        branches = list(self._branches)
        # 被其他（非主仓库）worktree 检出的分支在主仓库 checkout 必然失败
        # （fatal: already used by worktree），下拉里直接排除，只留主仓库可切分支
        taken = {
            w.branch
            for w in self._repo_info.worktrees
            if not w.is_main and w.branch and w.branch != "(detached)"
        }
        branches = [b for b in branches if b not in taken]
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
        path_label = ElidedLabel(root, self)
        path_label.setMinimumWidth(60)
        path_label.setFixedHeight(17)
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
        self._del_btn: QLabel = None
        self._delete_worker = None
        desc_text = str(getattr(wt, "description", "") or "")
        # 行高按「分支行 + 说明行 + 路径行」逐行累加（每行 17px，间距 1，上下 padding 8）
        # 用固定高度避免 QLabel 被拉成竖条；行数不同高度就不同，不能只写死两个值。
        rows = 2 + (1 if desc_text else 0)
        self.setFixedHeight(rows * 17 + 1 * (rows - 1) + 8)
        self.setObjectName("worktreeRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._setup_ui()

    def _setup_ui(self):
        wt = self._wt
        Colors.refresh()
        bar = Colors.TEXT_ACCENT if self._is_current else Colors.BORDER
        self.setStyleSheet(
            f"""
            QWidget#worktreeRow {{
                background-color: {Colors.CARD_BG.format(alpha=110)};
                border: none;
                border-left: 3px solid {bar};
                border-radius: 6px;
            }}
            """
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 8, 4)
        layout.setSpacing(6)

        dot = QLabel("", self)
        dot.setFixedSize(7, 7)
        if self._is_current:
            dot.setStyleSheet(f"background-color: {Colors.TEXT_ACCENT}; border-radius: 3px;")
        else:
            dot.setStyleSheet(f"background: transparent; border: 1.5px solid {Colors.BORDER}; border-radius: 3px;")
        # 顶对齐：与外层 QHBoxLayout 垂直居中会把圆点压到中间行
        dot_col = QVBoxLayout()
        dot_col.setContentsMargins(0, 0, 0, 0)
        dot_col.setSpacing(0)
        dot_col.addWidget(dot)
        dot_col.addStretch(1)
        layout.addLayout(dot_col)

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
        # ElidedLabel：sizeHint 取完整文本宽度，能向布局争取空间；不够时才省略。
        # 用裸 QLabel + Ignored 会让它放弃争空间，宽度塌到 minimumWidth，
        # 后面的领先/落后标签就会叠到它身上（实测 branch 宽 60px、need 396px）。
        branch_label = ElidedLabel(wt.branch, self)
        branch_label.setStyleSheet(f"{branch_ss} {get_font_family_css()} {font_size_css(12)}")
        branch_label.setFixedHeight(17)
        branch_label.setMinimumWidth(60)
        branch_label.setToolTip(wt.branch)
        top.addWidget(branch_label, 1)  # 拉伸因子 1：本行唯一可伸缩项

        desc = str(getattr(wt, "description", "") or "")
        # creator 键（human/ai）区分创建者；无键的存量按 AI 处理（旧行为：带说明即 AI）
        creator = str(getattr(wt, "creator", "") or "") or "ai"
        if desc and creator == "ai":
            ai_tag = QLabel("AI", self)
            on_accent = readable_text_on(Colors.TEXT_ACCENT, Colors.BUTTON_TEXT_ON_ACCENT)
            ai_tag.setStyleSheet(
                f"color: {on_accent}; background: {Colors.TEXT_ACCENT};"
                f" {get_font_family_css()} {font_size_css(9)} padding: 0 4px; border-radius: 2px;"
            )
            ai_tag.setFixedHeight(15)
            ai_tag.setToolTip(f"AI 创建：{desc}")
            top.addWidget(ai_tag, 0)

        # 领先/落后：固定宽度、右对齐，不参与伸缩
        deltas = []
        if wt.behind_main > 0:
            deltas.append((f"-{wt.behind_main}", f"落后主仓库 {wt.behind_main} 个提交", "#f85149"))
        if wt.ahead_main > 0:
            deltas.append((f"+{wt.ahead_main}", f"领先主仓库 {wt.ahead_main} 个提交", "#3fb950"))
        for label_text, tip, color in deltas:
            lb = QLabel(label_text, self)
            lb.setToolTip(tip)
            lb.setStyleSheet(f"color: {color}; {get_font_family_css()} {font_size_css(10)}")
            lb.setFixedHeight(17)
            lb.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            top.addWidget(lb, 0)

        text_box.addLayout(top)

        # 说明行（AI 创建的工作树必带，人工创建的可选）
        if desc:
            desc_label = ElidedLabel(desc, self)
            desc_label.setFixedHeight(17)
            desc_label.setMinimumWidth(80)
            desc_label.setStyleSheet(
                f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(11)}"
            )
            desc_label.setToolTip(desc)
            text_box.addWidget(desc_label)

        path_label = ElidedLabel(wt.path, self)
        path_label.setFixedHeight(17)
        path_label.setMinimumWidth(80)
        path_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)}"
        )
        path_label.setToolTip(wt.path)
        text_box.addWidget(path_label)

        layout.addLayout(text_box, 1)

        # 右侧操作：放进独立垂直布局并顶对齐。
        # 直接塞进外层 QHBoxLayout 会被垂直居中到 text_box 的中间行，
        # 三行内容时正好压住路径那一行（表现为文字互相叠印）。
        ops = QVBoxLayout()
        ops.setContentsMargins(0, 0, 0, 0)
        ops.setSpacing(0)

        op_row = QHBoxLayout()
        op_row.setContentsMargins(0, 0, 0, 0)
        op_row.setSpacing(6)

        if not self._is_current:
            switch_btn = QLabel("切换", self)
            switch_btn.setCursor(Qt.PointingHandCursor)
            switch_btn.setStyleSheet(
                f"color: {Colors.TEXT_ACCENT}; font-weight: 600;"
                f" {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
            )
            switch_btn.setFixedHeight(17)
            switch_btn.setToolTip("把当前会话工作目录切到该工作树")
            switch_btn.mousePressEvent = lambda e: self.switched.emit(self._wt.path)
            op_row.addWidget(switch_btn)

        self._del_btn = ThemeIconLabel("删除", 13, self)
        self._del_btn.setCursor(Qt.PointingHandCursor)
        self._del_btn.setToolTip("删除工作树")
        self._del_btn.mousePressEvent = lambda e: self._on_delete_click()
        op_row.addWidget(self._del_btn)

        ops.addLayout(op_row)
        ops.addStretch(1)  # 顶对齐：操作行贴住第一行，不被居中
        layout.addLayout(ops)

    def _on_delete_click(self):
        """弹框确认后删除：工作树目录一并移除，未合并提交的分支自动保留

        ConfirmDialog 用 close() 而非 accept()，exec_() 返回值恒为假，
        必须连 confirmed 信号触发，不能用返回值判断。
        """
        from app.plugins.sdk import ConfirmDialog

        dialog = ConfirmDialog(
            title="删除工作树",
            content=(
                f"确定要删除「{os.path.basename(self._wt.path)}」吗？\n"
                f"{self._wt.path}\n\n"
                "工作树目录将被一并删除；分支含未合并提交时仅删目录、分支保留。"
            ),
            confirm_text="删除",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(self._do_delete)
        dialog.exec_()

    def _do_delete(self):
        self._del_btn.setEnabled(False)
        self._del_btn.setToolTip("删除中…")
        path, branch = self._wt.path, self._wt.branch

        def _on_finished(_r):
            # 删除期间页面可能已重建（目录消失即触发 _check_paths/_mount_section），
            # 本行可能已被销毁；行死了走模块级 broker，页面直连它兜底刷新
            try:
                self.deleted.emit(path)
            except RuntimeError:
                _DELETE_DONE.done.emit(path)

        def _on_failed(msg: str):
            try:
                self._on_delete_failed(msg)
            except RuntimeError:
                logger.warning(f"[Worktree] 删除失败（行已销毁）: {msg}")

        worker = _WorktreeTaskWorker(_delete_worktree_job, path, branch)
        worker.signals.finished.connect(_on_finished)
        worker.signals.failed.connect(_on_failed)
        self._delete_worker = worker
        _WORKTREE_POOL.start(worker)

    def _on_delete_failed(self, msg: str):
        logger.warning(f"[Worktree] 删除失败: {msg}")
        self._del_btn.setEnabled(True)
        self._del_btn.setToolTip("删除工作树")


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
        layout.setContentsMargins(10, 2, 8, 2)
        layout.setSpacing(6)

        icon = ThemeIconLabel("新建", 13, self)
        layout.addWidget(icon)

        add_label = QLabel("新建工作树", self)
        add_label.setCursor(Qt.PointingHandCursor)
        add_label.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(11)}"
        )
        add_label.mousePressEvent = lambda e: self._on_add()
        icon.mousePressEvent = lambda e: self._on_add()
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
    repoInfoRefreshed = pyqtSignal(object, list)  # 后台刷新完成：(repo_info, branches)

    def __init__(
        self, repo_info, original_folder: str, parent=None, current_workdir: str = None, project: str = None,
        branches=None,
    ):
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
        # 分支列表由页面后台探测直传（一次后台并行拉全量，首帧即完整）；
        # 无直传时下拉框先只有当前分支，后续刷新操作会补全
        self._branches: list = list(branches or [])
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
        # 高度由 content_height() 决定（宿主按它 setFixedHeight），自身不参与拉伸
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)

        self._rows = QWidget(self)
        self._rows_layout = QVBoxLayout(self._rows)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(5)
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
        info, missing_paths, branches = payload
        self._branches = list(branches or [])
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
        # 先同步页面缓存再触发自动切换：切换的即时重建依赖页面缓存的最新数据
        # （否则新建后 pending_switch 用旧缓存覆盖，新工作树刚刷出就被旧列表顶掉）
        self.repoInfoRefreshed.emit(info, self._branches)

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
            self._main_card = MainRepoCard(
                self._repo_info, is_current, parent=self, branches=getattr(self, "_branches", None)
            )
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
        """本组件需要的高度（各子控件高度之和 + 间距 + margins）

        子控件都是 setFixedHeight 的，统一取 maximumHeight()：sizeHint 在
        布局尚未激活时会返回默认值，minimumHeight 又可能小于实际固定值。
        """
        m = self.layout().contentsMargins()
        spacing = self._rows_layout.spacing()
        total = 0
        count = 0
        for i in range(self._rows_layout.count()):
            w = self._rows_layout.itemAt(i).widget()
            if w is None:
                continue
            h = w.maximumHeight()
            if h <= 0 or h > 100000:
                h = max(w.minimumHeight(), w.sizeHint().height())
            total += h
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
        # 手动创建打 human 标：无此键的带说明工作树会按旧行为被标成 AI
        write_worktree_creator(worktree_dir, "human")
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
