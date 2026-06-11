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

import threading
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple

from loguru import logger

from app.core.store.file_operation_repository import FileOperationRepository
from app.core.store.input_history_repo import InputHistoryRepository
from app.core.store.memory_repository import MemoryRepository
# 导入子模块
from app.core.store.session_repository import SessionRepository
from app.core.store.subagent_log_repository import SubAgentLogRepository
from app.utils.db_manager import DatabaseManager
from app.utils.utils import get_app_data_dir


class SessionStore:
    """SQLite 会话存储层，提供原子性持久化（单例模式）"""

    TABLE_NAME = "sessions"
    MEMORIES_TABLE = "memories"
    DB_FILENAME = "sessions.db"
    _CLEAN_SHUTDOWN_FLAG = "clean_shutdown.flag"

    _instance: Optional["SessionStore"] = None

    def __new__(cls, db_dir: str = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls, db_dir: str = None) -> "SessionStore":
        """获取单例实例"""
        if db_dir is None:
            db_dir = str(get_app_data_dir())
        if cls._instance is None:
            cls._instance = cls(db_dir)
        return cls._instance

    def __init__(self, db_dir: str):
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
        self._subagent_log_repo: Optional[SubAgentLogRepository] = None
        self._input_history_repo: Optional[InputHistoryRepository] = None
        
        self._init_schema()

    @classmethod
    def mark_clean_shutdown(cls):
        """标记数据库正常关闭，下次启动跳过完整性检查"""
        try:
            from app.utils.utils import get_app_data_dir
            flag_path = Path(get_app_data_dir()) / cls._CLEAN_SHUTDOWN_FLAG
            flag_path.write_text("1", encoding="utf-8")
        except Exception:
            pass

    def _check_and_repair_database(self):
        """检查并修复损坏的 SQLite 数据库
        
        PyInstaller 打包后运行时可能遇到数据库损坏问题：
        1. WAL 文件与主数据库文件不同步
        2. 数据库文件被不完整地复制或更新
        3. 并发写入导致文件锁定冲突
        
        修复策略：
        1. 先尝试执行 PRAGMA wal_checkpoint(TRUNCATE) 同步 WAL
        2. 删除不匹配的 WAL/SHM 文件（如果存在）
        3. 执行 VACUUM 重建数据库
        
        优化：如果上次正常关闭，跳过耗时较长的 PRAGMA integrity_check
        """
        if not self._db or not self._db.is_connected:
            logger.warning("[SessionStore] 数据库未连接，跳过检查")
            return
        
        db_path = Path(self._db.db_path)
        
        # 检查是否为正常关闭后的首次启动
        clean_shutdown = False
        try:
            flag_path = Path(get_app_data_dir()) / self._CLEAN_SHUTDOWN_FLAG
            if flag_path.exists():
                clean_shutdown = True
                flag_path.unlink(missing_ok=True)
        except Exception:
            pass
        
        try:
            # 检查 WAL 模式
            success, result = self._db.execute_sql('PRAGMA journal_mode')
            if not success:
                logger.warning("[SessionStore] 无法读取 journal_mode")
                return
            journal_mode = list(result[0].values())[0] if result else 'unknown'
            
            # 获取数据库路径信息
            wal_path = db_path.with_suffix('.db-wal')
            shm_path = db_path.with_suffix('.db-shm')
            
            if journal_mode == 'wal':
                # WAL 模式下检查是否需要同步
                try:
                    self._db.execute_sql('PRAGMA wal_checkpoint(TRUNCATE)')
                    logger.debug("[SessionStore] WAL 检查点执行完成")
                except Exception as e:
                    logger.warning(f"[SessionStore] WAL 检查点失败: {e}")
                    
                    # 删除 WAL/SHM 文件强制同步
                    for p in [(wal_path, "WAL"), (shm_path, "SHM")]:
                        if p[0].exists():
                            try:
                                p[0].unlink()
                                logger.info(f"[SessionStore] 已删除损坏的 {p[1]} 文件")
                            except Exception as e2:
                                logger.warning(f"[SessionStore] 无法删除 {p[1]} 文件: {e2}")
            
            # 正常关闭后跳过完整行检查（PRAGMA integrity_check 在数据库较大时较慢）
            if clean_shutdown:
                logger.debug("[SessionStore] 上次正常关闭，跳过完整性检查")
                return
            
            # 尝试执行完整性检查
            try:
                success, result = self._db.execute_sql('PRAGMA integrity_check')
                if success and result:
                    check_result = list(result[0].values())[0] if isinstance(result[0], dict) else str(result[0])
                    if check_result != 'ok':
                        logger.warning(f"[SessionStore] 数据库完整性检查失败: {check_result}")
                        self._repair_database()
                    else:
                        logger.debug("[SessionStore] 数据库完整性检查通过")
            except Exception as e:
                logger.warning(f"[SessionStore] 完整性检查异常: {e}")
                
        except Exception as e:
            logger.warning(f"[SessionStore] 数据库检查异常（继续初始化）: {e}")

    def _repair_database(self):
        """尝试修复损坏的数据库"""
        try:
            # 切换为 DELETE 模式并执行 VACUUM
            self._db.execute_sql('PRAGMA journal_mode=DELETE')
            self._db.execute_sql('VACUUM')
            logger.info("[SessionStore] 数据库修复成功")
        except Exception as e:
            logger.error(f"[SessionStore] 数据库修复失败: {e}")

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

                # ========== 数据库完整性检查与自动修复 ==========
                # 必须在连接之后执行，因为需要 DatabaseManager 实例来修复
                self._check_and_repair_database()
                # ================================================

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
                    {"name": "worktree_path", "type": "TEXT", "default": ""},
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

                # 创建子智能体日志表
                self._db.create_table("sub_agent_logs", [
                    {"name": "task_id", "type": "TEXT", "primary_key": True},
                    {"name": "agent_name", "type": "TEXT"},
                    {"name": "task_description", "type": "TEXT"},
                    {"name": "status", "type": "TEXT"},
                    {"name": "result", "type": "TEXT"},
                    {"name": "error", "type": "TEXT"},
                    {"name": "logs", "type": "TEXT"},
                    {"name": "summary", "type": "TEXT"},
                    {"name": "created_at", "type": "TEXT"},
                    {"name": "updated_at", "type": "TEXT"},
                ])

                # 创建输入历史表
                self._db.create_table("input_history", [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
                    {"name": "content", "type": "TEXT", "not_null": True},
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
                self._migrate_add_user_edited_title_column()
                self._migrate_add_worktree_path_column()

                # 初始化子模块
                self._session_repo = SessionRepository(self._db)
                self._memory_repo = MemoryRepository(self._db)
                self._file_op_repo = FileOperationRepository(self._db)
                self._subagent_log_repo = SubAgentLogRepository(self._db)
                self._input_history_repo = InputHistoryRepository(self._db)
                self._input_history_repo.create_table()

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

    def _migrate_add_user_edited_title_column(self):
        """迁移：添加 user_edited_title 列（如果不存在）"""
        if not self._db or not self._db.is_connected:
            return
        try:
            columns = self._db.get_table_info(self.TABLE_NAME)
            col_names = [c.get("name", "") for c in columns]
            if "user_edited_title" not in col_names:
                logger.info("[SessionStore] 迁移：添加 user_edited_title 列")
                self._db.execute_sql(
                    f"ALTER TABLE {self.TABLE_NAME} ADD COLUMN user_edited_title INTEGER DEFAULT 0"
                )
                logger.info("[SessionStore] user_edited_title 列迁移完成")
        except Exception as e:
            logger.warning(f"[SessionStore] user_edited_title 列迁移失败(可能已存在): {e}")

    def _migrate_add_worktree_path_column(self):
        """迁移：添加 worktree_path 列（如果不存在）"""
        if not self._db or not self._db.is_connected:
            return
        try:
            columns = self._db.get_table_info(self.TABLE_NAME)
            col_names = [c.get("name", "") for c in columns]
            if "worktree_path" not in col_names:
                logger.info("[SessionStore] 迁移：添加 worktree_path 列")
                self._db.execute_sql(
                    f"ALTER TABLE {self.TABLE_NAME} ADD COLUMN worktree_path TEXT DEFAULT ''"
                )
                logger.info("[SessionStore] worktree_path 列迁移完成")
        except Exception as e:
            logger.warning(f"[SessionStore] worktree_path 列迁移失败(可能已存在): {e}")

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

    def get_session_counts(self) -> Dict[str, int]:
        """获取所有项目的会话数量（COUNT DISTINCT session_id 去重）"""
        if self._session_repo:
            return self._session_repo.get_session_counts()
        return {}

    def force_cleanup_project(self, project_name: str) -> bool:
        """强制清理项目的所有关联数据（绕过 repo 层，直接 SQL 删除三张表）

        归档时调用此方法，确保 sessions、key_documents、project_notes
        三张表中该项目的所有记录都被删除，避免 UNION 查询让已归档项目"复活"。
        """
        if not self._db or not self._db.is_connected:
            logger.error(f"[SessionStore] 数据库未连接，无法清理项目 {project_name}")
            return False
        try:
            # 删除会话（直接 SQL，不经过 repo 层）
            self._execute(
                f'DELETE FROM sessions WHERE project = ?',
                (project_name,)
            )
            # 删除关键文档
            self._execute(
                f'DELETE FROM key_documents WHERE project = ?',
                (project_name,)
            )
            # 删除旧版项目笔记
            self._execute(
                f'DELETE FROM project_notes WHERE project = ?',
                (project_name,)
            )
            logger.info(f"[SessionStore] 已强制清理项目 {project_name} 的所有关联数据")
            return True
        except Exception as e:
            logger.error(f"[SessionStore] 强制清理项目 {project_name} 异常: {e}")
            return False

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

    # ==================== 子智能体日志操作（委托给 SubAgentLogRepository）====================

    def save_subagent_task(self, task_id: str, agent_name: str, task_description: str,
                           status: str = "running", result: str = None, error: str = None,
                           logs: List[Dict] = None, summary: Dict = None,
                           session_id: str = "") -> bool:
        """保存子智能体任务"""
        if self._subagent_log_repo:
            return self._subagent_log_repo.save_task(
                task_id, agent_name, task_description, status, result, error, logs, summary, session_id
            )
        return False

    def update_subagent_task_status(self, task_id: str, status: str, result: str = None,
                                     error: str = None, logs: List[Dict] = None,
                                     summary: Dict = None) -> bool:
        """更新子智能体任务状态"""
        if self._subagent_log_repo:
            return self._subagent_log_repo.update_status(task_id, status, result, error, logs, summary)
        return False

    def get_subagent_task(self, task_id: str) -> Optional[Dict]:
        """获取单个子智能体任务"""
        if self._subagent_log_repo:
            return self._subagent_log_repo.get_task(task_id)
        return None

    def get_subagent_tasks(self, task_ids: List[str]) -> List[Dict]:
        """获取多个子智能体任务"""
        if self._subagent_log_repo:
            return self._subagent_log_repo.get_tasks(task_ids)
        return []

    def get_all_subagent_tasks(self, limit: int = 100) -> List[Dict]:
        """获取所有子智能体任务"""
        if self._subagent_log_repo:
            return self._subagent_log_repo.get_all_tasks(limit)
        return []

    def delete_subagent_task(self, task_id: str) -> bool:
        """删除子智能体任务"""
        if self._subagent_log_repo:
            return self._subagent_log_repo.delete_task(task_id)
        return False

    def clear_old_subagent_tasks(self, days: int = 7) -> int:
        """清理旧子智能体任务"""
        if self._subagent_log_repo:
            return self._subagent_log_repo.clear_old_tasks(days)
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

    # ==================== 输入历史操作（委托给 InputHistoryRepository）====================

    def add_input_history(self, content: str, attachments: Optional[list] = None) -> bool:
        """添加输入历史"""
        if self._input_history_repo:
            return self._input_history_repo.add(content, attachments)
        return False

    def get_input_history(self, limit: int = 50) -> list:
        """获取输入历史列表（最新在前）

        Returns:
            list[dict]: 每个元素含 ``text`` 和 ``attachments`` 键
        """
        if self._input_history_repo:
            return self._input_history_repo.get_all(limit)
        return []

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
            self._subagent_log_repo = None
            self._input_history_repo = None

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

    @property
    def subagent_log_repo(self) -> Optional[SubAgentLogRepository]:
        """获取子智能体日志仓储（用于高级操作）"""
        return self._subagent_log_repo