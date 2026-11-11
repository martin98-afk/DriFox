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

import concurrent.futures
import dataclasses
import os
from typing import Dict, Optional

from loguru import logger

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel

from app.plugins.sdk import Colors, font_size_css
from app.plugins.sdk import GitWorktreeDetector
from app.plugins.sdk import get_font_family_css
from .worktree_section import (
    WorktreeSectionWidget,
    _DELETE_DONE,
    list_local_branches,
    read_all_worktree_meta,
)


def _caps(cap_id):
    """取某能力（无 ctx 场景统一走 sdk 的 caps_for，host 由宿主解析）。"""
    from app.plugins.sdk import caps_for

    return caps_for(cap_id)


class _PageLoadSignals(QObject):
    """页面后台探测完成信号（后台线程 → 主线程）"""

    finished = Signal(object)  # dict: {"ok": bool, "reason": str, "git_root": str, "info": obj}


class _PageLoadJob(QRunnable):
    """后台线程：探测 git 仓库并拉取 worktree 信息。

    这一串操作（detect_git / get_repo_info）内部全是 git 子进程，Windows 下
    单个 100~300ms，串起来必然卡主线程（实测切换页签阻塞 500ms+）。
    全部搬到后台，主线程只做 UI 落地。
    """

    def __init__(self, workdir: str, signals: _PageLoadSignals):
        super().__init__()
        self._workdir = workdir
        self._signals = signals

    def run(self):
        payload = {"ok": False, "reason": "", "git_root": "", "info": None, "main_root": "", "branches": []}
        try:
            # 三路并行（互不依赖的只读 git 子进程）：Windows 下每条子进程
            # 300~600ms，串行会把首屏拖到数秒；并行使关键路径 = 最慢一路
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                f_info = ex.submit(GitWorktreeDetector.get_repo_info, self._workdir)
                f_branches = ex.submit(list_local_branches, self._workdir)
                f_desc = ex.submit(read_all_worktree_meta, self._workdir)
                info = f_info.result()
                branches = f_branches.result()
                descriptions, creators = f_desc.result()

            if info is None:
                payload["reason"] = "not_git"
            elif not info.worktrees:
                payload["reason"] = "no_info"
            else:
                # 说明与创建者标记就地补到 wt 对象（_populate_rows 直接 getattr 读取）
                for wt in info.worktrees:
                    if not wt.is_main:
                        name = os.path.basename(wt.path)
                        wt.description = descriptions.get(name, "")
                        wt.creator = creators.get(name, "")
                # 当前目录落在某个 worktree 里时，info.root/current_branch 是
                # 子工作树视角；页面（主仓库卡片、分支切换）必须永远渲染主仓库，
                # 否则主仓库卡片会显示成子工作树、下拉还能选到已被其他 worktree
                # 检出的分支（git 必拒）。这里统一替换为主仓库视角。
                main_wt = next((w for w in info.worktrees if w.is_main), None)
                if main_wt and info.root != main_wt.path:
                    info = dataclasses.replace(info, root=main_wt.path, current_branch=main_wt.branch)
                payload["ok"] = True
                payload["info"] = info
                payload["main_root"] = info.root
                payload["branches"] = branches
        except Exception as e:  # noqa: BLE001 - 后台探测兜底
            payload["reason"] = f"error: {e}"
        self._signals.finished.emit(payload)


# 页面探测专用线程池（单线程：避免多次切页签并发起 git 进程）
_PAGE_POOL = QThreadPool()
_PAGE_POOL.setMaxThreadCount(1)


