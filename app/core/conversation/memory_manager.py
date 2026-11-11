# -*- coding: utf-8 -*-
"""
长期记忆管理模块

职责（很薄，只是仓储门面）：
1. 条目记忆 (Entry Memories) - 用户手动管理的长期条目
2. 项目工作目录 - 每项目一个当前 cwd

历史：原先还有「关键文档」表（项目文件/URL 关联列表 + 工作目录标记）。
该功能已下线，工作目录改由 project_workdir 表独立承载。

注意：项目笔记已交由 BuildSystemPrompt hook (read_project_notes) 从 AGENTS.md 读取注入。
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.store import MemoryRepository, ProjectWorkdirRepository


# ========== 兼容旧接口（已废弃，保持向后兼容）==========
# 旧版 5 大类记忆已废弃，但 topic_summary.py 还在用


class MemoryManagerCore:
    """长期记忆管理器核心类 - 聚合各类记忆的访问（全局单例，跨窗口共享）"""

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

        self._entry_memories_repo: Optional[MemoryRepository] = None
        self._workdir_repo: Optional[ProjectWorkdirRepository] = None

        self._init_storage()

    def _init_storage(self):
        """初始化存储层（经 backend 门面获取活跃引擎，行为等价 SessionStore）"""
        try:
            # 函数体内延迟 import：避免与 backend 循环导入
            from app.core.conversation.backend import get_session_storage

            engine = get_session_storage()
            # hasattr 降级：引擎无 is_initialized / store（第三方实现）→ 视为未初始化
            if getattr(engine, "is_initialized", False) and getattr(engine, "store", None) is not None:
                self._session_store = engine
                self._db_manager = engine.store._db
                logger.info("[MemoryManager] SQLite 存储已启用")

                self._entry_memories_repo = MemoryRepository(self._db_manager)
                self._workdir_repo = ProjectWorkdirRepository(self._db_manager)
                return
            logger.warning("[MemoryManager] SQLite 初始化失败")
        except Exception as e:
            logger.warning(f"[MemoryManager] 初始化异常: {e}")

    @property
    def entry_memories(self) -> MemoryRepository:
        """获取条目记忆仓储"""
        return self._entry_memories_repo

    @property
    def project_workdir(self) -> Optional[ProjectWorkdirRepository]:
        """获取项目工作目录仓储"""
        return self._workdir_repo

    # ==================== 条目记忆 API ====================

    def get_entry_memories(self, query: str = "", limit: int = 9999) -> List[Dict]:
        """获取条目记忆列表，支持搜索"""
        if not self._entry_memories_repo:
            return []
        return self._entry_memories_repo.search(query, limit)

    # ==================== 项目工作目录 API ====================

    def get_working_directory(self, project: str) -> Optional[str]:
        """获取项目工作目录；未设置返回 None"""
        if not self._workdir_repo or not project:
            return None
        return self._workdir_repo.get(project)

    def set_working_directory(self, project: str, path: str) -> bool:
        """设置项目工作目录（path 为空或 "clear" 等价于清除）"""
        if not self._workdir_repo or not project:
            return False
        return self._workdir_repo.set(project, path)

    def clear_working_directory(self, project: str) -> bool:
        """清除项目工作目录"""
        if not self._workdir_repo or not project:
            return False
        return self._workdir_repo.clear(project)

    def list_working_directories(self) -> Dict[str, str]:
        """全部项目工作目录映射 {project: path}"""
        if not self._workdir_repo:
            return {}
        return self._workdir_repo.list_all()

    # ==================== 上下文格式化 ====================

    def format_memories_for_prompt(
        self,
        project: str = "默认项目",
        entry_limit: int = 100,
        workdir_override: Optional[str] = None,
    ) -> str:
        """
        格式化记忆注入到 prompt

        注意：项目笔记/路径建议/Worktree 信息已交由
        BuildSystemPrompt + PreUserMessage hooks 注入。

        Args:
            project: 当前项目名称
            entry_limit: 条目记忆最大数量
            workdir_override: 工作目录覆盖（多窗口隔离：实例缓存优先于 DB）

        Returns:
            str: 格式化后的记忆字符串
        """
        lines = ["## 长期记忆", ""]

        # 1. 条目记忆
        lines.append("### 条目记忆")
        entries = self.get_entry_memories(limit=entry_limit)
        if entries:
            for entry in entries:
                content = entry.get("content", "")
                lines.append(f"- {content}")
        else:
            lines.append("- 暂无条目记忆")
        lines.append("")

        # 2. 当前工作目录（一行，给模型一个路径锚点）
        wd_path = workdir_override if workdir_override is not None else self.get_working_directory(project)
        if wd_path:
            lines.append(f"### 工作目录\n- {wd_path}")
            lines.append("")

        return "\n".join(lines)

    # ==================== 兼容旧接口 ====================

    def search_memories(self, query: str = "", limit: int = 30) -> List[Dict]:
        """兼容旧接口"""
        return self.get_entry_memories(query, limit)
