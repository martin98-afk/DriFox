# -*- coding: utf-8 -*-
"""
输入历史仓储 - 管理用户输入历史记录（支持附件路径）
"""
import orjson
from typing import List, Optional
from datetime import datetime


class InputHistoryRepository:
    """用户输入历史数据仓储"""

    TABLE_NAME = "input_history"
    MAX_COUNT = 50

    def __init__(self, db_manager):
        self._db = db_manager
        self._ensure_attachments_column()

    def _ensure_attachments_column(self):
        """迁移：为旧表追加 attachments TEXT 列（JSON 数组）"""
        try:
            from app.utils.db_manager import DatabaseManager
            self._db.execute_sql(
                f"ALTER TABLE {self.TABLE_NAME} ADD COLUMN attachments TEXT DEFAULT '[]'"
            )
        except Exception:
            pass  # 列已存在，忽略

    @property
    def is_initialized(self) -> bool:
        return self._db is not None and self._db.is_connected

    def create_table(self) -> bool:
        """创建 input_history 表"""
        success, _ = self._db.create_table(self.TABLE_NAME, [
            {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
            {"name": "content", "type": "TEXT", "not_null": True},
            {"name": "attachments", "type": "TEXT", "default": "[]"},
            {"name": "created_at", "type": "TEXT"},
        ])
        return success

    def add(self, content: str, attachments: Optional[List[str]] = None) -> bool:
        """添加一条输入历史（相邻重复自动跳过）

        Args:
            content: 用户输入的文本
            attachments: 附件文件路径列表（可选）
        """
        if not content or not content.strip():
            return False
        # 相邻去重：最新一条完全相同则跳过
        latest = self._get_latest()
        if latest is not None:
            same_text = (latest.get("content") or "").strip() == content.strip()
            latest_attrs = latest.get("attachments") or "[]"
            new_attrs = orjson.dumps(attachments or []).decode("utf-8")
            if same_text and latest_attrs == new_attrs:
                return True
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        attrs_json = orjson.dumps(attachments or []).decode("utf-8")
        success, _ = self._db.insert_data(self.TABLE_NAME, {
            "content": content.strip(),
            "attachments": attrs_json,
            "created_at": now,
        })
        if success:
            self._trim_excess()
        return success

    def _get_latest(self) -> Optional[dict]:
        """获取最新一条记录，没有则返回 None"""
        success, result = self._db.execute_sql(
            f'SELECT content, attachments FROM {self.TABLE_NAME} ORDER BY id DESC LIMIT 1'
        )
        if success and result:
            return result[0]
        return None

    def get_all(self, limit: int = 50) -> List[dict]:
        """获取最近的输入历史（最新在前）

        Returns:
            list[dict]: 每个元素含 ``text`` 和 ``attachments`` 键。
                        旧条目（无 attachments 列）自动返回空附件列表。
        """
        success, result = self._db.execute_sql(
            f'SELECT content, attachments FROM {self.TABLE_NAME} ORDER BY id DESC LIMIT ?',
            (limit,)
        )
        if success and result:
            entries = []
            for row in result:
                text = row.get("content", "")
                raw_attachments = row.get("attachments")
                attachments = []
                if raw_attachments:
                    try:
                        attachments = orjson.loads(raw_attachments)
                        if not isinstance(attachments, list):
                            attachments = []
                    except orjson.JSONDecodeError:
                        attachments = []
                entries.append({"text": text, "attachments": attachments})
            return entries
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
