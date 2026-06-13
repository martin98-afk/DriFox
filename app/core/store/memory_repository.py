# -*- coding: utf-8 -*-
"""
条目记忆仓储模块 - 专门负责长期记忆的持久化

重构为无分类的简单条目记忆结构。
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
import hashlib

from loguru import logger


class MemoryRepository:
    """条目记忆数据仓储，处理记忆的 CRUD 操作"""

    TABLE_NAME = "entry_memories"  # 重命名表
    DB_FILENAME = "sessions.db"

    def __init__(self, db_manager):
        """
        Args:
            db_manager: DatabaseManager 实例
        """
        self._db = db_manager

    @property
    def is_initialized(self) -> bool:
        return self._db is not None and self._db.is_connected

    def _execute(self, sql: str, params: tuple = ()) -> Tuple[bool, Any]:
        """执行 SQL（内部使用）"""
        if not self._db:
            return False, "数据库未初始化"
        return self._db.execute_sql(sql, params)

    def _ensure_table(self) -> bool:
        """确保表存在（迁移旧表或创建新表）"""
        if not self.is_initialized:
            return False
        try:
            # 检查旧表是否存在
            success, rows = self._execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
            )
            has_old_table = success and rows and len(rows) > 0
            
            if has_old_table:
                # 检查旧表是否有 category 列
                success, cols = self._execute("PRAGMA table_info(memories)")
                has_category = False
                if success and cols:
                    # cols 是列表，每个元素是 (cid, name, type, notnull, dflt_value, pk)
                    for col in cols:
                        if isinstance(col, (list, tuple)) and len(col) > 1 and col[1] == 'category':
                            has_category = True
                            break
                
                if has_category:
                    # 创建新表并迁移数据
                    self._execute(f'''
                        CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                            id TEXT PRIMARY KEY,
                            content TEXT NOT NULL,
                            enabled INTEGER DEFAULT 1,
                            confidence REAL DEFAULT 0.8,
                            source TEXT DEFAULT 'manual',
                            created_at TEXT,
                            updated_at TEXT,
                            last_accessed TEXT
                        )
                    ''')
                    # 迁移数据（忽略 category）
                    self._execute(f'''
                        INSERT INTO {self.TABLE_NAME} (id, content, enabled, confidence, source, created_at, updated_at, last_accessed)
                        SELECT memory_id, content, enabled, confidence, source, created_at, updated_at, last_accessed FROM memories
                    ''')
                    logger.info("[MemoryRepository] 已迁移旧记忆数据到新表")
                    return True
            
            # 直接创建新表
            success, _ = self._execute(f'''
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    confidence REAL DEFAULT 0.8,
                    source TEXT DEFAULT 'manual',
                    created_at TEXT,
                    updated_at TEXT,
                    last_accessed TEXT
                )
            ''')
            return success
            
        except Exception as e:
            logger.error(f"[MemoryRepository] _ensure_table 异常: {e}")
            return False

    def _row_to_memory(self, row) -> Dict:
        """将数据库行转换为记忆字典"""
        if not row:
            return {}

        if hasattr(row, 'keys'):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = dict(row)
        else:
            return {}

        return {
            "id": d.get("id", "") or d.get("memory_id", ""),
            "content": d.get("content", ""),
            "enabled": bool(d.get("enabled", 1)),
            "confidence": float(d.get("confidence", 0.8)),
            "source": d.get("source", "manual"),
            "created_at": d.get("created_at", ""),
            "updated_at": d.get("updated_at", ""),
            "last_accessed": d.get("last_accessed", ""),
        }

    def save(self, memory: Dict) -> bool:
        """
        保存单条记忆

        Args:
            memory: 记忆数据，包含 content, confidence, source 等

        Returns:
            bool: 保存是否成功
        """
        if not self._ensure_table():
            return False

        # 生成唯一的 id
        existing_id = memory.get("id") or memory.get("memory_id")
        if existing_id:
            memory_id = str(existing_id)
        else:
            content_hash = hashlib.md5(str(memory.get("content", "")).encode()).hexdigest()[:8]
            memory_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{content_hash}"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        success, _ = self._execute(f'''
            INSERT OR REPLACE INTO {self.TABLE_NAME}
            (id, content, enabled, confidence, source, created_at, updated_at, last_accessed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            memory_id,
            memory.get("content", ""),
            1 if memory.get("enabled", True) else 0,
            memory.get("confidence", 0.8),
            memory.get("source", "manual"),
            memory.get("created_at") or now,
            now,
            now,
        ))

        return success

    def save_all(self, memories: List[Dict]) -> bool:
        """
        批量保存记忆（全量替换，先清空再插入）

        Args:
            memories: 记忆列表

        Returns:
            bool: 是否成功
        """
        if not self._ensure_table() or not self._db:
            return False

        try:
            conn = self._db._conn
            cursor = conn.cursor()

            # 清空现有记忆
            cursor.execute(f"DELETE FROM {self.TABLE_NAME}")

            # 批量插入
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for memory in memories:
                existing_id = memory.get("id") or memory.get("memory_id")
                if existing_id:
                    memory_id = str(existing_id)
                else:
                    content_hash = hashlib.md5(str(memory.get("content", "")).encode()).hexdigest()[:8]
                    memory_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{content_hash}"

                cursor.execute(f'''
                    INSERT INTO {self.TABLE_NAME}
                    (id, content, enabled, confidence, source, created_at, updated_at, last_accessed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    memory_id,
                    memory.get("content", ""),
                    1 if memory.get("enabled", True) else 0,
                    memory.get("confidence", 0.8),
                    memory.get("source", "manual"),
                    memory.get("created_at") or now,
                    now,
                    now,
                ))

            conn.commit()
            logger.info(f"[MemoryRepository] 已保存 {len(memories)} 条记忆")
            return True

        except Exception as e:
            logger.error(f"[MemoryRepository] save_all 异常: {e}")
            if self._db and self._db._conn:
                self._db._conn.rollback()
            return False

    def load_all(self, limit: int = 200, include_disabled: bool = False) -> List[Dict]:
        """
        加载所有记忆

        Args:
            limit: 最大返回数量
            include_disabled: 是否包含已禁用的记忆

        Returns:
            List[Dict]: 记忆列表
        """
        if not self._ensure_table():
            return []

        try:
            if include_disabled:
                sql = f'SELECT * FROM {self.TABLE_NAME} ORDER BY confidence DESC, updated_at DESC LIMIT ?'
                params = (limit,)
            else:
                sql = f'SELECT * FROM {self.TABLE_NAME} WHERE enabled = 1 ORDER BY confidence DESC, updated_at DESC LIMIT ?'
                params = (limit,)

            success, rows = self._execute(sql, params)
            if success:
                return [self._row_to_memory(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[MemoryRepository] load_all 异常: {e}")
            return []

    def get(self, memory_id: str) -> Optional[Dict]:
        """
        获取单条记忆

        Args:
            memory_id: 记忆 ID

        Returns:
            Optional[Dict]: 记忆数据
        """
        if not self._ensure_table():
            return None

        try:
            success, rows = self._execute(
                f'SELECT * FROM {self.TABLE_NAME} WHERE id = ?',
                (memory_id,)
            )
            if success and rows and len(rows) > 0:
                return self._row_to_memory(rows[0])
            return None
        except Exception as e:
            logger.error(f"[MemoryRepository] get 异常: {e}")
            return None

    def update(self, memory_id: str, content: str) -> bool:
        """
        更新记忆内容

        Args:
            memory_id: 记忆 ID
            content: 新内容

        Returns:
            bool: 更新是否成功
        """
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET content = ?, updated_at = ? WHERE id = ?',
                (content, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), memory_id)
            )
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] update 异常: {e}")
            return False

    def delete(self, memory_id: str) -> bool:
        """
        删除指定记忆

        Args:
            memory_id: 记忆 ID

        Returns:
            bool: 删除是否成功
        """
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(
                f'DELETE FROM {self.TABLE_NAME} WHERE id = ?',
                (memory_id,)
            )
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] delete 异常: {e}")
            return False

    def clear_all(self) -> bool:
        """
        清空所有记忆

        Returns:
            bool: 是否成功
        """
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(f'DELETE FROM {self.TABLE_NAME}')
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] clear_all 异常: {e}")
            return False

    def update_enabled(self, memory_id: str, enabled: bool) -> bool:
        """
        更新记忆的启用状态

        Args:
            memory_id: 记忆 ID
            enabled: 是否启用

        Returns:
            bool: 更新是否成功
        """
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET enabled = ?, updated_at = ? WHERE id = ?',
                (1 if enabled else 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), memory_id)
            )
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] update_enabled 异常: {e}")
            return False

    def update_last_accessed(self, memory_id: str) -> bool:
        """
        更新记忆的最后访问时间

        Args:
            memory_id: 记忆 ID

        Returns:
            bool: 更新是否成功
        """
        if not self._ensure_table():
            return False

        try:
            success, _ = self._execute(
                f'UPDATE {self.TABLE_NAME} SET last_accessed = ? WHERE id = ?',
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), memory_id)
            )
            return success
        except Exception as e:
            logger.error(f"[MemoryRepository] update_last_accessed 异常: {e}")
            return False

    def search(self, query: str = "", limit: int = 9999) -> List[Dict]:
        """
        搜索记忆

        Args:
            query: 搜索关键词
            limit: 最大返回数量

        Returns:
            List[Dict]: 匹配的记忆列表
        """
        if not self._ensure_table():
            return []

        try:
            if query:
                # 使用 LIKE 进行模糊搜索
                success, rows = self._execute(
                    f'SELECT * FROM {self.TABLE_NAME} WHERE content LIKE ? ORDER BY confidence DESC LIMIT ?',
                    (f'%{query}%', limit)
                )
            else:
                success, rows = self._execute(
                    f'SELECT * FROM {self.TABLE_NAME} ORDER BY confidence DESC LIMIT ?',
                    (limit,)
                )
            
            if success and rows:
                return [self._row_to_memory(row) for row in rows]
            return []
        except Exception as e:
            logger.error(f"[MemoryRepository] search 异常: {e}")
            return []

    def count(self) -> int:
        """
        获取记忆总数

        Returns:
            int: 记忆数量
        """
        if not self._ensure_table():
            return 0

        try:
            success, result = self._execute(f'SELECT COUNT(*) FROM {self.TABLE_NAME}')
            if success and result:
                return result[0][0] if isinstance(result[0], tuple) else result[0]
            return 0
        except Exception as e:
            logger.error(f"[MemoryRepository] count 异常: {e}")
            return 0