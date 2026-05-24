# -*- coding: utf-8 -*-
"""
输入历史仓储 - 管理用户输入历史记录
"""
from typing import List
from datetime import datetime


class InputHistoryRepository:
    """用户输入历史数据仓储"""

    TABLE_NAME = "input_history"
    MAX_COUNT = 50

    def __init__(self, db_manager):
        self._db = db_manager

    @property
    def is_initialized(self) -> bool:
        return self._db is not None and self._db.is_connected

    def create_table(self) -> bool:
        """创建 input_history 表"""
        success, _ = self._db.create_table(self.TABLE_NAME, [
            {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
            {"name": "content", "type": "TEXT", "not_null": True},
            {"name": "created_at", "type": "TEXT"},
        ])
        return success

    def add(self, content: str) -> bool:
        """添加一条输入历史"""
        if not content or not content.strip():
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        success, _ = self._db.insert_data(self.TABLE_NAME, {
            "content": content.strip(),
            "created_at": now,
        })
        if success:
            self._trim_excess()
        return success

    def get_all(self, limit: int = 50) -> List[str]:
        """获取最近的输入历史（最新在前）"""
        success, result = self._db.execute_sql(
            f'SELECT content FROM {self.TABLE_NAME} ORDER BY id DESC LIMIT ?',
            (limit,)
        )
        if success and result:
            return [row["content"] for row in result]
        return []

    def _trim_excess(self):
        """超出 MAX_COUNT 时删除最旧的记录"""
        success, result = self._db.execute_sql(
            f'SELECT COUNT(*) as cnt FROM {self.TABLE_NAME}'
        )
        if success and result:
            count = result[0]["cnt"]
            if count > self.MAX_COUNT:
                self._db.execute_sql(
                    f'DELETE FROM {self.TABLE_NAME} WHERE id IN ('
                    f'SELECT id FROM {self.TABLE_NAME} ORDER BY id ASC LIMIT ?)',
                    (count - self.MAX_COUNT,)
                )
