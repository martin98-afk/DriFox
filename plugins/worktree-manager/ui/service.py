# -*- coding: utf-8 -*-
"""WorktreeService — 工作树管理服务（插件内聚）

迁移自 main_widget：worktree 检测/切换/恢复 + 分支标签检测/缓存。
主程序经 UIPluginRegistry.get_service("worktree") 门面调用；
服务缺失时主程序门面 no-op，会话功能不受影响（降级语义见设计文档）。
"""
from __future__ import annotations

import os
from typing import Any, Dict

from loguru import logger


class WorktreeService:
    """工作树服务（插件注册到 UIPluginRegistry；无 Qt 对象成员，可安全单例）"""

    SERVICE_NAME = "worktree"

    # workdir→branch 类级缓存（原 OpenAIChatToolWindow._branch_cache 迁入）
    _branch_cache: Dict[str, str] = {}
    _MAX_BRANCH_CACHE = 64

    # ── 会话联动 ──

    def get_current_worktree_path(self, mw: Any) -> str:
        """检测当前工作目录是否在 git worktree 中，返回 worktree 路径（空字符串表示不在）。

        迁移自 main_widget._get_current_worktree_path。
        """
        workdir = mw._current_workdir.get(mw._current_project)
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

    def get_session_worktree_kwargs(self, mw: Any) -> dict:
        """发消息/保存会话时的 worktree 元数据（空串=主仓库，清除旧关联）。"""
        return {"worktree_path": self.get_current_worktree_path(mw)}

    def on_session_loaded(self, mw: Any, session_record: dict) -> None:
        """会话加载联动：有关联 worktree 切过去；没有则回主仓库。

        迁移自 main_widget 会话加载段（原 L13546-13553 / L16044-16051 同逻辑）。
        规则：会话有 worktree_path → 切到该 worktree；
              会话没有 worktree_path → 切回主仓库（如果当前在 worktree 中）。
        """
        worktree_path = (session_record or {}).get("worktree_path", "") or ""
        current_wt = self.get_current_worktree_path(mw)
        if worktree_path and os.path.isdir(worktree_path) and worktree_path != current_wt:
            self.switch_to_worktree(mw, worktree_path)
        elif not worktree_path and current_wt:
            self.restore_main_repo(mw)

    def switch_to_worktree(self, mw: Any, worktree_path: str) -> None:
        """切换到指定 worktree，幂等——已在目标 worktree 中则跳过。

        加载会话时自动调用：会话关联了哪个 worktree，就切到哪个 worktree。
        迁移自 main_widget._switch_to_worktree。
        """
        if not worktree_path or not os.path.isdir(worktree_path):
            return
        # zombie 过滤：.git 指向的 gitdir 已消失（主仓库 .git 被删/重建）的
        # worktree 不切换，避免把工作目录切进 git 已不认的目录
        from app.utils.git_worktree import GitWorktreeDetector

        if not GitWorktreeDetector.is_valid_worktree_link(worktree_path):
            logger.warning(
                f"[WorktreeService] 跳过切换到已失效的 worktree: {worktree_path}（项目: {mw._current_project}）"
            )
            return
        project = mw._current_project

        # 幂等：已在目标 worktree 中则跳过
        if mw._current_workdir.get(project) == worktree_path:
            return

        # 1. 通过 memory_manager 切换工作目录
        if mw.backend and mw.backend.memory_manager:
            mm = mw.backend.memory_manager
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
        mw._current_workdir[project] = worktree_path
        if mw.backend and mw.backend.tool_executor:
            mw.backend.tool_executor.set_workdir(worktree_path)
        self.update_branch(mw)

        # 3. 刷新右侧工作台关键文档/工作树 UI
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is not None:
                tm.refresh_workbench()
        except Exception:
            pass

        logger.info(f"[WorktreeService] 已自动切换到 worktree: {worktree_path}（项目: {project}）")
        # 团队模式：worktree 切换全员同步（统一工作树）
        mw._broadcast_team_workdir(worktree_path)

    def restore_main_repo(self, mw: Any) -> None:
        """从 worktree 切换回主仓库，幂等——已不在 worktree 中则跳过。

        加载主仓库会话时自动调用：会话没有关联 worktree，说明属于主仓库。
        迁移自 main_widget._restore_main_repo。
        """
        project = mw._current_project
        current_wt = self.get_current_worktree_path(mw)
        if not current_wt:
            # 已不在 worktree 中，无需切换
            return

        from app.utils.git_worktree import GitWorktreeDetector

        main_repo = GitWorktreeDetector.get_main_repo_path(current_wt)
        if not main_repo or not os.path.isdir(main_repo):
            logger.warning(f"[WorktreeService] 无法找到主仓库路径，跳过切换（当前 worktree: {current_wt}）")
            self.update_branch(mw)
            return

        # 幂等：已回到主仓库则跳过
        if mw._current_workdir.get(project) == main_repo:
            return

        if mw.backend and mw.backend.memory_manager:
            mm = mw.backend.memory_manager
            mm.set_working_directory(project, main_repo)

        mw._current_workdir[project] = main_repo
        if mw.backend and mw.backend.tool_executor:
            mw.backend.tool_executor.set_workdir(main_repo)
        self.update_branch(mw)

        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is not None:
                tm.refresh_workbench()
        except Exception:
            pass

        logger.info(f"[WorktreeService] 已自动切换回主仓库: {main_repo}（项目: {project}）")
        # 团队模式：切回主仓库全员同步（统一工作树）
        mw._broadcast_team_workdir(main_repo)

    # ── 分支标签 ──

    def update_branch(self, mw: Any) -> None:
        """刷新分支标签（原 main_widget._update_branch 迁入）。

        检测状态（request_id/signals）挂在 per-window 的 BranchChip 上，
        窗口销毁随之回收，服务单例无 per-window 残留。
        """
        bw = getattr(mw, "_branch_widget", None)
        if bw is None:
            return
        workdir = mw._resolve_project_workdir()

        # Phase A（同步、即时）：tooltip 先显示项目名 + 工作目录
        tooltip = mw._current_project
        if workdir:
            tooltip += f"\n{workdir}"
        mw._project_avatar.setToolTip(tooltip)

        # 先隐藏分支标签，等后台检测完成再决定显示
        bw.setVisible(False)

        if not workdir or not os.path.isdir(workdir):
            return

        # 缓存命中：直接应用，避免重复 git 调用（类级缓存，跨窗口共享）
        cache = WorktreeService._branch_cache
        if workdir in cache:
            self._apply_branch_to_ui(mw, workdir, cache[workdir])
            return

        # 发起后台检测：自增 request_id 使在飞旧任务过期（per-widget 状态）
        bw._detect_request_id = getattr(bw, "_detect_request_id", 0) + 1
        if getattr(bw, "_detect_signals", None) is None:
            from .branch_chip import BranchDetectSignals

            bw._detect_signals = BranchDetectSignals()
            bw._detect_signals.finished.connect(bw.on_branch_detected)
        from PyQt5.QtCore import QThreadPool

        from .branch_chip import BranchDetectTask

        task = BranchDetectTask(workdir, bw._detect_request_id, bw._detect_signals)
        QThreadPool.globalInstance().start(task)

    def on_branch_detected(self, mw: Any, request_id: int, workdir: str, branch: str, ok: bool) -> None:
        """后台 git 分支检测完成回调。

        ★ 修复缺陷1：ok=False（git 超时/文件锁/异常）不写缓存，保持隐藏待下次重检，
        杜绝「偶发失败 → 空结果进缓存 → 标签永久消失」。
        ★ 修复缺陷2：写缓存用任务携带的 workdir（不再重新 resolve）；
          UI 应用前校验当前 workdir 仍指向任务 workdir，防止切换竞态串显。
        """
        bw = getattr(mw, "_branch_widget", None)
        if bw is None:
            return
        if request_id != getattr(bw, "_detect_request_id", -1):
            return  # 过期结果：用户已切换，丢弃
        if not ok:
            return
        cache = WorktreeService._branch_cache
        if len(cache) >= self._MAX_BRANCH_CACHE:
            # Python 3.7+ dict 保插入序：弹出最早插入的 key
            cache.pop(next(iter(cache)), None)
        cache[workdir] = branch
        if request_id != getattr(bw, "_detect_request_id", -1):
            return  # 缓存写完后再校验一次（并发切换）
        if mw._resolve_project_workdir() != workdir:
            return  # 当前 workdir 已变：只缓存，不上屏
        self._apply_branch_to_ui(mw, workdir, branch)

    def _apply_branch_to_ui(self, mw: Any, workdir: str, branch: str) -> None:
        """应用分支结果到 tooltip 和分支标签。迁移自 main_widget._apply_branch_to_ui。"""
        tooltip = mw._current_project
        if workdir:
            tooltip += f"\n{workdir}"
        if branch:
            tooltip += f"\n🌿 {branch}"
        mw._project_avatar.setToolTip(tooltip)

        bw = mw._branch_widget
        if branch:
            display = branch if len(branch) <= 20 else branch[:8] + "…" + branch[-8:]
            bw.setText(display)
            bw.setToolTip(f"分支: {branch}\n点击打开关键文档")
            bw.setVisible(True)
        else:
            bw.setVisible(False)

    def copy_branch_state(self, target: Any, source: Any) -> None:
        """复制/分支窗口继承源窗口分支标签状态（原 main_widget._copy_branch_from 迁入）。

        复制窗口与源窗口共享完全相同的项目与工作目录，git 分支必然一致，
        跳过同步 git 子进程调用，直接复制源窗口已渲染的 UI 状态。

        ★ 修复缺陷3：源标签不可见（正在检测中）时安排 200ms 后重检，
        缓存命中零 git 开销，无缓存则一次后台检测，消除复制瞬态永久隐藏。
        """
        from PyQt5.QtCore import QTimer

        try:
            from PyQt5 import sip

            src_bw = getattr(source, "_branch_widget", None)
            tgt_bw = getattr(target, "_branch_widget", None)
            if src_bw is None or tgt_bw is None:
                return
            if sip.isdeleted(source) or sip.isdeleted(src_bw):
                return
            branch_visible = src_bw.isVisible()
            tgt_bw.setText(src_bw.text())
            tgt_bw.setVisible(branch_visible)
            tgt_bw.setToolTip(src_bw.toolTip())
            # 同步项目 avatar tooltip（含完整项目名、路径、分支）
            if hasattr(target, "_project_avatar") and hasattr(source, "_project_avatar"):
                target._project_avatar.setToolTip(source._project_avatar.toolTip())
            if not branch_visible:
                QTimer.singleShot(200, lambda: self.update_branch(target))
        except Exception:
            logger.exception("[WorktreeService] copy_branch_state 复制分支状态失败")
