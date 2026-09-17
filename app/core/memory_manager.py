# -*- coding: utf-8 -*-
"""
长期记忆管理模块 - 重构为 2 种记忆架构
1. 条目记忆 (Entry Memories) - 用户手动管理
2. 关键文档 (Key Documents) - 项目文件关联

注意：项目笔记已交由 BuildSystemPrompt hook (read_project_notes) 从 AGENTS.md 读取注入。
"""

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.store import (
    KeyDocumentsRepository,
    MemoryRepository,
)

# ========== 兼容旧接口（已废弃，保持向后兼容）==========
# 旧版 5 大类记忆已废弃，但 topic_summary.py 还在用




class MemoryManagerCore:
    """长期记忆管理器核心类 - 聚合 3 种记忆的访问（全局单例，跨窗口共享）"""

    _instance = None

    @classmethod
    def get_instance(cls) -> "MemoryManagerCore":
        """获取全局唯一的 MemoryManagerCore 实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._session_store: Optional[Any] = None
        self._db_manager = None

        # 两个仓储
        self._entry_memories_repo: Optional[MemoryRepository] = None
        self._key_documents_repo: Optional[KeyDocumentsRepository] = None

        # 失效 worktree 清理节流状态（key: 项目名或 "__all__"，value: monotonic 时间戳）
        self._pruned_at: Dict[str, float] = {}
        self._prune_lock = threading.Lock()

        # 初始化存储
        self._init_storage()

    def _init_storage(self):
        """初始化存储层（经 backend 门面获取活跃引擎，行为等价 SessionStore）"""
        try:
            # 函数体内延迟 import：避免与 backend 循环导入
            from app.core.backend import get_session_storage

            engine = get_session_storage()
            # hasattr 降级：引擎无 is_initialized / store（第三方实现）→ 视为未初始化
            if getattr(engine, "is_initialized", False) and getattr(engine, "store", None) is not None:
                self._session_store = engine
                self._db_manager = engine.store._db
                logger.info("[MemoryManager] SQLite 存储已启用")

                # 初始化仓储
                self._entry_memories_repo = MemoryRepository(self._db_manager)
                self._key_documents_repo = KeyDocumentsRepository(self._db_manager)

                return
            else:
                logger.warning("[MemoryManager] SQLite 初始化失败")
        except Exception as e:
            logger.warning(f"[MemoryManager] 初始化异常: {e}")

    @property
    def entry_memories(self) -> MemoryRepository:
        """获取条目记忆仓储"""
        return self._entry_memories_repo

    @property
    def key_documents(self) -> KeyDocumentsRepository:
        """获取关键文档仓储"""
        return self._key_documents_repo

    # ==================== 条目记忆 API ====================

    def get_entry_memories(self, query: str = "", limit: int = 9999) -> List[Dict]:
        """获取条目记忆列表，支持搜索"""
        if not self._entry_memories_repo:
            return []
        return self._entry_memories_repo.search(query, limit)






    # ==================== 项目笔记（精简版，仅文件读写，无 SQLite） ====================



    # ==================== 关键文档 API ====================

    def get_key_documents(self, project: str, limit: int = 9999) -> List[Dict]:
        """获取项目的关键文档列表"""
        if not self._key_documents_repo:
            return []
        return self._key_documents_repo.get_by_project(project, limit=limit)

    def add_key_document(self, project: str, file_path: str, added_by: str = "manual") -> bool:
        """添加关键文档"""
        if not self._key_documents_repo:
            return False
        return self._key_documents_repo.add(project, file_path, added_by)

    def remove_key_document(self, doc_id: str) -> bool:
        """移除关键文档"""
        if not self._key_documents_repo:
            return False
        return self._key_documents_repo.remove(doc_id)

    def clear_key_documents(self, project: str) -> int:
        """清空项目的关键文档"""
        if not self._key_documents_repo:
            return 0
        return self._key_documents_repo.clear_by_project(project)

    def prune_stale_worktrees(self, project: str = "", throttle_seconds: float = 60.0) -> List[Dict[str, str]]:
        """清理路径已失效的 git_worktree 关键文档（节流 + 工作目录善后）

        自净化兜底：worktree-manager 的缺失检测数据源是 `git worktree list`，
        目录被外部删除且 git 记录被 prune 之后，该路径再也不会出现在检测面，
        DB 残留永久化——关键文档注入持续输出不存在的目录，工作目录计数被污染。

        节流：PreUserMessage 每轮都拉关键文档上下文，这里按项目缓存上次执行时间
        （默认 60s 一次），把 N 次 os.path.isdir 探测 + 可能的 DELETE 压到低频。

        Args:
            project: 限定项目名；空串表示扫描全部项目
            throttle_seconds: 同一项目的节流窗口（秒）

        Returns:
            List[Dict[str, str]]: 被清理的 [{project, file_path}]，未执行时为空列表
        """
        if not self._key_documents_repo:
            return []

        key = project or "__all__"
        now = time.monotonic()
        with self._prune_lock:
            last = self._pruned_at.get(key)
            if last is not None and (now - last) < throttle_seconds:
                return []

        # 删除前先捕获各项目的工作目录：is_working_dir 标记就在待删记录上，
        # 记录删掉后 get_working_directory 查不到，善后逻辑会永不触发
        projects = [project] if project else self._key_documents_repo.get_all_projects()
        wd_before = {p: self.get_working_directory(p) for p in projects}

        removed = self._key_documents_repo.prune_stale_worktrees(project)

        with self._prune_lock:
            self._pruned_at[key] = now

        for proj, path in removed:
            wd = wd_before.get(proj)
            if wd and os.path.normpath(wd) == os.path.normpath(path):
                self._heal_dangling_workdir(proj)
        return [{"project": p, "file_path": f} for p, f in removed]

    def _heal_dangling_workdir(self, project: str) -> None:
        """工作目录悬空（指向的记录已被清理）时，回退到仍存在的根目录

        被清理的 worktree 恰是该项目工作目录时，标记随记录一起消失，工具链会在
        不存在的目录里执行。优先回退到同项目内仍存在的非 worktree 根目录；
        没有可用候选则清空该标记。
        """
        try:
            docs = self.get_key_documents(project)
            candidates = [
                d.get("file_path", "")
                for d in docs
                if d.get("added_by") != "git_worktree" and os.path.isdir(d.get("file_path", ""))
            ]
            # 优先仍带根目录标记的候选，其次任意可用目录
            root = next(
                (d.get("file_path", "") for d in docs if d.get("is_working_dir") and d.get("file_path") in candidates),
                "",
            )
            fallback = root or (candidates[0] if candidates else "")
            if fallback:
                self.set_working_directory(project, fallback)
                logger.info(f"[MemoryManager] 工作目录悬空，回退到 {fallback}（项目: {project}）")
            else:
                self.set_working_directory(project, "clear")
                logger.info(f"[MemoryManager] 工作目录悬空且无可用根目录，已清空（项目: {project}）")
        except Exception as e:
            logger.warning(f"[MemoryManager] 工作目录善后失败（项目: {project}）: {e}")

    def get_worktree_counts(self) -> Dict[str, int]:
        """获取所有项目的工作目录数量

        计数规则：工作目录数 = 主仓库(1 if is_working_dir=1) + 所有 git worktree 数
        """
        if not self._key_documents_repo:
            return {}
        return self._key_documents_repo.get_worktree_counts()

    def set_working_directory(self, project: str, file_path: str) -> bool:
        """设置项目的工作目录（互斥）"""
        if not self._key_documents_repo:
            return False
        return self._key_documents_repo.set_working_directory(project, file_path)

    def restore_working_directory_mark(self, project: str, file_path: str) -> bool:
        """恢复指定路径的工作目录标记（不清除其他标记）。

        用于 worktree 切换场景：set_working_directory 会先清除所有 is_working_dir，
        导致原根目录的标记丢失。此方法仅追加设置，不干扰已有的标记。
        """
        if not self._key_documents_repo:
            return False
        return self._key_documents_repo.set_working_directory_only(project, file_path)

    def get_working_directory(self, project: str) -> Optional[str]:
        """获取项目的工作目录"""
        if not self._key_documents_repo:
            return None
        return self._key_documents_repo.get_working_directory(project)

    # ==================== 上下文格式化 ====================

    def format_memories_for_prompt(
        self,
        project: str = "默认项目",
        entry_limit: int = 100,
        doc_limit: int = 50,
        workdir_override: Optional[str] = None,
    ) -> str:
        """
        格式化记忆注入到 prompt

        注意：项目笔记/路径建议/Worktree 信息已交由
        BuildSystemPrompt + PreUserMessage hooks 注入。

        Args:
            project: 当前项目名称
            entry_limit: 条目记忆最大数量
            doc_limit: 关键文档最大数量
            workdir_override: 工作目录覆盖（多窗口隔离：实例缓存优先于 DB）

        Returns:
            str: 格式化后的记忆字符串
        """
        lines = ["## 长期记忆", ""]

        # 1. 条目记忆
        lines.append("### 条目记忆")
        entries = self.get_entry_memories(limit=entry_limit)
        if entries:
            for idx, entry in enumerate(entries, 1):
                content = entry.get("content", "")
                lines.append(f"- {content}")
        else:
            lines.append("- 暂无条目记忆")
        lines.append("")

        # 2. 关键文档
        lines.append("### 关键文档")
        docs = self.get_key_documents(project)[:doc_limit]
        # 确保根目录（is_working_dir）始终在结果中，即使超过了 doc_limit
        if project and self._key_documents_repo:
            wd_path = workdir_override if workdir_override is not None else self.get_working_directory(project)
            if wd_path:
                all_docs = self.get_key_documents(project)
                root_in_list = any(d.get("file_path") == wd_path for d in docs)
                if not root_in_list:
                    for d in all_docs:
                        if d.get("file_path") == wd_path:
                            # 把根目录插到列表头部
                            docs.insert(0, d)
                            break
        # 获取当前项目的工作目录（多窗口隔离：优先使用实例缓存值）
        wd_path = workdir_override if workdir_override is not None else self.get_working_directory(project)
        if docs:
            for doc in docs:
                file_name = doc.get("file_name", "")
                file_path = doc.get("file_path", "")
                is_url = file_path and (file_path.startswith("http://") or file_path.startswith("https://"))
                is_wd = file_path == wd_path
                if is_wd:
                    lines.append(f"- {file_name} （项目根目录）./")
                elif is_url:
                    lines.append(f"- 🔗 [{file_name}]({file_path})")
                else:
                    try:
                        rel = Path(file_path).relative_to(Path(wd_path))
                        lines.append(f"- {file_name} ({rel})")
                    except ValueError:
                        lines.append(f"- {file_name} ({file_path})")
        else:
            lines.append("- 暂无关键文档")
        lines.append("")

        return "\n".join(lines)

    # ==================== 兼容旧接口 ====================



    def search_memories(self, query: str = "", limit: int = 30) -> List[Dict]:
        """兼容旧接口"""
        return self.get_entry_memories(query, limit)


