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


class TestFileRoutingMigration:
    """有 workdir 无 AGENTS.md → 从 SQLite 迁或写默认模板"""

    def test_migrates_from_sqlite_when_file_missing(self, patched_memory_manager, tmp_workdir):
        mgr = patched_memory_manager
        # 1) 先在 SQLite 写入自定义内容(模拟无 workdir 时期的笔记)
        mgr.save_project_note("demo", "# my custom note\nlegacy content")
        # 2) 模拟设置 workdir(用 mock 替换 get_working_directory)
        mgr._key_documents_repo.get_working_directory.return_value = str(tmp_workdir)
        # 3) 首次访问应触发迁移
        note = mgr.get_or_create_project_note("demo")
        assert note["content"] == "# my custom note\nlegacy content"
        # 4) 文件确实被创建
        assert (tmp_workdir / "AGENTS.md").exists()
        assert (tmp_workdir / "AGENTS.md").read_text(encoding="utf-8") == "# my custom note\nlegacy content"

    def test_writes_initial_template_when_no_sqlite_content(self, patched_memory_manager, tmp_workdir):
        mgr = patched_memory_manager
        # SQLite 无任何记录
        mgr._key_documents_repo.get_working_directory.return_value = str(tmp_workdir)
        note = mgr.get_or_create_project_note("fresh_project")
        from app.core.project_notes_manager import INITIAL_TEMPLATE
        assert note["content"] == INITIAL_TEMPLATE
        assert (tmp_workdir / "AGENTS.md").exists()
