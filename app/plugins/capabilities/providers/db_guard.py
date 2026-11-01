# -*- coding: utf-8 -*-
"""storage.db 的受限代理

插件拿到的是代理而不是 ``DatabaseManager`` 本身：主程序表（sessions / messages /
key_documents …）与插件表共用同一个库，直接给句柄等于把所有业务表交出去。
约束两条：

1. **语句白名单**：只允许 SELECT/INSERT/UPDATE/DELETE/REPLACE/CREATE/DROP/ALTER/WITH
   开头的单段语句；ATTACH / DETACH / PRAGMA / VACUUM / LOAD_EXTENSION 一律拒绝
   （这几个能绕过表名前缀约束，甚至读写文件）。
2. **表名前缀**：语句里出现的表名必须带 ``plugin_<name>_`` 前缀，``sqlite_*`` 系统表
   一律拒绝。

表名靠正则提取（FROM/INTO/UPDATE/JOIN/TABLE/EXISTS/INDEX…ON）。这是**防手滑**而不是
防恶意：插件在本机是可信代码，能力级别 HIGH 已声明「可执行 SQL」。真要绕过的人可以直接
import sqlite3 自己开库——所以这里的目标是让正常插件写不出误伤主程序表的语句。
"""

import re
from typing import Any, Dict, List, Tuple

_ALLOWED_VERBS = frozenset(
    {"select", "insert", "update", "delete", "replace", "create", "drop", "alter", "with"}
)
_FORBIDDEN = ("attach", "detach", "pragma", "vacuum", "load_extension", "reindex")
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN) + r")\b", re.IGNORECASE)
_TABLE_REF_RE = re.compile(
    r"\b(?:from|into|update|join|table|exists)\s+(?:if\s+not\s+exists\s+)?([A-Za-z_][\w]*)",
    re.IGNORECASE,
)
_INDEX_ON_RE = re.compile(
    r"\bcreate\s+(?:unique\s+)?index\s+(?:if\s+not\s+exists\s+)?[\w]+\s+on\s+([A-Za-z_][\w]*)",
    re.IGNORECASE,
)


def normalize_plugin_name(name: str) -> str:
    """插件名 → 合法表名片段（非字母数字折叠成下划线）。"""
    return re.sub(r"[^0-9a-zA-Z]+", "_", (name or "").strip().lower()).strip("_") or "plugin"


class PluginDBProxy:
    """插件侧数据库入口：语句白名单 + 表名前缀约束。"""

    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name or "plugin"
        self.table_prefix = f"plugin_{normalize_plugin_name(plugin_name)}_"

    # ── 对外 API ──

    def table(self, suffix: str) -> str:
        """拼出带前缀的表名（``table("task")`` → ``plugin_<name>_task``）。"""
        return f"{self.table_prefix}{(suffix or '').strip('_')}"

    def execute(self, sql: str, params: Tuple = ()) -> Tuple[bool, Any]:
        """执行一条语句，返回 ``(success, result|error)``（同 DatabaseManager 口径）。"""
        reason = self.check_sql(sql)
        if reason:
            return False, reason
        from app.utils.db_manager import DatabaseManager

        return DatabaseManager().execute_sql(sql, tuple(params or ()))

    def query(self, sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        """只读便捷入口：失败返回空列表（错误原因走日志，不打断调用方）。"""
        from loguru import logger

        ok, result = self.execute(sql, params)
        if not ok:
            logger.warning(f"[storage.db:{self.plugin_name}] 查询被拒或失败: {result}")
            return []
        return result if isinstance(result, list) else []

    # ── 校验 ──

    def check_sql(self, sql: str) -> str:
        """校验语句；通过返回空串，否则返回拒绝原因。"""
        text = (sql or "").strip()
        if not text:
            return "空语句"
        for stmt in self._split_statements(text):
            reason = self._check_statement(stmt)
            if reason:
                return reason
        return ""

    @staticmethod
    def _split_statements(sql: str) -> List[str]:
        """按分号拆分（'' 结尾的分号不产生空段）。"""
        return [s.strip() for s in sql.split(";") if s.strip()]

    def _check_statement(self, stmt: str) -> str:
        head = stmt.lstrip("( \n\t").split(None, 1)
        verb = head[0].lower() if head else ""
        if verb not in _ALLOWED_VERBS:
            return f"不允许的语句类型: {verb or '(空)'}"

        hit = _FORBIDDEN_RE.search(stmt)
        if hit is not None:
            return f"不允许的关键字: {hit.group(1).upper()}"

        tables = set(m.group(1) for m in _TABLE_REF_RE.finditer(stmt))
        tables.update(m.group(1) for m in _INDEX_ON_RE.finditer(stmt))
        for name in tables:
            lowered = name.lower()
            if lowered.startswith("sqlite_"):
                return f"不允许访问系统表: {name}"
            if not lowered.startswith(self.table_prefix):
                return f"表名必须带 {self.table_prefix} 前缀: {name}"
        return ""
