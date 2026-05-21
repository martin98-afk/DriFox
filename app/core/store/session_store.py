# -*- coding: utf-8 -*-
"""
会话存储层 - 基于 SQLite 的持久化存储

使用仓储模式重构，将职责分离到子模块：
- SessionRepository: 会话 CRUD
- MemoryRepository: 记忆 CRUD
- FileOperationRepository: 文件操作记录 CRUD

解决 issue #374：会话记录存储架构存在高风险
- 原子性写入
- 并发支持
- 损坏隔离
"""

import orjson as json
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple

from loguru import logger

from app.utils.db_manager import DatabaseManager
from app.core.message_content import consolidate_messages

# 导入子模块
from app.core.store.session_repository import SessionRepository
from app.core.store.memory_repository import MemoryRepository
from app.core.store.file_operation_repository import FileOperationRepository


class SessionStore:
    """SQLite 会话存储层，提供原子性持久化（单例模式）"""

    TABLE_NAME = "sessions"
    MEMORIES_TABLE = "memories"
    DB_FILENAME = "sessions.db"

    _instance: Optional["SessionStore"] = None

    def __new__(cls, db_dir: str = ".drifox"):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls, db_dir: str = None) -> "SessionStore":
        """获取单例实例"""
        if cls._instance is None:
            # 使用默认数据目录
            if db_dir is None:
                from app.utils.utils import get_app_data_dir
                db_dir = str(get_app_data_dir())
            cls._instance = cls(db_dir)
        return cls._instance

    def __init__(self, db_dir: str = ".drifox"):
        # 防止重复初始化
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._db_dir = db_dir
        self._db_path = str(Path(db_dir) / self.DB_FILENAME)
        self._db: Optional[DatabaseManager] = None
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        
        # 初始化子模块（将在 _init_schema 中完成）
        self._session_repo: Optional[SessionRepository] = None
        self._memory_repo: Optional[MemoryRepository] = None
        self._file_op_repo: Optional[FileOperationRepository] = None
        
        self._init_schema()

    def _init_schema(self):
        """初始化数据库和表结构"""
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            try:
                # 使用 DatabaseManager（单例模式）
                self._db = DatabaseManager()
                self._db.connect(self._db_path)

                # ========== WAL 模式优化：提升并发读写性能 ==========
                self._db.execute_sql('PRAGMA journal_mode=WAL')
                self._db.execute_sql('PRAGMA synchronous=NORMAL')
                self._db.execute_sql('PRAGMA cache_size=-64000')
                self._db.execute_sql('PRAGMA temp_store=MEMORY')
                # ======================================================

                # 创建会话表
                self._db.create_table(self.TABLE_NAME, [
                    {"name": "session_id", "type": "TEXT", "primary_key": True},
                    {"name": "title", "type": "TEXT"},
                    {"name": "messages", "type": "TEXT"},
                    {"name": "system_prompt", "type": "TEXT"},
                    {"name": "compaction_state", "type": "TEXT"},
                    {"name": "compaction_cache", "type": "TEXT"},
                    {"name": "message_count", "type": "INTEGER", "default": 0},
                    {"name": "project", "type": "TEXT", "default": "默认项目"},
                    {"name": "created_at", "type": "TEXT"},
                    {"name": "updated_at", "type": "TEXT"},
                ])

                # 创建记忆表
                self._db.create_table(self.MEMORIES_TABLE, [
                    {"name": "memory_id", "type": "TEXT", "primary_key": True},
                    {"name": "content", "type": "TEXT"},
                    {"name": "enabled", "type": "INTEGER", "default": 1},
                    {"name": "confidence", "type": "REAL", "default": 0.8},
                    {"name": "category", "type": "TEXT"},
                    {"name": "source", "type": "TEXT"},
                    {"name": "last_accessed", "type": "TEXT"},
                    {"name": "created_at", "type": "TEXT"},
                    {"name": "updated_at", "type": "TEXT"},
                ])

                # 创建文件操作记录表
                self._db.create_table("file_operations", [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
                    {"name": "session_id", "type": "TEXT"},
                    {"name": "call_id", "type": "TEXT"},
                    {"name": "tool_name", "type": "TEXT"},
                    {"name": "file_path", "type": "TEXT"},
                    {"name": "backup_path", "type": "TEXT"},
                    {"name": "created_at", "type": "TEXT"},
                ])

                # 创建索引
                self._db.execute_sql(
                    f'CREATE INDEX IF NOT EXISTS idx_updated ON {self.TABLE_NAME}(updated_at DESC)'
                )
                self._db.execute_sql(
                    f'CREATE INDEX IF NOT EXISTS idx_project ON {self.TABLE_NAME}(project)'
                )

                # 迁移逻辑
                self._migrate_add_project_column()
                self._migrate_remove_canvas_id()

                # 初始化子模块
                self._session_repo = SessionRepository(self._db)
                self._memory_repo = MemoryRepository(self._db)
                self._file_op_repo = FileOperationRepository(self._db)

                self._initialized = True
                logger.info("[SessionStore] 初始化完成（仓储模式）")

            except Exception as e:
                logger.error(f"[SessionStore] 初始化失败: {e}")
                self._initialized = False

    def _migrate_add_project_column(self):
        """迁移：添加 project 列（如果不存在）"""
        if not self._db or not self._db.is_connected:
            return
        try:
            columns = self._db.get_table_info(self.TABLE_NAME)
            col_names = [c.get("name", "") for c in columns]
            if "project" not in col_names:
                logger.info("[SessionStore] 迁移：添加 project 列")
                self._db.execute_sql(
                    f"ALTER TABLE {self.TABLE_NAME} ADD COLUMN project TEXT DEFAULT '默认项目'"
                )
                self._db.execute_sql(
                    f"UPDATE {self.TABLE_NAME} SET project = '默认项目' WHERE project IS NULL"
                )
                logger.info("[SessionStore] project 列迁移完成")
        except Exception as e:
            logger.warning(f"[SessionStore] project 列迁移失败(可能已存在): {e}")

    def _migrate_remove_canvas_id(self):
        """迁移：删除已废弃的 canvas_id 列（如果存在）"""
        if not self._db or not self._db.is_connected:
            return
        try:
            columns = self._db.get_table_info(self.TABLE_NAME)
            col_names = [c.get("name", "") for c in columns]
            if "canvas_id" in col_names:
                logger.info("[SessionStore] 迁移：删除 sessions 表的 canvas_id 列")
                self._db.execute_sql(f'''
                    CREATE TABLE {self.TABLE_NAME}_temp AS
                    SELECT session_id, title, messages, system_prompt,
                           compaction_state, compaction_cache, message_count,
                           project, created_at, updated_at
                    FROM {self.TABLE_NAME}
                ''')
                self._db.execute_sql(f'DROP TABLE {self.TABLE_NAME}')
                self._db.execute_sql(f'ALTER TABLE {self.TABLE_NAME}_temp RENAME TO {self.TABLE_NAME}')
                self._db.execute_sql(f'CREATE INDEX IF NOT EXISTS idx_updated ON {self.TABLE_NAME}(updated_at DESC)')
                self._db.execute_sql(f'CREATE INDEX IF NOT EXISTS idx_project ON {self.TABLE_NAME}(project)')
                logger.info("[SessionStore] sessions 表 canvas_id 列迁移完成")

            mem_columns = self._db.get_table_info(self.MEMORIES_TABLE)
            mem_col_names = [c.get("name", "") for c in mem_columns]
            if "canvas_id" in mem_col_names:
                logger.info("[SessionStore] 迁移：删除 memories 表的 canvas_id 列")
                self._db.execute_sql(f'''
                    CREATE TABLE {self.MEMORIES_TABLE}_temp AS
                    SELECT memory_id, content, enabled, confidence, category, source,
                           last_accessed, created_at, updated_at
                    FROM {self.MEMORIES_TABLE}
                ''')
                self._db.execute_sql(f'DROP TABLE {self.MEMORIES_TABLE}')
                self._db.execute_sql(f'ALTER TABLE {self.MEMORIES_TABLE}_temp RENAME TO {self.MEMORIES_TABLE}')
                self._db.execute_sql(f'CREATE INDEX IF NOT EXISTS idx_memories_canvas ON {self.MEMORIES_TABLE}(memory_id)')
                logger.info("[SessionStore] memories 表 canvas_id 列迁移完成")
        except Exception as e:
            logger.warning(f"[SessionStore] canvas_id 列迁移失败(可能已不存在): {e}")

    @property
    def is_initialized(self) -> bool:
        return self._initialized and self._db is not None and self._db.is_connected

    def _execute(self, sql: str, params: tuple = ()) -> Tuple[bool, Any]:
        """执行 SQL（内部使用）"""
        if not self._db:
            return False, "数据库未初始化"
        return self._db.execute_sql(sql, params)

    # ==================== 会话操作（委托给 SessionRepository）====================

    def save_session(self, session: Dict) -> bool:
        """保存会话"""
        if self._session_repo:
            return self._session_repo.save(session)
        return False

    def get_session(self, session_id: str) -> Optional[Dict]:
        """获取会话"""
        if self._session_repo:
            return self._session_repo.get(session_id)
        return None

    def get_sessions(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """获取会话列表"""
        if self._session_repo:
            return self._session_repo.get_all(limit, offset)
        return []

    def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        if self._session_repo:
            return self._session_repo.delete(session_id)
        return False

    def update_session_title(self, session_id: str, title: str) -> bool:
        """更新会话标题"""
        if self._session_repo:
            return self._session_repo.update_title(session_id, title)
        return False

    def get_projects(self) -> List[str]:
        """获取项目列表"""
        if self._session_repo:
            return self._session_repo.get_projects()
        return ["默认项目"]

    def update_session_project(self, session_id: str, project: str) -> bool:
        """更新会话的项目归属"""
        if self._session_repo:
            return self._session_repo.update_project(session_id, project)
        return False

    def get_sessions_by_project(self, project: str, limit: int = 100) -> List[Dict]:
        """获取指定项目的会话列表"""
        if self._session_repo:
            return self._session_repo.get_by_project(project, limit)
        return []

    def archive_sessions_by_project(self, project: str) -> int:
        """归档指定项目的所有会话"""
        if self._session_repo:
            return self._session_repo.archive_by_project(project)
        return 0

    def get_session_count(self) -> int:
        """获取会话总数"""
        if not self._db or not self._db.is_connected:
            return 0
        try:
            success, result = self._db.execute_sql(f'SELECT COUNT(*) FROM {self.TABLE_NAME}')
            if success and result:
                return result[0][0] if isinstance(result[0], tuple) else result[0].get("count", 0)
            return 0
        except Exception as e:
            logger.error(f"[SessionStore] get_session_count 异常: {e}")
            return 0

    # ==================== 长期记忆操作（委托给 MemoryRepository）====================

    def save_memory(self, memory: Dict) -> bool:
        """保存单条长期记忆"""
        if self._memory_repo:
            return self._memory_repo.save(memory)
        return False

    def save_memories(self, memories: List[Dict]) -> bool:
        """批量保存记忆"""
        if self._memory_repo:
            return self._memory_repo.save_all(memories)
        return False

    def load_memories(self, limit: int = 200, include_disabled: bool = False) -> List[Dict]:
        """加载所有记忆"""
        if self._memory_repo:
            return self._memory_repo.load_all(limit, include_disabled)
        return []

    def delete_memory(self, memory_id: str) -> bool:
        """删除指定记忆"""
        if self._memory_repo:
            return self._memory_repo.delete(memory_id)
        return False

    def delete_memories_by_category(self, category: str) -> int:
        """删除指定分类的所有记忆"""
        if self._memory_repo:
            return self._memory_repo.delete_by_category(category)
        return 0

    def clear_memories(self) -> bool:
        """清空所有记忆"""
        if self._memory_repo:
            return self._memory_repo.clear_all()
        return False

    def update_memory_enabled(self, memory_id: str, enabled: bool) -> bool:
        """更新记忆的启用状态"""
        if self._memory_repo:
            return self._memory_repo.update_enabled(memory_id, enabled)
        return False

    def update_last_accessed(self, memory_id: str) -> bool:
        """更新记忆的最后访问时间"""
        if self._memory_repo:
            return self._memory_repo.update_last_accessed(memory_id)
        return False

    def search_memories(self, query_terms: List[str], limit: int = 20) -> List[Dict]:
        """搜索记忆"""
        if self._memory_repo:
            return self._memory_repo.search(query_terms, limit)
        return []

    def migrate_memories_from_json(self, json_path: str) -> int:
        """从 JSON 文件迁移记忆到 SQLite"""
        from app.utils.utils import deserialize_from_json
        import json as json_module

        if not self.is_initialized:
            return 0

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = deserialize_from_json(json_module.load(f))

            if not isinstance(data, list):
                return 0

            count = 0
            for memory in data:
                memory_id = memory.get("memory_id") or memory.get("id")
                if memory_id:
                    existing = self._memory_repo.get(memory_id) if self._memory_repo else None
                    if not existing:
                        if self.save_memory(memory):
                            count += 1

            logger.info(f"[SessionStore] 从 {json_path} 迁移了 {count} 条记忆")
            return count

        except Exception as e:
            logger.error(f"[SessionStore] 记忆迁移失败: {e}")
            return 0

    # ==================== 文件操作记录（委托给 FileOperationRepository）====================

    def record_file_operation(self, session_id: str, call_id: str,
                              tool_name: str, file_path: str,
                              backup_path: str) -> bool:
        """记录文件操作"""
        if self._file_op_repo:
            return self._file_op_repo.record(session_id, call_id, tool_name, file_path, backup_path)
        return False

    def get_file_operations_after_call(self, session_id: str, call_id: str) -> List[Dict]:
        """获取某 call_id 之后的所有文件操作"""
        if self._file_op_repo:
            return self._file_op_repo.get_after_call(session_id, call_id)
        return []

    def get_file_operations_by_call_id(self, session_id: str, call_id: str) -> List[Dict]:
        """根据 call_id 获取文件操作记录"""
        if self._file_op_repo:
            return self._file_op_repo.get_by_call_id(session_id, call_id)
        return []

    def get_all_file_operations(self, session_id: str) -> List[Dict]:
        """获取指定会话的所有文件操作"""
        if self._file_op_repo:
            return self._file_op_repo.get_all(session_id)
        return []

    def delete_file_operations_after_id(self, session_id: str, after_id: int) -> int:
        """删除指定 session 中 id 大于 after_id 的所有操作记录"""
        if self._file_op_repo:
            return self._file_op_repo.delete_after_id(session_id, after_id)
        return 0

    def clear_session_file_operations(self, session_id: str) -> Tuple[int, List[str]]:
        """清空会话的所有文件操作记录"""
        if self._file_op_repo:
            return self._file_op_repo.clear_session(session_id)
        return 0, []

    def remove_file_operation(self, session_id: str, call_id: str) -> int:
        """删除指定 call_id 的文件操作记录"""
        if self._file_op_repo:
            return self._file_op_repo.delete_by_call_id(session_id, call_id)
        return 0

    # ==================== 生命周期 ====================

    def close(self):
        """关闭数据库连接"""
        if self._db:
            self._db.close()
            self._db = None
            self._initialized = False
            self._session_repo = None
            self._memory_repo = None
            self._file_op_repo = None

    # ==================== 公开子模块访问 ====================

    @property
    def session_repo(self) -> Optional[SessionRepository]:
        """获取会话仓储（用于高级操作）"""
        return self._session_repo

    @property
    def memory_repo(self) -> Optional[MemoryRepository]:
        """获取记忆仓储（用于高级操作）"""
        return self._memory_repo

    @property
    def file_op_repo(self) -> Optional[FileOperationRepository]:
        """获取文件操作记录仓储（用于高级操作）"""
        return self._file_op_repo