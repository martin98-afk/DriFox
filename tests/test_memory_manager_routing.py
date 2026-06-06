# -*- coding: utf-8 -*-
"""memory_manager 层 workdir 路由测试"""

import pytest


class TestSqliteRouting:
    """无 workdir → 走 SQLite"""

    def test_get_or_create_uses_initial_template_when_no_workdir(self, patched_memory_manager):
        mgr = patched_memory_manager
        # workdir 默认为 None
        note = mgr.get_or_create_project_note("demo")
        assert note["content"]  # 非空
        assert note["path"] == ""  # 没文件路径
        # 内容应该是模板
        from app.core.project_notes_manager import INITIAL_TEMPLATE
        assert note["content"] == INITIAL_TEMPLATE

    def test_save_then_get_roundtrips_via_sqlite(self, patched_memory_manager):
        mgr = patched_memory_manager
        content = "# my note\nhello"
        assert mgr.save_project_note("demo", content) is True
        note = mgr.get_or_create_project_note("demo")
        assert note["content"] == content
