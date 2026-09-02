# -*- coding: utf-8 -*-
"""
长期记忆管理模块 - 重构为 2 种记忆架构
1. 条目记忆 (Entry Memories) - 用户手动管理
2. 关键文档 (Key Documents) - 项目文件关联

注意：项目笔记已交由 BuildSystemPrompt hook (read_project_notes) 从 AGENTS.md 读取注入。
"""

from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from app.core.store import (
    KeyDocumentsRepository,
    MemoryRepository,
)

# ========== 兼容旧接口（已废弃，保持向后兼容）==========
# 旧版 5 大类记忆已废弃，但 topic_summary.py 还在用
MEMORY_CATEGORIES = {
    # 空字典，不再使用分类
}

MEMORY_CATEGORY_SUMMARIES = {}

MEMORY_CATEGORY_LIMITS = {}


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


