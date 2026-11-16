# -*- coding: utf-8 -*-
"""
项目工作目录管理模块

职责（很薄，只是仓储门面）：
- 项目工作目录 - 每项目一个当前 cwd（project_workdir 表）

历史：
- 「条目记忆」(Entry Memories) 与 prompt 记忆注入已随长期记忆系统下线，
  记忆/经验由 assistant_hub 插件接管；entry_memories 表不再读写。
- 「关键文档」表（项目文件/URL 关联列表 + 工作目录标记）已下线，
  工作目录改由 project_workdir 表独立承载。
- 项目笔记已交由 BuildSystemPrompt hook (read_project_notes) 从 AGENTS.md 读取注入。
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.store import ProjectWorkdirRepository

class MemoryManagerCore:
    """项目工作目录管理器核心类（全局单例，跨窗口共享）"""

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

                self._workdir_repo = ProjectWorkdirRepository(self._db_manager)
                return
            logger.warning("[MemoryManager] SQLite 初始化失败")
        except Exception as e:
            logger.warning(f"[MemoryManager] 初始化异常: {e}")

    @property
    def project_workdir(self) -> Optional[ProjectWorkdirRepository]:
        """获取项目工作目录仓储"""
        return self._workdir_repo

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