class WorkdirCard(QWidget):
    """工作目录卡片：显示当前目录 + 更换 / 打开目录。

    无 git 的机器上这是本页唯一可操作项；有 git 时它排在主仓库卡片上方，
    随时可以把工作目录换到任意文件夹（不必是 git 仓库）。
    """

    changed = Signal(str)  # 新的工作目录（空串=未选择）

    def __init__(self, workdir: str, is_git: bool, parent=None):
        super().__init__(parent)
        self._workdir = workdir or ""
        self._is_git = is_git
        self.setObjectName("workdirCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._setup_ui()

    def _setup_ui(self):
        Colors.refresh()
        self.setStyleSheet(
            f"""
            QWidget#workdirCard {{
                background-color: {Colors.CARD_BG.format(alpha=200)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("工作目录", self)
        title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(13)}"
        )
        head.addWidget(title)
        self._git_tag = QLabel("非 git", self)
        self._git_tag.setToolTip("该目录不是 git 仓库，因此不提供工作树管理")
        self._git_tag.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: {Colors.HOVER_BG};"
            f" {font_size_css(10)} padding: 1px 6px; border-radius: 3px;"
        )
        self._git_tag.setVisible(not self._is_git)
        head.addWidget(self._git_tag)
        head.addStretch()

        change = QLabel("更换", self)
        change.setCursor(Qt.PointingHandCursor)
        change.setStyleSheet(
            f"color: {Colors.TEXT_ACCENT}; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(11)}"
        )
        change.setToolTip("选择其它文件夹作为工作目录")
        change.mousePressEvent = lambda e: self._on_change()
        head.addWidget(change)
        layout.addLayout(head)

        self._path_label = QLabel(self._workdir or "（未设置）", self)
        self._path_label.setWordWrap(True)
        self._path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._path_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY};"
            f" {get_font_family_css()} {font_size_css(11)}"
        )
        layout.addWidget(self._path_label)

    def _on_change(self):
        from PySide6.QtWidgets import QFileDialog

        start = self._workdir if self._workdir and os.path.isdir(self._workdir) else ""
        folder = QFileDialog.getExistingDirectory(self, "选择工作目录", start, QFileDialog.ShowDirsOnly)
        if folder:
            self.changed.emit(folder)

    def set_workdir(self, workdir: str):
        self._workdir = workdir or ""
        self._path_label.setText(self._workdir or "（未设置）")

    def set_is_git(self, is_git: bool):
        """更新 git 归属标记（探测结果回来后调用）"""
        self._is_git = bool(is_git)
        self._git_tag.setVisible(not self._is_git)


class SystemWorktreePage(QWidget):
    """工作树页：主仓库卡片 + 子工作树列表"""

    workingDirChanged = Signal(str)  # 工作目录路径，空字符串=清除

    def __init__(self, parent=None, context: Optional[Dict[str, object]] = None):
        super().__init__(parent)
        self._context = context or {}
        self._memory_manager = None
        self._current_project = "默认项目"
        # 多窗口隔离：实例级工作目录缓存（{project: workdir_path}）
        self._instance_workdir: Dict[str, str] = {}
        self._section: WorktreeSectionWidget = None
        self._workdir_card: WorkdirCard = None
        self._body: QWidget = None
        self._empty_hint: QLabel = None
        self._repo_info = None
        # 后台探测去重：每次重建自增，回来时对不上就丢弃（防切项目串数据）
        self._request_id = 0
        self._load_signals = None
        self._load_job = None
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
        self._body_layout.addStretch(1)
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
        self._body_layout.insertWidget(0, self._empty_hint, 0)

        self._section = None
        self._branches: list = []
        # 删除进行中行/section 可能被重建销毁（其完成回调发不出信号），兜底刷新
        _DELETE_DONE.done.connect(lambda _p: self.refresh_data())

    def _clear_body(self):
        for attr in ("_section", "_workdir_card"):
            w = getattr(self, attr, None)
            if w is not None:
                w.hide()
                w.deleteLater()
                setattr(self, attr, None)
        self._empty_hint.setVisible(False)

    def _rebuild(self):
        """按当前项目/工作目录重建内容（git 探测走后台线程，主线程不阻塞）"""
        self._clear_body()
        self._repo_info = None

        project = self._current_project
        workdir = self._get_effective_workdir(project)

        if not workdir:
            self._mount_workdir_card("", is_git=False)
            self._show_empty("还没有设置工作目录。\n点击上方「更换」选择任意文件夹即可开始。")
            return

        if not os.path.isdir(workdir):
            self._mount_workdir_card(workdir, is_git=False)
            self._show_empty(f"工作目录不存在：\n{workdir}\n\n点击上方「更换」另选一个文件夹。")
            return

        # 目录有效但 git 归属未知：先挂卡片（is_git 待探测结果修正），再后台探测
        self._mount_workdir_card(workdir, is_git=True)

        # 探测中：占位提示（避免空白页误以为没数据）
        self._request_id += 1
        req = self._request_id
        self._show_empty("正在读取仓库信息…")

        signals = _PageLoadSignals()
        signals.finished.connect(lambda payload, r=req: self._on_repo_probed(r, payload, workdir, project))
        job = _PageLoadJob(workdir, signals)
        self._load_signals = signals
        self._load_job = job
        _PAGE_POOL.start(job)

    def _on_repo_probed(self, request_id: int, payload: dict, workdir: str, project: str):
        """后台探测完成（主线程）：落地 UI 或空态"""
        if request_id != self._request_id:
            return  # 过期结果：用户已切到别的项目/工作目录
        if not payload.get("ok"):
            reason = payload.get("reason", "")
            is_git = reason == "no_info"  # 是 git，只是读不出信息
            self._remount_workdir_card(workdir, is_git=is_git)
            if reason == "not_git":
                self._show_empty(
                    "该目录不是 git 仓库，无法管理工作树。\n"
                    "工作目录已生效，可正常使用；如需工作树管理，请「更换」到 git 仓库目录。"
                )
            elif reason == "no_info":
                self._show_empty(f"读取仓库信息失败：\n{payload.get('git_root', workdir)}")
            else:
                self._show_empty(f"读取仓库信息失败：\n{reason}")
            return

        repo_info = payload["info"]
        # original_folder 必须是主仓库根（worktree 缓存失效/刷新都按它做 key）
        main_root = payload.get("main_root") or repo_info.root
        branches = payload.get("branches") or []
        self._empty_hint.setVisible(False)
        self._remount_workdir_card(workdir, is_git=True)
        self._repo_info = repo_info
        self._branches = list(branches)
        self._mount_section(repo_info, main_root, workdir, project, branches)

    def _mount_section(self, repo_info, main_root: str, workdir: str, project: str, branches: list):
        """挂载工作树 section（旧实例同帧替换：新先插入、旧再 deleteLater，无空白帧）"""
        old = self._section
        section = WorktreeSectionWidget(
            repo_info,
            original_folder=main_root,
            parent=self._body,
            current_workdir=workdir,
            project=project,
            branches=branches,
        )
        section.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        section.setFixedHeight(section.content_height())
        section.sizeChanged.connect(section.setFixedHeight)
        section.worktreeSwitched.connect(self._on_worktree_switched)
        section.workingDirRestored.connect(self._on_workdir_restored)
        section.worktreeDeleted.connect(self._on_worktree_deleted)
        section.repoInfoRefreshed.connect(self._on_section_info_refreshed)
        # 拉伸因子 0：多余高度留给尾部弹性，不能被均摊到行间距上
        self._body_layout.insertWidget(1, section, 0)
        self._section = section
        if old is not None and old is not section:
            old.hide()
            old.deleteLater()

    def _mount_workdir_card(self, workdir: str, is_git: bool):
        """在 body 顶部挂工作目录卡片（索引 0，始终最上）"""
        card = WorkdirCard(workdir, is_git, parent=self._body)
        card.changed.connect(self._on_workdir_changed)
        self._body_layout.insertWidget(0, card)
        self._workdir_card = card

    def _remount_workdir_card(self, workdir: str, is_git: bool):
        """探测结果回来后更新卡片的 git 标记（原地更新，不重建：
        重建会打乱 body 内控件顺序，且 deleteLater 期间新旧控件会重叠一帧）"""
        if self._workdir_card is None:
            self._mount_workdir_card(workdir, is_git)
            return
        self._workdir_card.set_workdir(workdir)
        self._workdir_card.set_is_git(is_git)

    def _show_empty(self, text: str):
        self._empty_hint.setText(text)
        self._empty_hint.setVisible(True)

    def _on_workdir_changed(self, path: str):
        """用户更换工作目录：持久化 + 上抛宿主 + 重建（可能从非 git 变 git）"""
        if not path:
            return
        project = self._current_project
        memory_mgr = self._get_memory_manager()
        if memory_mgr is not None:
            try:
                memory_mgr.set_working_directory(project, path)
            except Exception as e:
                logger.warning(f"[worktree-manager] 持久化工作目录失败: {e}")
        self._instance_workdir[project] = path
        self._emit_working_dir(path)
        self._rebuild()

    # ── 交互 ──

    def _on_section_info_refreshed(self, repo_info, branches):
        """section 后台刷新完成：同步页面缓存（切换即时重建的数据源）

        不触发重建：section 已自行渲染；这里只同步数据，否则新建工作树后
        自动切换会用旧缓存把新列表覆盖回去（表现为建完不刷新）。
        """
        self._repo_info = repo_info
        self._branches = list(branches or [])

    def _on_worktree_deleted(self, worktree_path: str):
        """删除工作树后刷新；删的是当前工作目录所在子树则先回退主仓库根

        否则 _rebuild 会甩出「工作目录不存在」空态（workdir 仍指向已删目录）。
        """
        current = self._instance_workdir.get(self._current_project)
        if current and self._repo_info is not None:
            cur_n = os.path.normcase(os.path.normpath(current))
            del_n = os.path.normcase(os.path.normpath(worktree_path))
            if cur_n == del_n or cur_n.startswith(del_n + os.sep):
                root = self._repo_info.root
                memory_mgr = self._get_memory_manager()
                if memory_mgr is not None:
                    try:
                        memory_mgr.set_working_directory(self._current_project, root)
                    except Exception as e:
                        logger.warning(f"[worktree-manager] 回退工作目录持久化失败: {e}")
                self._instance_workdir[self._current_project] = root
                self._emit_working_dir(root)
        self.refresh_data()

    def _on_worktree_switched(self, original_folder: str, worktree_path: str):
        """切到指定工作树/主仓库：持久化 + 上抛宿主 + 缓存信息即时重建

        同仓库内切换只有「谁是当前」变化（worktree 列表/分支/说明/领先落后
        均不变），复用已探测的 repo_info 即时重建：零 git 探测、不清空页面、
        无「正在读取仓库信息…」占位闪烁。
        """
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
        if self._repo_info is None or self._workdir_card is None:
            self.refresh_data()
            return
        self._workdir_card.set_workdir(worktree_path)
        self._workdir_card.set_is_git(True)
        self._empty_hint.setVisible(False)
        self._mount_section(self._repo_info, self._repo_info.root, worktree_path, project, self._branches)

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
